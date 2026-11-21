from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
import threading
from typing import Any, Mapping, Optional

from .credentials import Credentials, credentials_from_env, redact_secrets
from .execution import LiveOrderDisabled
from .fak import FAKRequest


class TransportTimeout(TimeoutError):
    pass


class TransportNotSubmitted(RuntimeError):
    """Preparation failed before post_order was entered; no order was sent."""

    def __init__(self, message: str, *, exception_type: str) -> None:
        super().__init__(message)
        self.exception_type = exception_type


class TransportRejected(RuntimeError):
    """Authoritative provider rejection (the venue did not accept the order).

    This is deliberately separate from timeout/connection errors.  A response
    carrying an HTTP 4xx status is safe to classify as terminal and must not
    leave a local order in UNKNOWN.
    """

    def __init__(self, reason: str, *, status_code: int | None = None, provider_message: str = "") -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.provider_message = provider_message


def safe_provider_message(exc: BaseException) -> str:
    """Return a short, credential-safe provider error description."""
    raw = str(getattr(exc, "error_msg", "") or getattr(exc, "message", "") or str(exc))
    # Never persist headers, tokens, signatures, or private-key-like values.
    import re

    raw = re.sub(r'''(?i)(authorization|api[-_ ]?key|secret|passphrase|private[-_ ]?key|signature)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^,;\s}\]]+)''', r"\1=<redacted>", raw)
    raw = re.sub(r"0x[0-9a-fA-F]{32,}", "0x<redacted>", raw)
    return raw[:240]


def _status_code(exc: BaseException) -> int | None:
    for name in ("status_code", "status", "code"):
        value = getattr(exc, name, None)
        try:
            if value is not None and 100 <= int(value) <= 599:
                return int(value)
        except (TypeError, ValueError):
            continue
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ClobTransportConfig:
    host: str
    chain_id: int = 137
    signature_type: int = 0
    funder: str = ""
    transport_enabled: bool = False
    request_timeout_seconds: float = 10.0
    tick_size: str = "0.01"
    private_key_env: str = "POLYMARKET_PRIVATE_KEY"
    api_key_env: str = "POLYMARKET_API_KEY"
    api_secret_env: str = "POLYMARKET_API_SECRET"
    api_passphrase_env: str = "POLYMARKET_API_PASSPHRASE"
    proxy: str = ""


class PyClobClientTransport:
    """Optional py-clob-client-v2 adapter with fail-closed order gates.

    The SDK is imported lazily. No network method is reachable unless all three
    gates are true: config.transport_enabled, LIVE_TRADING_ENABLE=1, and an
    explicit preflight_passed=True supplied by the caller.
    """

    def __init__(
        self,
        config: ClobTransportConfig,
        credentials: Optional[Credentials] = None,
        sdk: Any = None,
        environ: Optional[Mapping[str, str]] = None,
        preflight_passed: bool = False,
    ) -> None:
        self.config = config
        self.environ = os.environ if environ is None else environ
        self.credentials = credentials or credentials_from_env(self.environ)
        self.sdk = sdk
        # SDK constructors belong to the module, not the ClobClient instance.
        # An injected test SDK may provide both on the same object.
        self._sdk_types = sdk
        self.preflight_passed = preflight_passed
        self._sdk_order_type: Any = getattr(sdk, "OrderType", None) if sdk is not None else None
        self._sdk_order_payload: Any = getattr(sdk, "OrderPayload", None) if sdk is not None else None
        self._sdk_trade_params: Any = getattr(sdk, "TradeParams", None) if sdk is not None else None
        # Reuse workers across CLOB calls.  Creating a new executor for every
        # request adds avoidable scheduling overhead on the signal hot path.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clob-request")
        self._close_lock = threading.Lock()
        self._closed = False

    @property
    def enabled(self) -> bool:
        return (
            self.config.transport_enabled
            and self.preflight_passed
            and self.environ.get("LIVE_TRADING_ENABLE", "") == "1"
        )

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise LiveOrderDisabled(
                "CLOB transport requires transport_enabled, preflight approval, and LIVE_TRADING_ENABLE=1"
            )

    def _load_sdk(self) -> Any:
        if self.sdk is not None:
            return self.sdk
        try:
            import py_clob_client_v2 as sdk  # type: ignore
        except ImportError:
            try:
                import py_clob_client as sdk  # type: ignore
            except ImportError as exc:
                raise RuntimeError("install optional dependency py-clob-client-v2 to enable CLOB transport") from exc
        self._configure_sdk_proxy()
        self.sdk = self._call(self._build_client, sdk)
        self._sdk_types = sdk
        self._sdk_order_type = getattr(sdk, "OrderType", None)
        self._sdk_order_payload = getattr(sdk, "OrderPayload", None)
        self._sdk_trade_params = getattr(sdk, "TradeParams", None)
        return self.sdk

    def prepare(self) -> None:
        """Warm SDK/client and L1/L2 credentials before the first signal.

        The call is intentionally separate from submit: startup can surface
        credential or SDK failures before a live market window, so the first
        eligible signal does not pay initialization/derivation latency.
        """
        self._require_enabled()
        client = self._load_sdk()
        if not callable(getattr(self._sdk_types, "OrderArgs", None)):
            raise RuntimeError("CLOB SDK must expose typed OrderArgs; install project live dependencies")
        if not callable(getattr(client, "create_order", None)) or not callable(getattr(client, "post_order", None)):
            raise RuntimeError("CLOB SDK must expose separate create_order and post_order methods")

    def error_message(self, exc: BaseException) -> str:
        """Redact runtime credentials before truncation or durable logging."""
        raw = str(getattr(exc, "error_msg", "") or getattr(exc, "message", "") or str(exc))
        return safe_provider_message(RuntimeError(redact_secrets(raw, self.credentials)))

    def prepare_market(self, condition_id: str) -> Mapping[str, str]:
        """Warm the per-market fee curve before an eligible signal arrives."""
        self._require_enabled()
        return self.market_fee_details(condition_id)

    def _configure_sdk_proxy(self) -> None:
        if not self.config.proxy:
            return
        try:
            import httpx
            from py_clob_client_v2.http_helpers import helpers
            # The local proxy used by the desktop setup is HTTP/1.1.  HTTP/2
            # connection reuse through an HTTP CONNECT proxy intermittently
            # produced TLS EOFs on CLOB auth/fee requests.  Use a short-lived
            # keep-alive pool and HTTP/1.1; order calls still have the same
            # explicit timeout and are never retried here.
            helpers._http_client = httpx.Client(
                http2=False,
                proxy=self.config.proxy,
                timeout=self.config.request_timeout_seconds,
                trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=5.0),
            )
        except ImportError:
            # Legacy SDKs use requests and can use HTTPS_PROXY supplied by the
            # caller; v2 has a process-global httpx client that needs explicit wiring.
            return

    def _build_client(self, sdk: Any) -> Any:
        private_key = self.credentials.private_key
        if not private_key:
            raise RuntimeError("private key is required at runtime from environment or Keychain")
        creds = None
        if all(
            value
            for value in (
                self.credentials.api_key or self.credentials_value(self.config.api_key_env),
                self.credentials.api_secret or self.credentials_value(self.config.api_secret_env),
                self.credentials.api_passphrase or self.credentials_value(self.config.api_passphrase_env),
            )
        ):
            api_creds = getattr(sdk, "ApiCreds")
            creds = api_creds(
                api_key=self.credentials.api_key or self.credentials_value(self.config.api_key_env),
                api_secret=self.credentials.api_secret or self.credentials_value(self.config.api_secret_env),
                api_passphrase=self.credentials.api_passphrase or self.credentials_value(self.config.api_passphrase_env),
            )
        client_cls = getattr(sdk, "ClobClient")
        kwargs = {"host": self.config.host, "chain_id": self.config.chain_id, "key": private_key, "retry_on_error": False}
        if self.config.signature_type:
            kwargs["signature_type"] = self.config.signature_type
        if self.config.funder:
            kwargs["funder"] = self.config.funder
        if creds is not None:
            kwargs["creds"] = creds
        client = client_cls(**kwargs)
        if creds is None:
            derive = getattr(client, "create_or_derive_api_key", None) or getattr(client, "derive_api_key", None)
            if derive is None:
                raise RuntimeError("SDK does not expose L2 API credential derivation")
            try:
                derived = derive()
            except Exception as exc:  # noqa: BLE001 - preserve safe provider diagnosis.
                status = _status_code(exc)
                suffix = f"（HTTP {status}）" if status is not None else ""
                message = safe_provider_message(exc) or "provider rejected credential derivation"
                raise RuntimeError(f"CLOB API 凭据派生失败{suffix}：{message}") from exc
            setter = getattr(client, "set_api_creds", None)
            if setter is None:
                raise RuntimeError("SDK does not expose set_api_creds")
            setter(derived)
        return client

    def credentials_value(self, env_name: str) -> str:
        return str(self.environ.get(env_name, ""))

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        if self._closed:
            raise RuntimeError("CLOB transport is closed")
        future = self._executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=self.config.request_timeout_seconds)
        except FutureTimeout as exc:
            if future.done():
                # Preserve a TimeoutError raised by the SDK itself.
                raise
            future.cancel()
            raise TransportTimeout("CLOB request timed out; do not retry without reconciliation") from exc

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _order_args(self, request: FAKRequest | Mapping[str, Any]) -> Any:
        self._load_sdk()
        order_args_cls = getattr(self._sdk_types, "OrderArgs", None)
        side_type = getattr(self._sdk_types, "Side", None)
        side = getattr(side_type, "BUY", "BUY") if side_type is not None else "BUY"
        def value(name: str) -> Any:
            if hasattr(request, name):
                return getattr(request, name)
            return request[name]

        token_id = value("token_id")
        price = value("limit_price")
        quantity = value("quantity")
        user_balance = value("expected_cost")
        if order_args_cls is None:
            raise RuntimeError("CLOB SDK is missing typed OrderArgs")
        # V2 adjusts BUY size at the signed limit to keep fees inside this
        # balance. Never fall back to a constructor that drops the budget.
        return order_args_cls(
            token_id=token_id,
            price=float(price),
            size=float(quantity),
            side=side,
            user_usdc_balance=float(user_balance),
        )

    def market_fee_details(self, condition_id: str) -> Mapping[str, str]:
        """Fetch the CLOB V2 fee curve used by the fee-inclusive budget."""
        self._require_enabled()
        if not condition_id:
            raise ValueError("condition_id is required for CLOB V2 fee lookup")
        client = self._load_sdk()
        fn = getattr(client, "get_clob_market_info", None)
        if not callable(fn):
            raise RuntimeError("CLOB V2 SDK does not expose get_clob_market_info")
        result = self._call(fn, condition_id)
        fee_details = result.get("fd") if isinstance(result, Mapping) else None
        if not isinstance(fee_details, Mapping):
            raise RuntimeError("CLOB V2 market fee details are unavailable")
        rate = str(fee_details.get("r", ""))
        exponent = str(fee_details.get("e", ""))
        if rate == "" or exponent == "":
            raise RuntimeError("CLOB V2 market fee details are incomplete")
        # Normalize provider floats without retaining binary artifacts.
        from decimal import Decimal

        return {"rate": str(Decimal(rate)), "exponent": str(Decimal(exponent))}

    def _order_type(self) -> Any:
        if self._sdk_order_type is None:
            return "FAK"
        return getattr(self._sdk_order_type, "FAK", "FAK")

    def submit(self, request: FAKRequest | Mapping[str, Any]) -> Mapping[str, Any]:
        self._require_enabled()
        try:
            client = self._load_sdk()
            # Keep signing and submission separate: a signer/metadata failure
            # is provably pre-send. Combined helpers hide that boundary and
            # may automatically post again after a version update.
            signed = self._call(client.create_order, self._order_args(request))
            post = client.post_order
            if not callable(post):
                raise RuntimeError("CLOB SDK is missing post_order")
        except LiveOrderDisabled:
            raise
        except Exception as exc:
            raise TransportNotSubmitted(
                f"订单准备失败，未发送：{self.error_message(exc)}",
                exception_type=type(exc).__name__,
            ) from exc
        # Recheck in case the gate was closed while metadata/signing ran.
        self._require_enabled()
        try:
            return self._call(post, signed, self._order_type())
        except (TransportTimeout, OSError, ConnectionError):
            raise
        except Exception as exc:  # noqa: BLE001 - classify provider response safely.
            status = _status_code(exc)
            if status is not None and 400 <= status < 500 and status != 408:
                message = self.error_message(exc)
                raise TransportRejected(
                    f"CLOB 拒单（HTTP {status}）：{message or 'provider rejected order'}",
                    status_code=status,
                    provider_message=message,
                ) from exc
            raise

    def fetch(self, remote_order_id: str) -> Mapping[str, Any]:
        self._require_enabled()
        return self._call(self._load_sdk().get_order, remote_order_id)

    def cancel(self, remote_order_id: str) -> Mapping[str, Any]:
        self._require_enabled()
        client = self._load_sdk()
        fn = getattr(client, "cancel_order", None) or getattr(client, "cancel")
        payload = self._sdk_order_payload(orderID=remote_order_id) if self._sdk_order_payload is not None else remote_order_id
        return self._call(fn, payload)

    def fills(self, remote_order_id: str) -> list[Mapping[str, Any]]:
        self._require_enabled()
        client = self._load_sdk()
        fn = getattr(client, "get_trades", None)
        if fn is None:
            return []
        order = self.fetch(remote_order_id)
        trade_ids = order.get("associate_trades", order.get("tradeIDs", order.get("trade_ids", []))) if isinstance(order, Mapping) else []
        if isinstance(trade_ids, str):
            trade_ids = [trade_ids]
        if not trade_ids:
            return []
        results: list[Mapping[str, Any]] = []
        for trade_id in trade_ids:
            if self._sdk_trade_params is not None:
                params = self._sdk_trade_params(id=str(trade_id))
                response = self._call(fn, params, True)
            else:
                response = self._call(fn, str(trade_id))
            items = response if isinstance(response, list) else response.get("data", []) if isinstance(response, Mapping) else []
            results.extend(item for item in items if isinstance(item, Mapping))
        return results

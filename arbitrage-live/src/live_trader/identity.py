from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional


ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
PRIVATE_KEY_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
SIGNATURE_TYPES = frozenset({0, 1, 2, 3})
IDENTITY_TYPE_NAMES = {
    0: "EOA",
    1: "POLY_PROXY",
    2: "GNOSIS_SAFE",
    3: "POLY_1271",
}


def account_type_for_signature(signature_type: int) -> str:
    try:
        return IDENTITY_TYPE_NAMES[int(signature_type)]
    except (KeyError, TypeError, ValueError):
        return "UNKNOWN"


def _mask(value: str) -> str:
    value = str(value)
    return value if len(value) <= 8 else f"{value[:4]}••••{value[-4:]}"


def credentials_from_store(store: Any) -> dict[str, str]:
    """Return runtime-only CLOB env names from the encrypted secret store."""
    try:
        stored = json.loads(str(store.get() or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(stored, Mapping):
        return {}
    values = {
        "POLYMARKET_API_KEY": str(stored.get("apiKey", "")),
        "POLYMARKET_API_SECRET": str(stored.get("apiSecret", "")),
        "POLYMARKET_API_PASSPHRASE": str(stored.get("apiPassphrase", "")),
    }
    return values if all(values.values()) else {}


class CLOBIdentityManager:
    """Derive and validate Polymarket CLOB identity without Polygon RPC.

    The manager deliberately treats the public ``funder`` address and the
    local signer key as separate values.  This matches Polymarket's proxy/Safe
    accounts and lets EOA mode enforce the one equality that is meaningful.
    Secret stores only expose ``get`` and ``set`` so macOS Keychain and tests
    can share the same implementation.
    """

    def __init__(
        self,
        *,
        private_key_store: Any,
        credential_store: Any,
        host: str = "https://clob.polymarket.com",
        client_factory: Optional[Callable[..., Any]] = None,
        api_creds_factory: Optional[Callable[..., Any]] = None,
        signer_address: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.private_key_store = private_key_store
        self.credential_store = credential_store
        self.host = host
        self.client_factory = client_factory or self._default_client_factory
        self.api_creds_factory = api_creds_factory
        self.signer_address = signer_address or self._default_signer_address

    def is_configured(self) -> bool:
        try:
            stored = json.loads(str(self.credential_store.get() or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(stored, Mapping)
            and stored.get("apiKey")
            and stored.get("apiSecret")
            and stored.get("apiPassphrase")
            and stored.get("funderAddress")
            and self.private_key_store.get()
        )

    def summary(self) -> dict[str, Any]:
        try:
            stored = json.loads(str(self.credential_store.get() or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            stored = {}
        configured = self.is_configured()
        return {
            "configured": configured,
            "signatureType": stored.get("signatureType") if isinstance(stored, Mapping) else None,
            "accountType": account_type_for_signature(stored.get("signatureType")) if isinstance(stored, Mapping) else "",
            "funderAddress": stored.get("funderAddress") if isinstance(stored, Mapping) else "",
            "signerAddress": stored.get("signerAddress") if isinstance(stored, Mapping) else "",
            "apiKeyMasked": _mask(str(stored.get("apiKey", ""))) if isinstance(stored, Mapping) and stored.get("apiKey") else "",
            "message": "CLOB 身份已派生并加密保存" if configured else "尚未派生 CLOB 身份",
        }

    def configure(
        self,
        *,
        signature_type: int,
        funder_address: str,
        signer_private_key: str = "",
        proxy: str = "",
    ) -> dict[str, Any]:
        signature_type = int(signature_type)
        if signature_type not in SIGNATURE_TYPES:
            raise ValueError("Polymarket 签名类型必须是 0、1、2 或 3")
        funder = str(funder_address).strip()
        if not ADDRESS_RE.fullmatch(funder):
            raise ValueError("funder 地址格式无效")
        private_key = str(signer_private_key or self.private_key_store.get() or "").strip()
        if not PRIVATE_KEY_RE.fullmatch(private_key):
            raise ValueError("订单签名私钥必须是 0x 开头的 32 字节十六进制值")
        signer = self.signer_address(private_key)
        if signature_type == 0 and signer.lower() != funder.lower():
            raise ValueError("EOA 签名类型要求 funder 地址与签名私钥对应地址一致")

        self._configure_proxy(proxy)
        client = self.client_factory(
            host=self.host,
            chain_id=137,
            key=private_key,
            signature_type=signature_type,
            funder=funder,
            use_server_time=True,
            retry_on_error=False,
        )
        derive = getattr(client, "create_or_derive_api_key", None) or getattr(client, "derive_api_key", None)
        if derive is None:
            raise RuntimeError("Polymarket SDK 不支持 CLOB API 凭据派生")
        try:
            derived = derive()
        except Exception as exc:  # noqa: BLE001 - expose only a provider status/type, never response secrets.
            status = getattr(exc, "status_code", None)
            response = getattr(exc, "response", None)
            status = status or getattr(response, "status_code", None)
            if status:
                raise ValueError(f"Polymarket CLOB 返回 HTTP {status}：请检查 CLOB 代理和钱包身份") from None
            raise ValueError(f"Polymarket CLOB 身份派生失败：{type(exc).__name__}") from None
        api_key, api_secret, api_passphrase = self._credential_values(derived)
        if not all((api_key, api_secret, api_passphrase)):
            raise RuntimeError("Polymarket 未返回完整 CLOB API 凭据")
        self.private_key_store.set(private_key)
        self.credential_store.set(json.dumps({
            "version": 1,
            "signatureType": signature_type,
            "funderAddress": funder,
            "signerAddress": signer,
            "apiKey": api_key,
            "apiSecret": api_secret,
            "apiPassphrase": api_passphrase,
        }, ensure_ascii=False, separators=(",", ":")))
        result = self.summary()
        result["status"] = "OK"
        result["message"] = "CLOB 身份已派生并加密保存；尚未提交订单"
        return result

    def validate(self, *, proxy: str = "") -> dict[str, Any]:
        try:
            stored = json.loads(str(self.credential_store.get() or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("CLOB 身份尚未完整配置") from None
        if not isinstance(stored, Mapping) or not self.is_configured():
            raise ValueError("CLOB 身份尚未完整配置")
        signature_type = int(stored.get("signatureType", 0))
        funder = str(stored.get("funderAddress", ""))
        private_key = str(self.private_key_store.get())
        signer = self.signer_address(private_key)
        credential_signer = str(stored.get("signerAddress", ""))
        if credential_signer and credential_signer.lower() != signer.lower():
            return {
                "status": "BLOCKED",
                "ok": False,
                "configured": True,
                "signatureType": signature_type,
                "accountType": account_type_for_signature(signature_type),
                "funderAddress": funder,
                "signerAddress": signer,
                "collateralBalance": "0",
                "allowanceConfigured": False,
                "openOrderCount": 0,
                "closedOnly": False,
                "diagnosticCode": "PRIVATE_KEY_CHANGED",
                "message": "当前 Keychain 私钥派生地址与已有 CLOB 凭据的 signer 不一致；请重新配置并派生身份",
            }
        self._configure_proxy(proxy)
        client = self.client_factory(
            host=self.host,
            chain_id=137,
            key=private_key,
            creds=self._make_api_creds(
                api_key=str(stored["apiKey"]),
                api_secret=str(stored["apiSecret"]),
                api_passphrase=str(stored["apiPassphrase"]),
            ),
            signature_type=signature_type,
            funder=funder,
            use_server_time=True,
            retry_on_error=False,
        )
        collateral_params = self._collateral_params()
        update_balance = getattr(client, "update_balance_allowance", None)
        if not callable(update_balance):
            raise RuntimeError("Polymarket SDK 不支持刷新 CLOB V2 余额缓存")
        # CLOB serves a cached collateral view. Refresh it before validating
        # so newly wrapped/transferred pUSD is not incorrectly reported as 0.
        update_balance(collateral_params)
        balance = client.get_balance_allowance(collateral_params)
        closed_only_result = client.get_closed_only_mode()
        raw_balance = str(balance.get("balance", "0")) if isinstance(balance, Mapping) else "0"
        collateral = Decimal(raw_balance).scaleb(-6).normalize()
        raw_allowances = balance.get("allowances", balance.get("allowance", {})) if isinstance(balance, Mapping) else {}
        if isinstance(raw_allowances, Mapping):
            allowance_values = [Decimal(str(value)) for value in raw_allowances.values()]
        elif raw_allowances not in (None, ""):
            allowance_values = [Decimal(str(raw_allowances))]
        else:
            allowance_values = []
        allowance_configured = bool(allowance_values) and all(value > 0 for value in allowance_values)
        get_open_orders = getattr(client, "get_open_orders", None)
        if not callable(get_open_orders):
            raise RuntimeError("Polymarket SDK 不支持未结订单查询")
        open_orders = get_open_orders() or []
        if isinstance(open_orders, Mapping):
            open_orders = open_orders.get("data", [])
        balance_mapped = collateral > 0
        return {
            "status": "OK" if balance_mapped else "BLOCKED",
            "ok": balance_mapped,
            "configured": True,
            "signatureType": signature_type,
            "accountType": account_type_for_signature(signature_type),
            "funderAddress": funder,
            "signerAddress": signer,
            "collateralBalance": format(collateral, "f"),
            "allowanceConfigured": allowance_configured,
            "openOrderCount": len(open_orders),
            "closedOnly": bool(closed_only_result.get("closed_only", False)) if isinstance(closed_only_result, Mapping) else False,
            "diagnosticCode": "OK" if balance_mapped else "SIGNER_FUNDER_UNMAPPED",
            "message": (
                "CLOB 身份、余额和账户状态验证通过；没有提交订单"
                if balance_mapped
                else (
                    "私钥本身有效并可认证，但 CLOB 对当前 signer "
                    f"{_mask(signer)} 与 Funder {_mask(funder)} 的组合返回 0 pUSD；"
                    "请确认 signer 是 MetaMask 当前登录地址，或是该 deposit wallet 已授权的 session signer"
                )
            ),
        }

    def _credential_values(self, derived: Any) -> tuple[str, str, str]:
        if isinstance(derived, Mapping):
            return (str(derived.get("apiKey", derived.get("key", ""))), str(derived.get("secret", "")), str(derived.get("passphrase", "")))
        return (str(getattr(derived, "api_key", getattr(derived, "key", ""))), str(getattr(derived, "api_secret", getattr(derived, "secret", ""))), str(getattr(derived, "api_passphrase", getattr(derived, "passphrase", ""))))

    def _make_api_creds(self, *, api_key: str, api_secret: str, api_passphrase: str) -> Any:
        if self.api_creds_factory:
            return self.api_creds_factory(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        sdk = self._load_sdk()
        return sdk.ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)

    @staticmethod
    def _collateral_params() -> Any:
        try:
            sdk = CLOBIdentityManager._load_sdk()
            return sdk.BalanceAllowanceParams(asset_type=sdk.AssetType.COLLATERAL)
        except (ImportError, AttributeError, RuntimeError):
            return None

    def _configure_proxy(self, proxy: str) -> None:
        if not proxy:
            return
        try:
            import httpx
            from py_clob_client_v2.http_helpers import helpers
            helpers._http_client = httpx.Client(http2=True, proxy=proxy, timeout=8.0, trust_env=False)
        except ImportError:
            # Legacy SDKs use requests and honor HTTPS_PROXY in the process;
            # the caller still gets a clear provider error if it cannot route.
            return

    @staticmethod
    def _load_sdk() -> Any:
        try:
            import py_clob_client_v2 as sdk  # type: ignore
            return sdk
        except ImportError:
            try:
                import py_clob_client as sdk  # type: ignore
                return sdk
            except ImportError as exc:
                raise RuntimeError("请使用项目 bundled Python，或安装 py-clob-client-v2") from exc

    @classmethod
    def _default_client_factory(cls, **kwargs: Any) -> Any:
        return cls._load_sdk().ClobClient(**kwargs)

    @staticmethod
    def _default_signer_address(private_key: str) -> str:
        try:
            from eth_account import Account
            return str(Account.from_key(private_key).address)
        except ImportError as exc:
            raise RuntimeError("当前运行环境缺少 eth-account，无法验证 EOA 私钥对应地址") from exc

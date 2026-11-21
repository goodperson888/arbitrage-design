from __future__ import annotations

import json
import re
import ssl
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from urllib.error import HTTPError, URLError


ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
DEPRECATED_POLYGON_RPC_URL = "https://polygon-rpc.com"
DEFAULT_SETTINGS: dict[str, Any] = {
    "walletAddress": "",
    "rpcUrl": "https://polygon.drpc.org",
    "proxy": "",
    "liveMode": "observe",
    "transportEnabled": False,
    "liveTradingEnabled": False,
    "orderEnabled": False,
    "preflightApproved": False,
    # Public market feeds are safe to enable by default; order transport and
    # live trading remain explicitly disabled.
    "polymarketReadonlyEnabled": True,
    "binanceReadonlyEnabled": True,
    "signatureType": 0,
    "strategySide": "DOWN",
    "funder": "",
    "accountType": "",
    "accountDetectionStatus": "",
    "maxBudgetUsd": "5",
    "activeTradeMinQuantity": "10",
    "activeTradeMultiplier": "1.5",
    # Daily settlement-based stop: either threshold halts new orders.
    "maxDailyLossCount": "2",
    "maxDailyLossTotalUsd": "10",
    "dataRecordingEnabled": True,
    "orderSubmissionEnabled": True,
}
ALLOWED_SETTINGS = frozenset(DEFAULT_SETTINGS)


class SettingsValidationError(ValueError):
    pass


def effective_order_budget(values: Mapping[str, Any]) -> Decimal:
    """Return the configured per-order amount (fees included)."""
    return Decimal(str(values.get("maxBudgetUsd", "5")))


def effective_daily_loss_count(values: Mapping[str, Any]) -> int:
    """Configured number of confirmed losing settlements that halts orders."""
    return int(str(values.get("maxDailyLossCount", "2")))


def effective_daily_loss_total(values: Mapping[str, Any]) -> Decimal:
    """Configured cumulative confirmed loss (U) that halts orders."""
    return Decimal(str(values.get("maxDailyLossTotalUsd", "10")))


class SettingsStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "ui_settings.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        values = dict(DEFAULT_SETTINGS)
        try:
            raw = json.loads(self.path.read_text())
        except (FileNotFoundError, OSError, ValueError):
            raw = {}
        if isinstance(raw, Mapping):
            values.update({key: raw[key] for key in ALLOWED_SETTINGS if key in raw})
        # Polygon retired the unauthenticated polygon-rpc.com endpoint. Keep
        # existing installs usable without forcing the operator to edit JSON.
        if values.get("rpcUrl") == DEPRECATED_POLYGON_RPC_URL:
            values["rpcUrl"] = DEFAULT_SETTINGS["rpcUrl"]
        # Older installs may have serialized a blank/null direction while the
        # UI had no direction selector. Preserve the original DOWN default.
        if str(values.get("strategySide") or "").upper() not in {"UP", "DOWN", "BOTH"}:
            values["strategySide"] = DEFAULT_SETTINGS["strategySide"]
        return values

    def save(self, values: Mapping[str, Any]) -> dict[str, Any]:
        merged = self.load()
        for key in ALLOWED_SETTINGS:
            if key in values:
                merged[key] = values[key]
        # The UI exposes one authoritative mode selector. Keep legacy switch
        # fields for compatibility, but derive them so stale checkboxes cannot
        # change execution mode.
        enabled = str(merged.get("liveMode", "observe")) != "observe"
        for key in ("transportEnabled", "liveTradingEnabled", "orderEnabled", "preflightApproved"):
            merged[key] = enabled
        merged["polymarketReadonlyEnabled"] = True
        merged["binanceReadonlyEnabled"] = True
        self._validate(merged)
        self.path.write_text(json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return merged

    @staticmethod
    def _validate(values: Mapping[str, Any]) -> None:
        address = str(values.get("walletAddress", ""))
        funder = str(values.get("funder", ""))
        if address and not ADDRESS_RE.fullmatch(address):
            raise SettingsValidationError("walletAddress must be a 20-byte Polygon address")
        if funder and not ADDRESS_RE.fullmatch(funder):
            raise SettingsValidationError("funder must be a 20-byte Polygon address")
        for key in ("rpcUrl", "proxy"):
            value = str(values.get(key, ""))
            if value and not value.startswith(("http://", "https://")):
                raise SettingsValidationError(f"{key} must be an http(s) URL")
        if values.get("liveMode") not in {"observe", "canary", "live"}:
            raise SettingsValidationError("liveMode must be observe, canary, or live")
        if str(values.get("strategySide", "DOWN")).upper() not in {"UP", "DOWN", "BOTH"}:
            raise SettingsValidationError("strategySide must be UP, DOWN, or BOTH")
        if int(values.get("signatureType", 0)) not in {0, 1, 2, 3}:
            raise SettingsValidationError("signatureType must be 0, 1, 2, or 3")
        try:
            max_budget = Decimal(str(values.get("maxBudgetUsd", "5")))
            min_quantity = Decimal(str(values.get("activeTradeMinQuantity", "10")))
            active_multiplier = Decimal(str(values.get("activeTradeMultiplier", "1.5")))
            daily_loss_count = Decimal(str(values.get("maxDailyLossCount", "2")))
            daily_loss_total = Decimal(str(values.get("maxDailyLossTotalUsd", "10")))
        except (InvalidOperation, ValueError):
            raise SettingsValidationError("capital, budget, and active-trade controls must be numbers") from None
        if not max_budget.is_finite() or max_budget < Decimal("2.5"):
            raise SettingsValidationError("maxBudgetUsd must be at least 2.5U")
        if not min_quantity.is_finite() or min_quantity <= 0:
            raise SettingsValidationError("activeTradeMinQuantity must be greater than 0")
        if not active_multiplier.is_finite() or active_multiplier < Decimal("1"):
            raise SettingsValidationError("activeTradeMultiplier must be at least 1")
        if not daily_loss_count.is_finite() or daily_loss_count < 1 or daily_loss_count != daily_loss_count.to_integral_value():
            raise SettingsValidationError("maxDailyLossCount must be a positive integer")
        if not daily_loss_total.is_finite() or daily_loss_total <= 0:
            raise SettingsValidationError("maxDailyLossTotalUsd must be greater than 0")
        if effective_order_budget(values) < Decimal("2.5"):
            raise SettingsValidationError("allocated order budget must be at least 2.5U")

    def public(self, *, private_key_configured: bool) -> dict[str, Any]:
        result = self.load()
        result["privateKeyConfigured"] = bool(private_key_configured)
        return result


class KeychainSecretStore:
    def __init__(
        self,
        *,
        service: str = "polymarket-live",
        account: str = "polymarket-private-key",
        runner: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.service = service
        self.account = account
        self.runner = runner or subprocess.run

    def get(self) -> str:
        try:
            result = self.runner(
                ["security", "find-generic-password", "-a", self.account, "-s", self.service, "-w"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return str(getattr(result, "stdout", "")).strip()

    def set(self, secret: str) -> None:
        value = str(secret).strip()
        if not value:
            raise ValueError("private key cannot be empty")
        self.runner(
            ["security", "add-generic-password", "-U", "-a", self.account, "-s", self.service, "-w", value],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )


def classify_polygon_code(code: str) -> tuple[str, bool, Optional[int]]:
    normalized = str(code or "").lower()
    if normalized in {"", "0x", "0x0"}:
        return "UNKNOWN" if not normalized else "EOA", False, 0 if normalized else None
    return "CONTRACT_PROXY", True, 1


class PolygonAccountDetector:
    def __init__(self, rpc_call: Optional[Callable[[str, str], str]] = None) -> None:
        # Keep two-argument injected RPC functions compatible with tests and
        # integrations, while the built-in transport can honor the UI proxy.
        self._uses_default_rpc = rpc_call is None
        self.rpc_call = rpc_call or self._rpc_call

    def detect(self, address: str, rpc_url: str, proxy: str = "") -> dict[str, Any]:
        value = str(address).strip()
        if not ADDRESS_RE.fullmatch(value):
            return {"status": "INVALID", "address": value, "accountType": "INVALID", "codePresent": False, "signatureTypeSuggestion": None, "funderSuggestion": "", "error": "地址格式无效"}
        try:
            if self._uses_default_rpc:
                # Call the attribute rather than the saved bound function so
                # a caller can instrument/replace the transport safely.
                code = self._rpc_call(value, rpc_url, proxy=proxy)
            else:
                code = self.rpc_call(value, rpc_url)
        except HTTPError as exc:
            if exc.code == 401:
                error = "RPC 身份验证失败（HTTP 401）：请检查 RPC 地址是否需要 API key，或端点是否已停用"
            elif exc.code == 403:
                error = "RPC 访问被拒绝（HTTP 403）：可能是公共 RPC 的 IP/地区/代理策略，请更换 RPC 或暂时关闭代理"
            else:
                error = f"RPC 返回 HTTP {exc.code}：{exc.reason or '请求被拒绝或限流'}"
            return {"status": "UNKNOWN", "address": value, "accountType": "UNKNOWN", "codePresent": False, "signatureTypeSuggestion": None, "funderSuggestion": "", "error": error}
        except URLError as exc:
            reason = getattr(exc, "reason", "网络不可用")
            if isinstance(reason, ssl.SSLError) or "SSL" in str(reason).upper() or "TLS" in str(reason).upper():
                message = f"RPC TLS/SSL 连接失败：{reason}；请检查 RPC 地址或代理是否可用"
            else:
                message = f"RPC 网络连接失败：{reason}"
            return {"status": "UNKNOWN", "address": value, "accountType": "UNKNOWN", "codePresent": False, "signatureTypeSuggestion": None, "funderSuggestion": "", "error": message}
        except ssl.SSLError as exc:
            return {"status": "UNKNOWN", "address": value, "accountType": "UNKNOWN", "codePresent": False, "signatureTypeSuggestion": None, "funderSuggestion": "", "error": f"RPC TLS/SSL 连接失败：{exc}；请检查 RPC 地址或代理是否可用"}
        except Exception as exc:  # noqa: BLE001 - UI receives a safe diagnostic only.
            return {"status": "UNKNOWN", "address": value, "accountType": "UNKNOWN", "codePresent": False, "signatureTypeSuggestion": None, "funderSuggestion": "", "error": f"RPC 检查失败：{type(exc).__name__}"}
        account_type, present, signature_type = classify_polygon_code(code)
        return {
            "status": "OK" if account_type != "UNKNOWN" else "UNKNOWN",
            "address": value,
            "accountType": account_type,
            "codePresent": present,
            "signatureTypeSuggestion": signature_type,
            "funderSuggestion": value if account_type == "EOA" else "",
            "error": "" if account_type != "UNKNOWN" else "RPC 返回空代码，无法分类",
        }

    @staticmethod
    def _rpc_call(address: str, rpc_url: str, proxy: str = "") -> str:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": [address, "latest"]}).encode()
        request = Request(rpc_url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        if proxy:
            opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
            response_context = opener.open(request, timeout=8)
        else:
            response_context = urlopen(request, timeout=8)  # noqa: S310 - operator-provided RPC URL is validated before use.
        with response_context as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping) or "result" not in payload:
            raise ValueError("RPC response has no result")
        return str(payload["result"])

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    safe: bool
    checks: Tuple[PreflightCheck, ...]


def run_preflight(
    environ: Mapping[str, str],
    transport_enabled: bool = False,
    require_credentials: bool = False,
    rollout_approved: bool = False,
) -> PreflightReport:
    # The L1 wallet key is sufficient: the official SDK can derive the L2
    # HMAC credentials at startup.  Supplying pre-derived L2 values remains
    # optional and is useful when the operator wants to avoid derivation.
    has_credentials = bool(environ.get("POLYMARKET_PRIVATE_KEY", ""))
    private_key_file_free = environ.get("POLYMARKET_PRIVATE_KEY_FILE", "") == ""

    if rollout_approved:
        # Explicit rollout path.  Every positive gate is required; this path
        # is never selected by the default CLI or checked-in configuration.
        checks = (
            PreflightCheck("config_transport_on", transport_enabled, "transport_enabled must be true for rollout"),
            PreflightCheck("order_switch_on", environ.get("POLYMARKET_ORDER_ENABLED", "") == "1", "POLYMARKET_ORDER_ENABLED=1 is required"),
            PreflightCheck("live_mode", environ.get("LIVE_MODE", "") in {"canary", "live"}, "LIVE_MODE must be canary or live"),
            PreflightCheck("live_switch_on", environ.get("LIVE_TRADING_ENABLE", "") == "1", "LIVE_TRADING_ENABLE=1 is required"),
            PreflightCheck("no_private_key_file", private_key_file_free, "private keys may not be loaded from files"),
            PreflightCheck("transport_review_gate", environ.get("POLYMARKET_PREFLIGHT_APPROVED", "") == "1", "explicit preflight approval is required"),
            PreflightCheck("credentials_present", (not require_credentials) or has_credentials, "runtime credentials must come from environment or Keychain"),
        )
    else:
        # Safe default path: an empty environment is a valid read-only
        # preflight, while any attempt to turn on live settings fails closed.
        checks = (
            PreflightCheck("config_transport_off", not transport_enabled, "transport_enabled must remain false until explicit rollout"),
            PreflightCheck("order_switch_off", environ.get("POLYMARKET_ORDER_ENABLED", "") != "1", "real order switch must remain off"),
            PreflightCheck("read_only_mode", environ.get("LIVE_MODE", "observe") == "observe", "commands must remain observe-only"),
            PreflightCheck("no_private_key_file", private_key_file_free, "private keys may not be loaded from files"),
            PreflightCheck("transport_review_gate", environ.get("POLYMARKET_PREFLIGHT_APPROVED", "") != "1", "real transport is not approved until rollout"),
            PreflightCheck("credentials_present", not require_credentials or has_credentials, "runtime credentials must come from environment or Keychain"),
        )
    return PreflightReport(all(check.passed for check in checks), checks)

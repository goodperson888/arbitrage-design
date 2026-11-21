from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .credentials import Credentials, credentials_from_env
from .execution import LiveOrderDisabled


@dataclass(frozen=True)
class ClobSigningConfig:
    chain_id: int
    host: str
    signature_type: int
    funder: str
    private_key_env: str = "POLYMARKET_PRIVATE_KEY"
    api_key_env: str = "POLYMARKET_API_KEY"


class ConfiguredClobTransport:
    """Signature/transport boundary; order submission remains disabled in phase 3."""

    def __init__(
        self,
        config: ClobSigningConfig,
        credentials: Optional[Credentials] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.config = config
        self.credentials = credentials or credentials_from_env(os.environ)
        self.enabled = (os.environ.get("POLYMARKET_ORDER_ENABLED") == "1") if enabled is None else enabled

    def signed_payload(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Build a deterministic signing envelope without performing a network call."""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return {
            "payload": dict(payload),
            "chain_id": self.config.chain_id,
            "signature_type": self.config.signature_type,
            "funder": self.config.funder,
            "payload_digest": digest,
        }

    def submit(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.enabled:
            raise LiveOrderDisabled("POLYMARKET_ORDER_ENABLED is not enabled")
        raise LiveOrderDisabled("signed CLOB submit is intentionally not implemented before preflight")


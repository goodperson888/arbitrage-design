from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class Credentials:
    """Credential values are runtime-only and intentionally have no useful repr."""

    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""

    def __repr__(self) -> str:
        return "Credentials(private_key=<redacted>, api_key=<redacted>, api_secret=<redacted>, api_passphrase=<redacted>)"


def credentials_from_env(environ: Optional[Mapping[str, str]] = None) -> Credentials:
    values = environ or {}
    return Credentials(
        private_key=str(values.get("POLYMARKET_PRIVATE_KEY", "")),
        api_key=str(values.get("POLYMARKET_API_KEY", values.get("CLOB_API_KEY", ""))),
        api_secret=str(values.get("POLYMARKET_API_SECRET", values.get("CLOB_SECRET", ""))),
        api_passphrase=str(values.get("POLYMARKET_API_PASSPHRASE", values.get("CLOB_PASS_PHRASE", ""))),
    )


def redact_secrets(text: str, credentials: Credentials) -> str:
    rendered = text
    for secret in (credentials.private_key, credentials.api_key, credentials.api_secret, credentials.api_passphrase):
        if secret:
            rendered = rendered.replace(secret, "<redacted>")
    return rendered

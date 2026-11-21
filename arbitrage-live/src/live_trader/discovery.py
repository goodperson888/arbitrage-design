from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Iterable, Mapping


def _parse_time(value: Any) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class MarketDefinition:
    market_id: str
    condition_id: str
    question: str
    start_time: datetime
    end_time: datetime
    up_token_id: str
    down_token_id: str
    slug: str = ""

    @property
    def duration_seconds(self) -> int:
        return int((self.end_time - self.start_time).total_seconds())


def normalize_market_payload(payload: Mapping[str, Any]) -> MarketDefinition:
    tokens = payload.get("tokens") or payload.get("clobTokenIds")
    outcomes = payload.get("outcomes")
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if not tokens and isinstance(outcomes, list) and isinstance(payload.get("clobTokenIds"), list):
        tokens = payload.get("clobTokenIds")
    if not isinstance(tokens, list):
        raise ValueError("market payload has no token list")
    if tokens and all(isinstance(item, Mapping) for item in tokens):
        by_outcome = {
            str(item.get("outcome", "")).lower(): str(item.get("token_id", item.get("tokenId")))
            for item in tokens
        }
    elif isinstance(outcomes, list) and len(outcomes) == len(tokens):
        by_outcome = {str(outcome).lower(): str(token) for outcome, token in zip(outcomes, tokens)}
    else:
        raise ValueError("market payload has no outcome/token mapping")
    if "up" not in by_outcome or "down" not in by_outcome:
        raise ValueError("market must expose UP and DOWN tokens")
    return MarketDefinition(
        market_id=str(payload.get("id", payload.get("market_id", payload.get("slug")))),
        condition_id=str(payload.get("conditionId", payload.get("condition_id"))),
        question=str(payload.get("question", "")),
        start_time=_parse_time(payload.get("startDate", payload.get("start_time"))),
        end_time=_parse_time(payload.get("endDate", payload.get("end_time"))),
        up_token_id=by_outcome["up"],
        down_token_id=by_outcome["down"],
        slug=str(payload.get("slug", payload.get("id", payload.get("market_id", "")))),
    )


def discover_btc_5m(markets: Iterable[Mapping[str, Any]], now: datetime) -> list[MarketDefinition]:
    result: list[MarketDefinition] = []
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    for payload in markets:
        if not payload.get("active", False) or payload.get("closed", False):
            continue
        try:
            market = normalize_market_payload(payload)
        except (KeyError, TypeError, ValueError):
            continue
        text = market.question.lower()
        if "btc" not in text or "5m" not in text or market.duration_seconds != 300:
            continue
        if not (market.start_time <= now_utc < market.end_time):
            continue
        result.append(market)
    return result

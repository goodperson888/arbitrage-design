#!/usr/bin/env python3
"""Fetch resolved BTC 5m markets and public taker-BUY trades from Polymarket."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any

import requests


GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"


def iso_to_us(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


class OfficialTradeFetcher:
    def __init__(self, proxy: str | None, request_delay_seconds: float) -> None:
        self.session = requests.Session()
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.request_delay_seconds = request_delay_seconds

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    proxies=self.proxies,
                    timeout=30,
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                if self.request_delay_seconds > 0:
                    time.sleep(self.request_delay_seconds)
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(2 ** attempt)
        raise RuntimeError(f"request failed after retries: {url}: {last_error}")

    def fetch_market(self, slug: str) -> dict[str, Any] | None:
        value = self.get_json(f"{GAMMA_URL}/markets/slug/{slug}")
        return value if isinstance(value, dict) else None

    def fetch_buy_trades(self, condition_id: str) -> list[dict[str, Any]]:
        all_trades: list[dict[str, Any]] = []
        for offset in (0, 10_000):
            page = self.get_json(
                f"{DATA_API_URL}/trades",
                params={
                    "market": condition_id,
                    "limit": 10_000,
                    "offset": offset,
                    "takerOnly": "true",
                    "side": "BUY",
                },
            )
            if not isinstance(page, list):
                raise RuntimeError(f"unexpected trades response for {condition_id}")
            all_trades.extend(page)
            if len(page) < 10_000:
                return all_trades
        raise RuntimeError(f"trade history exceeds the public 20,000-row pagination limit: {condition_id}")


def market_events(
    market: dict[str, Any],
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    slug = str(market["slug"])
    slug_start_seconds = int(slug.rsplit("-", 1)[1])
    event_rows = market.get("events") or []
    actual_start = event_rows[0].get("eventStartTime") if event_rows else None
    event_start_us = iso_to_us(actual_start) if actual_start else slug_start_seconds * 1_000_000
    event_end_us = iso_to_us(str(market["endDate"]))
    outcomes = json.loads(market["outcomes"])
    outcome_prices = json.loads(market["outcomePrices"])
    clob_token_ids = json.loads(market["clobTokenIds"])
    winner_index = next(
        (index for index, value in enumerate(outcome_prices) if float(value) >= 0.999),
        None,
    )
    if winner_index is None:
        raise ValueError("market is not resolved to a binary winner")
    winner = str(outcomes[winner_index]).upper()
    condition_id = str(market["conditionId"])

    events: list[dict[str, Any]] = [{
        "schemaVersion": 1,
        "eventId": f"official-{slug}-definition",
        "eventType": "MARKET_DEFINITION",
        "venue": "POLYMARKET",
        "marketId": slug,
        "sourceTimeUs": event_start_us,
        "receivedTimeUs": event_start_us,
        "source": {"type": "official_gamma", "collector": "fetch_polymarket_official_trades"},
        "payload": {
            "underlying": "BTC/USD",
            "eventStartUs": str(event_start_us),
            "eventEndUs": str(event_end_us),
            "outcomes": [str(value).upper() for value in outcomes],
            "conditionId": condition_id,
            "clobTokenIds": clob_token_ids,
            "feeSchedule": market.get("feeSchedule"),
            "tradeHistoryComplete": True,
        },
    }]

    seen: set[tuple[str, str, str, str, int]] = set()
    for trade in trades:
        timestamp_us = int(trade["timestamp"]) * 1_000_000
        if not (event_start_us <= timestamp_us <= event_end_us):
            continue
        outcome = str(trade["outcome"]).upper()
        if outcome not in {"UP", "DOWN"}:
            continue
        identity = (
            str(trade.get("transactionHash") or ""),
            str(trade["asset"]),
            str(trade["price"]),
            str(trade["size"]),
            timestamp_us,
        )
        if identity in seen:
            continue
        seen.add(identity)
        events.append({
            "schemaVersion": 1,
            "eventId": (
                f"official-trade-{identity[0]}-{identity[1]}-"
                f"{identity[2]}-{identity[3]}-{timestamp_us}"
            ),
            "eventType": "TRADE",
            "venue": "POLYMARKET",
            "marketId": slug,
            "instrumentId": str(trade["asset"]),
            "outcome": outcome,
            "sourceTimeUs": timestamp_us,
            "receivedTimeUs": timestamp_us,
            "source": {"type": "official_data_api", "collector": "fetch_polymarket_official_trades"},
            "payload": {
                "outcome": outcome,
                "side": "BUY",
                "price": str(trade["price"]),
                "size": str(trade["size"]),
                "transactionHash": identity[0],
                "conditionId": condition_id,
            },
        })

    events.append({
        "schemaVersion": 1,
        "eventId": f"official-{slug}-settlement",
        "eventType": "SETTLEMENT",
        "venue": "POLYMARKET",
        "marketId": slug,
        "sourceTimeUs": event_end_us + 1,
        "receivedTimeUs": event_end_us + 1,
        "source": {"type": "official_gamma", "collector": "fetch_polymarket_official_trades"},
        "payload": {"winnerOutcome": winner, "settlementPrice": "1"},
    })
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch official Polymarket BTC 5m trade history")
    parser.add_argument("--latest-start", required=True, type=int, help="Latest 5m market start Unix timestamp")
    parser.add_argument("--market-count", type=int, default=500)
    parser.add_argument(
        "--stride-seconds",
        type=int,
        default=300,
        help="Spacing between sampled market starts (default: every 5m market)",
    )
    parser.add_argument("--proxy")
    parser.add_argument("--request-delay-seconds", type=float, default=0.02)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    fetcher = OfficialTradeFetcher(args.proxy, args.request_delay_seconds)
    all_events: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    skipped: list[str] = []
    successful_markets = 0

    if args.stride_seconds <= 0 or args.stride_seconds % 300:
        parser.error("--stride-seconds must be a positive multiple of 300")

    for index in range(args.market_count):
        start_seconds = args.latest_start - index * args.stride_seconds
        slug = f"btc-updown-5m-{start_seconds}"
        try:
            market = fetcher.fetch_market(slug)
            if not market or not market.get("closed"):
                skipped.append(slug)
                continue
            trades = fetcher.fetch_buy_trades(str(market["conditionId"]))
            events = market_events(market, trades)
            all_events.extend(events)
            successful_markets += 1
            print(
                f"[{index + 1}/{args.market_count}] {slug}: "
                f"{sum(event['eventType'] == 'TRADE' for event in events)} BUY trades",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - retain per-market diagnostics.
            failed.append({"marketId": slug, "error": str(exc)})
            print(f"[{index + 1}/{args.market_count}] {slug}: ERROR {exc}", flush=True)

    all_events.sort(key=lambda event: (int(event["sourceTimeUs"]), str(event["eventId"])))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for event in all_events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    manifest_path = args.out.with_suffix(".manifest.json")
    manifest = {
        "source": "Polymarket official Gamma + Data API",
        "requestedMarketCount": args.market_count,
        "successfulMarketCount": successful_markets,
        "skippedMarketCount": len(skipped),
        "failedMarketCount": len(failed),
        "eventCount": len(all_events),
        "latestStart": args.latest_start,
        "strideSeconds": args.stride_seconds,
        "oldestAttemptedStart": args.latest_start - (args.market_count - 1) * args.stride_seconds,
        "skippedMarkets": skipped,
        "failedMarkets": failed,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(all_events)} events across {successful_markets} markets: {args.out}")
    print(f"Manifest: {manifest_path}")
    return 0 if successful_markets else 2


if __name__ == "__main__":
    raise SystemExit(main())

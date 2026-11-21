from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Any, Iterable

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


@dataclass(frozen=True)
class MarketLabel:
    market_id: str
    start_ms: int
    end_ms: int
    winner: str


@dataclass(frozen=True)
class Candle:
    open_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


def load_market_labels(path: Path, limit: int | None = None) -> list[MarketLabel]:
    definitions: dict[str, tuple[int, int]] = {}
    winners: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            event = json.loads(line)
            market_id = str(event.get("marketId", ""))
            payload = event.get("payload") or {}
            if event.get("eventType") == "MARKET_DEFINITION":
                try:
                    definitions[market_id] = (
                        int(payload["eventStartUs"]) // 1_000,
                        int(payload["eventEndUs"]) // 1_000,
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            elif event.get("eventType") == "SETTLEMENT":
                winner = str(payload.get("winnerOutcome", "")).upper()
                if winner in {"UP", "DOWN"}:
                    winners[market_id] = winner
    rows = [
        MarketLabel(market_id, times[0], times[1], winners[market_id])
        for market_id, times in definitions.items() if market_id in winners
    ]
    rows.sort(key=lambda item: item.start_ms)
    return rows[-limit:] if limit else rows


class HistoricalVenueClient:
    BINANCE_URLS = (
        "https://api.binance.com/api/v3/klines",
        "https://data-api.binance.vision/api/v3/klines",
        "https://api1.binance.com/api/v3/klines",
    )
    HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
    DERIBIT_URL = "https://www.deribit.com/api/v2"

    def __init__(self, *, proxy: str | None = None, timeout: float = 30) -> None:
        if httpx is None:
            raise ImportError("httpx is required for historical backfill")
        self.client = httpx.Client(proxy=proxy, timeout=timeout)
        self.endpoints_used: dict[str, str] = {}

    def _get_with_fallback(
        self, name: str, urls: Iterable[str], *, params: dict[str, Any]
    ) -> httpx.Response:
        last_error: Exception | None = None
        for url in urls:
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                self.endpoints_used[name] = url
                return response
            except Exception as exc:  # preserve the final error after trying official alternatives.
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"no endpoints configured for {name}")

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "HistoricalVenueClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def binance_klines(self, start_ms: int, end_ms: int) -> list[Candle]:
        rows: list[Candle] = []
        cursor = start_ms
        while cursor < end_ms:
            response = self._get_with_fallback("binance", self.BINANCE_URLS, params={
                "symbol": "BTCUSDT", "interval": "1m", "startTime": cursor,
                "endTime": end_ms, "limit": 1000,
            })
            response.raise_for_status()
            values = response.json()
            if not values:
                break
            rows.extend(Candle(
                open_ms=int(item[0]), close_ms=int(item[6]), open=float(item[1]),
                high=float(item[2]), low=float(item[3]), close=float(item[4]), volume=float(item[5]),
            ) for item in values)
            next_cursor = int(values[-1][6]) + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            time.sleep(0.05)
        return _deduplicate_candles(rows)

    def hyperliquid_klines(self, start_ms: int, end_ms: int) -> list[Candle]:
        rows: list[Candle] = []
        cursor = start_ms
        # Candle snapshot responses are bounded; seven-hour chunks stay well below the limit.
        chunk_ms = 7 * 60 * 60 * 1_000
        while cursor < end_ms:
            chunk_end = min(end_ms, cursor + chunk_ms)
            response = self.client.post(self.HYPERLIQUID_URL, json={
                "type": "candleSnapshot",
                "req": {"coin": "BTC", "interval": "1m", "startTime": cursor, "endTime": chunk_end},
            })
            response.raise_for_status()
            values = response.json()
            rows.extend(Candle(
                open_ms=int(item["t"]), close_ms=int(item["T"]), open=float(item["o"]),
                high=float(item["h"]), low=float(item["l"]), close=float(item["c"]),
                volume=float(item["v"]),
            ) for item in values)
            cursor = chunk_end + 1
            time.sleep(0.05)
        return _deduplicate_candles(rows)

    def deribit_perpetual_klines(self, start_ms: int, end_ms: int) -> list[Candle]:
        rows: list[Candle] = []
        cursor = start_ms
        chunk_ms = 2 * 24 * 60 * 60 * 1_000
        while cursor < end_ms:
            chunk_end = min(end_ms, cursor + chunk_ms)
            response = self._get_with_fallback(
                "deribit", (
                    f"{self.DERIBIT_URL}/public/get_tradingview_chart_data",
                    "https://history.deribit.com/api/v2/public/get_tradingview_chart_data",
                ), params={
                    "instrument_name": "BTC-PERPETUAL", "start_timestamp": cursor,
                    "end_timestamp": chunk_end, "resolution": "1",
                },
            )
            result = response.json().get("result") or {}
            ticks = result.get("ticks") or []
            for index, tick in enumerate(ticks):
                rows.append(Candle(
                    open_ms=int(tick), close_ms=int(tick) + 60_000 - 1,
                    open=float(result["open"][index]), high=float(result["high"][index]),
                    low=float(result["low"][index]), close=float(result["close"][index]),
                    volume=float(result["volume"][index]) if result.get("volume") else None,
                ))
            cursor = chunk_end + 1
            time.sleep(0.05)
        return _deduplicate_candles(rows)

    def deribit_dvol(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = start_ms
        chunk_ms = 12 * 60 * 60 * 1_000
        while cursor < end_ms:
            chunk_end = min(end_ms, cursor + chunk_ms)
            response = self._get_with_fallback(
                "dvol", (
                    f"{self.DERIBIT_URL}/public/get_volatility_index_data",
                    "https://history.deribit.com/api/v2/public/get_volatility_index_data",
                ), params={"currency": "BTC", "start_timestamp": cursor,
                           "end_timestamp": chunk_end, "resolution": "60"},
            )
            result = response.json().get("result") or {}
            rows.extend(
                {"timestampMs": int(item[0]), "open": item[1], "high": item[2],
                 "low": item[3], "close": item[4]}
                for item in result.get("data", [])
            )
            cursor = chunk_end + 1
            time.sleep(0.05)
        return sorted(
            {int(item["timestampMs"]): item for item in rows}.values(),
            key=lambda item: int(item["timestampMs"]),
        )


def _deduplicate_candles(rows: Iterable[Candle]) -> list[Candle]:
    return sorted({item.open_ms: item for item in rows}.values(), key=lambda item: item.open_ms)


def save_history_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_history_cache(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def candles_to_json(candles: list[Candle]) -> list[dict[str, Any]]:
    return [item.__dict__ for item in candles]


def candles_from_json(rows: list[dict[str, Any]]) -> list[Candle]:
    return [Candle(**item) for item in rows]


def _price_at_start(candles: list[Candle], timestamp_ms: int) -> float | None:
    for candle in candles:
        if candle.open_ms <= timestamp_ms <= candle.close_ms + 1:
            return candle.open
    return None


def _last_closed(candles: list[Candle], timestamp_ms: int) -> Candle | None:
    result = None
    for candle in candles:
        if candle.close_ms <= timestamp_ms:
            result = candle
        else:
            break
    return result


def _dvol_at(rows: list[dict[str, Any]], timestamp_ms: int) -> float | None:
    result = None
    for item in rows:
        if int(item["timestampMs"]) <= timestamp_ms:
            result = float(item["close"])
        else:
            break
    return result


def build_historical_rows(
    markets: list[MarketLabel], venue_candles: dict[str, list[Candle]],
    dvol: list[dict[str, Any]], observation_seconds: Iterable[int],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for market in markets:
        starts = {
            venue: _price_at_start(candles, market.start_ms)
            for venue, candles in venue_candles.items()
        }
        for remaining in observation_seconds:
            observation_ms = market.end_ms - remaining * 1_000
            row: dict[str, Any] = {
                "marketId": market.market_id, "marketStartMs": market.start_ms,
                "marketEndMs": market.end_ms, "observationMs": observation_ms,
                "remainingSeconds": remaining, "winner": market.winner,
                "labelUp": 1 if market.winner == "UP" else 0,
            }
            signals: list[str] = []
            for venue, candles in venue_candles.items():
                candle = _last_closed(candles, observation_ms)
                start = starts[venue]
                current = candle.close if candle else None
                log_return = (
                    math.log(current / start)
                    if current is not None and start is not None and current > 0 and start > 0 else None
                )
                prefix = venue.lower()
                row[f"{prefix}StartPrice"] = start
                row[f"{prefix}Price"] = current
                row[f"{prefix}ReturnFromStart"] = log_return
                row[f"{prefix}DataAgeSeconds"] = (
                    (observation_ms - candle.close_ms) / 1_000 if candle else None
                )
                signal = "UP" if log_return is not None and log_return > 0 else (
                    "DOWN" if log_return is not None and log_return < 0 else "NEUTRAL"
                )
                row[f"{prefix}Signal"] = signal
                if signal != "NEUTRAL":
                    signals.append(signal)
            up_votes = sum(value == "UP" for value in signals)
            down_votes = sum(value == "DOWN" for value in signals)
            row["consensusSignal"] = (
                "UP" if len(signals) == 3 and up_votes > down_votes
                else "DOWN" if len(signals) == 3 and down_votes > up_votes
                else "NEUTRAL"
            )
            row["dvol"] = _dvol_at(dvol, observation_ms)
            output.append(row)
    return output


def _wilson(wins: int, count: int) -> tuple[float, float]:
    if not count:
        return 0.0, 0.0
    z = 1.959963984540054
    p = wins / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * count)) / count) / denominator
    return center - radius, center + radius


def evaluate_historical_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    remaining_values = sorted({int(row["remainingSeconds"]) for row in rows}, reverse=True)
    signals = ("binanceSignal", "hyperliquidSignal", "deribitSignal", "consensusSignal")
    for remaining in remaining_values:
        group = [row for row in rows if int(row["remainingSeconds"]) == remaining]
        for signal_key in signals:
            usable = [row for row in group if row.get(signal_key) in {"UP", "DOWN"}]
            wins = sum(row[signal_key] == row["winner"] for row in usable)
            low, high = _wilson(wins, len(usable))
            up_rows = [row for row in usable if row[signal_key] == "UP"]
            down_rows = [row for row in usable if row[signal_key] == "DOWN"]
            results.append({
                "remainingSeconds": remaining, "signal": signal_key.removesuffix("Signal"),
                "samples": len(usable), "wins": wins,
                "accuracy": wins / len(usable) if usable else None,
                "wilson95Low": low, "wilson95High": high,
                "upSamples": len(up_rows),
                "upAccuracy": sum(row["winner"] == "UP" for row in up_rows) / len(up_rows) if up_rows else None,
                "downSamples": len(down_rows),
                "downAccuracy": sum(row["winner"] == "DOWN" for row in down_rows) / len(down_rows) if down_rows else None,
            })
    return results


def write_historical_outputs(
    output_dir: Path, rows: list[dict[str, Any]], results: list[dict[str, Any]],
    *, source_file: Path, availability: dict[str, str],
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "historical_cross_market_rows.csv"
    fields = sorted({key for row in rows for key in row})
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    json_path = output_dir / "historical_cross_market_results.json"
    json_path.write_text(json.dumps({
        "schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceFile": str(source_file), "rows": len(rows), "availability": availability,
        "results": results,
    }, ensure_ascii=False, indent=2) + "\n")
    report_path = output_dir / "历史跨市场预测关系验证.md"
    lines = [
        "# 历史跨市场预测关系验证", "", 
        f"- Polymarket 已结算市场：{len({row['marketId'] for row in rows})} 个",
        f"- 对齐样本行：{len(rows)} 行", f"- 标签文件：`{source_file}`", "",
        "## 数据可用性", "",
    ]
    lines.extend(f"- {venue}：{status}" for venue, status in availability.items())
    lines.extend([
        "", "## 方向预测结果", "",
        "| 剩余秒数 | 信号源 | 样本 | 胜率 | 95%区间 | UP样本/胜率 | DOWN样本/胜率 |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ])
    def pct(value: Any) -> str:
        return "—" if value is None else f"{float(value):.2%}"
    labels = {"binance": "Binance", "hyperliquid": "Hyperliquid", "deribit": "Deribit永续",
              "consensus": "三者多数票"}
    for item in results:
        lines.append(
            f"| {item['remainingSeconds']} | {labels[item['signal']]} | {item['samples']} | "
            f"{pct(item['accuracy'])} | {pct(item['wilson95Low'])}–{pct(item['wilson95High'])} | "
            f"{item['upSamples']}/{pct(item['upAccuracy'])} | "
            f"{item['downSamples']}/{pct(item['downAccuracy'])} |"
        )
    lines.extend([
        "", "## 口径说明", "",
        "1. 每个信号只使用观察时点以前已经完整收盘的 1 分钟 K 线，避免未来数据泄漏。",
        "2. `returnFromStart > 0` 预测 UP，`< 0` 预测 DOWN；三者多数票只统计三家同时有数据的市场，不做样本内调参。",
        "3. 90秒、30秒等非整分钟观察点只能使用上一根已收盘 K 线，CSV 中保留 `DataAgeSeconds`。",
        "4. Deribit 公共接口不提供完整历史期权盘口/逐时点 IV 快照，所以历史部分用 BTC-PERPETUAL 与可用 DVOL；ATM IV、RR、期权流由实时采集器从现在开始积累。",
        "5. 这是关系研究，不等同于可交易收益。后续需与 Polymarket 入场价、盘口深度、手续费及成交条件交叉验证。", "",
    ])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, json_path, report_path

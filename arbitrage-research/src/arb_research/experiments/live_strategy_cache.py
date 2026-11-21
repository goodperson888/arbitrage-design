from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator

from ..fees import taker_fee
from .orderbook_depth import BookLevel, consume_asks, wilson_interval


SCHEMA_VERSION = 1
# Bump this whenever fill causality, partial-fill, queue, slippage, or fee semantics
# change. Indexed market events remain reusable, but cached strategy results must not.
EXECUTION_MODEL_VERSION = 1


@dataclass(frozen=True)
class CachedStrategyCase:
    case_id: str
    outcome: str
    price_min: Decimal
    price_max: Decimal
    max_remaining_seconds: int
    min_remaining_seconds: int = 0
    quantity: Decimal = Decimal("1")
    trigger: str = "quote"
    min_trade_size: Decimal = Decimal("0")
    execution_price_cap: Decimal | None = None
    entry_vwap_min: Decimal | None = None
    entry_momentum_min: Decimal | None = None
    entry_momentum_lookback_seconds: int = 5
    max_book_age_ms: int = 2_000
    max_quote_gap_seconds: Decimal = Decimal("5")
    fee_model: str = "polymarket_crypto_taker"
    fee_rate: Decimal = Decimal("0.07")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CachedStrategyCase":
        return cls(
            case_id=str(raw["case_id"]),
            outcome=str(raw["outcome"]).upper(),
            price_min=Decimal(str(raw["price_min"])),
            price_max=Decimal(str(raw["price_max"])),
            max_remaining_seconds=int(raw["max_remaining_seconds"]),
            min_remaining_seconds=int(raw.get("min_remaining_seconds", 0)),
            quantity=Decimal(str(raw.get("quantity", 1))),
            trigger=str(raw.get("trigger", "quote")),
            min_trade_size=Decimal(str(raw.get("min_trade_size", 0))),
            execution_price_cap=(
                Decimal(str(raw["execution_price_cap"]))
                if raw.get("execution_price_cap") is not None else None
            ),
            entry_vwap_min=(
                Decimal(str(raw["entry_vwap_min"]))
                if raw.get("entry_vwap_min") is not None else None
            ),
            entry_momentum_min=(
                Decimal(str(raw["entry_momentum_min"]))
                if raw.get("entry_momentum_min") is not None else None
            ),
            entry_momentum_lookback_seconds=int(raw.get("entry_momentum_lookback_seconds", 5)),
            max_book_age_ms=int(raw.get("max_book_age_ms", 2_000)),
            max_quote_gap_seconds=Decimal(str(raw.get("max_quote_gap_seconds", 5))),
            fee_model=str(raw.get("fee_model", "polymarket_crypto_taker")),
            fee_rate=Decimal(str(raw.get("fee_rate", "0.07"))),
        )

    def validate(self) -> None:
        if self.outcome not in {"UP", "DOWN"}:
            raise ValueError(f"{self.case_id}: outcome must be UP or DOWN")
        if self.trigger not in {"quote", "buy_trade"}:
            raise ValueError(f"{self.case_id}: trigger must be quote or buy_trade")
        if not (Decimal("0") <= self.price_min <= self.price_max <= Decimal("1")):
            raise ValueError(f"{self.case_id}: invalid price range")
        if not (0 <= self.min_remaining_seconds < self.max_remaining_seconds):
            raise ValueError(f"{self.case_id}: invalid remaining-seconds band")
        if self.quantity <= 0:
            raise ValueError(f"{self.case_id}: quantity must be positive")
        if self.entry_vwap_min is not None and not (self.price_min <= self.entry_vwap_min <= self.price_max):
            raise ValueError(f"{self.case_id}: entry_vwap_min must be within price range")
        if self.entry_momentum_min is not None and self.entry_momentum_lookback_seconds <= 0:
            raise ValueError(f"{self.case_id}: entry_momentum_lookback_seconds must be positive")

    @property
    def price_cap(self) -> Decimal:
        return self.execution_price_cap or self.price_max

    def canonical(self) -> dict[str, Any]:
        values = {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self).items()
        }
        values["execution_model_version"] = EXECUTION_MODEL_VERSION
        return values

    @property
    def case_hash(self) -> str:
        encoded = json.dumps(
            self.canonical(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def connect_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS source_files (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS markets (
            market_id TEXT PRIMARY KEY,
            start_us INTEGER,
            end_us INTEGER,
            winner TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            event_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL,
            source_us INTEGER NOT NULL,
            received_us INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            best_ask TEXT,
            best_ask_size TEXT,
            asks_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_market_outcome_source
            ON snapshots(market_id, outcome, source_us);
        CREATE INDEX IF NOT EXISTS idx_snapshots_market_outcome_received
            ON snapshots(market_id, outcome, received_us);
        CREATE TABLE IF NOT EXISTS trades (
            event_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL,
            source_us INTEGER NOT NULL,
            received_us INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            side TEXT NOT NULL,
            price TEXT NOT NULL,
            size TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trades_market_outcome_source
            ON trades(market_id, outcome, source_us);
        CREATE TABLE IF NOT EXISTS case_specs (
            case_hash TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            spec_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS case_results (
            case_hash TEXT NOT NULL,
            market_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            winner TEXT NOT NULL,
            covered INTEGER NOT NULL,
            filled INTEGER NOT NULL,
            signal_us INTEGER,
            execution_us INTEGER,
            remaining_seconds TEXT,
            signal_price TEXT,
            entry_vwap TEXT,
            worst_price TEXT,
            fee_per_share TEXT,
            pnl_per_share TEXT,
            book_age_ms TEXT,
            PRIMARY KEY(case_hash, market_id)
        );
        CREATE INDEX IF NOT EXISTS idx_case_results_hash ON case_results(case_hash);
        """
    )
    return connection


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            while True:
                try:
                    line = handle.readline()
                except EOFError:
                    break
                if not line:
                    break
                if line.strip():
                    yield json.loads(line)
    except EOFError:
        return


def refresh_event_cache(connection: sqlite3.Connection, events_dir: Path) -> dict[str, int]:
    counters = {"filesScanned": 0, "filesSkipped": 0, "rowsRead": 0}
    paths = sorted(events_dir.glob("date=*/hour=*/part-*.ndjson*"))
    for path in paths:
        stat = path.stat()
        relative = str(path.resolve())
        cached = connection.execute(
            "SELECT size, mtime_ns FROM source_files WHERE path = ?", (relative,)
        ).fetchone()
        if cached and int(cached["size"]) == stat.st_size and int(cached["mtime_ns"]) == stat.st_mtime_ns:
            counters["filesSkipped"] += 1
            continue
        counters["filesScanned"] += 1
        for row in _rows(path):
            counters["rowsRead"] += 1
            _ingest_row(connection, row)
        connection.execute(
            "INSERT OR REPLACE INTO source_files(path,size,mtime_ns) VALUES(?,?,?)",
            (relative, stat.st_size, stat.st_mtime_ns),
        )
        connection.commit()
    return counters


def _ingest_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    event_type = str(row.get("eventType", ""))
    market_id = str(row.get("marketId", ""))
    if not market_id:
        return
    payload = row.get("payload") or {}
    if event_type == "MARKET_DEFINITION":
        connection.execute(
            """INSERT INTO markets(market_id,start_us,end_us,winner) VALUES(?,?,?,NULL)
               ON CONFLICT(market_id) DO UPDATE SET start_us=excluded.start_us,end_us=excluded.end_us""",
            (market_id, int(payload["eventStartUs"]), int(payload["eventEndUs"])),
        )
        return
    if event_type == "SETTLEMENT":
        winner = str(payload.get("winnerOutcome", "")).upper()
        if winner in {"UP", "DOWN"}:
            connection.execute(
                """INSERT INTO markets(market_id,winner) VALUES(?,?)
                   ON CONFLICT(market_id) DO UPDATE SET winner=excluded.winner""",
                (market_id, winner),
            )
        return
    event_id = str(row.get("eventId") or hashlib.sha256(
        json.dumps(row, sort_keys=True).encode()
    ).hexdigest())
    source_us = int(row.get("sourceTimeUs") or row.get("receivedTimeUs") or 0)
    received_us = int(row.get("receivedTimeUs") or source_us)
    if event_type == "ORDERBOOK_SNAPSHOT":
        for item in payload.get("outcomes") or []:
            outcome = str(item.get("outcome", "")).upper()
            if outcome not in {"UP", "DOWN"}:
                continue
            asks = [
                [str(level[0]), str(level[1])]
                for level in item.get("asks") or []
                if len(level) == 2 and Decimal(str(level[1])) > 0
            ]
            connection.execute(
                """INSERT OR IGNORE INTO snapshots(
                       event_id,market_id,source_us,received_us,outcome,best_ask,best_ask_size,asks_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    f"{event_id}:{outcome}", market_id, source_us, received_us, outcome,
                    str(item["bestAsk"]) if item.get("bestAsk") is not None else None,
                    str(item["bestAskSize"]) if item.get("bestAskSize") is not None else None,
                    json.dumps(asks, separators=(",", ":")),
                ),
            )
    elif event_type == "TRADE":
        outcome = str(row.get("outcome") or payload.get("outcome") or "").upper()
        if outcome in {"UP", "DOWN"}:
            connection.execute(
                """INSERT OR IGNORE INTO trades(
                       event_id,market_id,source_us,received_us,outcome,side,price,size
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    event_id, market_id, source_us, received_us, outcome,
                    str(payload.get("side", "")).upper(),
                    str(payload.get("price", "0")), str(payload.get("size", "0")),
                ),
            )


def load_cases(path: Path) -> list[CachedStrategyCase]:
    payload = json.loads(path.read_text())
    raw_cases = payload.get("cases") if isinstance(payload, dict) else payload
    cases = [CachedStrategyCase.from_dict(raw) for raw in raw_cases]
    for case in cases:
        case.validate()
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("case_id values must be unique")
    return cases


def evaluate_cases_incrementally(
    connection: sqlite3.Connection, cases: Iterable[CachedStrategyCase]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    summaries: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    counters = {"marketsEvaluated": 0, "cachedMarketResults": 0}
    markets = connection.execute(
        """SELECT market_id,start_us,end_us,winner FROM markets
           WHERE start_us IS NOT NULL AND end_us IS NOT NULL AND winner IN ('UP','DOWN')
           ORDER BY start_us"""
    ).fetchall()
    for case in cases:
        case_hash = case.case_hash
        connection.execute(
            "INSERT OR REPLACE INTO case_specs(case_hash,case_id,spec_json) VALUES(?,?,?)",
            (case_hash, case.case_id, json.dumps(case.canonical(), sort_keys=True)),
        )
        completed = {
            str(row["market_id"])
            for row in connection.execute(
                "SELECT market_id FROM case_results WHERE case_hash = ?", (case_hash,)
            )
        }
        counters["cachedMarketResults"] += len(completed)
        for market in markets:
            if str(market["market_id"]) in completed:
                continue
            result = _evaluate_market(connection, market, case)
            _insert_case_result(connection, case_hash, result)
            counters["marketsEvaluated"] += 1
        connection.commit()
        case_rows = connection.execute(
            "SELECT * FROM case_results WHERE case_hash = ? ORDER BY market_id", (case_hash,)
        ).fetchall()
        summaries.append(_summarize(case, case_rows))
        fills.extend(
            {"caseId": case.case_id, **dict(row)} for row in case_rows if row["filled"]
        )
    return summaries, fills, counters


def _evaluate_market(
    connection: sqlite3.Connection, market: sqlite3.Row, case: CachedStrategyCase
) -> dict[str, Any]:
    market_id = str(market["market_id"])
    end_us = int(market["end_us"])
    band_start = end_us - case.max_remaining_seconds * 1_000_000
    band_end = end_us - case.min_remaining_seconds * 1_000_000
    covered = _has_coverage(
        connection, market_id, case.outcome, band_start, band_end,
        int(case.max_quote_gap_seconds * 1_000_000),
    )
    empty = {
        "market_id": market_id, "outcome": case.outcome, "winner": str(market["winner"]),
        "covered": int(covered), "filled": 0, "signal_us": None, "execution_us": None,
        "remaining_seconds": None, "signal_price": None, "entry_vwap": None,
        "worst_price": None, "fee_per_share": None, "pnl_per_share": None,
        "book_age_ms": None,
    }
    if case.trigger == "quote":
        if not covered:
            return empty
        candidates = connection.execute(
            """SELECT source_us,received_us,best_ask,asks_json FROM snapshots
               WHERE market_id=? AND outcome=? AND source_us BETWEEN ? AND ?
               ORDER BY source_us,received_us""",
            (market_id, case.outcome, band_start, band_end),
        )
        for snapshot in candidates:
            price = _decimal(snapshot["best_ask"])
            if price is None or not (case.price_min <= price <= case.price_max):
                continue
            consumed = _consume_json(snapshot["asks_json"], case)
            if consumed is None or (case.entry_vwap_min is not None and consumed[0] < case.entry_vwap_min):
                continue
            if not _passes_entry_momentum(
                connection, market_id, case.outcome, int(snapshot["source_us"]),
                price, case,
            ):
                continue
            return _filled_result(empty, case, market, int(snapshot["source_us"]),
                                  int(snapshot["received_us"]), price, consumed, Decimal("0"))
        return empty

    trades = connection.execute(
        """SELECT source_us,received_us,price,size FROM trades
           WHERE market_id=? AND outcome=? AND side='BUY' AND source_us BETWEEN ? AND ?
           ORDER BY source_us,received_us""",
        (market_id, case.outcome, band_start, band_end),
    )
    for trade in trades:
        price = Decimal(str(trade["price"]))
        size = Decimal(str(trade["size"]))
        if not (case.price_min <= price <= case.price_max) or size < case.min_trade_size:
            continue
        snapshot = connection.execute(
            """SELECT source_us,received_us,best_ask,asks_json FROM snapshots
               WHERE market_id=? AND outcome=? AND received_us<=?
               ORDER BY received_us DESC LIMIT 1""",
            (market_id, case.outcome, int(trade["received_us"])),
        ).fetchone()
        if snapshot is None:
            continue
        age_ms = Decimal(int(trade["received_us"]) - int(snapshot["received_us"])) / Decimal("1000")
        if age_ms < 0 or age_ms > case.max_book_age_ms:
            continue
        consumed = _consume_json(snapshot["asks_json"], case)
        if consumed is None or (case.entry_vwap_min is not None and consumed[0] < case.entry_vwap_min):
            continue
        snapshot_price = _decimal(snapshot["best_ask"])
        if snapshot_price is None or not _passes_entry_momentum(
            connection, market_id, case.outcome, int(snapshot["source_us"]),
            snapshot_price, case,
        ):
            continue
        return _filled_result(empty, case, market, int(trade["source_us"]),
                              int(trade["received_us"]), price, consumed, age_ms)
    return empty


def _passes_entry_momentum(
    connection: sqlite3.Connection,
    market_id: str,
    outcome: str,
    current_source_us: int,
    current_price: Decimal,
    case: CachedStrategyCase,
) -> bool:
    """Require the outcome's displayed ask to rise by the configured amount.

    The comparison is against the latest snapshot at or before the lookback
    boundary, so the test never uses information received after entry.
    Cases without a floor retain the original behavior.
    """
    if case.entry_momentum_min is None:
        return True
    boundary_us = current_source_us - case.entry_momentum_lookback_seconds * 1_000_000
    prior = connection.execute(
        """SELECT best_ask FROM snapshots
           WHERE market_id=? AND outcome=? AND source_us<=?
           ORDER BY source_us DESC, received_us DESC LIMIT 1""",
        (market_id, outcome, boundary_us),
    ).fetchone()
    prior_price = _decimal(prior["best_ask"]) if prior is not None else None
    return prior_price is not None and current_price - prior_price >= case.entry_momentum_min


def _has_coverage(
    connection: sqlite3.Connection, market_id: str, outcome: str,
    band_start: int, band_end: int, tolerance_us: int,
) -> bool:
    times = [
        int(row[0]) for row in connection.execute(
            """SELECT source_us FROM snapshots
               WHERE market_id=? AND outcome=? AND best_ask IS NOT NULL
               AND source_us BETWEEN ? AND ? ORDER BY source_us""",
            (market_id, outcome, band_start - tolerance_us, band_end + tolerance_us),
        )
    ]
    if not times or times[0] > band_start + tolerance_us or times[-1] < band_end - tolerance_us:
        return False
    relevant = [value for value in times if band_start <= value <= band_end]
    boundaries = [band_start, *relevant, band_end]
    return all(later - earlier <= tolerance_us for earlier, later in zip(boundaries, boundaries[1:]))


def _consume_json(raw: str, case: CachedStrategyCase) -> tuple[Decimal, Decimal, Decimal] | None:
    levels = [
        BookLevel(Decimal(str(item[0])), Decimal(str(item[1])))
        for item in json.loads(raw)
    ]
    return consume_asks(
        levels, case.quantity, case.price_cap,
        fee_model=case.fee_model, fee_rate=case.fee_rate,
    )


def _filled_result(
    result: dict[str, Any], case: CachedStrategyCase, market: sqlite3.Row,
    signal_us: int, execution_us: int, signal_price: Decimal,
    consumed: tuple[Decimal, Decimal, Decimal], book_age_ms: Decimal,
) -> dict[str, Any]:
    vwap, fee_per_share, worst = consumed
    won = case.outcome == str(market["winner"])
    pnl = (Decimal("1") if won else Decimal("0")) - vwap - fee_per_share
    return {
        **result,
        "filled": 1,
        "signal_us": signal_us,
        "execution_us": execution_us,
        "remaining_seconds": str(
            Decimal(int(market["end_us"]) - signal_us) / Decimal("1000000")
        ),
        "signal_price": str(signal_price),
        "entry_vwap": str(vwap),
        "worst_price": str(worst),
        "fee_per_share": str(fee_per_share),
        "pnl_per_share": str(pnl),
        "book_age_ms": str(book_age_ms),
    }


def _insert_case_result(connection: sqlite3.Connection, case_hash: str, row: dict[str, Any]) -> None:
    columns = list(row)
    connection.execute(
        f"INSERT OR REPLACE INTO case_results(case_hash,{','.join(columns)}) "
        f"VALUES({','.join('?' for _ in range(len(columns) + 1))})",
        (case_hash, *(row[column] for column in columns)),
    )


def _summarize(case: CachedStrategyCase, rows: list[sqlite3.Row]) -> dict[str, Any]:
    covered = [row for row in rows if row["covered"]]
    filled = [row for row in rows if row["filled"]]
    wins = sum(row["outcome"] == row["winner"] for row in filled)
    pnl = sum((Decimal(str(row["pnl_per_share"])) for row in filled), Decimal("0"))
    entry = sum((Decimal(str(row["entry_vwap"])) for row in filled), Decimal("0"))
    low, high = wilson_interval(wins, len(filled))
    return {
        **case.canonical(),
        "caseHash": case.case_hash,
        "settledMarketCount": len(rows),
        "coverageQualifiedMarketCount": len(covered),
        "fillCount": len(filled),
        "winCount": wins,
        "lossCount": len(filled) - wins,
        "winRate": str(Decimal(wins) / len(filled)) if filled else None,
        "winRateCi95Low": low,
        "winRateCi95High": high,
        "averageEntryVwap": str(entry / len(filled)) if filled else None,
        "netPnlPerShare": str(pnl / len(filled)) if filled else None,
    }


def _decimal(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def write_cached_report(
    output_dir: Path, summaries: list[dict[str, Any]], fills: list[dict[str, Any]],
    refresh: dict[str, int], evaluation: dict[str, int], cache_path: Path,
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "incremental_strategy_results.json"
    json_path.write_text(json.dumps({
        "schemaVersion": SCHEMA_VERSION,
        "cache": str(cache_path.resolve()),
        "refresh": refresh,
        "evaluation": evaluation,
        "results": summaries,
    }, ensure_ascii=False, indent=2) + "\n")
    csv_path = output_dir / "incremental_strategy_results.csv"
    fields = sorted({key for row in summaries for key in row})
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summaries)
    fills_path = output_dir / "incremental_strategy_fills.csv"
    fill_fields = sorted({key for row in fills for key in row})
    with fills_path.open("w", newline="", encoding="utf-8-sig") as handle:
        if fill_fields:
            writer = csv.DictWriter(handle, fieldnames=fill_fields)
            writer.writeheader()
            writer.writerows(fills)
    report_path = output_dir / "实时策略增量验证.md"
    lines = [
        "# 实时策略增量验证", "",
        f"- 本次读取发生变化的事件文件：{refresh['filesScanned']}",
        f"- 跳过未变化文件：{refresh['filesSkipped']}",
        f"- 本次新增计算市场×策略：{evaluation['marketsEvaluated']}",
        f"- 直接复用缓存市场×策略：{evaluation['cachedMarketResults']}", "",
        "| 策略 | 触发 | 方向 | 价格 | 时间带 | 数量 | 覆盖市场 | 成交 | 胜/负 | 胜率 | 净收益/份 |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        rate = "—" if row["winRate"] is None else f"{float(row['winRate']):.2%}"
        pnl = "—" if row["netPnlPerShare"] is None else f"{float(row['netPnlPerShare']):+.4f}"
        lines.append(
            f"| {row['case_id']} | {row['trigger']} | {row['outcome']} | "
            f"{row['price_min']}–{row['price_max']} | "
            f"{row['max_remaining_seconds']}–{row['min_remaining_seconds']}秒 | "
            f"{row['quantity']} | {row['coverageQualifiedMarketCount']} | {row['fillCount']} | "
            f"{row['winCount']}/{row['lossCount']} | {rate} | {pnl} |"
        )
    lines.extend([
        "", "说明：quote策略要求完整连续盘口覆盖；buy_trade策略按历史BUY成交触发，",
        "并只使用触发时本机已经收到、且未超过最大盘口年龄的最近盘口执行。每个策略、每个市场最多一笔。", "",
    ])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, csv_path, report_path

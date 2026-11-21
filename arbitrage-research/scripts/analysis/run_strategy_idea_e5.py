#!/usr/bin/env python3
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime
import gzip
import json
from pathlib import Path
import statistics
import sys
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.experiments.strategy_idea_validation import read_partition_rows


FILTERS = (
    "none", "spot_from_start", "spot_5s", "futures_from_start",
    "spot_futures_from_start", "hyperliquid_from_start",
    "deribit_from_start", "three_venues_from_start", "chainlink_from_start",
)


@dataclass
class Summary:
    entries: int = 0
    wins: int = 0
    positive: int = 0
    pnl_sum: float = 0.0

    def add(self, row: dict[str, Any]) -> None:
        self.entries += 1
        self.wins += int(row["won"])
        self.positive += int(float(row["pnlPerShare"]) > 0)
        self.pnl_sum += float(row["pnlPerShare"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行E5跨市场与时段增量复验")
    parser.add_argument(
        "--cross-dir", type=Path,
        default=ROOT / "data/cross_market_validation",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/research_tests/e5_cross_market_time_20260826",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates()
    market_ids = {row["marketId"] for row in candidates}
    cross = load_cross(args.cross_dir, market_ids)
    joined: list[dict[str, Any]] = []
    for row in candidates:
        state = latest_state(cross.get(row["marketId"], []), int(row["entryUs"]))
        if state is None:
            continue
        joined.append({**row, **state})
    medians: dict[str, float | None] = {}
    for candidate in sorted({row["candidate"] for row in joined}):
        values = [
            abs(float(row["spot60s"])) for row in joined
            if row["candidate"] == candidate and row["split"] == "validation"
            and row.get("spot60s") is not None
        ]
        medians[candidate] = statistics.median(values) if values else None
    filter_summaries: defaultdict[tuple[str, str, str], Summary] = defaultdict(Summary)
    daily_filter_summaries: defaultdict[tuple[str, str, str], Summary] = defaultdict(Summary)
    time_summaries: defaultdict[tuple[str, str, str], Summary] = defaultdict(Summary)
    detail_rows: list[dict[str, Any]] = []
    for row in joined:
        for filter_name in FILTERS:
            if passes_filter(row, filter_name):
                for scope in ("all", row["split"]):
                    filter_summaries[(scope, row["candidate"], filter_name)].add(row)
                detail_rows.append({
                    "candidate": row["candidate"], "split": row["split"],
                    "marketId": row["marketId"], "filter": filter_name,
                    "beijingDate": row["beijingDate"],
                    "direction": row["direction"], "pnlPerShare": row["pnlPerShare"],
                    "won": row["won"], "crossAgeMs": row["crossAgeUs"] / 1_000,
                })
                if row["split"] == "validation":
                    daily_filter_summaries[(
                        row["beijingDate"], row["candidate"], filter_name,
                    )].add(row)
        hour = int(row["beijingDateTimeHour"])
        session = (
            "00_06" if hour < 6 else "06_12" if hour < 12
            else "12_18" if hour < 18 else "18_24"
        )
        volatility = volatility_regime(
            row.get("spot60s"), median=medians[row["candidate"]]
        )
        regimes = [f"session_{session}"]
        if volatility is not None:
            regimes.append(volatility)
        for regime in regimes:
            for scope in ("all", row["split"]):
                time_summaries[(scope, row["candidate"], regime)].add(row)
    filter_rows = [summary_row(key, value) for key, value in filter_summaries.items()]
    time_rows = [summary_row(key, value, label_key="regime") for key, value in time_summaries.items()]
    daily_filter_rows = [
        {"beijingDate": key[0], **summary_row(("validation", *key[1:]), value)}
        for key, value in daily_filter_summaries.items()
    ]
    result = {
        "schemaVersion": 1,
        "experiment": "E5_跨市场和时段组合复验",
        "candidateRows": len(candidates),
        "joinedRows": len(joined),
        "joinedMarkets": len({row["marketId"] for row in joined}),
        "volatilityMedianAbsSpot60s": medians,
        "filterSummaries": sorted(filter_rows, key=lambda row: (
            row["scope"], row["candidate"], row["filter"]
        )),
        "dailyValidationFilterSummaries": sorted(daily_filter_rows, key=lambda row: (
            row["beijingDate"], row["candidate"], row["filter"]
        )),
        "regimeSummaries": sorted(time_rows, key=lambda row: (
            row["scope"], row["candidate"], row["regime"]
        )),
    }
    result_path = args.out / "e5_results.json"
    detail_path = args.out / "e5_joined_filters.csv"
    report_path = args.out / "E5_跨市场与时段组合复验.md"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with detail_path.open("w", newline="", encoding="utf-8-sig") as handle:
        fields = list(detail_rows[0]) if detail_rows else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(detail_rows)
    write_report(report_path, result)
    print(result_path)
    print(detail_path)
    print(report_path)
    return 0


def load_candidates() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    e1 = ROOT / "outputs/research_tests/e1_low_price_target_exit_20260826/e1_simulations.csv.gz"
    with gzip.open(e1, "rt", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not (
                row["outcome"] == "UP" and row["window"] == "180_120"
                and row["band"] == "30_40" and float(row["recovery"]) == 0
                and float(row["target"]) == 0.70 and float(row["delaySeconds"]) == 0
                and row["fallback"] == "resolution"
            ):
                continue
            rows.append({
                "candidate": "E1_UP_180_120_30_40_target70",
                "split": row["split"], "marketId": row["marketId"],
                "entryUs": int(row["entryUs"]), "direction": "UP",
                "beijingDate": beijing_date(int(row["marketStartUs"])),
                "beijingDateTimeHour": beijing_hour(int(row["marketStartUs"])),
                "won": row["winner"] == "UP", "pnlPerShare": float(row["pnlPerShare"]),
            })
    e2 = ROOT / "outputs/research_tests/e2_early_stability_20260826/e2_entries.csv.gz"
    with gzip.open(e2, "rt", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not (
                row["outcome"] == "UP" and row["window"] == "240_180"
                and row["band"] == "85_90" and int(row["stabilitySeconds"]) == 0
                and float(row["worstPriceCap"]) == 0.90
            ):
                continue
            rows.append({
                "candidate": "E2_UP_240_180_85_90_stable0",
                "split": row["split"], "marketId": row["marketId"],
                "entryUs": int(row["entryUs"]), "direction": "UP",
                "beijingDate": row["beijingDate"],
                "beijingDateTimeHour": beijing_hour(int(row["marketStartUs"])),
                "won": row["won"] == "True", "pnlPerShare": float(row["pnlPerShare"]),
            })
    e4 = ROOT / "outputs/research_tests/e4_reverse_low_win_20260826/e4_entries.csv"
    with e4.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if not (
                row["case"] == "pullback_5s_01" and row["reverseDirection"] == "DOWN"
            ):
                continue
            rows.append({
                "candidate": "E4_pullback5_reverse_DOWN",
                "split": row["split"], "marketId": row["marketId"],
                "entryUs": int(row["signalUs"]), "direction": "DOWN",
                "beijingDate": row["beijingDate"],
                "beijingDateTimeHour": beijing_hour_from_date_and_market(
                    row["beijingDate"], row["marketId"]
                ),
                "won": row["won"] == "True", "pnlPerShare": float(row["pnlPerShare"]),
            })
    return rows


def beijing_hour(market_start_us: int) -> int:
    return int((market_start_us // 1_000_000 + 8 * 3600) % 86400 // 3600)


def beijing_date(market_start_us: int) -> str:
    return datetime.fromtimestamp(
        market_start_us / 1_000_000, tz=ZoneInfo("Asia/Shanghai")
    ).strftime("%Y-%m-%d")


def beijing_hour_from_date_and_market(_date: str, market_id: str) -> int:
    return beijing_hour(int(market_id.rsplit("-", 1)[-1]) * 1_000_000)


def load_cross(
    cross_dir: Path, market_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    output: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    paths = sorted((cross_dir / "venue_snapshots").glob("date=*/hour=*/part-*.ndjson*"))
    for row in read_partition_rows(paths):
        market_id = str(row.get("marketId", ""))
        if market_id not in market_ids:
            continue
        features = row.get("features") or {}
        item: dict[str, Any] = {"sourceUs": int(row.get("sourceTimeUs") or 0)}
        mappings = {
            "spot": "binance", "futures": "binanceFutures",
            "hyperliquid": "hyperliquid", "deribit": "deribit",
            "chainlink": "chainlink",
        }
        for prefix, source in mappings.items():
            values = features.get(source) or {}
            item[f"{prefix}FromStart"] = values.get("returnFromStart")
            item[f"{prefix}5s"] = values.get("return5s")
            item[f"{prefix}60s"] = values.get("return60s")
        output[market_id].append(item)
    for market_id in output:
        output[market_id].sort(key=lambda row: row["sourceUs"])
    return dict(output)


def latest_state(rows: list[dict[str, Any]], entry_us: int) -> dict[str, Any] | None:
    if not rows:
        return None
    index = bisect_right([row["sourceUs"] for row in rows], entry_us) - 1
    if index < 0:
        return None
    selected = rows[index]
    age = entry_us - int(selected["sourceUs"])
    if age > 3_000_000:
        return None
    return {**selected, "crossAgeUs": age}


def aligned(value: Any, direction: str) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return numeric > 0 if direction == "UP" else numeric < 0


def volatility_regime(value: Any, *, median: float | None) -> str | None:
    if value is None or median is None:
        return None
    try:
        numeric = abs(float(value))
    except (TypeError, ValueError):
        return None
    return "high_abs_spot60s" if numeric > median else "low_abs_spot60s"


def passes_filter(row: dict[str, Any], filter_name: str) -> bool:
    direction = row["direction"]
    if filter_name == "none":
        return True
    if filter_name == "spot_from_start":
        return aligned(row.get("spotFromStart"), direction)
    if filter_name == "spot_5s":
        return aligned(row.get("spot5s"), direction)
    if filter_name == "futures_from_start":
        return aligned(row.get("futuresFromStart"), direction)
    if filter_name == "spot_futures_from_start":
        return aligned(row.get("spotFromStart"), direction) and aligned(row.get("futuresFromStart"), direction)
    if filter_name == "hyperliquid_from_start":
        return aligned(row.get("hyperliquidFromStart"), direction)
    if filter_name == "deribit_from_start":
        return aligned(row.get("deribitFromStart"), direction)
    if filter_name == "three_venues_from_start":
        return all(aligned(row.get(key), direction) for key in (
            "spotFromStart", "futuresFromStart", "hyperliquidFromStart"
        ))
    if filter_name == "chainlink_from_start":
        return aligned(row.get("chainlinkFromStart"), direction)
    raise ValueError(filter_name)


def summary_row(
    key: tuple[str, str, str], value: Summary, *, label_key: str = "filter"
) -> dict[str, Any]:
    scope, candidate, label = key
    return {
        "scope": scope, "candidate": candidate, label_key: label,
        "entries": value.entries, "wins": value.wins,
        "losses": value.entries - value.wins,
        "winRate": value.wins / value.entries,
        "positiveRate": value.positive / value.entries,
        "averageNetPnlPerShare": value.pnl_sum / value.entries,
        "totalNetPnlPerShare": value.pnl_sum,
    }


def write_report(path: Path, result: dict[str, Any]) -> None:
    filters = [
        row for row in result["filterSummaries"] if row["scope"] == "validation"
    ]
    regimes = [
        row for row in result["regimeSummaries"] if row["scope"] == "validation"
    ]
    lines = [
        "# E5：跨市场与时段组合复验", "",
        "## 固定候选", "",
        "- E1：UP、剩余180–120秒、VWAP 0.30–0.40、目标0.70。",
        "- E2：UP、剩余240–180秒、VWAP 0.85–0.90、稳定0秒、最坏价≤0.90。",
        "- E4：高位方向5秒回落0.01后，严格反向买DOWN。",
        f"- 候选记录{result['candidateRows']}行；成功对齐跨市场{result['joinedRows']}行/{result['joinedMarkets']}个市场；最大年龄3秒。",
        "- 只验证预先固定的外部过滤，不重新搜索价格和时间阈值。", "",
        "## 时间外验证：跨市场过滤", "",
        "| 候选 | 过滤 | 笔数 | 胜/负 | 胜率 | 正收益率 | 平均净收益/份 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in filters:
        if row["entries"] < 10:
            continue
        lines.append(
            f"| {row['candidate']} | {row['filter']} | {row['entries']} | "
            f"{row['wins']}/{row['losses']} | {row['winRate']:.1%} | "
            f"{row['positiveRate']:.1%} | {row['averageNetPnlPerShare']:+.4f} |"
        )
    lines.extend([
        "", "## 时间外验证：时段和波动环境", "",
        "| 候选 | 环境 | 笔数 | 胜率 | 正收益率 | 平均净收益/份 |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for row in regimes:
        if row["entries"] < 10:
            continue
        lines.append(
            f"| {row['candidate']} | {row['regime']} | {row['entries']} | "
            f"{row['winRate']:.1%} | {row['positiveRate']:.1%} | {row['averageNetPnlPerShare']:+.4f} |"
        )
    stable = [
        row for row in filters
        if row["filter"] != "none" and row["entries"] >= 100
        and row["averageNetPnlPerShare"] > 0
    ]
    lines.extend(["", "## 结论", ""])
    if stable:
        top = max(stable, key=lambda row: row["averageNetPnlPerShare"])
        lines.append(
            f"有{len(stable)}个外部过滤达到验证段至少100笔且正收益；最佳为"
            f"{top['candidate']} + {top['filter']}，{top['entries']}笔，平均"
            f"{top['averageNetPnlPerShare']:+.4f}/份。但还必须与同候选无过滤基线比较机会损失并继续新日期验证。"
        )
        daily = [
            row for row in result["dailyValidationFilterSummaries"]
            if row["candidate"] == top["candidate"] and row["filter"] == top["filter"]
        ]
        lines.extend([
            "", "### 最佳外部过滤逐日", "",
            "| 北京日期 | 笔数 | 胜率 | 平均净收益/份 |",
            "|---|---:|---:|---:|",
        ])
        for row in daily:
            lines.append(
                f"| {row['beijingDate']} | {row['entries']} | {row['winRate']:.1%} | {row['averageNetPnlPerShare']:+.4f} |"
            )
        if len(daily) < 2 or any(row["averageNetPnlPerShare"] <= 0 for row in daily):
            lines.append("\n过滤后仍未在每个验证自然日保持正收益，E5不判定通过。")
        else:
            lines.append("\n过滤后跨至少两个验证自然日为正，可作为固定候选继续新日期验证，但不自动修改实盘。")
    else:
        lines.append("没有外部过滤同时达到验证段至少100笔和正净收益；E5不通过，外部市场继续作为记录字段而非硬门槛。")
    lines.extend([
        "", "## 限制", "",
        "- 时段只分四个6小时区间，避免按单个小时过拟合；少于10笔不展示。",
        "- 高低波动按每个候选验证段的Binance Spot绝对60秒收益中位数切分。",
        "- 跨市场过滤减少机会，必须同时看保留笔数和净收益，不能只看胜率。",
        "- Deribit这里使用永续方向；期权DVOL、Skew和资金流仍作为独立状态字段，不直接替代方向。", "",
        "## 复现命令", "", "```bash",
        "PYTHONPATH=src python3 scripts/analysis/run_strategy_idea_e5.py", "```", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

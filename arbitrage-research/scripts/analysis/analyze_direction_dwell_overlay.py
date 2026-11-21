#!/usr/bin/env python3
"""T056：方向停留时间因子在成熟策略上的增量回测。"""

from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from statistics import mean
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.analysis.analyze_intramarket_exits import consume, load_market_snapshots  # noqa: E402

QTY = Decimal("10")
MAX_INTERVAL_SECONDS = Decimal("2")

FACTORS = (
    ("share_ge_60", "入场前本方向停留占比≥60%"),
    ("share_ge_70", "入场前本方向停留占比≥70%"),
    ("run_ge_15", "入场前本方向最长连续≥15秒"),
    ("run_ge_30", "入场前本方向最长连续≥30秒"),
    ("share60_flips3", "停留占比≥60%且方向切换≤3次"),
)


def as_float(row: dict[str, Any], key: str) -> float | None:
    try:
        value = row.get(key)
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(bool(row.get("won")) for row in rows)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "winRate": wins / len(rows) if rows else None,
        "avgPnlPerShare": mean(float(row["pnlPerShare"]) for row in rows) if rows else None,
        "totalPnlPerShare": sum(float(row["pnlPerShare"]) for row in rows),
    }


def direction_dwell(snapshots: list[dict[str, Any]], end_us: int, execution_us: int) -> dict[str, Any]:
    observations: list[tuple[int, Decimal, Decimal]] = []
    for snapshot in snapshots:
        source_us = int(snapshot["sourceUs"])
        if source_us > execution_us or source_us >= end_us:
            continue
        up = consume(snapshot["asks"].get("UP") or [], QTY, cap=Decimal("0.99"))
        down = consume(snapshot["asks"].get("DOWN") or [], QTY, cap=Decimal("0.99"))
        if up is None or down is None:
            continue
        observations.append((source_us, up[0], down[0]))
    if len(observations) < 2:
        return {"upSeconds": 0.0, "downSeconds": 0.0, "neutralSeconds": 0.0, "totalSeconds": 0.0,
                "upShare": None, "downShare": None, "longestUpRun": 0.0, "longestDownRun": 0.0,
                "flipCount": None, "lastDirection": None}

    up_seconds = down_seconds = neutral_seconds = Decimal("0")
    current_direction: str | None = None
    current_run = Decimal("0")
    longest = {"UP": Decimal("0"), "DOWN": Decimal("0")}
    flips = 0
    for (previous_us, up, down), (current_us, _, _) in zip(observations, observations[1:]):
        gap = Decimal(current_us - previous_us) / Decimal("1000000")
        if gap <= 0 or gap > MAX_INTERVAL_SECONDS:
            current_direction = None
            current_run = Decimal("0")
            continue
        direction = "UP" if up > down else "DOWN" if down > up else None
        interval = min(gap, MAX_INTERVAL_SECONDS)
        if direction is None:
            neutral_seconds += interval
            current_direction = None
            current_run = Decimal("0")
            continue
        if direction == current_direction:
            current_run += interval
        else:
            if current_direction in longest:
                longest[current_direction] = max(longest[current_direction], current_run)
            if current_direction is not None:
                flips += 1
            current_direction = direction
            current_run = interval
        if direction == "UP":
            up_seconds += interval
        else:
            down_seconds += interval
    if current_direction in longest:
        longest[current_direction] = max(longest[current_direction], current_run)
    total = up_seconds + down_seconds + neutral_seconds
    return {
        "upSeconds": float(up_seconds), "downSeconds": float(down_seconds),
        "neutralSeconds": float(neutral_seconds), "totalSeconds": float(total),
        "upShare": float(up_seconds / total) if total else None,
        "downShare": float(down_seconds / total) if total else None,
        "longestUpRun": float(longest["UP"]), "longestDownRun": float(longest["DOWN"]),
        "flipCount": flips, "lastDirection": current_direction,
    }


def qualifies(row: dict[str, Any], factor: str) -> bool:
    share = as_float(row, "targetShare")
    longest = as_float(row, "targetLongestRun")
    flips = as_float(row, "flipCount")
    if factor == "share_ge_60":
        return share is not None and share >= 0.60
    if factor == "share_ge_70":
        return share is not None and share >= 0.70
    if factor == "run_ge_15":
        return longest is not None and longest >= 15
    if factor == "run_ge_30":
        return longest is not None and longest >= 30
    if factor == "share60_flips3":
        return share is not None and share >= 0.60 and flips is not None and flips <= 3
    return False


def fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def fmt_pnl(value: float | None) -> str:
    return "—" if value is None else f"{value:+.4f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path,
        default=ROOT / "outputs/research_tests/t055_mature_reversal_overlay_20260901/mature_baseline_rows.json",
    )
    parser.add_argument("--cache", type=Path, default=ROOT / "data/derived/live_strategy_cache.sqlite3")
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/research_tests/t056_direction_dwell_20260901",
    )
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))
    source_rows = [dict(row) for row in source.get("rows", [])]
    mature = [
        row for row in source_rows
        if as_float(row, "momentum5") is not None and float(row["momentum5"]) >= 0.01
        and 0.80 <= float(row["entryVwap"]) <= 0.85
        and 30 <= float(row["remainingSeconds"]) <= 90
    ]
    mature.sort(key=lambda row: int(row["executionUs"]))

    connection = sqlite3.connect(args.cache)
    end_cache = {str(mid): int(end) for mid, end in connection.execute("SELECT market_id,end_us FROM markets WHERE end_us IS NOT NULL")}
    snapshot_cache: dict[str, list[dict[str, Any]]] = {}
    enriched: list[dict[str, Any]] = []
    for row in mature:
        market_id = str(row["marketId"])
        if market_id not in snapshot_cache:
            snapshot_cache[market_id] = load_market_snapshots(connection, market_id)
        dwell = direction_dwell(snapshot_cache[market_id], end_cache[market_id], int(row["executionUs"]))
        item = dict(row)
        outcome = str(row["outcome"])
        item["dwell"] = dwell
        item["targetShare"] = dwell["upShare"] if outcome == "UP" else dwell["downShare"]
        item["targetLongestRun"] = dwell["longestUpRun"] if outcome == "UP" else dwell["longestDownRun"]
        item["targetSeconds"] = dwell["upSeconds"] if outcome == "UP" else dwell["downSeconds"]
        item["targetDirection"] = outcome
        enriched.append(item)
    connection.close()

    results: dict[str, Any] = {}
    for outcome in ("UP", "DOWN"):
        baseline = [row for row in enriched if row["outcome"] == outcome]
        split = max(1, int(len(baseline) * 0.70))
        train, validation = baseline[:split], baseline[split:]
        base_stats = stats(baseline)
        variants = []
        for factor, label in FACTORS:
            kept = [row for row in baseline if qualifies(row, factor)]
            variants.append({
                "factor": factor, "label": label, "all": stats(kept),
                "train": stats([row for row in train if qualifies(row, factor)]),
                "validation": stats([row for row in validation if qualifies(row, factor)]),
                "retainedRate": len(kept) / len(baseline) if baseline else None,
                "filteredWins": base_stats["wins"] - stats(kept)["wins"],
                "avoidedLosses": base_stats["losses"] - stats(kept)["losses"],
                "winRateDelta": (stats(kept)["winRate"] - base_stats["winRate"] if kept and baseline else None),
                "avgPnlDelta": (stats(kept)["avgPnlPerShare"] - base_stats["avgPnlPerShare"] if kept and baseline else None),
            })
        results[outcome] = {"baseline": base_stats, "trainBaseline": stats(train), "validationBaseline": stats(validation), "variants": variants}

    args.out.mkdir(parents=True, exist_ok=True)
    json_path = args.out / "direction_dwell_overlay_results.json"
    json_path.write_text(json.dumps({
        "schemaVersion": 1, "experiment": "T056_direction_dwell_overlay",
        "definition": {
            "baseline": "真实主动买入≥10份 + 10份VWAP 0.80–0.85 + 剩余90–30秒 + 盘口年龄≤2秒 + 5秒同方向动量≥0.01",
            "dwell": "入场前两边均可执行时，按不超过2秒的连续快照间隔累计UP/DOWN较高一侧的停留时间",
            "factors": dict(FACTORS), "maxGapSeconds": 2,
        }, "sourceRows": len(source_rows), "matureRows": len(mature), "results": results, "rows": enriched,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# T056：方向停留时间因子叠加成熟策略",
        "",
        "## 口径",
        "",
        "- 成熟基线：真实主动买入≥10份、实际10份VWAP 0.80–0.85、剩余90–30秒、盘口年龄≤2秒、入场前5秒同方向动量≥0.01。",
        f"- 475笔主动成交中有 {len(mature)} 笔满足完整成熟条件；UP/DOWN分开。",
        "- 方向停留只使用入场前数据：两边10份可执行VWAP谁更高，就把不超过2秒的连续快照间隔计入该方向；超过2秒的缺口不外推。",
        "- 因子逐一加入，按执行时间前70%训练、后30%时间外验证。净收益沿用成熟策略源记录。",
        "",
        "## 结论先行",
        "",
        "- 方向停留因子本身不能替代成熟策略；应同时看保留率、误删的胜单和避开的败单。",
        "- 如果某组只在全样本高、后30%明显回落，视为历史分段现象，不进入实盘。",
        "- UP的停留占比≥60%全样本为56笔、94.6%、+0.1170/份，后30%为17笔全胜；但仍过滤了46笔胜单，不能据此提高整体累计收益。",
        "- DOWN的停留占比≥70%全样本为71笔、91.5%、+0.0883/份，与基线胜率和收益几乎持平；后30%为24笔全胜，暂按样本外候选观察。",
        "- 最长连续15/30秒没有稳定增益；‘占比≥60%且切换≤3次’在当前快照口径下没有形成可用样本。",
        "",
    ]
    for outcome in ("UP", "DOWN"):
        base = results[outcome]["baseline"]
        lines += [f"## {outcome}（基线{base['trades']}笔，{fmt_rate(base['winRate'])}，{fmt_pnl(base['avgPnlPerShare'])}/份）", "", "| 附加因子 | 笔数 | 胜/负 | 胜率 | 平均净收益/份 | 保留率 | 胜率变化 | 收益变化 | 过滤胜单 | 避开败单 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for item in results[outcome]["variants"]:
            s = item["all"]
            lines.append(f"| {item['label']} | {s['trades']} | {s['wins']}/{s['losses']} | {fmt_rate(s['winRate'])} | {fmt_pnl(s['avgPnlPerShare'])} | {fmt_rate(item['retainedRate'])} | {fmt_rate(item['winRateDelta'])} | {fmt_pnl(item['avgPnlDelta'])} | {item['filteredWins']} | {item['avoidedLosses']} |")
        lines += ["", "### 后30%时间外", "", "| 附加因子 | 笔数 | 胜/负 | 胜率 | 平均净收益/份 |", "|---|---:|---:|---:|---:|"]
        vb = results[outcome]["validationBaseline"]
        lines.append(f"| 成熟基线 | {vb['trades']} | {vb['wins']}/{vb['losses']} | {fmt_rate(vb['winRate'])} | {fmt_pnl(vb['avgPnlPerShare'])} |")
        for item in results[outcome]["variants"]:
            s = item["validation"]
            lines.append(f"| {item['label']} | {s['trades']} | {s['wins']}/{s['losses']} | {fmt_rate(s['winRate'])} | {fmt_pnl(s['avgPnlPerShare'])} |")
        lines.append("")
    lines += [
        "## 限制",
        "",
        "- 方向停留是预测市场两边可执行VWAP的相对高低，不等同于Binance相对开盘价的方向停留；两者需另行对齐。",
        "- 快照缺口超过2秒不向前填充，因此稀疏期的停留统计会变短。",
        "- 少于100笔的因子结果只作候选，不能直接改实盘；本实验不修改纸上交易和实盘。",
        "",
        "## 复现命令",
        "",
        "```bash",
        "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_direction_dwell_overlay.py",
        "```",
    ]
    md_path = args.out / "方向停留时间叠加成熟策略回测.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"source={len(source_rows)} mature={len(mature)}")
    print(json_path.resolve())
    print(md_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""T057：成熟策略 + 回落恢复 + 方向停留的最小固定组合验证。"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs/research_tests/t057_reversal_dwell_combo_20260901"
T055 = ROOT / "outputs/research_tests/t055_mature_reversal_overlay_20260901/mature_reversal_overlay_results.json"
T056 = ROOT / "outputs/research_tests/t056_direction_dwell_20260901/direction_dwell_overlay_results.json"

COMBOS = (
    ("DOWN", "amplitude_ge_03", 0.70, "DOWN：回落幅度≥0.03、5秒内恢复≥0.01 + 停留占比≥70%"),
    ("DOWN", "medium_3_5_recover_1_3", 0.70, "DOWN：回落3–5秒、1–3秒恢复≥0.01 + 停留占比≥70%"),
    ("UP", "amplitude_ge_03", 0.60, "UP：回落幅度≥0.03、5秒内恢复≥0.01 + 停留占比≥60%"),
)


def stats(rows):
    wins = sum(bool(row["won"]) for row in rows)
    return {"trades": len(rows), "wins": wins, "losses": len(rows) - wins,
            "winRate": wins / len(rows) if rows else None,
            "avgPnlPerShare": mean(float(row["pnlPerShare"]) for row in rows) if rows else None,
            "totalPnlPerShare": sum(float(row["pnlPerShare"]) for row in rows)}


def rate(v):
    return "—" if v is None else f"{v:.1%}"


def pnl(v):
    return "—" if v is None else f"{v:+.4f}"


def main():
    a = json.loads(T055.read_text(encoding="utf-8"))
    b = json.loads(T056.read_text(encoding="utf-8"))
    dwell = {(str(row["marketId"]), str(row["outcome"]), int(row["executionUs"])): row for row in b["rows"]}
    rows = []
    for row in a["rows"]:
        key = (str(row["marketId"]), str(row["outcome"]), int(row["executionUs"]))
        right = dwell.get(key)
        if right is None:
            continue
        item = dict(row)
        item["won"] = str(row["winner"]) == str(row["outcome"])
        item["targetShare"] = right.get("targetShare")
        item["dwellUpShare"] = right.get("dwell", {}).get("upShare")
        item["dwellDownShare"] = right.get("dwell", {}).get("downShare")
        rows.append(item)
    rows.sort(key=lambda r: int(r["executionUs"]))
    results = []
    for outcome, reversal, share_threshold, label in COMBOS:
        base = [row for row in rows if row["outcome"] == outcome]
        split = max(1, int(len(base) * 0.70))
        selected = [row for row in base if row.get("reversalFactors", {}).get(reversal) is not None and row.get("targetShare") is not None and float(row["targetShare"]) >= share_threshold]
        train = selected[:split]
        validation = [row for row in selected if row in base[split:]]
        bstats = stats(base); sstats = stats(selected)
        results.append({"outcome": outcome, "label": label, "reversal": reversal, "shareThreshold": share_threshold,
                        "baseline": bstats, "all": sstats, "train": stats(train), "validation": stats(validation),
                        "retainedRate": len(selected) / len(base) if base else None,
                        "filteredWins": bstats["wins"] - sstats["wins"], "avoidedLosses": bstats["losses"] - sstats["losses"],
                        "winRateDelta": (sstats["winRate"] - bstats["winRate"]) if sstats["winRate"] is not None else None,
                        "avgPnlDelta": (sstats["avgPnlPerShare"] - bstats["avgPnlPerShare"]) if sstats["avgPnlPerShare"] is not None else None})
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"schemaVersion": 1, "experiment": "T057_reversal_dwell_combo", "sourceT055": str(T055), "sourceT056": str(T056), "matureRows": len(rows), "combos": results, "rows": rows}
    (OUT / "reversal_dwell_combo_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# T057：成熟策略 + 回落恢复 + 方向停留组合", "", "## 固定口径", "", "- 基线：真实主动买入≥10份、实际10份VWAP 0.80–0.85、剩余90–30秒、盘口年龄≤2秒、5秒同方向动量≥0.01。", f"- 可同时对齐T055回落路径与T056停留字段：{len(rows)}笔。", "- 只验证预先固定的3个组合，不再搜索新阈值；前70%/后30%按执行时间切分。", "", "## 结果", "", "| 组合 | 基线n | 组合n | 胜率 | 基线胜率 | 平均收益/份 | 基线收益/份 | 保留率 | 过滤胜单 | 避开败单 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for item in results:
        bstats, sstats = item["baseline"], item["all"]
        lines.append(f"| {item['label']} | {bstats['trades']} | {sstats['trades']} | {rate(sstats['winRate'])} | {rate(bstats['winRate'])} | {pnl(sstats['avgPnlPerShare'])} | {pnl(bstats['avgPnlPerShare'])} | {rate(item['retainedRate'])} | {item['filteredWins']} | {item['avoidedLosses']} |")
    lines += ["", "## 后30%时间外", "", "| 组合 | 笔数 | 胜/负 | 胜率 | 平均收益/份 |", "|---|---:|---:|---:|---:|"]
    for item in results:
        s = item["validation"]
        lines.append(f"| {item['label']} | {s['trades']} | {s['wins']}/{s['losses']} | {rate(s['winRate'])} | {pnl(s['avgPnlPerShare'])} |")
    lines += ["", "## 结论", "", "- 组合只能用于观察‘高质量子集’，不能把过滤后的平均收益误当成整套策略累计收益。", "- 必须同时考虑过滤掉的胜单、保留率和新增日期样本；本轮少于100笔的组合不进入实盘硬条件。", "- 这轮只验证回落恢复与方向停留的组合，没有把尚未重新对齐的Binance概率/Chainlink距离硬塞进来。", "", "## 复现", "", "```bash", "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_mature_reversal_dwell_combo.py", "```"]
    (OUT / "成熟策略回落恢复方向停留组合.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"matureRows={len(rows)}")
    print(OUT / "成熟策略回落恢复方向停留组合.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

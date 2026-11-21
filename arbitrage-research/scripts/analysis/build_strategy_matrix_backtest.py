#!/usr/bin/env python3
"""Build a Chinese synthesis of the refreshed standalone and ablation runs."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def pnl(value: float | None) -> str:
    return "—" if value is None else f"{value:+.4f}"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pick_e1(rows: list[dict[str, Any]], outcome: str) -> list[dict[str, Any]]:
    eligible = [
        row for row in rows
        if row.get("scope") == "validation" and row.get("outcome") == outcome
        and int(row.get("entries", 0)) >= 20
    ]
    return sorted(eligible, key=lambda row: row["averageNetPnlPerShare"], reverse=True)[:3]


def pick_e2(rows: list[dict[str, Any]], outcome: str) -> list[dict[str, Any]]:
    eligible = [
        row for row in rows
        if row.get("scope") == "validation" and row.get("outcome") == outcome
        and int(row.get("entries", 0)) >= 20
    ]
    return sorted(eligible, key=lambda row: row["averageNetPnlPerShare"], reverse=True)[:3]


def pick_e6(data: dict[str, Any]) -> dict[str, Any]:
    validation = [row for row in data["summaries"] if row.get("scope") == "validation"]
    derived = [row for row in validation if row.get("limitScheme") == "derived_best_bid"]
    best_hybrid = max(derived, key=lambda row: row["hybridPnlPerOfferedSignalShare"]) if derived else None
    best_strict = max(derived, key=lambda row: row["strictPnlPerOfferedSignalShare"]) if derived else None
    return {
        "caseId": data.get("currentCase"),
        "signalCount": data.get("signalCount"),
        "fakValidation": data.get("fakBaseline", {}).get("validation"),
        "bestDerivedHybrid": best_hybrid,
        "bestDerivedStrict": best_strict,
    }


def e10_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "label": row["label"], "trades": row["trades"], "winRate": row["winRate"],
            "avgPnlPerShare": row["avgPnlPerShare"], "keptTradeRate": row["keptTradeRate"],
            "excludedLosses": row["excludedLosses"], "excludedWins": row["excludedWins"],
        }
        for row in data["variants"]
    ]


def e12_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row for row in data["signalSummaries"]
        if row["split"] == "validation" and row["selection"] == "first_side"
        and row["outcome"] == "ANY" and row["momentumFloor"] is None
        and row["threshold"] in {"0.60", "0.65", "0.70"}
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=ROOT,
        help="arbitrage-research根目录",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/research_tests/e13_strategy_matrix_20260828",
    )
    args = parser.parse_args()
    out = args.out
    e1 = load(out / "e1_low_price_target_exit/e1_results.json")
    e2 = load(out / "e2_early_stability/e2_results.json")
    e6_down = load(out / "e6_down/e6_results.json")
    e6_up = load(out / "e6_up/e6_results.json")
    e10_down = load(out / "e10_down/e10_results.json")
    e10_up = load(out / "e10_up/e10_results.json")
    e12 = load(out.parent / "e12_strict_upcross_20260828/e12_strict_upcross_results.json")
    regime = load(out / "market_regime_results.json")

    e1_selected = {outcome: pick_e1(e1["summaries"], outcome) for outcome in ("UP", "DOWN")}
    e2_selected = {outcome: pick_e2(e2["summaries"], outcome) for outcome in ("UP", "DOWN")}
    e6_selected = {"DOWN": pick_e6(e6_down), "UP": pick_e6(e6_up)}
    e10_selected = {"DOWN": e10_rows(e10_down), "UP": e10_rows(e10_up)}
    e12_selected = e12_rows(e12)
    e12_exits = [
        row for row in e12["exitSummaries"]
        if row["split"] == "validation" and row["selection"] == "first_side"
        and row["outcome"] == "ANY" and row["threshold"] == "0.60"
        and row["windowSeconds"] in {60, 90}
    ]
    e12_chain = [
        row for row in e12["chainlinkSummaries"]
        if row["split"] == "validation" and row["selection"] == "first_side"
        and row["outcome"] == "ANY" and row["threshold"] == "0.60"
        and row["windowSeconds"] == 90 and row["entries"] >= 5
    ]
    overall = regime["summaries"]["overall"]
    by_outcome = {row["label"]: row for row in regime["summaries"]["byOutcome"]}

    lines = [
        "# E13：独立策略与成熟基线逐一叠加回测汇总", "",
        "本报告把前面列出的独立策略和辅助条件全部用刷新后的数据重跑，并统一列出UP、DOWN。它只用于研究比较，不自动改变纸上交易或实盘。", "",
        "## 统一数据口径", "",
        f"- 成熟基线特征样本：{overall['trades']}笔，UP {by_outcome['UP']['trades']}笔、DOWN {by_outcome['DOWN']['trades']}笔；平均收益分别为UP `{pnl(by_outcome['UP']['avgPnlPerShare'])}`、DOWN `{pnl(by_outcome['DOWN']['avgPnlPerShare'])}`/份。",
        f"- E12严格突破使用1477个已结算市场；验证段按市场时间70/30切分，验证段不是训练段。",
        "- E1/E2的‘最佳’仅是从本次网格的验证段排序，存在多重参数选择偏差；不能直接当成实盘参数。",
        "- 所有收益都应同时看胜率、平均净收益、样本数、保留率和是否能真实成交/退出。", "",
        "## 一、适合独立运行的策略", "",
        "### E12严格向上首次突破（一次只买最先突破的一侧）", "",
        "| 剩余窗口 | 突破阈值 | 验证笔数 | 胜率 | 平均净收益/份 |",
        "|---:|---:|---:|---:|---:|",
    ]
    for row in sorted(e12_selected, key=lambda r: (r["windowSeconds"], r["threshold"])):
        lines.append(f"| {row['windowSeconds']}秒内 | {row['threshold']} | {row['entries']} | {pct(row['winRate'])} | {pnl(row['averageHoldPnlPerShare'])} |")
    lines += [
        "", "解释：这是‘上涨进阈值’，不是‘回落到阈值’。时间外样本中0.60的60秒窗口为22笔、`-0.0030/份`，90秒为61笔、`+0.0062/份`；0.65/90秒为67笔、73.1%、`+0.0546/份`，但仍少于100笔。", "",
        "### E1低价买入与目标退出：UP/DOWN各自验证段最好的3个网格", "",
        "| 方向 | 时间窗 | 入场带 | 恢复 | 目标 | 退出兜底 | 笔数 | 胜率(正收益) | 平均净收益/份 |",
        "|---|---|---|---:|---:|---|---:|---:|---:|",
    ]
    for outcome in ("UP", "DOWN"):
        for row in e1_selected[outcome]:
            lines.append(
                f"| {outcome} | {row['window']} | {row['band']} | {row['recovery']:.2f} | {row['target']:.2f} | {row['fallback']} | "
                f"{row['entries']} | {pct(row['positiveRate'])} | {pnl(row['averageNetPnlPerShare'])} |"
            )
    lines += [
        "", "E1的高点估计来自大量组合筛选，UP和DOWN都能找到验证段正收益组合，但不是预注册参数；下一轮必须冻结少数参数并做新日期验证。", "",
        "### E2早期稳定高位：UP/DOWN各自验证段最好的3个网格", "",
        "| 方向 | 时间窗 | 价格带 | 稳定秒数 | 最坏价上限 | 笔数 | 胜率 | 平均净收益/份 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for outcome in ("UP", "DOWN"):
        for row in e2_selected[outcome]:
            lines.append(
                f"| {outcome} | {row['window']} | {row['band']} | {row['stabilitySeconds']} | {row['worstPriceCap']:.2f} | "
                f"{row['entries']} | {pct(row['winRate'])} | {pnl(row['averageNetPnlPerShare'])} |"
            )
    lines += [
        "", "E2部分组合验证段胜率很高，但它们改变了入场时点，且稳定5/10秒可能只剩几十笔；必须作为独立策略和成熟基线平行比较。", "",
        "### E6 Maker/FAK：UP/DOWN执行方式对照", "",
        "历史只能用盘口和成交回放近似Maker，不能知道真实排队位置；下表是可比的派生最佳买价方案，仅供方向和执行风险参考。E6采用最新 `trade_*_80_85_90_30_q10_size10` 基础成交案例，没有额外加入5秒动量；它只验证成交方式，不能与带动量的主候选直接合并。", "",
        "| 方向 | 策略信号数 | FAK验证胜率/收益 | 最佳严格Maker等待 | 严格Maker成交率 | 严格Maker收益/提供信号 | 最佳混合方案等待 | 混合成交率 | 混合收益/提供信号 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for outcome in ("DOWN", "UP"):
        row = e6_selected[outcome]
        fak = row["fakValidation"] or {}
        strict = row["bestDerivedStrict"] or {}
        hybrid = row["bestDerivedHybrid"] or {}
        lines.append(
            f"| {outcome} | {row['signalCount']} | {fak.get('signals', '—')} / {pct(fak.get('winRate'))} / {pnl(fak.get('pnlPerShare'))} | "
            f"{strict.get('waitSeconds', '—')}秒 | {pct(strict.get('strictFillRateOffered'))} | {pnl(strict.get('strictPnlPerOfferedSignalShare'))} | "
            f"{hybrid.get('waitSeconds', '—')}秒 | {pct(hybrid.get('hybridFillRateOffered'))} | {pnl(hybrid.get('hybridPnlPerOfferedSignalShare'))} |"
        )
    lines += [
        "", "E6不是方向策略。历史回放中Maker的成交率和收益受等待时间、报价方案影响很大，UP/DOWN都不能据此直接切换实盘执行方式；需要真实订单回报。", "",
        "## 二、成熟基线逐一加入辅助条件（E10，UP/DOWN分开）", "",
        "### DOWN基线", "",
        "| 变体 | 笔数 | 胜率 | 平均净收益/份 | 保留率 | 排除失败 | 同时排除成功 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in e10_selected["DOWN"]:
        lines.append(f"| {row['label']} | {row['trades']} | {pct(row['winRate'])} | {pnl(row['avgPnlPerShare'])} | {pct(row['keptTradeRate'])} | {row['excludedLosses']} | {row['excludedWins']} |")
    lines += [
        "", "### UP基线", "",
        "| 变体 | 笔数 | 胜率 | 平均净收益/份 | 保留率 | 排除失败 | 同时排除成功 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in e10_selected["UP"]:
        lines.append(f"| {row['label']} | {row['trades']} | {pct(row['winRate'])} | {pnl(row['avgPnlPerShare'])} | {pct(row['keptTradeRate'])} | {row['excludedLosses']} | {row['excludedWins']} |")
    lines += [
        "", "E10解释：5秒动量和Binance Spot在这批数据中对UP、DOWN都有点估计改善，但机会保留率约40%–63%，且同时过滤了不少成功单。E0方向概率覆盖率很低，UP/DOWN都不能作为硬过滤。", "",
        "## 三、Chainlink锚定距离加入成熟基线/突破策略", "",
        "E12在0.60、90秒、最先突破一侧的验证段中，Chainlink方向化终端距离支持0–5bps为16笔、81.2%、`+0.1751/份`；反向为13笔、46.2%、`-0.1694/份`。所需剩余均价支持0–5bps为9笔、77.8%、`+0.1444/份`。这些结果有启发，但样本小、Chainlink与入场平均相差十几秒，先做分层和记录，不设硬门槛。", "",
        "## 四、最终组合建议", "",
        "| 用途 | 当前建议 |",
        "|---|---|",
        "| 主策略对照 | 保留成熟基线，不因E13直接改动 |",
        "| 最优先叠加 | Binance Spot最近5分钟同向；UP/DOWN分别验证，不要求三方共识 |",
        "| 第二优先级 | 15秒动量、盘口质量，逐一添加，不同时加多个条件 |",
        "| 平行替代策略 | E12-0.65-90-FIRST-SIDE、E12-0.60-90-FIRST-SIDE、E1/E2固定参数组 |",
        "| 只记录 | Chainlink锚定距离、Futures/Hyperliquid/Deribit、时段、深度和失败类型 |",
        "| 暂不使用 | 高位回落反买、低胜率直接反向、E0概率硬过滤、Maker历史模拟结果 |",
        "", "",
        "## 五、归档与复现", "",
        "本次每个实验仍保留原始独立报告；本汇总只是导航和比较，不替代逐笔明细。",
        "- E1：`e1_low_price_target_exit/`；E2：`e2_early_stability/`；E6：`e6_down/`、`e6_up/`；E10：`e10_down/`、`e10_up/`；E12：`../e12_strict_upcross_20260828/`。",
        "- 组合矩阵：[策略组合适配矩阵](../../../docs/策略组合适配矩阵_20260828.md)。",
        "- 总索引：[研究测试总索引](../../../docs/研究测试总索引.md)。", "",
        "复现本汇总：", "```bash",
        "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/build_strategy_matrix_backtest.py",
        "```",
    ]
    report_path = out / "E13独立策略与成熟基线叠加回测汇总.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_payload = {
        "schemaVersion": 1,
        "experiment": "E13_strategy_matrix_backtest",
        "data": {
            "matureBaseline": overall,
            "byOutcome": by_outcome,
            "e12": e12_selected,
        },
        "standalone": {"E1": e1_selected, "E2": e2_selected, "E6": e6_selected},
        "ablation": e10_selected,
        "chainlinkE12": e12_chain,
    }
    json_path = out / "e13_strategy_matrix_results.json"
    json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    csv_path = out / "e13_strategy_matrix_results.csv"
    flat: list[dict[str, Any]] = []
    for outcome, rows in e10_selected.items():
        for row in rows:
            flat.append({"section": "E10_ablation", "outcome": outcome, **row})
    for row in e12_selected:
        flat.append({
            "section": "E12_strict_upcross", "outcome": row["outcome"],
            "windowSeconds": row["windowSeconds"], "threshold": row["threshold"],
            "entries": row["entries"], "winRate": row["winRate"],
            "averagePnlPerShare": row["averageHoldPnlPerShare"],
        })
    if flat:
        fieldnames: list[str] = []
        seen: set[str] = set()
        for row in flat:
            for key in row:
                if key not in seen:
                    seen.add(key); fieldnames.append(key)
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader(); writer.writerows(flat)
    print(report_path.resolve())
    print(json_path.resolve())
    print(csv_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""T069: fixed-UP/fixed-DOWN martingale replay on the frozen mature signal cohort.

This is a research replay only. It does not place orders or change paper/live
trading. The accepted rows and books are selected exactly as in T064; the
direction is kept fixed by running the UP cohort and DOWN cohort separately.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_one_dollar_doubling as base  # noqa: E402

INPUT = ROOT / "outputs/research_tests/t061_loss_sizing_volatility_20261001/diagnostic_rows.json"
OUT = ROOT / "outputs/research_tests/t069_fixed_direction_martingale_20261118"


def compact(result: dict) -> dict:
    keys = (
        "rule", "initialCapital", "maxRounds", "settlementDelaySeconds",
        "on11thLoss", "trades", "wins", "losses", "winRate", "netPnl",
        "principalTurnover", "totalFees", "roiOnPrincipalTurnover",
        "maxSettledEquityDrawdown", "maxPrincipalPerTrade", "maxCashCostPerTrade",
        "minimumInitialCapitalForObservedPath", "maxConsecutiveLosses",
        "roundTradeCounts", "closedCycles", "elevenLossCycles",
        "unclosedCyclePnl", "unclosedNextRound", "skipped",
    )
    return {key: result.get(key) for key in keys}


def pct(value):
    return "—" if value is None else f"{value:.1%}"


def money(value):
    return f"{value:+.3f}"


def run():
    source = json.loads(INPUT.read_text(encoding="utf-8"))
    groups = {}
    for side in ("UP", "DOWN"):
        rows, audit = base.select_mature(source, side)
        scenarios = {
            "fixed_1": base.replay(rows, doubling=False),
            "martin3": base.replay(rows, doubling=True, max_rounds=3),
            "martin5": base.replay(rows, doubling=True, max_rounds=5),
            "martin11": base.replay(rows, doubling=True, max_rounds=11),
            "martin3_100": base.replay(rows, doubling=True, max_rounds=3, bankroll=100),
            "martin5_100": base.replay(rows, doubling=True, max_rounds=5, bankroll=100),
            "martin11_100": base.replay(rows, doubling=True, max_rounds=11, bankroll=100),
            "martin11_2047": base.replay(rows, doubling=True, max_rounds=11, bankroll=2047),
        }
        groups[side] = {
            "selection": audit,
            "dates": [base.stamp(rows[0]["execution_us"]), base.stamp(rows[-1]["execution_us"])] if rows else [],
            "scenarios": {name: compact(result) for name, result in scenarios.items()},
        }

    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "testId": "T069",
        "generatedOn": "2026-11-18",
        "input": str(INPUT),
        "feeRateAssumption": base.FEE_RATE,
        "definition": {
            "fixedDirection": "UP and DOWN are run as separate fixed-direction cohorts; no direction switching within a cohort.",
            "entryCohort": "T064 mature signal: real active buy >=10 shares, remaining 90-30s, actual 10-share VWAP 0.80-0.85, worst <=0.85, 5s same-direction momentum >=0.01, book ages <=2s.",
            "stakes": "1,2,4,... according to maximum 3/5/11 rounds; a win resets to 1; a cap loss resets to 1.",
            "settlementDelaySeconds": 60,
            "fees": "0.07*p*(1-p) per share, inherited frozen research assumption.",
            "notAnExecutionChange": True,
        },
        "groups": groups,
    }
    (OUT / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# T069：固定全 UP / 固定全 DOWN 的有限倍投回测",
        "",
        "## 先说清楚测试含义",
        "",
        "本轮把方向拆成两个独立账户：UP 账户只使用严格合格的 UP 入场机会，DOWN 账户只使用严格合格的 DOWN 入场机会。每个账户在连续亏损后只增加下一笔金额，赢一笔就重置为 1U；不会因为上一笔结果把方向改成另一边。",
        "",
        "入场机会、实际盘口和官方结算标签沿用 T064 的冻结口径，因此这是‘固定方向资金管理’对照，不是把 UP 的盘口强行改成 DOWN，也不是凭空补造另一方向的成交。它不会修改纸上交易或实盘。",
        "",
        "- 数据文件：`t061_loss_sizing_volatility_20261001/diagnostic_rows.json`。",
        "- 时间范围：严格合格行自身的北京时间范围；原数据有缺口，不能当作连续全市场。",
        "- 费用：冻结研究假设 `0.07 × p × (1-p)`/份，费用另扣；不是当前平台报价。",
        "- 每笔都按缓存卖盘逐档重放，限价 0.85；深度不足或重算 VWAP 超带宽会跳过。",
        "- 结算后 60 秒才允许下一笔；这会比真实平台的结算到账更保守或更宽松，需以实盘记录复验。",
        "",
        "## 结果",
        "",
        "表中净收益已经扣研究手续费；ROI 是净收益 / 累计本金投入，不是账户本金收益率。最大回撤按结算后的累计权益计算。",
        "",
        "|固定方向|规则|成交|胜率|净收益U|投入ROI|最大回撤U|最高本金U|最长连败|11级打穿|本金不足停止|",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    order = ("fixed_1", "martin3", "martin5", "martin11", "martin3_100", "martin5_100", "martin11_100", "martin11_2047")
    labels = {"fixed_1": "固定1U", "martin3": "亏后×2最多3单", "martin5": "亏后×2最多5单", "martin11": "亏后×2最多11单", "martin3_100": "×2最多3单/100U", "martin5_100": "×2最多5单/100U", "martin11_100": "×2最多11单/100U", "martin11_2047": "×2最多11单/2047U"}
    for side in ("UP", "DOWN"):
        for name in order:
            result = groups[side]["scenarios"][name]
            skip = result.get("skipped") or {}
            lines.append(
                f"|{side}|{labels[name]}|{result['trades']}|{pct(result['winRate'])}|{money(result['netPnl'])}|{pct(result['roiOnPrincipalTurnover'])}|"
                f"{result['maxSettledEquityDrawdown']:.3f}|{result['maxPrincipalPerTrade']:.0f}|{result['maxConsecutiveLosses']}|"
                f"{result['elevenLossCycles']}|{skip.get('bankroll_exhausted', 0)}|"
            )

    lines += [
        "",
        "## 方向分组与样本范围",
        "",
    ]
    for side in ("UP", "DOWN"):
        group = groups[side]
        lines.append(f"- **{side}**：严格合格 {group['selection']['accepted']} 笔；候选行 {group['selection']['cached_signal_rows']} 笔；北京时间范围 `{group['dates']}`。排除计数：`{json.dumps(group['selection']['exclusions'], ensure_ascii=False)}`。")

    lines += [
        "",
        "## 怎么读这个结果",
        "",
        "1. 方向账户的胜率不会因为倍投而改变；改变的是单笔金额、累计投入、回撤和净收益。",
        "2. 3级/5级/11级是资金管理上限，不代表历史真的覆盖了相应连续亏损长度。若历史最长只到第3笔，11级结果仍然没有被验证。",
        "3. 100U 和 2047U 是有限本金压力测试。出现本金不足停止时，后续机会不再下注；这比把账户当成无限资金更接近风险评估。",
        "4. UP 与 DOWN 必须各自看验证段和新增日期。整段正收益、但后段转负或笔数太少，都不能直接升级成实盘规则。",
        "",
        "## 本轮结论",
        "",
        "全 UP 或全 DOWN 可以作为资金管理的独立对照，但这批历史信号没有证明‘固定一个方向’本身能消除连续亏损风险。若倍投后 ROI 上升，主要来自把更大本金压在原有信号上，不能解释成方向预测能力提高。11级的最坏资金需求是 2047U 加手续费；在没有覆盖多次连续亏损的样本外数据前，不建议用于真实马丁。固定金额仍应作为主对照，倍投最多保留为纸上影子。",
        "",
        "## 复现",
        "",
        "```bash",
        "cd /Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research",
        "python3 scripts/analysis/analyze_fixed_direction_martingale.py",
        "```",
        "",
        "完整机器结果见同目录 `results.json`；T064 原始结果保持不变。",
    ]
    (OUT / "固定全UP全DOWN有限倍投验证.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(OUT / "固定全UP全DOWN有限倍投验证.md")
    for side in ("UP", "DOWN"):
        for name in ("fixed_1", "martin3", "martin5", "martin11", "martin11_100", "martin11_2047"):
            result = groups[side]["scenarios"][name]
            print(side, name, json.dumps({key: result[key] for key in ("trades", "wins", "losses", "winRate", "netPnl", "roiOnPrincipalTurnover", "maxSettledEquityDrawdown", "maxPrincipalPerTrade", "maxConsecutiveLosses", "elevenLossCycles", "skipped")}, ensure_ascii=False))


if __name__ == "__main__":
    run()

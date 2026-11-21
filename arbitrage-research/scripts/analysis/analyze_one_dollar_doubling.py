#!/usr/bin/env python3
"""Frozen historical mature-strategy cash-stake replay; never places orders."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / 'outputs/research_tests/t061_loss_sizing_volatility_20261001/diagnostic_rows.json'
DEFAULT_OUT = ROOT / 'outputs/research_tests/t064_one_dollar_doubling_20261117'
FEE_RATE = 0.07  # Frozen historical research assumption, not a current fee quote.
EPS = 1e-8


def stamp(us):
    return datetime.fromtimestamp(us / 1_000_000, timezone(timedelta(hours=8))).isoformat()


def buy_notional(levels, notional, cap=0.85):
    """Spend the requested principal at visible asks; charge fees separately.

    None means visible depth cannot cover the whole principal. No infinite
    liquidity, payout scaling from a 10-share fill, or fee-free doubling.
    """
    remaining, quantity, fee, worst = notional, 0.0, 0.0, 0.0
    for price, size in sorted(levels):
        if price <= 0 or size <= 0:
            continue
        if price > cap + EPS:
            break
        take = min(size, remaining / price)
        quantity += take
        remaining -= take * price
        fee += take * FEE_RATE * price * (1 - price)
        worst = price
        if remaining <= EPS:
            return {'principal': notional, 'quantity': quantity, 'fee': fee,
                    'cost': notional + fee, 'vwap': notional / quantity,
                    'worst': worst}
    return None


def select_mature(rows, outcome):
    result, excluded, seen = [], Counter(), set()
    strategy = f'trade_{outcome.lower()}_80_85_90_30_q10_size10'
    source = sorted((r for r in rows if r.get('strategy') == strategy),
                    key=lambda r: r['execution_us'])
    for row in source:
        vwap = float(row['entry_vwap'])
        if not 0.80 - EPS <= vwap <= 0.85 + EPS:
            excluded['actual_10_share_vwap_outside_band'] += 1
            continue
        if not 30 - EPS <= float(row['remaining_seconds']) <= 90 + EPS:
            excluded['time_outside_window'] += 1
            continue
        momentum = row.get('momentum5')
        if momentum is None or momentum < 0.01 - EPS:
            excluded['momentum_missing_or_below_001'] += 1
            continue
        age = row.get('book_source_age_s')
        if age is None or not 0 <= age <= 2:
            excluded['book_source_stale_or_future'] += 1
            continue
        age = row.get('book_age_ms')
        if age is None or not 0 <= float(age) <= 2000:
            excluded['book_received_stale'] += 1
            continue
        if float(row['worst_price']) > 0.85 + EPS:
            excluded['worst_price_over_cap'] += 1
            continue
        if not row.get('filled') or row.get('winner') not in {'UP', 'DOWN'}:
            excluded['unfilled_or_unsettled'] += 1
            continue
        # Validate the cached book against the original 10-share price. Cached
        # extraction and signal replay can otherwise refer to different books.
        need, cost = 10.0, 0.0
        for price, size in sorted(row.get('levels', [])):
            if price <= 0 or size <= 0:
                continue
            if price > 0.85 + EPS:
                break
            take = min(need, size)
            cost += take * price
            need -= take
            if need <= EPS:
                break
        if need > EPS or abs(cost / 10 - vwap) > 1e-6:
            excluded['cached_book_disagrees_with_original_fill'] += 1
            continue
        if row['market_id'] in seen:
            excluded['duplicate_market'] += 1
            continue
        seen.add(row['market_id'])
        result.append(row)
    return result, {'cached_signal_rows': len(source), 'accepted': len(result),
                    'exclusions': dict(excluded)}


def replay(rows, doubling=True, max_rounds=11, bankroll=None, delay=60,
           cap_action='reset'):
    profit = turnover = fees = required = peak_profit = max_dd = 0.0
    step = cycle = loss_streak = max_loss_streak = available = 0
    closed = negative_win_cycles = cap_losses = 0
    skipped, rounds, ledger = Counter(), Counter(), []
    for row in sorted(rows, key=lambda r: r['execution_us']):
        if row['execution_us'] < available:
            skipped['previous_order_not_settled_yet'] += 1
            continue
        principal = float(2 ** step) if doubling else 1.0
        fill = buy_notional(row['levels'], principal)
        if fill is None:
            skipped['insufficient_depth_for_full_stake'] += 1
            continue
        if not 0.80 - EPS <= fill['vwap'] <= 0.85 + EPS:
            skipped['resized_actual_vwap_outside_band'] += 1
            continue
        if bankroll is not None and fill['cost'] > bankroll + profit + EPS:
            skipped['bankroll_exhausted'] += 1
            break
        required = max(required, fill['cost'] - profit)
        won = row['outcome'] == row['winner']
        net = (fill['quantity'] if won else 0.0) - fill['cost']
        profit += net
        turnover += principal
        fees += fill['fee']
        cycle += net
        peak_profit = max(peak_profit, profit)
        max_dd = max(max_dd, peak_profit - profit)
        loss_streak = 0 if won else loss_streak + 1
        max_loss_streak = max(max_loss_streak, loss_streak)
        available = row['end_us'] + delay * 1_000_000
        rounds[step + 1] += 1
        ledger.append({'marketId': row['market_id'], 'entryTime': stamp(row['execution_us']),
                       'availableTime': stamp(available), 'round': step + 1,
                       **fill, 'won': won, 'netPnl': net, 'cumulativePnl': profit,
                       'cyclePnlAtThisTrade': cycle,
                       'cashAfterSettlement': None if bankroll is None else bankroll + profit})
        at_cap_loss = doubling and not won and step + 1 >= max_rounds
        if won or at_cap_loss or not doubling:
            closed += 1
            negative_win_cycles += int(won and cycle < -EPS)
            cap_losses += int(at_cap_loss)
            cycle, step = 0.0, 0
            if at_cap_loss and cap_action == 'stop':
                skipped['stopped_after_11_losses'] += 1
                break
        elif doubling:
            step += 1
    wins = sum(r['won'] for r in ledger)
    return {'rule': 'loss_double_11' if doubling else 'fixed_1',
            'initialCapital': bankroll, 'maxRounds': max_rounds, 'settlementDelaySeconds': delay,
            'on11thLoss': cap_action, 'trades': len(ledger), 'wins': wins,
            'losses': len(ledger) - wins, 'winRate': wins / len(ledger) if ledger else None,
            'netPnl': profit, 'principalTurnover': turnover, 'totalFees': fees,
            'roiOnPrincipalTurnover': profit / turnover if turnover else None,
            'maxSettledEquityDrawdown': max_dd,
            'maxPrincipalPerTrade': max((r['principal'] for r in ledger), default=0),
            'maxCashCostPerTrade': max((r['cost'] for r in ledger), default=0),
            'minimumInitialCapitalForObservedPath': max(0.0, required),
            'maxConsecutiveLosses': max_loss_streak, 'roundTradeCounts': dict(rounds),
            'closedCycles': closed, 'winningCyclesStillNetNegative': negative_win_cycles,
            'elevenLossCycles': cap_losses, 'unclosedCyclePnl': cycle,
            'unclosedNextRound': step + 1, 'skipped': dict(skipped), 'ledger': ledger}


def describe(result):
    n = result['trades']
    rate = f"{result['winRate']:.1%}" if n else '—'
    roi = f"{result['roiOnPrincipalTurnover']:.1%}" if n else '—'
    return (f"{n} | {rate} | {result['netPnl']:+.3f} | {result['maxSettledEquityDrawdown']:.3f} | "
            f"{result['maxPrincipalPerTrade']:.0f} | {roi} | {result['winningCyclesStillNetNegative']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=DEFAULT_INPUT)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    source = json.loads(args.input.read_text())
    output = {'generatedOn': '2026-11-17', 'input': str(args.input),
              'feeRateAssumption': FEE_RATE, 'groups': {}}
    lines = ['# T064：成熟五分钟策略，1U起投、亏后翻倍、最多11单', '',
             '## 本次规则', '',
             '- 用户确认使用原成熟策略，UP、DOWN分别记账：真实主动买入≥10份、剩余90–30秒、原10份实际VWAP为0.80–0.85、最坏价≤0.85、5秒同向动量≥0.01、盘口源时间和接收时间年龄均≤2秒。',
             '- 首单买入本金1U，亏后本金变为2/4/8…1024U；赢后重置1U。11单含首单；第11单仍输则该轮止损、下一轮重置。另计算触顶即停止的结果。',
             '- 金额指不含手续费的买入本金，费用额外扣除。历史费模型沿用0.07×p×(1−p)/份，按每档价格求和；不是对当前平台费率的确认。',
             '- 每笔从原始入场时刻已缓存卖盘重新逐档计算数量、VWAP、手续费。必须能全额买入且新VWAP仍为0.80–0.85，深度不足跳过并保留倍投层数；不把10份成交等比例无限放大。',
             '- 单方向一次最多一笔，假设市场结束后60秒结果和资金可用，再允许下一笔；另测300秒。没有每日两笔亏损停机约束，用于单独检验本次资金管理想法。',
             '- 不重扫原始高频文件，复用T061冻结逐笔和当时盘口。缺失5秒动量、时效不合格及重建盘口不一致的信号不推定合格；不把同市场重复策略当新机会。',
             '- 这不是所有五分钟市场的逐场下注，也不是完整无缺口的成熟策略实盘流水；只研究字段足以确认合格的历史信号。缺失信号可能改变真实连续亏损分布，不能用这里的最大连亏外推实际尾部风险。',
             '- 1U在高价区约买1.2份：本研究不证明符合平台当时最小金额/份数/精度约束。盘口覆盖是成交上界，未模拟实际延迟、撤单、部分成交和市场冲击。', '',
             '## 固定1U与翻倍对比（历史全路径，无本金限制，实际盘口限制）', '',
             '| 方向 | 规则 | 成交 | 胜率 | 净收益U | 最大回撤U | 最大本金U | 净收益/累计本金 | 赢后整轮仍亏次数 |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    details, sensitivity, dates = [], [], []
    for side in ('UP', 'DOWN'):
        rows, audit = select_mature(source, side)
        split = int(len(rows) * 0.7)
        fixed, doubled = replay(rows, doubling=False), replay(rows)
        group = {'selection': audit, 'dates': [stamp(rows[0]['execution_us']), stamp(rows[-1]['execution_us'])] if rows else [],
                 'fixed': fixed, 'double11': doubled, 'validationCutTime': stamp(rows[split]['execution_us']) if split < len(rows) else None,
                 'later30Fixed': replay(rows[split:], doubling=False), 'later30Double': replay(rows[split:]),
                 'delay300Fixed': replay(rows, doubling=False, delay=300),
                 'delay300Double': replay(rows, delay=300), 'stopAtCap': replay(rows, cap_action='stop'),
                 'capitalSensitivity': [replay(rows, bankroll=capital) for capital in (20, 100, 500, 2100)]}
        output['groups'][side] = group
        lines += [f'| {side} | 固定1U | {describe(fixed)} |', f'| {side} | 失败后×2，最多11单 | {describe(doubled)} |']
        dates.append(f"- {side}：原策略候选{audit['cached_signal_rows']}笔，严格合格{len(rows)}笔；范围（北京时间）{group['dates']}。剔除：`{json.dumps(audit['exclusions'], ensure_ascii=False)}`。")
        details += [f'### {side}', '',
                    f"- 翻倍层数实际分布：`{doubled['roundTradeCounts']}`；触发11连输次数：{doubled['elevenLossCycles']}。未触发不等于尾部风险为零。",
                    f"- 全路径重放的最低初始资金：固定1U约{fixed['minimumInitialCapitalForObservedPath']:.3f}U；翻倍约{doubled['minimumInitialCapitalForObservedPath']:.3f}U。仅针对这条历史路径，不是未来资金下限。",
                    f"- 翻倍手续费共{doubled['totalFees']:.3f}U；跳过：`{doubled['skipped']}`。",
                    f"- 后30%从{group['validationCutTime']}开始，单独重置为1U：固定{group['later30Fixed']['trades']}笔/净收益{group['later30Fixed']['netPnl']:+.3f}U；翻倍{group['later30Double']['trades']}笔/净收益{group['later30Double']['netPnl']:+.3f}U。旧历史已被研究，属于回顾性留后，不是新数据样本外。",
                    f"- 结算资金延迟300秒：固定{group['delay300Fixed']['trades']}笔/净收益{group['delay300Fixed']['netPnl']:+.3f}U；翻倍{group['delay300Double']['trades']}笔/净收益{group['delay300Double']['netPnl']:+.3f}U。", '']
        for result in group['capitalSensitivity']:
            sensitivity.append(f"| {side} | {result['initialCapital']:.0f} | {result['trades']} | {result['netPnl']:+.3f} | {result['maxSettledEquityDrawdown']:.3f} | {result['skipped'].get('bankroll_exhausted', 0)} |")
    example1 = buy_notional([[0.82, 10000]], 1)
    example2 = buy_notional([[0.82, 10000]], 2)
    example_net = -example1['cost'] + example2['quantity'] - example2['cost']
    lines += ['', '## 数据日期与排除统计', ''] + dates + ['', '## 逐方向与留后验证', ''] + details
    lines += ['## 本金敏感性（失败后翻倍）', '', '| 方向 | 初始资金U | 成交 | 净收益U | 最大回撤U | 资金不足停止 |', '|---|---:|---:|---:|---:|---:|'] + sensitivity
    lines += ['', '## 为什么赢一单也不一定回本', '',
              f'例如两次都在0.82买入：首单本金1U亏损共{example1["cost"]:.6f}U；下一单本金2U赢，净赚{example2["quantity"]-example2["cost"]:.6f}U；合起来仍亏{abs(example_net):.6f}U。赢后重置会把这笔净亏留在账户里。', '',
              '十一单本金序列：1、2、4、8、16、32、64、128、256、512、1024U，累计2047U，另加手续费。如果指“首单之后再翻11次”，则共12单、末单2048U、累计4095U另加手续费；本次采用已说明的总共11单。', '',
              '本次是资金管理对照，不能把翻倍后的账户净利润增加直接解释为预测能力提高。还需要同时比较累计投入、回撤、最低资金和净收益/累计本金；历史未出现11连输也无法证明不会发生。', '',
              '## 本轮判断', '',
              'UP全路径翻倍的净收益/累计本金提高，但回撤和历史最低资金要求都增加；DOWN总利润略增，而净收益/累计本金下降，后30%留后段也没有超过固定1U。两边只实际到达第3单（4U），第4–11单没有真实触发证据。本轮不足以把11级倍投作为成熟策略升级，固定额仍应保留为对照。', '',
              '## 文件与复现', '', '完整逐笔、层数、资金变化和排除计数见本目录的 `results.json`。', '', '```bash',
              'cd /Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research',
              'python3 scripts/analysis/analyze_one_dollar_doubling.py', '```', '']
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'results.json').write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    report = args.out / '成熟策略1U起投11单倍投验证.md'
    report.write_text('\n'.join(lines))
    print(report)
    for side, group in output['groups'].items():
        for label in ('fixed', 'double11'):
            print(side, label, json.dumps({k: group[label][k] for k in ('trades', 'wins', 'losses', 'netPnl', 'maxSettledEquityDrawdown', 'maxPrincipalPerTrade', 'maxConsecutiveLosses', 'winningCyclesStillNetNegative', 'skipped')}, ensure_ascii=False))


if __name__ == '__main__':
    main()

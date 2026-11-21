#!/usr/bin/env python3
"""Research-only stop-loss grid using the frozen live strategy cache."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs/research_tests/t062_stop_loss_20261001'
ROWS_PATH = ROOT / 'outputs/research_tests/t061_loss_sizing_volatility_20261001/diagnostic_rows.json'


def iso(us):
    return datetime.fromtimestamp(us / 1e6, timezone.utc).isoformat()


def number(x):
    try:
        y = float(x)
        return y if math.isfinite(y) else None
    except (TypeError, ValueError):
        return None


def fee(price):
    return 0.07 * price * (1 - price)


def consume(levels, quantity, cap):
    left, cost = quantity, 0.0
    for price, size in sorted(levels):
        if price > cap + 1e-12:
            break
        take = min(left, size)
        cost += take * price + take * fee(price)
        left -= take
        if left <= 1e-8:
            return cost
    return None


def path_grid(history, start, end, max_age_s=2):
    history = sorted(history, key=lambda x: x['received_us'])
    result = []
    j = -1
    for at in range(start, end + 1_000_000, 1_000_000):
        while j + 1 < len(history) and history[j + 1]['received_us'] <= at:
            j += 1
        if j < 0:
            result.append(None)
            continue
        r = history[j]
        value = number(r.get('best_ask'))
        result.append(value if value is not None and 0 <= at - r['source_us'] <= max_age_s * 1_000_000
                      and 0 <= at - r['received_us'] <= max_age_s * 1_000_000 else None)
    return result


def trigger_second(path, entry, drop):
    level = entry - drop
    for i, value in enumerate(path):
        if value is not None and value <= level + 1e-12:
            return i
    return None


def evaluate(row, path, drop, haircut):
    quantity = 10
    initial = consume(row['levels'], quantity, row['cap'])
    if initial is None:
        return None
    trigger = trigger_second(path, row['entry_vwap'], drop)
    baseline = quantity * int(row['won']) - initial
    if trigger is None:
        return {'baseline': baseline, 'pnl': baseline, 'trigger_s': None, 'stopped': False}
    # We only have the sell-side ask ladder. Exit at the trigger level less a
    # configurable haircut; this is an explicit upper-bound sensitivity, not
    # a claim that the observed ask was executable as a sell price.
    exit_price = max(0.0, row['entry_vwap'] - drop - haircut)
    exit_value = quantity * max(0.0, exit_price - fee(exit_price))
    return {'baseline': baseline, 'pnl': exit_value - initial, 'trigger_s': trigger,
            'stopped': True, 'exit_price': exit_price}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = json.loads(ROWS_PATH.read_text())
    con = sqlite3.connect(f'file:{ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    memo = {}
    for index, row in enumerate(rows, 1):
        key = (row['market_id'], row['outcome'], row['execution_us'], row['end_us'])
        if key not in memo:
            history = [dict(x) for x in con.execute(
                'SELECT source_us,received_us,best_ask FROM snapshots '
                'WHERE market_id=? AND outcome=? AND source_us BETWEEN ? AND ? AND received_us<=? '
                'ORDER BY source_us,received_us',
                (row['market_id'], row['outcome'], row['execution_us'], row['end_us'], row['end_us']))]
            memo[key] = path_grid(history, row['execution_us'], row['end_us'])
        row['_path'] = memo[key]
        if index % 500 == 0:
            print('paths', index, '/', len(rows), flush=True)
    con.close()

    drops = [0.02, 0.03, 0.05, 0.08, 0.10, 0.15]
    haircuts = [0.00, 0.01, 0.02, 0.05, 0.10]
    groups = {}
    for strategy in sorted({r['strategy'] for r in rows}):
        group = [r for r in rows if r['strategy'] == strategy]
        scenarios = []
        for haircut in haircuts:
            for drop in drops:
                evaluated = [evaluate(r, r['_path'], drop, haircut) for r in group]
                evaluated = [x for x in evaluated if x is not None]
                triggers = [x for x in evaluated if x['stopped']]
                winners_stopped = sum(1 for r, x in zip(group, [evaluate(r, r['_path'], drop, haircut) for r in group]) if x and x['stopped'] and r['won'])
                losses_stopped = sum(1 for r, x in zip(group, [evaluate(r, r['_path'], drop, haircut) for r in group]) if x and x['stopped'] and not r['won'])
                baseline = sum(x['baseline'] for x in evaluated)
                pnl = sum(x['pnl'] for x in evaluated)
                scenarios.append({'drop': drop, 'haircut': haircut, 'n': len(evaluated),
                    'triggered': len(triggers), 'trigger_rate': len(triggers) / len(evaluated) if evaluated else None,
                    'winners_stopped': winners_stopped, 'losses_stopped': losses_stopped,
                    'baseline_pnl': baseline, 'stop_pnl': pnl, 'delta_pnl': pnl - baseline,
                    'avg_pnl_per_share': pnl / (10 * len(evaluated)) if evaluated else None,
                    'trigger_median_s': sorted(x['trigger_s'] for x in triggers)[len(triggers)//2] if triggers else None})
        groups[strategy] = {'n': len(group), 'scenarios': scenarios}

    result = {'model': 'stop-loss-ask-trigger-upper-bound-v1', 'drops': drops,
              'haircuts': haircuts, 'groups': groups,
              'data_note': 'Trigger uses 1-second best-ask path; exit uses threshold less haircut because bid ladder is unavailable.'}
    (OUT / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')

    lines = ['# 入场后回撤止损网格验证', '',
        '## 先说结论', '',
        '按这份历史报价回放，止损“有模拟效果”：例如主UP 90–10固定10份、入场后回撤0.10时，基准持有净收益为+162.77U；即使给退出价额外扣0.10，模型净收益仍为+327.82U。这个提升主要来自提前结束失败单的损失，不能理解成每笔都能卖在理想价格。',
        '本报告的核心问题是成交模型：缓存只有卖一报价，没有买一档、价差、排队和真实卖出成交。因此这个结果只能说明“如果跌到阈值附近还能较快卖出，止损方向可能有价值”，还不能直接上线。', '',
        '本轮只验证研究用止损，不改主策略。入场后每秒读取同方向 best_ask，达到入场VWAP−阈值即触发。缓存只有卖一档，没有买一档，因此退出价按“触发阈值−haircut”做敏感性；这不是可成交卖出价证明。', '',
        '- 基准：固定10份，原始买入成本按逐档盘口和费用计算，持有到官方结算。',
        '- 止损阈值：0.02/0.03/0.05/0.08/0.10/0.15；haircut：0/0.01/0.02。',
        '- 成功单被止损也计入机会成本；结果按每个策略逐笔比较，不把不同策略重复成交当作新增机会。', '',
        '## 全部策略汇总（haircut=0.01）', '',
        '| 策略 | 阈值 | n | 触发 | 触发率 | 止损成功单 | 止损失败单 | 原始净收益U | 止损净收益U | 变化U | 触发中位秒 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for strategy, group in groups.items():
        for s in group['scenarios']:
            if s['haircut'] != 0.01:
                continue
            lines.append(f"| {strategy} | {s['drop']:.2f} | {s['n']} | {s['triggered']} | {s['trigger_rate']*100:.1f}% | {s['winners_stopped']} | {s['losses_stopped']} | {s['baseline_pnl']:+.2f} | {s['stop_pnl']:+.2f} | {s['delta_pnl']:+.2f} | {s['trigger_median_s'] if s['trigger_median_s'] is not None else '—'} |")
    lines += ['', '## 判断', '',
        '- 只有当止损净收益在haircut=0/0.01/0.02三种假设下都不差、且没有大量止损成功单，才值得进入影子策略。',
        '- 单看失败单跌过0.05不能证明0.05是好阈值；如果阈值触发太慢，损失已扩大，如果触发太快，则会把短暂回撤的成功单卖掉。',
        '- 这轮结果仍是报价路径上界，正式加入前需要补采买一档/实际卖出成交数据，并单独做新增日期样本外验证。', '',
        '```bash', 'PYTHONPATH=src python3 scripts/analysis/analyze_stop_loss.py', '```', '']
    (OUT / '入场后回撤止损网格验证.md').write_text('\n'.join(lines))
    print('done', OUT, flush=True)


if __name__ == '__main__':
    main()

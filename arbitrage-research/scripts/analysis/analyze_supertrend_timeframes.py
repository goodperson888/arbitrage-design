#!/usr/bin/env python3
"""T072: closed 1/5/15m Supertrend x 3/2/1.5, fixed stakes and finite martingale."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import hashlib
import math
from pathlib import Path
import sqlite3

import analyze_martingale_candidates as base
import analyze_supertrend_direction as old
from analyze_supertrend_robustness import concentration

OUT = base.ROOT / 'outputs/research_tests/t072_supertrend_timeframes_20261120'
PLAN = base.ROOT / 'docs/Supertrend多周期与倍数验证计划_20261120.md'
VERSION = 't072-v1-complete-utc-bars-original-expiry'
TIMEFRAMES = (1, 5, 15)
FACTORS = {'3': 3., '2': 2., '1p5': 1.5}
LAGS = (0, 1000)
DELAY = (0, 300, 1000)
SIDES = ('UP', 'DOWN', 'BOTH')
SPLITS = ('all', 'train', 'validation', 'test')
ODDS = ('gross_le_050', 'net_cost_le_050')
REPORT = 'Supertrend多周期与倍数验证.md'
SPECS = {f'{mode}_{tf}m_k{tag}': {'mode': mode, 'timeframe': tf, 'factorTag': tag,
                                 'factor': factor, 'atrPeriod': 14}
         for tf in TIMEFRAMES for tag, factor in FACTORS.items() for mode in ('P', 'F')}
SPECS.update({f'C_{tf}m_k{tag}': {'mode': 'C', 'timeframe': tf, 'factorTag': tag,
                               'factor': factor, 'atrPeriod': 14}
              for tf in (5, 15) for tag, factor in FACTORS.items()})


def aggregate_bars(bars, minutes):
    """Keep only complete UTC-aligned groups; never roll across a missing minute."""
    if minutes not in TIMEFRAMES:
        raise ValueError('unsupported timeframe')
    groups = defaultdict(list)
    previous = None
    width = minutes * base.MINUTE
    for bar in bars:
        if (bar['open_us'] % base.MINUTE or bar['end_us'] - bar['open_us'] != base.MINUTE
                or previous is not None and bar['open_us'] <= previous):
            raise ValueError('minute bars must be aligned, ordered and unique')
        if not (0 < bar['low'] <= min(bar['open'], bar['close']) <= max(bar['open'], bar['close']) <= bar['high']):
            raise ValueError('invalid OHLC')
        groups[bar['open_us'] // width * width].append(bar)
        previous = bar['open_us']
    result, rejected = [], 0
    for start, rows in sorted(groups.items()):
        if len(rows) != minutes or any(r['open_us'] != start + i * base.MINUTE for i, r in enumerate(rows)):
            rejected += 1
            continue
        result.append({'open_us': start, 'end_us': start + width, 'open': rows[0]['open'],
                       'high': max(r['high'] for r in rows), 'low': min(r['low'] for r in rows),
                       'close': rows[-1]['close']})
    return result, {'inputMinuteBars': len(bars), 'completeBars': len(result), 'rejectedIncompleteBuckets': rejected}


def build_supertrend(bars, timeframe, period=14, factor=3.):
    """T070 recursion with an explicit bar duration, leaving frozen code intact."""
    if timeframe not in TIMEFRAMES or period < 1 or factor <= 0:
        raise ValueError('invalid Supertrend parameters')
    times, values, seed = [], [], []
    previous = None
    atr = upper = lower = direction = None
    confirmed = False
    for bar in bars:
        if bar['open_us'] % (timeframe * base.MINUTE) or bar['end_us'] - bar['open_us'] != timeframe * base.MINUTE:
            raise ValueError('wrong timeframe bar')
        if previous is None or bar['open_us'] != previous['end_us']:
            previous = None
            atr = upper = lower = direction = None
            seed, confirmed = [], False
        tr = bar['high'] - bar['low']
        if previous is not None:
            tr = max(tr, abs(bar['high'] - previous['close']), abs(bar['low'] - previous['close']))
        if atr is None:
            seed.append(tr)
            if len(seed) == period:
                atr = math.fsum(seed) / period
        else:
            atr = (atr * (period - 1) + tr) / period
        flip = None
        if atr is not None:
            midpoint = (bar['high'] + bar['low']) / 2
            basic_upper, basic_lower = midpoint + factor * atr, midpoint - factor * atr
            old_direction = direction
            upper = basic_upper if upper is None or basic_upper < upper or previous['close'] > upper else upper
            lower = basic_lower if lower is None or basic_lower > lower or previous['close'] < lower else lower
            if direction is None:
                direction = 'DOWN'
            elif direction == 'DOWN' and bar['close'] > upper:
                direction = 'UP'
            elif direction == 'UP' and bar['close'] < lower:
                direction = 'DOWN'
            if old_direction is not None and direction != old_direction:
                flip, confirmed = direction, True
        line = lower if direction == 'UP' else upper if direction == 'DOWN' else None
        values.append({'barEndUs': bar['end_us'], 'close': bar['close'], 'atr': atr, 'tr': tr,
                       'upper': upper, 'lower': lower, 'line': line, 'rawDirection': direction,
                       'direction': direction if confirmed and atr is not None and atr > 0 else None,
                       'flip': flip if atr is not None and atr > 0 else None})
        times.append(bar['end_us'])
        previous = bar
    return times, values


def build_pack(bars):
    pack, quality = {}, {}
    for tf in TIMEFRAMES:
        grouped, counts = aggregate_bars(bars, tf)
        quality[str(tf)] = counts
        for tag, factor in FACTORS.items():
            pack[(tf, tag)] = build_supertrend(grouped, tf, factor=factor)
    # All recursive state fields must match the original minute implementation.
    for tag, factor in FACTORS.items():
        if pack[(1, tag)] != old.build_supertrend(bars, 14, factor):
            raise RuntimeError('one-minute indicator implementation drift')
    return pack, quality


def row_at(series, timeframe, lag_ms, at):
    times, rows = series
    j = bisect_right(times, at - lag_ms * 1000) - 1
    if j < 0:
        return None
    row = rows[j]
    return row if 0 <= at - row['barEndUs'] < timeframe * base.MINUTE else None


def choose_signal(spec, rows):
    key = (spec['timeframe'], spec['factorTag'])
    high = rows.get(key)
    if high is None:
        return None
    if spec['mode'] == 'P':
        return high['direction']
    if spec['mode'] == 'F':
        return high['flip']
    fast = rows.get((1, spec['factorTag']))
    return fast['flip'] if fast and fast['flip'] and fast['flip'] == high['direction'] else None


def make_callback(pack):
    last_at, last_result = None, None

    def callback(at, ind=None, xc=None, remaining=None, **kwargs):
        nonlocal last_at, last_result
        if last_at == at:
            return last_result
        signals = {}
        for lag in LAGS:
            rows = {key: row_at(series, key[0], lag, at) for key, series in pack.items()}
            signals.update({f'{name}_L{lag}': choose_signal(spec, rows) for name, spec in SPECS.items()})
        last_at, last_result = at, signals
        return signals
    return callback


def entry_features(entry, pack):
    name, lag_text = entry['strategy'].rsplit('_L', 1)
    spec, lag = SPECS[name], int(lag_text)
    tfs = (spec['timeframe'], 1) if spec['mode'] == 'C' else (spec['timeframe'],)
    sources = []
    for tf in tfs:
        row = row_at(pack[(tf, spec['factorTag'])], tf, lag, entry['decisionUs'])
        if row is None:
            raise RuntimeError('missing signal source')
        sources.append({'timeframe': tf, 'barEndUs': row['barEndUs'],
                        'availableUs': row['barEndUs'] + lag * 1000,
                        'expiresUs': row['barEndUs'] + tf * base.MINUTE,
                        **{k: row[k] for k in ('close', 'line', 'atr', 'direction', 'flip')}})
    return {'rule': name, **spec, 'publicationLagMs': lag, 'indicatorSources': sources}


def audit_entries(entries, pack):
    violations, seen = Counter(), set()
    callback = make_callback(pack)
    for e in entries:
        key = (e['strategy'], e['marketId'], e['side'], e['delayMs'], e['odds'])
        violations['duplicates'] += int(key in seen)
        seen.add(key)
        for src in e['indicatorSources']:
            violations['indicatorTiming'] += int(not src['barEndUs'] <= src['availableUs'] <= e['decisionUs'] < src['expiresUs'])
            violations['barBoundary'] += int(src['barEndUs'] % (src['timeframe'] * base.MINUTE) != 0)
            direction = src['direction']
            violations['directionLine'] += int(direction == 'UP' and src['close'] < src['line']
                                               or direction == 'DOWN' and src['close'] > src['line'])
        violations['signalReproduction'] += int(callback(e['decisionUs'])[e['strategy']] != e['side'])
        violations['executionTiming'] += int(e['executionUs'] != e['decisionUs'] + e['delayMs'] * 1000)
        for field in ('bookReceivedUs', 'bookSourceUs'):
            violations['bookAge'] += int(not 0 <= e['executionUs'] - e[field] <= 2 * base.SECOND)
        violations['budget'] += int(not 0 < e['fixedFill']['cost'] <= 1 + 1e-9)
        violations['priceCap'] += int(e['fixedFill']['worst'] > e['priceCap'] + 1e-9)
        violations['netPriceCap'] += int(e['odds'] == ODDS[1] and e['fixedFill']['effectivePrice'] > .5 + 1e-9)
    if any(violations.values()):
        raise RuntimeError(dict(violations))
    return {'violations': dict(violations), 'uniqueEntries': len(seen)}


def baseline_check(entries):
    prior = base.load(old.OUT / 'entries.json.gz')
    mapping = {'P_1m_k3': 'S0', 'F_1m_k3': 'S1', 'P_1m_k2': 'S0_k2', 'F_1m_k2': 'S1_k2'}
    fields = ('decisionUs', 'executionUs', 'levels', 'decisionLevels', 'priceCap', 'fixedFill', 'won', 'labelAvailableUs')
    expected = {(e['strategy'], e['marketId'], e['side'], e['delayMs'], e['odds']): e
                for e in prior if e['strategy'] in mapping.values()}
    actual = {(mapping[e['rule']], e['marketId'], e['side'], e['delayMs'], e['odds']): e
              for e in entries if e['rule'] in mapping and e['publicationLagMs'] == 0}
    if expected.keys() != actual.keys() or any(any(e[f] != actual[k][f] for f in fields) for k, e in expected.items()):
        raise RuntimeError('T070 one-minute entry mismatch')
    return {'matchedEntries': len(actual), 'fields': list(fields), 'violations': 0}


def indicator_coverage(markets, pack):
    counts = defaultdict(Counter)
    observed = defaultdict(set)
    for m in markets:
        for at in range(m['start_us'] + 30 * base.SECOND, m['end_us'] - 30 * base.SECOND + 1, base.SECOND):
            for (tf, tag), series in pack.items():
                row = row_at(series, tf, 1000, at)
                label = f'{tf}m_k{tag}'
                c = counts[label]
                c['decisionGrid'] += 1
                c['availableGrid'] += int(row is not None)
                c['atrReadyGrid'] += int(row is not None and row['atr'] is not None)
                c['directionReadyGrid'] += int(row is not None and row['direction'] is not None)
                c['flipActiveGrid'] += int(row is not None and row['flip'] is not None)
                if row is not None and row['flip'] is not None:
                    observed[label].add(row['barEndUs'])
    return {name: {**c, 'observedFlipBars': len(observed[name])} for name, c in counts.items()}


def analyze(entries):
    groups = defaultdict(list)
    for e in entries:
        for side in (e['side'], 'BOTH'):
            groups[(e['strategy'], side, e['delayMs'], e['odds'])].append(e)
    results, ledgers = [], {}
    for name, spec in SPECS.items():
        for lag in LAGS:
            strategy = f'{name}_L{lag}'
            for side in SIDES:
                for delay in DELAY:
                    for odds in ODDS:
                        rows = groups[(strategy, side, delay, odds)]
                        for split in SPLITS:
                            selected = rows if split == 'all' else [r for r in rows if r['split'] == split]
                            for sizing, rounds, capital in (('fixed', 1, 100.), ('martin11_100', 11, 100.),
                                                           ('martin11_2047', 11, 2047.)):
                                summary, ledger = base.replay(selected, rounds, capital)
                                summary.update(rule=name, **spec, publicationLagMs=lag, side=side, delayMs=delay,
                                               odds=odds, split=split, sizing=sizing, qualifiedSignals=len(selected))
                                summary['concentration'] = concentration(summary)
                                if sizing == 'fixed' and lag == 1000 and delay == 300:
                                    summary['dailyBlockRoiCI95'] = base.bootstrap_daily_ci(summary['daily'])
                                if split == 'all' and lag == 1000 and delay == 300:
                                    ledgers[f'{name}|{side}|{odds}|{sizing}'] = ledger
                                results.append(summary)
    return results, ledgers


def select(results, *, mode=None, side='BOTH', lag=1000, delay=300, split='all', odds=ODDS[0], sizing='fixed', rule=None):
    return [r for r in results if (mode is None or r['mode'] == mode) and (rule is None or r['rule'] == rule)
            and r['side'] == side and r['publicationLagMs'] == lag and r['delayMs'] == delay
            and r['split'] == split and r['odds'] == odds and r['sizing'] == sizing]


def qualifies(results, rule, side):
    return all((r := select(results, rule=rule, side=side, split=split, delay=delay)[0])['n'] >= 100
               and r['netPnl'] > 0 for split in ('validation', 'test') for delay in (300, 1000))


def table(rows):
    lines = ['|规则|周期|ATR倍数|方向|区间|胜/笔|胜率|净收益U|投入ROI|最大回撤U|最长连亏|',
             '|---|---:|---:|---|---|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        lines.append(f"|{r['mode']}|{r['timeframe']}m|{r['factor']:g}|{r['side']}|{r['split']}|{r['wins']}/{r['n']}|"
                     f"{base.pct(r['winRate'])}|{r['netPnl']:+.3f}|{base.pct(r['roi'])}|{r['maxDrawdown']:.3f}|{r['maxConsecutiveLosses']}|")
    return lines


def render(results, manifest, audit, coverage):
    qualified = [{'rule': name, 'side': side} for name in SPECS for side in SIDES if qualifies(results, name, side)]
    fixed = select(results)
    fixed_last = select(results, split='test')
    best_whole = max(fixed, key=lambda r: r['netPnl'])
    best_last = max(fixed_last, key=lambda r: r['netPnl'])
    lines = ['# T072：Supertrend多周期与倍数验证', '', '## 结论', '',
             f"全部24个固定规则完成UP、DOWN与双向共享账户比较。主口径为K线晚到1秒、订单晚到300ms、固定1U、名义买价上限0.50。",
             f"通过验证/末段各100笔且300/1000ms订单延迟均正收益的规则方向组合：{len(qualified)}个。",
             f"共享账户全段净收益最高为{best_whole['rule']}，{best_whole['wins']}/{best_whole['n']}胜、净{best_whole['netPnl']:+.3f}U；"
             f"末段净收益最高为{best_last['rule']}，{best_last['wins']}/{best_last['n']}胜、净{best_last['netPnl']:+.3f}U。这是24组中事后描述，不等于验证通过。", '',
             'P=持续方向；F=翻转后一个自身周期；C=5/15分钟持续方向与1分钟翻转同向（有效60秒）。'
             '系数相同时，组合使用同系数的两条趋势线。所有策略交易的都是五分钟市场。', '',
             '## 持续方向P：完整九宫格', ''] + table(select(results, mode='P'))
    lines += ['', '## 翻转触发F：完整九宫格', ''] + table(select(results, mode='F'))
    lines += ['', '## 大周期定方向＋1分钟翻转触发C', ''] + table(select(results, mode='C'))
    lines += ['', '## UP与DOWN独立账户', '']
    for side in ('UP', 'DOWN'):
        lines += [f'### {side}', ''] + table(select(results, side=side))
    lines += ['', '## 共享账户的训练/验证/末段', '']
    for split in ('train', 'validation', 'test'):
        lines += [f'### {split}', ''] + table(select(results, split=split))
    lines += ['', '## 1秒订单延迟：末段', ''] + table(select(results, split='test', delay=1000))
    lines += ['', '## 含费每份成本≤0.50：全段与末段', '']
    lines += table(select(results, odds=ODDS[1]) + select(results, odds=ODDS[1], split='test'))
    lines += ['', '## 11级倍投压力：2047U共享账户', '',
              '固定金额亏损或资金停止不能靠较短连亏解释为改善。倍投发生深度拒绝时可能改变入场集合。', '',
              '|规则|胜/笔|净收益U|最大回撤U|最大预算U|11级失败轮|资金不足停止|', '|---|---:|---:|---:|---:|---:|---:|']
    for r in select(results, sizing='martin11_2047'):
        lines.append(f"|{r['rule']}|{r['wins']}/{r['n']}|{r['netPnl']:+.3f}|{r['maxDrawdown']:.3f}|{r['maxBudget']:.0f}|"
                     f"{r['capLossCycles']}|{r['rejections'].get('bankroll_stop',0)}|")
    lines += ['', '## 周期覆盖与翻转', '',
              '翻转数为至少覆盖一个允许决策时点的独立翻转K线数；覆盖基于全部3752市场的逐秒决策网格，尚未筛盘口/价格。', '',
              '|周期/系数|有效方向网格/全部|方向覆盖率|翻转K线数|', '|---|---:|---:|---:|']
    for name, row in coverage.items():
        lines.append(f"|{name}|{row['directionReadyGrid']}/{row['decisionGrid']}|"
                     f"{base.pct(row['directionReadyGrid']/row['decisionGrid'])}|{row['observedFlipBars']}|")
    lines += ['', '## 使用边界与复跑', '',
              '- 数据沿用2026-08-21 19:00至2026-10-03 01:50的3752个官方市场，20个有采集日期，中间有缺口。不是11月新增数据，也不是全新样本外。',
              '- 5/15分钟只由全部连续分钟聚合；UTC整点对齐。已收盘后加1秒才能可见，原定到期即失效；缺口重置指标，不从未来分钟补值。',
              '- 决策卖一0.30–0.50，剩余270–30秒，1U含费总预算，按可见卖盘逐档模拟。决策/执行盘口源与接收均≤2秒；首个可发机会失败不重挑。',
              '- 固定手续费模型每份0.07×p×(1-p)，不是当前费率承诺；1U研究订单不保证满足平台实盘最小金额。持有官方结算，实际收到结果与结束+60秒较晚者后才能再买。',
              '- 60/20/20历史切分分别从头重置账户；验证从2026-09-02 04:10、末段从2026-09-04 19:15开始，均已在历史研究中看过。',
              '- 一分钟、三倍/两倍、零K线公布延迟与T070逐笔复现。所有指标和成交时序、预算与限价审计见审计文件。',
              '- ATR周期固定14。三倍/两倍/1.5倍比较不能自动代表所有ATR参数；更少翻转、少交易或较高累计胜率不等于提高净收益。', '',
              '```sh', 'python3 arbitrage-research/scripts/analysis/analyze_supertrend_timeframes.py',
              'python3 arbitrage-research/scripts/analysis/analyze_supertrend_timeframes.py --reuse-entries', '```', '',
              '## 独立规则档案', '']
    for name, spec in SPECS.items():
        lines.append(f'- [{name}]({name}.md)')
        report = [f'# T072：{name}', '',
                  f"模式{spec['mode']}，{spec['timeframe']}分钟、ATR(14)×{spec['factor']:g}；"
                  '主口径K线晚到1秒、订单300ms、固定1U、名义价≤0.50。C的快周期固定1分钟，同系数。', '',
                  '## 完整方向与分段', '']
        for side in SIDES:
            report += table([r for split in SPLITS for r in select(results, rule=name, side=side, split=split)])
            report.append('')
        report += ['## 末段的订单延迟1秒压力', ''] + table([r for side in SIDES for r in select(results, rule=name, side=side, split='test', delay=1000)])
        report += ['', '## K线公布延迟0毫秒参考', ''] + table([r for side in SIDES for r in select(results, rule=name, side=side, lag=0)])
        report += ['', '## 含费成本上限', ''] + table([r for side in SIDES for split in ('all','test')
                                                  for r in select(results, rule=name, side=side, split=split, odds=ODDS[1])])
        report += ['', '## 11级本金压力', '',
                   '|本金U|方向|胜/笔|净收益U|最大回撤U|最大预算U|11级失败轮|资金不足停止|', '|---:|---|---:|---:|---:|---:|---:|---:|']
        for sizing in ('martin11_100','martin11_2047'):
            for side in SIDES:
                r = select(results, rule=name, side=side, sizing=sizing)[0]
                report.append(f"|{r['startingBankroll']:.0f}|{side}|{r['wins']}/{r['n']}|{r['netPnl']:+.3f}|{r['maxDrawdown']:.3f}|"
                              f"{r['maxBudget']:.0f}|{r['capLossCycles']}|{r['rejections'].get('bankroll_stop',0)}|")
        report += ['', '收益为固定研究手续费后的历史模拟；零交易和小样本不代表安全。各情景胜率区间、费用、每日收益、深度/等待拒绝见完整JSON。', '',
                   f'[返回总报告]({REPORT})；[完整统计](results.json)；[冻结计划](../../../docs/{PLAN.name})。', '']
        (OUT / f'{name}.md').write_text('\n'.join(report))
    lines += ['', f'[冻结计划](../../../docs/{PLAN.name})；[数据审计](数据质量与因果审计.md)；'
              '[完整统计](results.json)；[逐笔情景](entries.json.gz)；[资金账本](sizing_ledgers.json.gz)；[输入清单](manifest.json)。', '']
    (OUT / REPORT).write_text('\n'.join(lines))
    quality = ['# T072：数据质量与因果审计', '',
               f"候选情景{audit['uniqueEntries']}条；资金统计{len(results)}组，不是独立样本数。", '',
               f"T070对照{audit['baseline']['matchedEntries']}条候选逐字段一致；一分钟递推全部字段与原实现一致。", '',
               f"违规计数：`{audit['violations']}`。", '',
               '5/15分钟聚合完整性：', '', '```json', __import__('json').dumps(manifest['aggregation'], ensure_ascii=False, indent=2), '```', '',
               '所有输入/代码/计划SHA256见manifest；源缓存只读，与T065源清单逐项一致；官方标签逐场核对。'
               '高周期缺根即重置；公布延迟不延长原有效期。全部当前市场标签只用于事后计分和收到后的资金推进。', '',
               '完整规则24组×公布延迟2×订单延迟3×成本口径2×方向3×时间分段4×资金3，全部保留，不按结果删组。'
               '每秒决策和历史盘口不能证明实盘排队、网络与最小订单可实现。', '',
               f'[返回总报告]({REPORT})。', '']
    (OUT / '数据质量与因果审计.md').write_text('\n'.join(quality))
    return qualified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-entries', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    parent = base.load(base.OUT / 'manifest.json')
    deps = [PLAN, Path(__file__), Path(base.__file__), Path(old.__file__),
            Path(__file__).with_name('analyze_supertrend_robustness.py'), old.OUT / 'entries.json.gz']
    deps += [base.OUT / name for name in ('manifest.json','market_coverage.json','official_label_arrivals.json',
                                         'binance_official_1m.json.gz','spot_projection.json.gz')]
    inputs = {str(p.relative_to(base.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in deps}
    con = sqlite3.connect(f'file:{base.ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    if [list(r) for r in con.execute('SELECT path,size,mtime_ns FROM source_files ORDER BY path')] != parent['sourceFiles']:
        raise RuntimeError('frozen source cache changed')
    labels = base.load(base.OUT / 'official_label_arrivals.json')['markets']
    markets = []
    for c in base.load(base.OUT / 'market_coverage.json'):
        row = dict(con.execute('SELECT market_id,start_us,end_us,winner FROM markets WHERE market_id=?', (c['marketId'],)).fetchone())
        if row['winner'] != labels[row['market_id']]['winner'] or row['end_us'] - row['start_us'] != 5 * base.MINUTE:
            raise RuntimeError('market label/boundary mismatch')
        row['label_available_us'] = c['labelAvailableUs']
        markets.append(row)
    if len(markets) != 3752 or any(a['start_us'] >= b['start_us'] for a, b in zip(markets, markets[1:])):
        raise RuntimeError('market cohort mismatch')
    bars = base.load(base.OUT / 'binance_official_1m.json.gz')['bars']
    pack, aggregation = build_pack(bars)
    callback = make_callback(pack)
    if args.reuse_entries:
        cached = base.load(OUT / 'manifest.json')
        if cached['inputs'] != inputs or cached['version'] != VERSION:
            raise RuntimeError('cached inputs/version mismatch')
        entries, extraction = base.load(OUT / 'entries.json.gz'), cached['extraction']
    else:
        crosses = base.load(base.OUT / 'spot_projection.json.gz')['markets']
        print('extracting T072', len(markets), 'markets', len(SPECS), 'rules', flush=True)
        entries, extraction, _ = base.extract_entries(con, markets, crosses, ([],[]), None, OUT,
                                                     (), include_builtin=False, signal_callback=callback)
        for e in entries:
            e.update(entry_features(e, pack))
        base.dump(OUT / 'entries.json.gz', entries)
    con.close()
    print('auditing', len(entries), 'candidate scenarios', flush=True)
    audit = audit_entries(entries, pack)
    audit['baseline'] = baseline_check(entries)
    print('calculating coverage and finite accounts', flush=True)
    coverage = indicator_coverage(markets, pack)
    results, ledgers = analyze(entries)
    if any(r['negativeWinningCycles'] for r in results if r['odds'] == ODDS[1]):
        raise RuntimeError('net odds cycle invariant failed')
    manifest = {'version':VERSION, 'runDate':'2026-11-20', 'inputs':inputs, 'rules':SPECS,
                'settledMarkets':len(markets), 'barCount':len(bars), 'aggregation':aggregation,
                'publicationLagsMs':LAGS, 'orderDelaysMs':DELAY, 'decisionGridMs':1000,
                'sourceFiles':parent['sourceFiles'], 'extraction':extraction, 'newOutOfSampleData':False}
    for filename, data in [('results.json',results),('sizing_ledgers.json.gz',ledgers),
                           ('indicator_coverage.json',coverage),('audit.json',audit),('manifest.json',manifest)]:
        base.dump(OUT / filename, data)
    qualified = render(results, manifest, audit, coverage)
    print({'entries':len(entries),'scenarios':len(results),'qualified':qualified,'audit':audit,'out':str(OUT)}, flush=True)


if __name__ == '__main__':
    main()

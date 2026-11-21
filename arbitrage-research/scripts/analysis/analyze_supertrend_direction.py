#!/usr/bin/env python3
"""T070: closed-minute Supertrend direction and finite-stake research only."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import math
from pathlib import Path
import sqlite3

import analyze_martingale_candidates as base

OUT = base.ROOT / 'outputs/research_tests/t070_supertrend_direction_20261120'
VERSION = 'supertrend-v1-closed-minute-exact-binance-open'
CONFIGS = {'primary': (14, 3.), 'n10': (10, 3.), 'k2': (14, 2.)}
RULES = {'S0': '持续方向', 'S1': '翻转触发', 'SX': '持续方向同覆盖对照',
         'S2': '持续方向+本场开盘同侧', 'S3': '持续方向+开盘同侧+距离时间'}
NAMES = {rule + ('' if config == 'primary' else '_' + config):
         f'{name} ATR({period})×{factor:g}'
         for config, (period, factor) in CONFIGS.items() for rule, name in RULES.items()}
PLAN = '../../../docs/Supertrend线上做多线下做空验证计划_20261120.md'
REPORT = 'Supertrend线上做多线下做空验证.md'


def build_supertrend(bars, period=14, factor=3.):
    """Wilder ATR, final trailing bands, strict closed-price crossings.

    Seed DOWN is only a recursive initial condition, not a trade signal. Wait
    for an observed flip. All state is reset on a missing or misaligned minute.
    """
    if period < 1 or factor <= 0:
        raise ValueError('positive ATR period and factor required')
    times, values, seed = [], [], []
    previous = None
    atr = upper = lower = direction = None
    confirmed = False
    for bar in bars:
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
            upper = (basic_upper if upper is None or basic_upper < upper or previous['close'] > upper else upper)
            lower = (basic_lower if lower is None or basic_lower > lower or previous['close'] < lower else lower)
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


def build_indicators(bars):
    variants = {name: build_supertrend(bars, *params)[1] for name, params in CONFIGS.items()}
    times = [bar['end_us'] for bar in bars]
    values = [{'barEndUs': at, 'rsi': None, 'sigma': None,
               'supertrends': {name: rows[i] for name, rows in variants.items()}}
              for i, at in enumerate(times)]
    return times, values


def distance_time_score(spot, anchor, atr, remaining, side):
    if spot <= 0 or anchor <= 0 or atr is None or atr <= 0 or remaining <= 0:
        return None
    sign = 1 if side == 'UP' else -1
    return sign * (spot - anchor) / (atr * math.sqrt(remaining / 60))


def make_signal_callback(opens):
    def signals(at, ind, xc, remaining, **kwargs):
        if ind is None:
            return {}
        start = at // (5 * base.MINUTE) * (5 * base.MINUTE)
        anchor = opens.get(start)
        fresh = xc is not None and anchor is not None and anchor > 0
        result = {}
        for config, row in ind['supertrends'].items():
            suffix = '' if config == 'primary' else '_' + config
            side = row['direction']
            result['S0' + suffix] = side
            result['S1' + suffix] = row['flip']
            result['SX' + suffix] = side if fresh else None
            score = distance_time_score(xc[2], anchor, row['atr'], remaining, side) if fresh and side else None
            result['S2' + suffix] = side if score is not None and score > 0 else None
            result['S3' + suffix] = side if score is not None and score >= 1 else None
        return result
    return signals


def add_entry_features(entries, indicators, opens, crosses):
    cross_times = {mid: [r[0] for r in rows] for mid, rows in crosses.items()}
    for entry in entries:
        config = entry['strategy'].split('_', 1)[1] if '_' in entry['strategy'] else 'primary'
        row = base.indicator_at(indicators, entry['decisionUs'])['supertrends'][config]
        xs = crosses.get(entry['marketId'], [])
        xc = base.cross_at(xs, cross_times.get(entry['marketId'], []), entry['decisionUs'])
        anchor = opens.get(entry['startUs'])
        entry.update(indicatorBarEndUs=row['barEndUs'], supertrendLine=row['line'],
                     indicatorClose=row['close'], atr=row['atr'], trendDirection=row['direction'],
                     flip=row['flip'], atrPeriod=CONFIGS[config][0], atrMultiplier=CONFIGS[config][1],
                     binanceAnchorOpen=anchor, anchorOpenUs=entry['startUs'] if anchor is not None else None,
                     spotReceivedUs=xc[0] if xc else None, spotSourceUs=xc[1] if xc else None,
                     spotPrice=xc[2] if xc else None, recordedSnapshotAnchor=xc[3] if xc else None,
                     distanceTimeScore=distance_time_score(xc[2], anchor, row['atr'], entry['remaining'], entry['side'])
                     if xc and anchor else None)


def scenarios(delay):
    result = [('fixed', 1, 100., 60, 1.), ('martin3_100', 3, 100., 60, 1.),
              ('martin5_100', 5, 100., 60, 1.), ('martin11_100', 11, 100., 60, 1.),
              ('martin11_2047', 11, 2047., 60, 1.)]
    if delay == 300:
        result += [('fixed_fee2', 1, 100., 60, 2.), ('fixed_settle300', 1, 100., 300, 1.)]
    return result


def analyze(entries, out):
    groups = defaultdict(list)
    for entry in entries:
        for side in (entry['side'], 'BOTH'):
            groups[(entry['strategy'], side, entry['delayMs'], entry['odds'])].append(entry)
    results, ledgers = [], {}
    for strategy in NAMES:
        for side in ('UP', 'DOWN', 'BOTH'):
            for delay in (0, 300, 1000):
                for odds in ('gross_le_050', 'net_cost_le_050'):
                    rows = groups[(strategy, side, delay, odds)]
                    for split in ('all', 'train', 'validation', 'test'):
                        selected = rows if split == 'all' else [r for r in rows if r['split'] == split]
                        for label, rounds, capital, wait, fees in scenarios(delay):
                            summary, ledger = base.replay(selected, rounds, capital, wait, fees)
                            summary.update(strategy=strategy, side=side, delayMs=delay, odds=odds,
                                           split=split, sizing=label, maxRounds=rounds, qualifiedSignals=len(selected))
                            if label == 'fixed':
                                summary['dailyBlockRoiCI95'] = base.bootstrap_daily_ci(summary['daily'])
                            results.append(summary)
                            if delay == 300 and split in ('all', 'validation', 'test'):
                                ledgers['|'.join((strategy, side, odds, split, label))] = ledger
    base.dump(out / 'results.json', results)
    base.dump(out / 'sizing_ledgers.json.gz', ledgers)
    return results, ledgers


def exact_entry_overlay(entries, ledgers):
    """Descriptions on SX's actual fixed-size entries, without replacing rejects."""
    lookup = {(e['strategy'], e['marketId'], e['side'], e['executionUs']): e
              for e in entries if e['delayMs'] == 300 and e['odds'] == 'gross_le_050'}
    result = []
    for config in CONFIGS:
        suffix = '' if config == 'primary' else '_' + config
        for side in ('UP', 'DOWN', 'BOTH'):
            for split in ('all', 'validation', 'test'):
                key = '|'.join(('SX' + suffix, side, 'gross_le_050', split, 'fixed'))
                reference = ledgers[key]
                matched = [lookup[('SX' + suffix, r['marketId'], r['side'], r['executionUs'])] for r in reference]
                for target, threshold in (('S2', 0), ('S3', 1)):
                    kept = [e for e in matched if e['distanceTimeScore'] is not None and
                            (e['distanceTimeScore'] > 0 if threshold == 0 else e['distanceTimeScore'] >= 1)]
                    rejected = [e for e in matched if e not in kept]
                    metrics, _ = base.replay(kept)
                    result.append({'strategy': target + suffix, 'side': side, 'split': split,
                                   'referenceTrades': len(matched), 'kept': len(kept),
                                   'removedWins': sum(e['won'] for e in rejected),
                                   'removedLosses': sum(not e['won'] for e in rejected),
                                   'metricsWithoutReplacementEntries': metrics})
    return result


def coverage_audit(markets, indicators, opens, crosses):
    market_counts, grids = Counter(), Counter()
    for i, market in enumerate(markets):
        split = base.market_split(i, len(markets))
        xs = crosses.get(market['market_id'], [])
        xtimes = [r[0] for r in xs]
        ready, fresh_ready = set(), set()
        for at in range(market['start_us'] + 30 * base.SECOND, market['end_us'] - 30 * base.SECOND + 1, base.SECOND):
            ind = base.indicator_at(indicators, at)
            xc = base.cross_at(xs, xtimes, at)
            for segment in ('all', split):
                grids[(segment, 'total')] += 1
                grids[(segment, 'freshSpot')] += int(xc is not None)
            if ind is None:
                continue
            for config, row in ind['supertrends'].items():
                if row['direction']:
                    ready.add(config)
                    if xc is not None and market['start_us'] in opens:
                        fresh_ready.add(config)
        for config in CONFIGS:
            for segment in ('all', split):
                market_counts[(config, segment, 'ready')] += int(config in ready)
                market_counts[(config, segment, 'freshReady')] += int(config in fresh_ready)
    return {'markets': [{'config': c, 'split': s, 'indicatorReadyMarkets': market_counts[(c, s, 'ready')],
                         'indicatorAndSpotReadyMarkets': market_counts[(c, s, 'freshReady')]}
                        for c in CONFIGS for s in ('all', 'train', 'validation', 'test')],
            'grid': [{'split': s, 'total': grids[(s, 'total')], 'freshSpot': grids[(s, 'freshSpot')]}
                     for s in ('all', 'train', 'validation', 'test')],
            'exactBinanceOpenMarkets': sum(m['start_us'] in opens for m in markets)}


def audit_entries(entries):
    counts = Counter()
    keys = []
    for e in entries:
        keys.append((e['strategy'], e['marketId'], e['side'], e['odds'], e['delayMs']))
        if not (e['indicatorBarEndUs'] <= e['decisionUs'] <= e['executionUs'] < e['endUs']
                and 0 <= e['decisionUs'] - e['indicatorBarEndUs'] < base.MINUTE
                and 0 <= e['executionUs'] - e['bookSourceUs'] <= 2 * base.SECOND
                and 0 <= e['executionUs'] - e['bookReceivedUs'] <= 2 * base.SECOND):
            counts['timestampViolations'] += 1
        if not (e['fixedFill']['cost'] <= 1 + 1e-9 and e['fixedFill']['worst'] <= e['priceCap'] + 1e-9):
            counts['budgetOrCapViolations'] += 1
        if e['trendDirection'] != e['side']:
            counts['directionViolations'] += 1
        rule = e['strategy'].split('_', 1)[0]
        if rule == 'S1' and e['flip'] != e['side']:
            counts['flipViolations'] += 1
        if rule in ('SX', 'S2', 'S3'):
            if not (e['anchorOpenUs'] == e['startUs'] and e['anchorOpenUs'] < e['decisionUs']
                    and e['spotReceivedUs'] is not None and e['spotSourceUs'] is not None
                    and 0 <= e['decisionUs'] - e['spotReceivedUs'] <= 2 * base.SECOND
                    and 0 <= e['decisionUs'] - e['spotSourceUs'] <= 2 * base.SECOND):
                counts['spotOrAnchorViolations'] += 1
        if rule == 'S2' and not (e['distanceTimeScore'] is not None and e['distanceTimeScore'] > 0):
            counts['anchorDirectionViolations'] += 1
        if rule == 'S3' and not (e['distanceTimeScore'] is not None and e['distanceTimeScore'] >= 1):
            counts['distanceTimeViolations'] += 1
    result = {name: counts[name] for name in ('timestampViolations', 'budgetOrCapViolations',
              'directionViolations', 'flipViolations', 'spotOrAnchorViolations',
              'anchorDirectionViolations', 'distanceTimeViolations')}
    result.update(entries=len(entries), uniqueEntries=len(set(keys)), duplicates=len(keys) - len(set(keys)))
    if any(result[k] for k in result if k.endswith('Violations') or k == 'duplicates'):
        raise RuntimeError(f'causality/uniqueness audit failed: {result}')
    return result


def primary(r, odds='gross_le_050'):
    return r['delayMs'] == 300 and r['odds'] == odds and r['sizing'] == 'fixed'


def result_table(rows):
    lines = ['|规则|方向|分段|胜/笔数|胜率|含费净收益U|投入ROI|最大回撤U|最长连败|',
             '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        lines.append(f"|{r['strategy']}|{r['side']}|{r['split']}|{r['wins']}/{r['n']}|"
                     f"{base.pct(r['winRate'])}|{r['netPnl']:+.3f}|{base.pct(r['roi'])}|"
                     f"{r['maxDrawdown']:.3f}|{r['maxConsecutiveLosses']}|")
    return '\n'.join(lines) + '\n'


def qualification(results):
    qualifying = []
    for strategy in NAMES:
        for side in ('UP', 'DOWN', 'BOTH'):
            rows = [r for r in results if primary(r) and r['strategy'] == strategy and r['side'] == side
                    and r['split'] in ('validation', 'test')]
            if len(rows) == 2 and all(r['n'] >= 100 and r['netPnl'] > 0 for r in rows):
                qualifying.append(strategy + '/' + side)
    return qualifying


def write_failure_examples(entries, ledgers, out):
    lookup = {(e['strategy'], e['marketId'], e['side'], e['executionUs']): e for e in entries
              if e['delayMs'] == 300 and e['odds'] == 'gross_le_050'}
    lines = ['# T070：失败样本与小样本检查', '',
             f'以下为主参数 S1/S3 的共享账户、固定1U、300ms延迟中所有失败单。规则见[总报告]({REPORT})。', '',
             '保留失败样本是为检查交易何时失败，不根据这些已知结果重新调参。ATR方向预测的是趋势状态；五分钟胜负仍看官方结算。', '',
             '|规则|入场时间（北京）|市场|方向|剩余秒数|实际VWAP|Spot|Binance开盘|ATR|距离时间分数|亏损U|',
             '|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    records = []
    for strategy in ('S1', 'S3'):
        for row in ledgers['|'.join((strategy, 'BOTH', 'gross_le_050', 'all', 'fixed'))]:
            if row['won']:
                continue
            entry = lookup[(strategy, row['marketId'], row['side'], row['executionUs'])]
            score = entry['distanceTimeScore']
            num = lambda v: '—' if v is None else f'{v:.3f}'
            lines.append(f"|{strategy}|{row['entryTime']}|{row['marketId']}|{row['side']}|{row['remaining']:.0f}|{row['vwap']:.4f}|"
                         f"{num(entry['spotPrice'])}|{num(entry['binanceAnchorOpen'])}|{entry['atr']:.3f}|{num(score)}|{row['pnl']:+.3f}|")
            records.append({'strategy': strategy, 'trade': row, 'entry': entry})
    lines += ['', 'S1/S3共有不少低于30笔的方向或留后分组；100%胜率的敏感性子组也不能视为确定性。'
              '不在本表出现的赢单与其他规则的全部失败单都保存在entries.json.gz与sizing_ledgers.json.gz。']
    (out / '失败样本与小样本检查.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    base.dump(out / 'failure_examples.json', records)


def render_reports(results, overlays, manifest, audit, out):
    core = [r for r in results if primary(r)]
    main = [r for r in core if '_' not in r['strategy']]
    passed = qualification(results)
    get = lambda strategy, side, split='all', sizing='fixed', delay=300: next(
        r for r in results if r['strategy'] == strategy and r['side'] == side and r['split'] == split
        and r['sizing'] == sizing and r['delayMs'] == delay and r['odds'] == 'gross_le_050')
    s0 = get('S0', 'BOTH')
    s1 = get('S1', 'BOTH')
    s2 = get('S2', 'BOTH')
    s3 = get('S3', 'DOWN')
    s3last = get('S3', 'DOWN', 'test')
    s0martin = get('S0', 'BOTH', sizing='martin11_2047')
    lines = [f'# T070：Supertrend 线上做多、线下做空', '',
             f"沿用 {manifest['settledMarkets']} 个官方结算 BTC 五分钟市场：北京时间 {manifest['firstMarket']} 至 {manifest['lastMarket']}，有采集缺口。规则见[冻结计划]({PLAN})。本轮仅离线研究。", '',
             '## 本轮发现', '',
             f"- 持续方向S0的单一共享账户：{s0['wins']}/{s0['n']}胜、含费净收益{s0['netPnl']:+.3f}U、最长{s0['maxConsecutiveLosses']}连败；线上持续多、线下持续空未形成稳定优势。",
             f"- 翻转触发S1共享账户：{s1['wins']}/{s1['n']}胜、净{s1['netPnl']:+.3f}U；主参数末段为{get('S1','BOTH','test')['wins']}/{get('S1','BOTH','test')['n']}胜、净{get('S1','BOTH','test')['netPnl']:+.3f}U。"
             f"但1秒延迟末段净{get('S1','BOTH','test',delay=1000)['netPnl']:+.3f}U，样本少且执行敏感，不能升级。",
             f"- 加开盘同侧S2改善共享账户全段至{s2['netPnl']:+.3f}U，但末段{get('S2','BOTH','test')['netPnl']:+.3f}U；没有跨段稳定盈利。",
             f"- S3距离时间过滤的DOWN全段{s3['wins']}/{s3['n']}胜（{base.pct(s3['winRate'])}）、净{s3['netPnl']:+.3f}U；末段仅{s3last['wins']}/{s3last['n']}胜、净{s3last['netPnl']:+.3f}U。高累计胜率暂不足以确认有效。",
             '- 敏感性并未建立统一稳健优势：把ATR系数从3降至2后，翻转触发的UP在验证段和末段均亏损；不能挑最好参数作结论。',
             f"- S0共享账户、2047U起始本金、11级倍投：{s0martin['n']}笔后净{s0martin['netPnl']:+.3f}U，出现{s0martin['capLossCycles']}次11级打穿，"
             f"本金不足停止{s0martin['rejections'].get('bankroll_stop',0)}次。这里只证明这条历史路径发生严重损失，不外推未来爆仓概率。", '',
             '## 规则与结果口径', '',
             '- 主参数：Binance 已收盘 1 分钟 K 线、Wilder ATR(14)、3 倍系数；敏感性为 ATR(10)×3、ATR(14)×2。趋势由 Supertrend 最终上下轨递推，不是把价格与 ATR 数字直接比较。',
             '- S0 持续方向；S1 仅翻转后60秒；SX 为 S0 加新鲜 Spot 的覆盖对照；S2 在 SX 上要求 Spot 与本场 Binance 开盘价同侧；S3 再要求方向化距离/(ATR×sqrt(剩余秒数/60))≥1。',
             '- 剩余270–30秒、决策卖一0.30–0.50、逐档1U含费预算、300ms延迟、决策/执行盘口年龄≤2秒；首个可发送机会失败不追单，持有官方结算。不是原成熟策略0.80–0.85尾盘条件。',
             '- 胜负以 Polymarket 官方标签为准；Binance 精确开盘价是辅助参考，不替代 Chainlink 结算线。距离时间分数不是胜率。',
             '- 前60%/中20%/末20%按全部市场时间划分；中段从 '+manifest['validationStart']+'，末段从 '+manifest['testStart']+' 开始；末段曾用于其他研究，是历史留后检查。分段资金各自重置。',
             '- UP/DOWN 为独立账户；BOTH 才是随指标方向切换、共享本金与结算等待的单账户。不能把两方向独立收益直接相加当作 BOTH。',
             '- 费用冻结假设0.07*p*(1-p)/份；1U不证明符合真实最低金额，未模拟撤单、冲击、真实网络抖动或结算到账。下一单必须等实际收到标签且结束至少60秒。', '',
             '## 主参数：全段固定金额', '', result_table([r for r in main if r['split'] == 'all' and r['side'] != 'BOTH']),
             '## 主参数：验证段与末段固定金额', '', result_table([r for r in main if r['split'] in ('validation', 'test') and r['side'] != 'BOTH']),
             '## 主参数：共享本金双方向账户', '', result_table([r for r in main if r['side'] == 'BOTH' and r['split'] != 'train']),
             '## 参数敏感性：验证段与末段', '', result_table([r for r in core if '_' in r['strategy'] and r['split'] in ('validation', 'test') and r['side'] != 'BOTH']),
             '## 同覆盖、同一组成交的因子检查', '',
             '以下只对 SX 已实际执行的固定金额入场应用过滤，拒绝后不另找本场更晚入场；是描述性配对对照，不等于 S2/S3 独立重新发现首单的收益。', '',
             '|过滤|方向|分段|SX基准笔数|保留|剔除胜单|剔除败单|过滤后净收益U|',
             '|---|---|---|---:|---:|---:|---:|---:|']
    for row in overlays:
        if '_' not in row['strategy'] and row['side'] != 'BOTH' and row['split'] != 'all':
            lines.append(f"|{row['strategy']}|{row['side']}|{row['split']}|{row['referenceTrades']}|{row['kept']}|"
                         f"{row['removedWins']}|{row['removedLosses']}|{row['metricsWithoutReplacementEntries']['netPnl']:+.3f}|")
    lines += ['', '## 判断与局限', '',
              ('满足验证段和末段各≥100笔且固定金额均盈利的组合：'+ '、'.join(passed)) if passed else
              '没有组合同时满足验证段和末段各≥100笔且固定金额含费均盈利的初步门槛。', '',
              '该门槛不是统计显著或实盘盈利证明。多规则、参数和分段比较存在选择偏差，必须新增日期前瞻验证。倍投收益不能当作信号预测优势；没有触及高等级不代表高等级已验证。', '',
              'S0/S1可以在没有新鲜Spot时用已收盘K线判断；S2/S3依赖新鲜Spot，须先看SX覆盖对照。已收盘历史K线的实际发布延迟没有日志，300ms指订单延迟，未额外模拟K线推送延迟。', '',
              '## 规则独立报告与复现', '',
              '- [有限本金、费用与延迟压力](有限本金与压力对照.md)',
              '- [数据质量与因果审计](数据质量与因果审计.md)',
              '- [失败样本与小样本检查](失败样本与小样本检查.md)',
              '- [机器结果](results.json)；[候选逐笔](entries.json.gz)；[资金逐笔](sizing_ledgers.json.gz)；[配对因子检查](paired_overlay.json)', '']
    for rule, label in RULES.items():
        filename = f'{rule}_{label}.md'
        lines.append(f'- [{rule} {label}]({filename})')
        body = f'# {rule} {label}\n\n规则见[冻结计划]({PLAN})与[总报告]({REPORT})。下表为固定金额，包含主参数和两组预设敏感性，UP/DOWN与共享账户分开。\n\n'
        body += result_table([r for r in core if r['strategy'].split('_', 1)[0] == rule])
        (out / filename).write_text(body, encoding='utf-8')
    lines += ['', '```bash', 'cd /Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research',
              'python3 scripts/analysis/analyze_supertrend_direction.py',
              '# 仅复算资金与报告：追加 --reuse-entries', '```', '']
    (out / REPORT).write_text('\n'.join(lines), encoding='utf-8')

    pressure = ['# T070：有限本金与压力对照', '', f'规则见[总报告]({REPORT})。以下全部包含研究手续费。', '',
                '## 主参数全段资金管理', '',
                '|规则|账户|情景|笔数|胜率|净收益U|ROI|回撤U|最长连败|最大注额U|级数打穿|本金停止|等结算跳过|决策深度拒绝|执行深度拒绝|',
                '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in results:
        if '_' not in r['strategy'] and r['delayMs'] == 300 and r['odds'] == 'gross_le_050' and r['split'] == 'all' and r['sizing'] not in ('fixed_fee2', 'fixed_settle300'):
            reject = r['rejections']
            pressure.append(f"|{r['strategy']}|{r['side']}|{r['sizing']}|{r['n']}|{base.pct(r['winRate'])}|{r['netPnl']:+.3f}|{base.pct(r['roi'])}|"
                            f"{r['maxDrawdown']:.3f}|{r['maxConsecutiveLosses']}|{r['maxBudget']:.0f}|{r['capLossCycles']}|{reject.get('bankroll_stop', 0)}|"
                            f"{reject.get('waiting_for_settlement', 0)}|{reject.get('insufficient_decision_depth', 0)}|{reject.get('insufficient_full_stake_depth', 0)}|")
    pressure += ['', '## 主参数末段固定金额压力', '',
                 '|规则|账户|延迟ms|情景|胜/笔数|含费净收益U|投入ROI|', '|---|---|---:|---|---:|---:|---:|']
    for r in results:
        if ('_' not in r['strategy'] and r['odds'] == 'gross_le_050' and r['split'] == 'test'
                and (r['sizing'] == 'fixed' or r['delayMs'] == 300 and r['sizing'] in ('fixed_fee2', 'fixed_settle300'))):
            pressure.append(f"|{r['strategy']}|{r['side']}|{r['delayMs']}|{r['sizing']}|{r['wins']}/{r['n']}|{r['netPnl']:+.3f}|{base.pct(r['roi'])}|")
    pressure += ['', '## 发送前每档含费成本不超过0.50', '',
                 '订单限价在发送前确定，主延迟300ms；不能用未来平均成交成本筛单。毛价0.50的盈利赔率扣费后小于1:1，含费限价版本用于对照。', '',
                 result_table([r for r in results if primary(r, 'net_cost_le_050') and '_' not in r['strategy'] and r['split'] in ('validation', 'test')]),
                 '3/5/11单打穿后重置；100U可能在第11单之前已资金不足停止。原始预算1、2、4…1024U包含费用；最高级盘口深度、可用资金都需满足，不能按小单等比例放大。']
    (out / '有限本金与压力对照.md').write_text('\n'.join(pressure) + '\n', encoding='utf-8')

    quality = ['# T070：数据质量与因果审计', '',
               f"- 冻结官方市场 {manifest['settledMarkets']} 个；K线 {manifest['candles']['bars']} 根；逐笔候选情景 {audit['entries']} 条，唯一 {audit['uniqueEntries']} 条；资金统计 {audit['scenarios']} 组。候选情景包含规则/参数/延迟/费用上限重复，不是独立样本数。",
               f"- 冻结K线、Spot投影、父清单与计划都记录SHA256：见 [manifest.json](manifest.json)。SQLite以只读方式连接；source_files清单与T065逐项一致；官方结算与冻结标签逐场一致。",
               '- Supertrend递推仅用已收盘K线；真实跨轨之前不从初始化下注；分钟缺口重置ATR、轨道和方向。',
               '- 开盘基准只读取恰好本场开始时刻的Binance K线open，未读取未收盘H/L/C。实时Spot与盘口均按收到时间及源时间≤2秒过滤；官方结算标签只用于事后计分和已收到后的资金推进。',
               '- 所有方向/翻转/距离/时序/预算/限价/重复审计违规均应为0，具体计数见 [audit.json](audit.json)。',
               '- 缺失Spot不插值、不从未来K线构造盘中价格；覆盖计数见 [coverage_audit.json](coverage_audit.json)。原Spot投影的损坏文件和排除计数保留在manifest中，不能把缺失当作信号失败。',
               '- S2/S3参考的是Binance基准，不是Chainlink官方基准；300ms没有模拟历史K线公布时间与客户端推送延迟；1U不证明符合实盘最低金额。', '',
               f'[返回总报告]({REPORT})。']
    (out / '数据质量与因果审计.md').write_text('\n'.join(quality) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-entries', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    parent = base.load(base.OUT / 'manifest.json')
    cohort = base.load(base.OUT / 'market_coverage.json')
    labels = base.load(base.OUT / 'official_label_arrivals.json')['markets']
    inputs = {name: hashlib.sha256((base.OUT / name).read_bytes()).hexdigest() for name in
              ('manifest.json', 'binance_official_1m.json.gz', 'spot_projection.json.gz', 'market_coverage.json', 'official_label_arrivals.json')}
    plan = base.ROOT / 'docs/Supertrend线上做多线下做空验证计划_20261120.md'
    inputs['planSha256'] = hashlib.sha256(plan.read_bytes()).hexdigest()
    con = sqlite3.connect(f'file:{base.ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    sources = [list(r) for r in con.execute('SELECT path,size,mtime_ns FROM source_files ORDER BY path')]
    if sources != parent['sourceFiles']:
        raise RuntimeError('frozen source cache changed; refreeze separately')
    markets = []
    for record in cohort:
        row = dict(con.execute('SELECT market_id,start_us,end_us,winner FROM markets WHERE market_id=?', (record['marketId'],)).fetchone())
        if row['winner'] != labels[row['market_id']]['winner']:
            raise RuntimeError('official label mismatch')
        if row['start_us'] % (5 * base.MINUTE) or row['end_us'] - row['start_us'] != 5 * base.MINUTE:
            raise RuntimeError('unexpected market boundary')
        row['label_available_us'] = record['labelAvailableUs']
        markets.append(row)
    if len(markets) != parent['settledMarkets'] or any(a['start_us'] >= b['start_us'] for a, b in zip(markets, markets[1:])):
        raise RuntimeError('market cohort/order mismatch')
    bars = base.load(base.OUT / 'binance_official_1m.json.gz')['bars']
    indicators = build_indicators(bars)
    opens = {r['open_us']: r['open'] for r in bars}
    projected = base.load(base.OUT / 'spot_projection.json.gz')
    crosses = projected['markets']
    callback = make_signal_callback(opens)
    if args.reuse_entries:
        cached = base.load(OUT / 'manifest.json')
        if cached['version'] != VERSION or cached['inputs'] != inputs:
            raise RuntimeError('cached entries inputs/version mismatch')
        entries, extraction = base.load(OUT / 'entries.json.gz'), cached['extraction']
    else:
        print('extracting frozen Supertrend candidates', len(markets), 'markets', flush=True)
        entries, extraction, _ = base.extract_entries(con, markets, crosses, indicators, None, OUT,
                                                     (), include_builtin=False, signal_callback=callback)
        add_entry_features(entries, indicators, opens, crosses)
        base.dump(OUT / 'entries.json.gz', entries)
    con.close()
    audit = audit_entries(entries)
    for entry in entries:
        ind = base.indicator_at(indicators, entry['decisionUs'])
        xs = crosses.get(entry['marketId'], [])
        xc = base.cross_at(xs, [r[0] for r in xs], entry['decisionUs'])
        if callback(entry['decisionUs'], ind, xc, entry['remaining']).get(entry['strategy']) != entry['side']:
            raise RuntimeError('entry signal cannot be reproduced')
    print('candidate audit passed; calculating finite accounts', len(entries), flush=True)
    results, ledgers = analyze(entries, OUT)
    if any(r['odds'] == 'net_cost_le_050' and r['feeMultiplier'] == 1 and r['negativeWinningCycles'] for r in results):
        raise RuntimeError('net-cost odds invariant failed')
    overlays = exact_entry_overlay(entries, ledgers)
    coverage = coverage_audit(markets, indicators, opens, crosses)
    base.dump(OUT / 'paired_overlay.json', overlays)
    base.dump(OUT / 'coverage_audit.json', coverage)
    manifest = {**parent, 'version': VERSION, 'runDate': '2026-11-20', 'parentVersion': parent['version'],
                'inputs': inputs, 'candidates': NAMES, 'configs': CONFIGS, 'extraction': extraction,
                'cross': projected['audit'], 'model': None,
                'rules': {**parent['rules'], 'supertrendParameters': CONFIGS, 'flipValidSeconds': 60,
                          'initialStateRequiresObservedFlip': True, 'distanceTimeThreshold': 1,
                          'anchorSource': 'exact Binance BTCUSDT 1m open at market start',
                          'distanceTimeIsProbability': False, 'accounts': ['UP', 'DOWN', 'BOTH']}}
    audit.update(scenarios=len(results), signalReproductionViolations=0, netOddsCycleViolations=0)
    base.dump(OUT / 'audit.json', audit)
    base.dump(OUT / 'manifest.json', manifest)
    write_failure_examples(entries, ledgers, OUT)
    render_reports(results, overlays, manifest, audit, OUT)
    print({'markets': len(markets), 'entries': len(entries), 'scenarios': len(results),
           'qualified': qualification(results), 'out': str(OUT)}, flush=True)
    for row in results:
        if primary(row) and '_' not in row['strategy'] and row['split'] in ('all', 'validation', 'test'):
            print({k: row[k] for k in ('strategy', 'side', 'split', 'n', 'wins', 'netPnl', 'roi', 'maxConsecutiveLosses')}, flush=True)


if __name__ == '__main__':
    main()

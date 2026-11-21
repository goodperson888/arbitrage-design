#!/usr/bin/env python3
"""T073: retrospective low-streak catalog and paired, causal EMA20 filters."""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
import hashlib
import math
from pathlib import Path
import sqlite3

import analyze_martingale_candidates as base
import analyze_atr_candidates as atr
import analyze_supertrend_timeframes as st
import analyze_regime_martingale_candidates as regime
import analyze_rsi_persistent_crossings as rsi
from analyze_supertrend_robustness import concentration

OUT = base.ROOT / 'outputs/research_tests/t073_ema_filters_20261120'
PLAN = base.ROOT / 'docs/低连亏候选汇总与大周期EMA过滤验证计划_20261120.md'
VERSION = 't073-v1-frozen-paired-ema20-utc-availability'
REPORT = '大周期EMA过滤验证.md'
CATALOG = '历史低连亏正收益候选汇总.md'
VARIANTS = ('NONE', '15m-X', '15m-E', '15m-ES', '60m-X', '60m-E', '60m-ES')
SOURCES = {
    'A5': ('T068', 'ATR距离≥1.0'), 'A6': ('T068', 'ATR距离≥1.5'),
    'A7': ('T068', 'ATR一分钟突破延续'),
    'P_5m_k3': ('T072', '5分钟ATR14×3持续方向'),
    'P_15m_k3': ('T072', '15分钟ATR14×3持续方向'),
    'F_1m_k3': ('T072', '1分钟ATR14×3翻转'),
    'F_5m_k3': ('T072', '5分钟ATR14×3翻转'),
    'C_5m_k1p5': ('T072', '5分钟×1.5方向＋1分钟翻转'),
    'C_15m_k3': ('T072', '15分钟×3方向＋1分钟翻转'),
}
SIZINGS = (('fixed', 1, 100.), ('martin11_100', 11, 100.), ('martin11_2047', 11, 2047.))
WARMUP = base.ROOT / 'outputs/research_tests/t067_regime_martingale_candidates_20261118/warmup/binance_official_1m.json.gz'


def merge_bars(warmup, original):
    rows, overlaps = {}, 0
    for bar in warmup + original:
        at = bar['open_us']
        if at in rows:
            if rows[at] != bar:
                raise ValueError('overlapping bars differ')
            overlaps += 1
        rows[at] = bar
    return [rows[k] for k in sorted(rows)], overlaps


def aggregate_bars(bars, minutes):
    """Complete UTC buckets only; invalid input fails rather than being filled."""
    if minutes not in (15, 60):
        raise ValueError('unsupported EMA timeframe')
    width = minutes * base.MINUTE
    groups, previous = defaultdict(list), None
    for b in bars:
        if (b['open_us'] % base.MINUTE or b['end_us'] - b['open_us'] != base.MINUTE
                or previous is not None and b['open_us'] <= previous):
            raise ValueError('minute bars must be aligned, ordered and unique')
        vals = [b[k] for k in ('open', 'high', 'low', 'close')]
        if (not all(math.isfinite(v) and v > 0 for v in vals)
                or b['low'] > min(b['open'], b['close'])
                or b['high'] < max(b['open'], b['close']) or b['high'] < b['low']):
            raise ValueError('invalid OHLC')
        groups[b['open_us'] // width * width].append(b)
        previous = b['open_us']
    result = []
    for start, rows in sorted(groups.items()):
        if len(rows) != minutes or any(r['open_us'] != start + i * base.MINUTE for i, r in enumerate(rows)):
            continue
        result.append({'open_us': start, 'end_us': start + width, 'open': rows[0]['open'],
                       'high': max(r['high'] for r in rows), 'low': min(r['low'] for r in rows),
                       'close': rows[-1]['close']})
    return result, {'completeBars': len(result), 'incompleteBuckets': len(groups) - len(result)}


def build_ema(bars, minutes, period=20):
    times, rows, seed = [], [], []
    ema = previous_end = None
    for bar in bars:
        if previous_end is None or bar['open_us'] != previous_end:
            seed, ema = [], None
        prev = ema
        if ema is None:
            seed.append(bar['close'])
            if len(seed) == period:
                ema = sum(seed) / period
        else:
            ema += 2. / (period + 1) * (bar['close'] - ema)
        ready = ema is not None and prev is not None
        e = ('UP' if bar['close'] > ema else 'DOWN' if bar['close'] < ema else None) if ready else None
        slope = ('UP' if ema > prev else 'DOWN' if ema < prev else None) if ready else None
        end = bar['end_us']
        rows.append({'barEndUs': end, 'availableUs': end + base.SECOND,
                     'expiresUs': end + minutes * base.MINUTE, 'ema': ema, 'previousEma': prev,
                     'close': bar['close'], 'ready': ready, 'E': e, 'ES': e if e == slope else None})
        times.append(end + base.SECOND)
        previous_end = end
    return times, rows


def row_at(series, at):
    j = bisect_right(series[0], at) - 1
    if j < 0:
        return None
    row = series[1][j]
    return row if row['availableUs'] <= at < row['expiresUs'] else None


def allows(variant, side, features):
    if variant == 'NONE':
        return True
    tf, mode = variant.split('-')
    row = features[int(tf[:-1])]
    return bool(row and row['ready'] and (mode == 'X' or row[mode] == side))


def filter_entries(entries, variant, decisions):
    return [e for e in entries if allows(variant, e['side'], decisions[e['_emaTime']])]


def ledger_stats(ledger):
    pnl = peak = dd = spent = fees = 0.
    streak = longest = wins = 0
    for r in ledger:
        pnl += r['pnl']; spent += r['cost']; fees += r['fee']
        peak = max(peak, pnl); dd = max(dd, peak - pnl)
        wins += int(r['won'])
        streak = 0 if r['won'] else streak + 1
        longest = max(longest, streak)
    return {'n': len(ledger), 'wins': wins, 'netPnl': pnl, 'spent': spent, 'fees': fees,
            'roi': pnl / spent if spent else None, 'maxDrawdown': dd,
            'maxConsecutiveLosses': longest, 'winRate': wins / len(ledger) if ledger else None}


def paired_overlay(ledger, entries, decisions, variant):
    by_fill = {(e['marketId'], e['side'], e['executionUs']): e for e in entries}
    kept, rejected = [], []
    for r in ledger:
        e = by_fill[(r['marketId'], r['side'], r['executionUs'])]
        (kept if allows(variant, e['side'], decisions[e['_emaTime']]) else rejected).append(r)
    return {'kept': ledger_stats(kept), 'rejected': ledger_stats(rejected)}


def load_sources():
    sources, references = defaultdict(list), []
    for label, directory in (('T068', atr.OUT), ('T072', st.OUT)):
        rows = base.load(directory / 'entries.json.gz')
        for i, row in enumerate(rows):
            rule = row.get('rule', row['strategy'])
            if rule not in SOURCES or SOURCES[rule][0] != label:
                continue
            if label == 'T072' and row['publicationLagMs'] != 1000:
                continue
            row['_emaTime'] = row['decisionUs']
            sources[rule].append(row)
            references.append({'source': label, 'sourceEntryIndex': i, 'rule': rule,
                               'marketId': row['marketId'], 'side': row['side'], 'odds': row['odds'],
                               'delayMs': row['delayMs'], 'decisionUs': row['decisionUs'],
                               'executionUs': row['executionUs']})
    if set(sources) != set(SOURCES):
        raise ValueError('missing frozen source')
    return sources, references


def analyze(sources, decisions):
    results, ledgers, overlays, coverage = [], {}, [], []
    for rule in SOURCES:
        print('replaying EMA controls:', rule, flush=True)
        for side in st.SIDES:
            for delay in st.DELAY:
                for odds in st.ODDS:
                    original = [e for e in sources[rule] if (side == 'BOTH' or e['side'] == side)
                                and e['delayMs'] == delay and e['odds'] == odds]
                    for split in st.SPLITS:
                        subset = original if split == 'all' else [e for e in original if e['split'] == split]
                        original_ledger = None
                        for variant in VARIANTS:
                            selected = filter_entries(subset, variant, decisions)
                            meta = dict(rule=rule, side=side, delayMs=delay, odds=odds, split=split, variant=variant)
                            if split == 'all':
                                cov = Counter()
                                if variant != 'NONE':
                                    tf = int(variant.split('m-')[0])
                                    for e in subset:
                                        r = decisions[e['_emaTime']][tf]
                                        if not r: cov['unavailableOrExpired'] += 1
                                        elif not r['ready']: cov['warmup'] += 1
                                        elif not allows(variant, e['side'], decisions[e['_emaTime']]): cov['directionRejected'] += 1
                                coverage.append({**meta, 'originalCandidates': len(subset),
                                                 'acceptedCandidates': len(selected), 'reasons': dict(cov)})
                            for sizing, rounds, capital in SIZINGS:
                                summary, ledger = base.replay(selected, rounds, capital)
                                summary.update(meta, sizing=sizing, qualifiedSignals=len(selected))
                                summary['concentration'] = concentration(summary)
                                if sizing == 'fixed' and delay == 300:
                                    summary['dailyBlockRoiCI95'] = base.bootstrap_daily_ci(summary['daily'])
                                if sizing == 'fixed':
                                    if variant == 'NONE': original_ledger = ledger
                                    if delay == 300:
                                        overlays.append({**meta, **paired_overlay(original_ledger, subset, decisions, variant)})
                                if split == 'all' and delay == 300:
                                    ledgers['|'.join((rule, side, odds, variant, sizing))] = ledger
                                results.append(summary)
    return results, ledgers, overlays, coverage


def baseline_audit(results):
    priors = {'T068': base.load(atr.OUT / 'results.json'), 'T072': base.load(st.OUT / 'results.json')}
    checked = 0
    fields = ('n', 'wins', 'netPnl', 'spent', 'fees', 'maxConsecutiveLosses', 'maxDrawdown', 'maxBudget',
              'capLossCycles', 'negativeWinningCycles', 'qualifiedSignals', 'rejections')
    for r in results:
        if r['variant'] != 'NONE': continue
        label = SOURCES[r['rule']][0]
        if label == 'T068' and r['side'] == 'BOTH': continue
        old = [x for x in priors[label] if x.get('rule', x.get('strategy')) == r['rule']
               and all(x[k] == r[k] for k in ('side', 'delayMs', 'odds', 'split', 'sizing'))
               and (label != 'T072' or x['publicationLagMs'] == 1000)]
        if len(old) != 1: raise ValueError('missing baseline reference')
        for k in fields:
            a, b = r[k], old[0][k]
            equal = math.isclose(a, b, abs_tol=1e-8, rel_tol=1e-10) if isinstance(a, float) else a == b
            if not equal: raise ValueError(f'baseline differs: {r["rule"]} {r["side"]} {k}: {a} vs {b}')
        checked += 1
    return {'matchedGroups': checked, 'fieldsPerGroup': len(fields), 'mismatches': 0,
            'T068BothNewNotPreviouslyPublished': True}


def historical_catalog():
    """All fixed main controls; later exact designs supersede older duplicates."""
    catalog, omitted, fingerprints = [], [], set()
    skip_t070 = {'S0', 'S0_k2', 'S1', 'S1_k2', 'S3'}
    for tid in range(65, 73):
        if tid == 69: continue
        directory = next((base.ROOT / 'outputs/research_tests').glob(f't{tid:03d}_*'))
        rows = base.load(directory / 'results.json')
        selected = [r for r in rows if r['delayMs'] == 300 and r['odds'] == st.ODDS[0]
                    and r['sizing'] == 'fixed' and r['split'] == 'all'
                    and (tid not in (71, 72) or r['publicationLagMs'] == 1000)]
        for r in selected:
            rule = r.get('rule', r.get('strategy'))
            if tid == 70 and rule in skip_t070 or tid == 71 and rule == 'S1':
                omitted.append({'study': tid, 'rule': rule, 'side': r['side'], 'reason': 'superseded by T071/T072 main design'})
                continue
            parts = {s: next(x for x in rows if x.get('rule', x.get('strategy')) == rule
                             and all(x[k] == r[k] for k in ('side','delayMs','odds','sizing'))
                             and x['split'] == s and x.get('publicationLagMs') == r.get('publicationLagMs'))
                     for s in ('train', 'validation', 'test')}
            signature = (rule, r['side'], tuple((x['n'], x['wins'], round(x['netPnl'], 9), x['maxConsecutiveLosses'])
                                               for x in (r, *parts.values())))
            if signature in fingerprints:
                omitted.append({'study': tid, 'rule': rule, 'side': r['side'], 'reason': 'identical main and split statistics'})
                continue
            fingerprints.add(signature)
            if tid == 72:
                spec = st.SPECS[rule]
                label = f'{spec["timeframe"]}分钟ATR14×{spec["factor"]:g} '+{'P':'持续方向','F':'翻转触发','C':'方向＋1分钟同向翻转'}[spec['mode']]
            else:
                names = {65:base.NAMES,66:rsi.NAMES,67:regime.NAMES,68:atr.NAMES,
                         70:st.old.NAMES,71:{'S3':'趋势＋开盘同侧＋距离时间','S13':'翻转与距离交集','D1':'仅距离时间'}}
                label = names[tid].get(rule,rule)
            exact = directory / f'{rule}.md'
            reports = [exact] if exact.is_file() else sorted(directory.glob(f'{rule}_*.md'))
            if not reports:
                reports = sorted(directory.glob(f'{rule.split("_")[0]}_*.md'))
            if not reports:
                main_name = {65:'六类低价策略与有限马丁验证.md',66:'RSI回穿持续方向验证.md',
                             67:'行情过滤三候选验证.md',68:'ATR波动率与锚定距离候选验证.md',
                             70:'Supertrend线上做多线下做空验证.md',71:'Supertrend候选严格复核.md',
                             72:'Supertrend多周期与倍数验证.md'}[tid]
                reports = [directory / main_name]
                if not reports[0].is_file(): raise RuntimeError(f'missing original report: T{tid} {rule}')
            source_report = reports[0] if reports else directory
            catalog.append({'study': f'T{tid:03d}', 'rule': rule, 'name': label, 'side': r['side'], 'all': r,
                            **parts, 'publicationLagMs': r.get('publicationLagMs', 0),
                            'source': str(source_report.relative_to(base.ROOT))})
    catalog.sort(key=lambda x: (x['all']['maxConsecutiveLosses'], -x['all']['netPnl'], x['study'], x['rule'], x['side']))
    mature = []
    directory = next((base.ROOT / 'outputs/research_tests').glob('t069_*'))
    for side, g in base.load(directory / 'results.json')['groups'].items():
        r = g['scenarios']['fixed_1']
        mature.append({'side': side, 'dates': g['dates'], 'n': r['trades'], 'wins': r['wins'],
                       'netPnl': r['netPnl'], 'roi': r['roiOnPrincipalTurnover'],
                       'maxConsecutiveLosses': r['maxConsecutiveLosses'],
                       'maxDrawdown': r['maxSettledEquityDrawdown']})
    return {'mainControls': catalog, 'omittedDuplicates': omitted, 'matureDifferentExecutionModel': mature}


def pct(x):
    return '—' if x is None else f'{x:.1%}'


def table(rows, include_side=True):
    lines = ['|规则/EMA|方向|分段|胜/笔数|胜率|净收益U|投入ROI|费用U|最大回撤U|最长连亏|',
             '|---|---|---|---|---|---|---|---|---|---|']
    for r in rows:
        lines.append(f'|{r["rule"]}/{r["variant"]}|{r["side"]}|{r["split"]}|{r["wins"]}/{r["n"]}|'
                     f'{pct(r["winRate"])}|{r["netPnl"]:+.3f}|{pct(r["roi"])}|{r["fees"]:.3f}|'
                     f'{r["maxDrawdown"]:.3f}|{r["maxConsecutiveLosses"]}|')
    return lines


def select(results, rule=None, side='BOTH', variant=None, split='all', delay=300,
           sizing='fixed', odds=st.ODDS[0]):
    return [r for r in results if (rule is None or r['rule'] == rule) and r['side'] == side
            and (variant is None or r['variant'] == variant) and r['split'] == split
            and r['delayMs'] == delay and r['sizing'] == sizing and r['odds'] == odds]


def qualifies(results, rule, side, variant):
    return all((r := select(results, rule, side, variant, split=split, delay=delay)[0])['n'] >= 100
               and r['netPnl'] > 0 for split in ('validation', 'test') for delay in (300, 1000))


def render_catalog(catalog):
    lines = ['# 历史低连亏正收益候选汇总（T073）', '',
             '2026-11-20整理。主口径为T065–T072固定1U含费预算、订单300ms、名义价格≤0.50；'
             '只作事后描述性筛选。T071/T072用K线公布延迟1秒，其余旧结果0公布延迟，不能假定同等可实现。', '',
             '低连亏定义为历史最长≤3笔；盈利定义为扣冻结手续费后净收益>0。'
             '历史连亏上限不是未来保证。UP/DOWN独立账户与BOTH共享结算等待会改变交易集合；不能直接相加。', '',
             '数据来自3752个官方结算BTC五分钟市场，北京时间2026-08-21 19:00至10-03 01:50，20个活跃日期、有缺口。'
             '不是11月新增样本，也不是连续43天采集。各旧研究有效覆盖不同；完整清单保留来源、各分段指标。', '']
    def catalog_table(rows):
        out = ['|研究/规则|方向|胜/笔数|胜率|净收益U|ROI|最长连亏|验证笔数/收益U|末段笔数/收益U|',
               '|---|---|---|---|---|---|---|---|---|']
        for c in rows:
            r, v, t = c['all'], c['validation'], c['test']
            out.append(f'|[{c["study"]}/{c["rule"]} {c["name"]}](../../../{c["source"]})|{c["side"]}|'
                       f'{r["wins"]}/{r["n"]}|{pct(r["winRate"])}|{r["netPnl"]:+.3f}|{pct(r["roi"])}|'
                       f'{r["maxConsecutiveLosses"]}|{v["n"]}/{v["netPnl"]:+.3f}|{t["n"]}/{t["netPnl"]:+.3f}|')
        return out
    positive = [c for c in catalog['mainControls'] if c['all']['netPnl'] > 0]
    for title, predicate in (
        ('低连亏、至少20笔（20–29笔仍属小样本）', lambda r: r['maxConsecutiveLosses'] <= 3 and r['n'] >= 20),
        ('低连亏、少于20笔，仅供观察', lambda r: r['maxConsecutiveLosses'] <= 3 and r['n'] < 20),
        ('扩展清单：最长4–5连亏', lambda r: 3 < r['maxConsecutiveLosses'] <= 5)):
        lines += [f'## {title}', ''] + catalog_table([c for c in positive if predicate(c['all'])]) + ['']
    lines += ['## 原成熟尾盘策略，单列口径', '',
              'T064/T069同一批，不重复计两套：0.80–0.85、主动成交/5秒动量等原严格条件；固定1U本金、费用另计。'
              '该旧模型与低价组的1U含费预算、0.01份精度、可用官方标签模型不同，不能直接比较收益排名。', '',
              '|方向|胜/笔数|胜率|净收益U|本金ROI|最长连亏|回撤U|交易日期（北京时间）|',
              '|---|---|---|---|---|---|---|---|']
    for r in catalog['matureDifferentExecutionModel']:
        lines.append(f'|{r["side"]}|{r["wins"]}/{r["n"]}|{r["wins"]/r["n"]:.1%}|{r["netPnl"]:+.3f}|'
                     f'{pct(r["roi"])}|{r["maxConsecutiveLosses"]}|{r["maxDrawdown"]:.3f}|{r["dates"][0]}至{r["dates"][-1]}|')
    lines += ['', '## 如何读这张表', '',
              'T068的A6 DOWN是有价值的历史候选：归一化距离≥1.5，并非ATR线自身预测方向。'
              '它用当时Spot价格相对快照锚点的对数距离，除以1分钟ATR14/Spot价格；锚点沿用原冻结快照，不能叫精确Chainlink开盘价。'
              '仍要同时满足低价盘口可买、时间、深度和资金可用要求。', '',
              '先看固定金额是否盈利、末段是否延续，再看倍投。2047U路径偶然盈利、单注升到1024U，不能作为信号有效依据。'
              '本表并未重新扫描更早T001–T063的全部不同成交模型；这些保留在原索引，不能混入同口径排名。', '',
              '[EMA固定对照](大周期EMA过滤验证.md)；[机器清单与去重说明](historical_catalog.json)；'
              '[研究总索引](../../../docs/研究测试总索引.md)。', '']
    (OUT / CATALOG).write_text('\n'.join(lines), encoding='utf-8')


def render(results, manifest, audit, overlays, coverage):
    eligible = [{'rule': rule, 'side': side, 'variant': variant} for rule in SOURCES for side in st.SIDES
                for variant in VARIANTS if qualifies(results, rule, side, variant)]
    lines = ['# 大周期EMA过滤验证（T073）', '',
             '固定9个原策略、7种EMA对照，UP/DOWN独立账户及BOTH共享账户全部完成。'
             '这是同一段已观察历史的离线配对研究，没有修改或启动实盘、纸上及采集。', '',
             '3752个BTC五分钟官方结算市场；北京时间2026-08-21 19:00至10-03 01:50，20个活跃日期，有缺口。'
             '前60%/中20%/后20%划分；验证起点09-02 04:10、末段起点09-04 19:15。不是新样本外证据。', '',
             '## 固定定义', '',
             '15/60分钟EMA20由完整UTC对齐的1分钟OHLC聚合；复用前24小时冻结预热。SMA20种子、α=2/21，缺口重置。'
             '须EMA及上根EMA同时就绪；收盘1秒后可用，至下一原收盘边界失效。只在原入场决策时读取，不看未来。', '',
             'NONE为原规则；X仅检查同样的指标覆盖；E要求已收盘价在EMA上只做UP、下只做DOWN；'
             'ES再要求EMA同向上升/下降，相等或冲突跳过。拒绝原候选后不补找本场稍晚的入场。'
             '注意：E与ES在这种同根收盘价定义下数学等价，不能当作两个独立有效因子；下文保留冻结对照供核验。', '',
             '剩余270–30秒、决策卖一0.30–0.50、价格上限0.50；1U含费预算、0.01份精度，决策/执行盘口均≤2秒。'
             '费用冻结每份0.07×p×(1−p)，并非现行费率声明；另一组要求逐份含费成本≤0.50。订单0/300/1000ms，主表300ms。'
             '持有官方结算，等待市场结束+60秒且官方标签实际到达后才能下一单。T068原ATR使用0公布延迟假设，T072原信号1秒；EMA均1秒。', '',
             f'复核原NONE：{audit["baseline"]["matchedGroups"]}组×{audit["baseline"]["fieldsPerGroup"]}字段一致；'
             f'合计{len(results)}资金统计组，不是独立样本数。达到验证/末段各100笔且300/1000ms均正收益的组：{len(eligible)}。', '',
             '## 共享账户主表：固定1U', '']
    for rule in SOURCES:
        lines += [f'### {rule} {SOURCES[rule][1]}', ''] + table(select(results, rule)) + ['']
    lines += ['## 单方向、分段和压力明细', '']
    for rule in SOURCES:
        lines.append(f'- [{rule} {SOURCES[rule][1]}]({rule}_EMA对照.md)')
        report = [f'# {rule}：{SOURCES[rule][1]}＋EMA', '', '[返回总报告](大周期EMA过滤验证.md)', '',
                  '主表固定1U含费预算、订单300ms、名义价≤0.50；末段独立重置账户。'
                  'X是覆盖对照，E/ES方向过滤必须与相同周期X比较。胜率95%区间、日块ROI区间、费用、集中度见JSON。', '']
        for side in st.SIDES:
            for split in ('all','validation','test'):
                report += [f'## {side} / {split}', ''] + table(select(results, rule, side, split=split)) + ['']
        report += ['## 末段订单1秒压力', '']
        for side in st.SIDES:
            report += table(select(results, rule, side, split='test', delay=1000)) + ['']
        report += ['## 逐份含费成本≤0.50', '']
        for side in st.SIDES:
            for split in ('all','test'):
                report += table(select(results, rule, side, split=split, odds=st.ODDS[1])) + ['']
        report += ['## EMA候选覆盖（订单300ms、名义价≤0.50）', '',
                   '|方向|EMA|原候选|保留|不可用/过期|预热不足|方向拒绝|', '|---|---|---|---|---|---|---|']
        for c in coverage:
            if c['rule'] == rule and c['delayMs'] == 300 and c['odds'] == st.ODDS[0]:
                q = c['reasons']
                report.append(f'|{c["side"]}|{c["variant"]}|{c["originalCandidates"]}|{c["acceptedCandidates"]}|'
                              f'{q.get("unavailableOrExpired",0)}|{q.get("warmup",0)}|{q.get("directionRejected",0)}|')
        report += ['', '## 原共享固定账本的同笔过滤', '',
                   '只对原已成交账本删单，不补买；避免把重新排队产生的新交易当作因果增益。下表与重新回放收益可不同。', '',
                   '|EMA|原账本保留笔数/收益U|原账本拒绝笔数/收益U|', '|---|---|---|']
        for c in overlays:
            if c['rule'] == rule and c['side'] == 'BOTH' and c['split'] == 'all' and c['odds'] == st.ODDS[0]:
                report.append(f'|{c["variant"]}|{c["kept"]["n"]}/{c["kept"]["netPnl"]:+.3f}|'
                              f'{c["rejected"]["n"]}/{c["rejected"]["netPnl"]:+.3f}|')
        report += ['', '## 11级有限资金主口径', '',
                   '|本金|方向|EMA|笔数|净收益U|最长连亏|回撤U|最大预算U|11级打穿|本金停止|深度拒绝|',
                   '|---|---|---|---|---|---|---|---|---|---|---|']
        for sizing in ('martin11_100','martin11_2047'):
            for side in st.SIDES:
                for r in select(results, rule, side, sizing=sizing):
                    q = r['rejections']; depth = q.get('insufficient_decision_depth',0)+q.get('insufficient_full_stake_depth',0)
                    report.append(f'|{r["startingBankroll"]:.0f}|{side}|{r["variant"]}|{r["n"]}|{r["netPnl"]:+.3f}|'
                                  f'{r["maxConsecutiveLosses"]}|{r["maxDrawdown"]:.3f}|{r["maxBudget"]:.0f}|'
                                  f'{r["capLossCycles"]}|{q.get("bankroll_stop",0)}|{depth}|')
        report += ['', '单边止于11次失败后归1U；本金不足停止。加大预算重新走逐档深度，会跳过原固定金额能买的单，'
                   '所以固定金额连亏少不代表倍投账户也相同；未完成周期亏损已计入。', '',
                   '[完整结果](results.json)；[资金账本](sizing_ledgers.json.gz)；[同笔对照](paired_overlay.json)；'
                   '[数据审计](数据质量与审计.md)。', '']
        (OUT / f'{rule}_EMA对照.md').write_text('\n'.join(report), encoding='utf-8')
    lines += ['', '## 评价与限制', '',
              '不凭全段最高收益选择EMA。至少同时看原NONE、同覆盖X、E/ES的全段/验证/末段，'
              '以及订单1秒压力。少于30笔是小样本；分段不足100笔不能升级为实盘硬条件。'
              '上升期跳过DOWN可能减少失败，也会删掉逆势成功单；实际结果见各组，不能预设大周期过滤一定有利。', '',
              '[结果解读与候选比较](结果解读与候选比较.md)；[历史低连亏清单](历史低连亏正收益候选汇总.md)；[审计](数据质量与审计.md)；'
              '[完整结果](results.json)；[同笔过滤](paired_overlay.json)；[覆盖](indicator_coverage.json)；'
              '[冻结清单](manifest.json)；[冻结计划](../../../docs/'+PLAN.name+')。', '',
              '复现（项目根目录）：', '', '```sh',
              'python3 arbitrage-research/scripts/analysis/analyze_ema_filters.py', '```', '']
    (OUT / REPORT).write_text('\n'.join(lines), encoding='utf-8')
    base.dump(OUT / 'qualified.json', eligible)
    (OUT / '数据质量与审计.md').write_text(
        '# T073数据质量与审计\n\n'
        f'输入哈希见[manifest.json](manifest.json)，原数据库只读且source_files与T065冻结列表一致。'
        f'主输入3752市场；预热合并重叠{manifest["overlapMinutes"]}分钟且OHLC逐字段一致。\n\n'
        f'原候选{audit["sourceCandidates"]}条，EMA使用{audit["decisionTimes"]}个不同事前时间；'
        f'时序违规{audit["timingViolations"]}，原NONE核对{audit["baseline"]["matchedGroups"]}组、差异0。\n\n'
        '完整UTC聚合、EMA递推、缺口重置、收盘晚到与原边界失效均有单元测试。'
        '指标逐行保存[ema_indicators.json.gz](ema_indicators.json.gz)；'
        '原候选仅存[源索引](source_references.json.gz)，盘口引用原冻结文件，避免7倍复制；'
        '[逐决策指标](ema_decisions.json.gz)不含未来结果标签。\n\n'
        '配对拒绝不补找本场新机会。重新回放时资金等待变化可能接纳别的已存在候选，'
        '所以附同一原实际固定账本保留/拒绝的独立对照。BOTH不是UP与DOWN账户相加。\n\n'
        '回撤为已结算权益回撤，非逐秒浮动亏损。盈亏统计已计费用；未完成倍投周期已计入。'
        '原T068保留0公布延迟旧假设，不将其伪装成新严格指标重算；其锚点不是官方精确开盘。'
        '1U研究预算不能证明交易所最低金额/最小份额可下。\n\n'
        '[返回总报告](大周期EMA过滤验证.md)；[机器审计](audit.json)。\n', encoding='utf-8')
    return eligible


def render_interpretation(results):
    rows = [select(results,rule,side,variant)[0] for rule,side,variant in (
        ('A6','DOWN','NONE'),('A6','DOWN','15m-E'),('A6','DOWN','60m-E'),
        ('F_5m_k3','BOTH','NONE'),('F_5m_k3','BOTH','15m-E'),('F_5m_k3','BOTH','60m-E'),
        ('C_15m_k3','BOTH','NONE'),('F_1m_k3','BOTH','NONE'))]
    lines = ['# T073结果解读与候选比较', '',
             'T068里确实有值得继续研究的候选，尤其A6 DOWN；但本轮不支持把大周期EMA统一加到所有策略。'
             '最明确的新观察是5分钟Supertrend翻转＋1小时EMA20方向过滤，属于19笔的小样本候选。', '',
             '以下均固定1U含费预算、订单300ms、名义价≤0.50、原低价入场口径；净收益已经扣冻结费用。', ''] + table(rows)
    lines += ['', '## T068该看哪一项', '',
              'A6 DOWN：29胜/39笔（74.4%），净+20.405U、投入ROI52.4%、最大连亏3、回撤2.991U；'
              '验证10笔+5.864U，末段6笔+0.563U。UP为13胜/25笔、+5.851U、最大连亏5，末段6笔−3.532U。'
              '因此这一条历史上DOWN比UP更值得跟踪，不能把它扩展成“所有策略只做DOWN”。', '',
              '弱点同样明确：A6 DOWN的末段只赢3/6，订单1秒时末段变为−0.198U；'
              '若要求含费买入成本≤0.50，整段只剩13笔+1.944U，末段4笔−1.338U。'
              '减少单份成本改变了可入场集合，并未保证更赚钱。', '',
              'A6 DOWN加15分钟EMA只剩17笔+8.526U、末段4笔−1.338U；'
              '加1小时EMA只剩19笔+0.333U、末段4笔−2.060U。两者都不支持升级，保留原候选更合理。', '',
              '## 新候选：5分钟Supertrend翻转＋1小时EMA20', '',
              '入场不是上一根5分钟K线红就买UP。信号为已收盘5分钟ATR14×3 Supertrend出现真实翻转，'
              '翻转信号在该5分钟周期内有效；再要求最新完整1小时收盘价在EMA20上方只买UP、下方只买DOWN。'
              '两种K线均收盘1秒后可用，缺口/预热不足跳过。还要符合原低价、剩余时间、深度及资金可用条件。', '',
              '共享固定账户由28胜/60笔、净+4.041U、最长8连亏，变为12胜/19笔、净+9.975U、最长2连亏；'
              'ROI6.7%→52.6%，回撤9.405U→2.060U。原实际账本保留19笔+9.975U，'
              '拒绝41笔−5.934U；这里改善确实对应删掉原账本中的负收益子集，没有来自重新排队补入的交易。', '',
              '不过验证只有5笔+3.109U，末段4笔+0.154U；订单1秒末段2笔+0.135U。'
              '末段优势很薄，1秒口径少笔不能与300ms逐笔等同。去掉最佳日期后仍+6.871U，'
              '日块ROI95%区间约−1.7%至109.6%，仍跨0，不能称为已验证。', '',
              '单独UP仅7笔+5.069U、最长1连亏，DOWN12笔+4.906U、最长2连亏；'
              'UP末段3笔+1.154U，DOWN末段1笔−1.000U。样本不足以据此认定只做UP最好，后续同时记录两方向。', '',
              '15分钟EMA方案保留29笔+6.815U、最长4连亏；验证8笔+2.252U、末段4笔+0.642U、'
              '订单1秒末段4笔+0.594U。与1小时方案并列观察，不能只按全段收益选择一个。', '',
              '## 其他对照说明什么', '',
              '5/15分钟Supertrend持续方向加EMA，多数固定金额仍亏；例如15分钟持续DOWN＋15分钟EMA虽然'
              '全段+4.428U，末段91笔−24.203U、最长12连亏，不能当成救活持续下注策略。', '',
              '1分钟×3翻转本来30胜/49笔、+15.459U；1小时EMA后14胜/24笔、+5.642U、最长2连亏，'
              '但订单1秒末段仍−0.417U。15分钟×3方向＋1分钟翻转原24笔+7.834U、最长2连亏，'
              '加EMA收益下降；没有理由为了多一个指标而强行加入。', '',
              '部分历史100%组都已保留在[候选清单](历史低连亏正收益候选汇总.md)。'
              '例如T070 S3_k2 DOWN为24/24全胜、+22.349U，但末段0笔；S3_n10 DOWN为21/21全胜，'
              '末段仅1笔。不是删掉好结果，而是把缺乏后期覆盖和原0公布延迟假设一起展示。', '',
              '## E与ES为什么相同', '',
              '`EMA_t = EMA_(t−1) + α×(C_t−EMA_(t−1))`。因此'
              '`EMA_t−EMA_(t−1)=α×(C_t−EMA_(t−1))`，且'
              '`C_t−EMA_t=(1−α)×(C_t−EMA_(t−1))`。0<α<1时二者符号一致。'
              '所以“同一根收盘价在当前EMA上”已意味着EMA相较上一根上升；ES没有新增信息。'
              '本轮全部E/ES入场决定与结果一致，7对照并非7个独立因子；若以后要测试独立趋势强度，应另行冻结多根斜率或幅度。', '',
              '15/60分钟X覆盖对照与NONE在本轮候选上相同，说明EMA方向效果来自方向取舍，'
              '不是指标缺失恰好删掉失败单。缺口仍会重置，完整指标与逐决策时间已归档。', '',
              '## 有限倍投放在哪一步', '',
              'A6 DOWN固定+20.405U、11级路径+35.868U，最大预算8U、回撤6.992U；'
              '5分钟翻转＋1小时EMA固定+9.975U、11级路径+22.305U，最大预算4U、回撤2.999U。'
              '100U/2047U这两条历史路径相同，因为尚未碰到大注额；不证明第8–11级可承受。'
              '先验证固定金额信号，不将历史未出现的长连败当成不会发生。', '',
              '没有任何组达到预先规定的验证/末段各≥100笔、300/1000ms固定金额都正收益门槛。'
              '本轮只形成观察候选，全部仍属2026-08-21至10-03已看过的历史，不改实盘或纸上。', '',
              '[主报告与完整9策略对照](大周期EMA过滤验证.md)；[历史候选](历史低连亏正收益候选汇总.md)；'
              '[数据审计](数据质量与审计.md)；[研究索引](../../../docs/研究测试总索引.md)。', '']
    (OUT / '结果解读与候选比较.md').write_text('\n'.join(lines),encoding='utf-8')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    parent = base.load(base.OUT / 'manifest.json')
    con = sqlite3.connect(f'file:{base.ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro', uri=True)
    if [list(r) for r in con.execute('SELECT path,size,mtime_ns FROM source_files ORDER BY path')] != parent['sourceFiles']:
        raise RuntimeError('frozen database source cache changed')
    labels = base.load(base.OUT / 'official_label_arrivals.json')['markets']
    cohort = base.load(base.OUT / 'market_coverage.json')
    for c in cohort:
        mid, start, end, winner = con.execute('SELECT market_id,start_us,end_us,winner FROM markets WHERE market_id=?', (c['marketId'],)).fetchone()
        if winner != labels[mid]['winner'] or end-start != 5*base.MINUTE:
            raise RuntimeError('official market mismatch')
    con.close()
    if len(cohort) != 3752: raise RuntimeError('cohort count changed')
    catalog = historical_catalog()
    base.dump(OUT / 'historical_catalog.json', catalog)
    render_catalog(catalog)
    sources, refs = load_sources()
    cohort_ids = {c['marketId'] for c in cohort}
    for rule, entries in sources.items():
        keys = [(e['marketId'],e['side'],e['delayMs'],e['odds']) for e in entries]
        if len(keys) != len(set(keys)): raise RuntimeError('duplicate original candidate')
        for e in entries:
            if (e['marketId'] not in cohort_ids or e['won'] != (e['side'] == labels[e['marketId']]['winner'])
                    or e['decisionUs'] > e['executionUs'] or e['bookReceivedUs'] > e['executionUs']
                    or not 0 <= e['executionUs']-e['bookSourceUs'] <= 2*base.SECOND
                    or e['fixedFill']['cost'] > 1+1e-9 or e['fixedFill']['worst'] > e['priceCap']+1e-9):
                raise RuntimeError('original candidate label/timing/budget violation')
    bars, overlap = merge_bars(base.load(WARMUP)['bars'], base.load(base.OUT / 'binance_official_1m.json.gz')['bars'])
    pack, aggregation = {}, {}
    for tf in (15, 60):
        data, aggregation[tf] = aggregate_bars(bars, tf)
        pack[tf] = build_ema(data, tf)
    times = sorted({e['decisionUs'] for es in sources.values() for e in es})
    decisions = {at: {tf: row_at(pack[tf], at) for tf in pack} for at in times}
    violations = sum(not(r['availableUs'] <= at < r['expiresUs'] and r['barEndUs'] < at)
                     for at, fs in decisions.items() for r in fs.values() if r)
    if violations: raise RuntimeError('future/stale EMA')
    es_mismatches = sum(r['E'] != r['ES'] for series in pack.values() for r in series[1] if r['ready'])
    if es_mismatches: raise RuntimeError('EMA price/slope identity failed')
    results, ledgers, overlays, coverage = analyze(sources, decisions)
    audit = {'sourceCandidates': len(refs), 'decisionTimes': len(times), 'timingViolations': violations,
             'baseline': baseline_audit(results), 'resultGroups': len(results),
             'sameBarEAndESDecisionMismatches':es_mismatches}
    if len(results) != 13608: raise RuntimeError('missing matrix groups')
    if any(r['negativeWinningCycles'] for r in results if r['odds'] == st.ODDS[1]):
        raise RuntimeError('net odds invariant failed')
    deps = [PLAN, Path(__file__), Path(__file__).parents[2] / 'tests/test_ema_filters.py',
            Path(base.__file__), Path(atr.__file__), Path(st.__file__),
            Path(st.__file__).with_name('analyze_supertrend_robustness.py'),
            Path(regime.__file__),Path(rsi.__file__),Path(st.old.__file__),WARMUP]
    deps += [base.OUT / x for x in ('manifest.json','market_coverage.json','official_label_arrivals.json','binance_official_1m.json.gz')]
    for tid in range(64,73):
        d = next((base.ROOT/'outputs/research_tests').glob(f't{tid:03d}_*'))
        deps.append(d / 'results.json')
    deps += [atr.OUT / 'entries.json.gz', st.OUT / 'entries.json.gz']
    manifest = {'version':VERSION, 'runDate':'2026-11-20', 'newOutOfSampleData':False,
                'inputs':{str(p.relative_to(base.ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in deps},
                'sourceFiles':parent['sourceFiles'], 'settledMarkets':len(cohort), 'sources':SOURCES,
                'barCountWithWarmup':len(bars), 'overlapMinutes':overlap, 'aggregation':aggregation,
                'period':20,'publicationLagMs':1000,'orderDelaysMs':st.DELAY,'variants':VARIANTS,
                'filterSemantics':'paired original decision; no later-entry replacement'}
    for filename, data in (('results.json',results), ('sizing_ledgers.json.gz',ledgers),
                           ('paired_overlay.json',overlays), ('indicator_coverage.json',coverage),
                           ('source_references.json.gz',refs), ('ema_indicators.json.gz',pack),
                           ('ema_decisions.json.gz',[{'decisionUs':at,'features':fs} for at,fs in decisions.items()]),
                           ('audit.json',audit), ('manifest.json',manifest)):
        base.dump(OUT / filename, data)
    qualified = render(results, manifest, audit, overlays, coverage)
    render_interpretation(results)
    print({'scenarios':len(results),'qualified':qualified,'audit':audit,'out':str(OUT)},flush=True)


if __name__ == '__main__':
    main()

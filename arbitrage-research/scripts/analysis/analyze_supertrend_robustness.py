#!/usr/bin/env python3
"""T071: frozen S1/S3 timing/concentration controls and actual martingale trace."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import sqlite3
from pathlib import Path

import analyze_martingale_candidates as base
import analyze_supertrend_direction as st

OUT = base.ROOT / 'outputs/research_tests/t071_supertrend_robustness_20261120'
PLAN = base.ROOT / 'docs/Supertrend候选严格复核计划_20261120.md'
VERSION = 't071-v1-availability-lag-original-expiry'
RULES = {'S1': '翻转触发', 'S3': '趋势加距离时间',
         'S13': '翻转与距离同时成立', 'D1': '仅距离时间，不要求趋势'}
LAGS = (0, 500, 1000, 2000)
SIDES = ('UP', 'DOWN', 'BOTH')
SPLITS = ('all', 'train', 'validation', 'test')
ODDS = ('gross_le_050', 'net_cost_le_050')
REPORT = 'Supertrend候选严格复核.md'


def delayed_indicators(indicators, lag_ms):
    return [at + lag_ms * 1000 for at in indicators[0]], indicators[1]


def available_indicator(indicators, at):
    row = base.indicator_at(indicators, at)
    # Expiration is tied to the ORIGINAL minute boundary, not delivery time.
    return row if row is not None and 0 <= at - row['barEndUs'] < base.MINUTE else None


def rule_signals(row, xc, anchor, remaining):
    result = dict.fromkeys(RULES)
    if row is None:
        return result
    trend = row['supertrends']['primary']
    result['S1'] = trend['flip']
    if xc is None or anchor is None or anchor <= 0:
        return result
    direction = 'UP' if xc[2] > anchor else 'DOWN' if xc[2] < anchor else None
    score = st.distance_time_score(xc[2], anchor, trend['atr'], remaining, direction) if direction else None
    if score is not None and score >= 1:
        result['D1'] = direction
        result['S3'] = direction if trend['direction'] == direction else None
        result['S13'] = direction if trend['flip'] == direction else None
    return result


def make_callback(indicators, opens):
    delayed = {lag: delayed_indicators(indicators, lag) for lag in LAGS}

    def callback(at, ind, xc, remaining, **kwargs):
        anchor = opens.get(at // (5 * base.MINUTE) * 5 * base.MINUTE)
        return {f'{rule}_L{lag}': side for lag in LAGS
                for rule, side in rule_signals(available_indicator(delayed[lag], at), xc, anchor, remaining).items()}
    return callback


def add_features(entries, indicators, opens, crosses):
    delayed = {lag: delayed_indicators(indicators, lag) for lag in LAGS}
    times = {mid: [r[0] for r in rows] for mid, rows in crosses.items()}
    for e in entries:
        rule, rawlag = e['strategy'].split('_L')
        lag = int(rawlag)
        row = available_indicator(delayed[lag], e['decisionUs'])
        if row is None:
            raise RuntimeError('unavailable indicator selected')
        trend = row['supertrends']['primary']
        xc = base.cross_at(crosses.get(e['marketId'], []), times.get(e['marketId'], []), e['decisionUs'])
        anchor = opens.get(e['startUs'])
        e.update(rule=rule, publicationLagMs=lag, indicatorBarEndUs=row['barEndUs'],
                 indicatorAvailableUs=row['barEndUs'] + lag * 1000, atr=trend['atr'],
                 trendDirection=trend['direction'], flip=trend['flip'],
                 anchorOpenUs=e['startUs'] if anchor else None, binanceAnchorOpen=anchor,
                 spotReceivedUs=xc[0] if xc else None, spotSourceUs=xc[1] if xc else None,
                 spotPrice=xc[2] if xc else None,
                 distanceTimeScore=st.distance_time_score(xc[2], anchor, trend['atr'], e['remaining'], e['side'])
                 if xc and anchor else None)


def audit_entries(entries, callback, indicators, crosses):
    errors = Counter()
    seen = set()
    times = {mid: [r[0] for r in rows] for mid, rows in crosses.items()}
    for e in entries:
        key = (e['strategy'], e['marketId'], e['side'], e['delayMs'], e['odds'])
        errors['duplicate'] += int(key in seen)
        seen.add(key)
        errors['indicatorTiming'] += int(not (e['indicatorBarEndUs'] <= e['indicatorAvailableUs'] <= e['decisionUs']
            < e['indicatorBarEndUs'] + base.MINUTE))
        errors['executionTiming'] += int(e['executionUs'] != e['decisionUs'] + e['delayMs'] * 1000)
        for field in ('bookSourceUs', 'bookReceivedUs'):
            errors['bookAge'] += int(not 0 <= e['executionUs'] - e[field] <= 2 * base.SECOND)
        errors['budget'] += int(not 0 < e['fixedFill']['cost'] <= 1 + 1e-9)
        errors['cap'] += int(e['fixedFill']['worst'] > e['priceCap'] + 1e-9)
        errors['netCap'] += int(e['odds'] == 'net_cost_le_050' and e['fixedFill']['effectivePrice'] > .5 + 1e-9)
        errors['distance'] += int(e['rule'] != 'S1' and not (e['distanceTimeScore'] is not None and e['distanceTimeScore'] >= 1))
        xc = base.cross_at(crosses.get(e['marketId'], []), times.get(e['marketId'], []), e['decisionUs'])
        errors['signal'] += int(callback(e['decisionUs'], None, xc, e['remaining']).get(e['strategy']) != e['side'])
    if any(errors.values()):
        raise RuntimeError(dict(errors))
    return {'violations': dict(errors), 'uniqueEntries': len(seen)}


def baseline_check(entries):
    old = base.load(st.OUT / 'entries.json.gz')
    fields = ('marketId', 'side', 'delayMs', 'odds', 'decisionUs', 'executionUs',
              'levels', 'decisionLevels', 'fixedFill', 'priceCap', 'won', 'labelAvailableUs')
    selected = {(r['strategy'], r['marketId'], r['side'], r['delayMs'], r['odds']): r
                for r in old if r['strategy'] in ('S1', 'S3')}
    current = {(r['rule'], r['marketId'], r['side'], r['delayMs'], r['odds']): r
               for r in entries if r['rule'] in ('S1', 'S3') and r['publicationLagMs'] == 0}
    if selected.keys() != current.keys() or any(any(a[f] != current[k][f] for f in fields) for k, a in selected.items()):
        raise RuntimeError('T070 exact baseline replication failed')
    return {'matchedT070Entries': len(selected), 'comparedFields': list(fields), 'violations': 0}


def concentration(summary):
    daily = summary['daily']
    profits = {day: row[0] for day, row in daily.items()}
    if not profits:
        return {'activeDays': 0, 'pnlWithoutBestDay': None, 'worstLeaveOneDayPnl': None}
    best = max(profits, key=profits.get)
    return {'activeDays': len(daily), 'bestDay': best, 'bestDayPnl': profits[best],
            'bestDayTradeCount': daily[best][2], 'pnlWithoutBestDay': summary['netPnl'] - profits[best],
            'worstLeaveOneDayPnl': min(summary['netPnl'] - p for p in profits.values()),
            'positiveDays': sum(p > 0 for p in profits.values())}


def analyze(entries):
    groups = defaultdict(list)
    for e in entries:
        for side in (e['side'], 'BOTH'):
            groups[(e['strategy'], side, e['delayMs'], e['odds'])].append(e)
    results, ledgers = [], {}
    for rule in RULES:
        for lag in LAGS:
            name = f'{rule}_L{lag}'
            for side in SIDES:
                for delay in (0, 300, 1000):
                    for odds in ODDS:
                        rows = groups[(name, side, delay, odds)]
                        for split in SPLITS:
                            chosen = rows if split == 'all' else [e for e in rows if e['split'] == split]
                            for sizing, rounds, capital in (('fixed', 1, 100.), ('martin11_2047', 11, 2047.)):
                                summary, ledger = base.replay(chosen, rounds, capital)
                                summary.update(rule=rule, publicationLagMs=lag, side=side, delayMs=delay,
                                               odds=odds, split=split, sizing=sizing, qualifiedSignals=len(chosen))
                                summary['concentration'] = concentration(summary)
                                if sizing == 'fixed':
                                    summary['dailyBlockRoiCI95'] = base.bootstrap_daily_ci(summary['daily'])
                                results.append(summary)
                                if split == 'all' and delay == 300 and odds == 'gross_le_050':
                                    ledgers[f'{rule}|{lag}|{side}|{sizing}'] = ledger
    return results, ledgers


def consecutive_outcomes(markets):
    longest = {'UP': {'count': 0}, 'DOWN': {'count': 0}}
    last = None
    run = []
    for row in markets:
        if last is None or row['start_us'] != last['end_us'] or row['winner'] != last['winner']:
            run = []
        run.append(row)
        if len(run) > longest[row['winner']]['count']:
            longest[row['winner']] = {'count': len(run), 'from': base.local(run[0]['start_us']),
                'toExclusive': base.local(row['end_us']), 'marketIds': [m['market_id'] for m in run]}
        last = row
    return longest


def loss_cycles(ledger, rounds=11):
    cycles, current = [], []
    for row in ledger:
        if row['round'] == 1:
            current = []
        current.append(row)
        if row['round'] == rounds and not row['won']:
            if len(current) != rounds or any(r['won'] or r['round'] != i + 1 for i, r in enumerate(current)):
                raise RuntimeError('invalid loss cycle')
            cycles.append(list(current))
            current = []
    return cycles


def failure_trace(markets, labels):
    prior = base.load(st.OUT / 'sizing_ledgers.json.gz')
    ledger = prior['S0|BOTH|gross_le_050|all|martin11_2047']
    cycles = loss_cycles(ledger)
    enriched = []
    for cycle in cycles:
        rows, previous_start = [], None
        for row in cycle:
            winner = labels[row['marketId']]['winner']
            if row['won'] != (winner == row['side']):
                raise RuntimeError('failure ledger official outcome mismatch')
            start = int(row['marketId'].rsplit('-', 1)[1]) * base.SECOND
            rows.append({**row, 'officialWinner': winner, 'marketStart': base.local(start),
                         'interveningFiveMinuteSlots': (start - previous_start) // (5 * base.MINUTE) - 1
                         if previous_start is not None else None})
            previous_start = start
        enriched.append({'pnl': sum(r['pnl'] for r in rows), 'trades': rows})
    return {'source': str(st.OUT / 'sizing_ledgers.json.gz'), 'ledgerKey': 'S0|BOTH|gross_le_050|all|martin11_2047',
            'cycles': enriched, 'adjacentOfficialRuns': consecutive_outcomes(markets),
            'lastTrades': ledger[-8:], 'wholePnl': ledger[-1]['cumulativePnl'],
            'endingCash': ledger[-1]['cashAfterSettlement']}


def row_table(rows):
    lines = ['|规则|方向|K线晚到ms|下单延迟ms|区间|胜/笔|胜率|净收益U|投入ROI|最长连亏|',
             '|---|---|---:|---:|---|---:|---:|---:|---:|---:|']
    for r in rows:
        lines.append(f"|{r['rule']}|{r['side']}|{r['publicationLagMs']}|{r['delayMs']}|{r['split']}|"
                     f"{r['wins']}/{r['n']}|{base.pct(r['winRate'])}|{r['netPnl']:+.3f}|"
                     f"{base.pct(r['roi'])}|{r['maxConsecutiveLosses']}|")
    return lines


def render(results, trace, manifest, audit):
    def select(rule=None, lags=(0,), delays=(300,), sides=SIDES, splits=('all',), odds='gross_le_050', sizing='fixed'):
        return [r for r in results if (rule is None or r['rule'] == rule) and r['publicationLagMs'] in lags
                and r['delayMs'] in delays and r['side'] in sides and r['split'] in splits
                and r['odds'] == odds and r['sizing'] == sizing]
    common = [
        '本轮复用同一3752个官方结算市场，北京时间2026-08-21 19:00至2026-10-03 01:50，20个有采集日期，中间有缺口；不是11月新增行情验证。',
        '主参数ATR(14)×3。S1=真实翻转后60秒；S3=趋势同向且距离时间分数≥1；S13=二者同时成立；D1=仅距离时间，不要求趋势。',
        '剩余270–30秒、决策卖一0.30–0.50、1U含费预算，逐档深度/0.01份精度、盘口源/接收均≤2秒、首个可发订单仅尝试一次。持有官方结算并等待结果后才可再买。',
        '研究费用每份0.07×p×(1-p)，不是当前费率承诺。名义价上限0.50并不等于含费成本≤0.50，后者另外对照。1U也不证明满足实盘最小金额。',
        'K线可用时点=真实收盘+模拟晚到；仍于原收盘后60秒过期。每秒决策会使500/1000ms量化到相同网格，不能证明毫秒差异。历史实际推送延迟未知。',
        '60/20/20沿用历史时间切分：validation从2026-09-02 04:10，test从2026-09-04 19:15开始。历史留后已多次被查看，不是全新样本外。',
        '固定1U是信号主要评价；翻倍资金账本只是压力对照。胜率不等于收益，多个情景不是独立样本。'
    ]
    s1late = select('S1', delays=(1000,), sides=('BOTH',), splits=('test',))[0]
    distance = select('D1', sides=('BOTH',))[0]
    distance_test = select('D1', sides=('BOTH',), splits=('test',))[0]
    combo = select('S13', sides=('BOTH',))[0]
    combo_ci = combo['winRateCI95']
    lines = ['# T071：Supertrend候选严格复核', '', '## 结论与使用边界', '',
             '四项均完成UP/DOWN独立及双向共享账户对照，暂没有可以升级为成熟实盘策略的一项。', '',
             f"- S1翻转仍有累计正收益，但1秒下单延迟的末段仅{s1late['wins']}/{s1late['n']}胜、净{s1late['netPnl']:+.3f}U，执行敏感性没有消除。",
             '- S3趋势加距离时间的高累计胜率仍值得观察，但DOWN末段仅2笔、共享末段仅3笔，不能证明跨阶段稳定。',
             f"- D1去掉趋势要求后累计净{distance['netPnl']:+.3f}U，反而比S3高；末段{distance_test['wins']}/{distance_test['n']}胜、净{distance_test['netPnl']:+.3f}U。距离信号值得继续研究，但当前不能宣称独立趋势过滤提升总收益。",
             f"- S13交集{combo['wins']}/{combo['n']}全胜，验证段0笔、末段仅1笔。其胜率Wilson 95%区间约{base.pct(combo_ci[0])}–{base.pct(combo_ci[1])}，100%只是本段样本结果。",
             '- 模拟K线晚到0.5–2秒影响较小，不能据此认为实际链路不重要：每秒决策网格、盘口缓存和1秒订单延迟会改变交易集合。',
             '- 四规则独立寻找首个合格入场，交易集合不同；收益差不能直接当作同一笔订单上新增因子的因果增益。', '',
             '## 同一口径的四个对照', ''] + row_table(select())
    lines += ['', '## K线与订单同时延迟', '',
              '以下固定1U、名义价上限0.50、共享账户；0毫秒订单仅为理想参考，完整方向和含费上限结果见results.json。', '']
    lines += row_table(select(lags=LAGS, delays=(300, 1000), sides=('BOTH',)))
    lines += ['', '## 历史分段', ''] + row_table(select(splits=('validation', 'test')))
    lines += ['', '## 最后一段的联合延迟压力', '',
              'K线模拟晚到2秒，再叠加订单延迟1秒；其余条件不变。', '']
    lines += row_table(select(lags=(2000,), delays=(1000,), splits=('test',)))
    lines += ['', '## 日期集中度', '',
              '以下从既有固定金额账本删除最佳日期，未重新回放资金或挑更优日期。不能据此宣称新的可交易策略。', '',
              '|规则|方向|有交易日期|最佳日期|该日收益U|去掉该日净收益U|日区块ROI 95%区间|',
              '|---|---|---:|---|---:|---:|---|']
    for r in select():
        c = r['concentration']
        ci = r['dailyBlockRoiCI95']
        ci_text = '—' if ci is None else f'{base.pct(ci[0])} 至 {base.pct(ci[1])}'
        lines.append(f"|{r['rule']}|{r['side']}|{c['activeDays']}|{c.get('bestDay','—')}|"
                     f"{base.fmt(c.get('bestDayPnl'))}|{base.fmt(c['pnlWithoutBestDay'])}|{ci_text}|")
    qualifying = [r for r in select(splits=('validation', 'test')) if r['n'] >= 100 and r['netPnl'] > 0]
    lines += ['', f'验证/末段单组达到100笔且固定收益为正的分组数：{len(qualifying)}。'
              '这只是继续研究门槛，不能把100%小样本当稳定胜率。T070 ATR参数敏感性风险仍有效。', '',
              '## 规则、数据与复跑', ''] + ['- ' + s for s in common]
    lines += ['', '```sh', 'python3 arbitrage-research/scripts/analysis/analyze_supertrend_robustness.py',
              '# 仅重算资金/报告，先校验冻结输入哈希',
              'python3 arbitrage-research/scripts/analysis/analyze_supertrend_robustness.py --reuse-entries', '```', '',
              '## 归档', '', '[冻结计划](../../../docs/Supertrend候选严格复核计划_20261120.md)；'
              '[11笔连败逐笔说明](11级打穿逐笔说明.md)；[审计](数据质量与因果审计.md)；'
              '[完整统计](results.json)；[逐笔](entries.json.gz)；[资金账本](sizing_ledgers.json.gz)；[清单](manifest.json)。', '']
    for rule, label in RULES.items():
        lines.append(f'- [{rule} {label}]({rule}_独立验证.md)')
        report = [f'# T071 {rule}：{label}', '', *common, '', '## 固定金额全段与分段', '']
        report += row_table(select(rule, splits=SPLITS))
        report += ['', '## 延迟压力', ''] + row_table(select(rule, lags=LAGS, delays=(300, 1000)))
        report += ['', '## 含费每份成本不超过0.50', ''] + row_table(select(rule, odds='net_cost_le_050', splits=('all','test')))
        report += ['', '## 11级/2047U资金压力（不作为信号优势证据）', '',
                   '|方向|笔数|净收益U|最大回撤U|最大注额U|11级失败轮|资金不足停止|', '|---|---:|---:|---:|---:|---:|---:|']
        for r in select(rule, sizing='martin11_2047'):
            report.append(f"|{r['side']}|{r['n']}|{r['netPnl']:+.3f}|{r['maxDrawdown']:.3f}|"
                          f"{r['maxBudget']:.0f}|{r['capLossCycles']}|{r['rejections'].get('bankroll_stop',0)}|")
        report += ['', f'[返回总报告]({REPORT})。全部预设条件均保留，不能从延迟/方向/价格上限中事后挑最优。', '']
        (OUT / f'{rule}_独立验证.md').write_text('\n'.join(report))
    (OUT / REPORT).write_text('\n'.join(lines) + '\n')
    failure = ['# T071：11级打穿逐笔说明', '',
        '打穿的是T070 S0持续方向、UP/DOWN共享账户、名义价≤0.50、300ms下单延迟、2047U初始本金的11级翻倍压力对照。不是S1或S3候选，也不是原先0.80–0.85尾盘成熟策略。', '',
        '**11笔策略连亏不等于连续11个五分钟市场下跌。** 策略会换方向，也会因信号、价格、盘口、等待结算而跳过市场。买UP遇DOWN输；买DOWN遇UP也输。', '']
    for i, cycle in enumerate(trace['cycles'], 1):
        failure += [f'## 第{i}轮完整11笔失败', '',
                    '|级数|北京时间入场|买入|官方赢家|预算U|实际含费支出U|本笔收益U|累计本轮收益U|中间跳过时槽|',
                    '|---:|---|---|---|---:|---:|---:|---:|---:|']
        pnl = 0.
        for r in cycle['trades']:
            pnl += r['pnl']
            slots = r['interveningFiveMinuteSlots']
            failure.append(f"|{r['round']}|{r['entryTime']}|{r['side']}|{r['officialWinner']}|{r['budget']:.0f}|"
                           f"{r['cost']:.6f}|{r['pnl']:+.6f}|{pnl:+.6f}|{slots if slots is not None else '—'}|")
        failure += ['', f"本轮损失{cycle['pnl']:.6f}U，名义预算1+2+4+…+1024=2047U。实际支出略少是0.01份取整造成。"
                    '中间时槽只代表不相邻的五分钟位置，不能据此认定每一场都有完整可交易数据。']
    failure += ['', f"整段最终净收益{trace['wholePnl']:+.6f}U、余款{trace['endingCash']:.6f}U，"
                '与单轮损失不同：此前有盈利；打穿后重置1U继续，后来资金不足才停止。', '',
                '## 最后的资金停止路径', '', '|入场时间|方向|级数|预算U|输赢|结算后现金U|', '|---|---|---:|---:|---|---:|']
    for r in trace['lastTrades']:
        failure.append(f"|{r['entryTime']}|{r['side']}|{r['round']}|{r['budget']:.0f}|{'赢' if r['won'] else '输'}|{r['cashAfterSettlement']:.6f}|")
    failure += ['', '资金不足的具体检查发生在下一笔候选下单前。最后是第8级128U亏损，下一笔需256U，而现金只剩约46.11U。', '',
                '## 真正连续相邻市场的官方结果', '']
    for side, run in trace['adjacentOfficialRuns'].items():
        failure.append(f"- {side}最长{run['count']}场：{run['from']}至{run['toExclusive']}（末端为最后一场结束时间）。")
    failure += ['', '只检查冻结3752市场，缺一场或不相邻就重置，因此不能断言全历史从未出现11连跌。'
                '本金与执行深度还会影响能否进入下一档；简单翻倍不能消除连续判断错误。', '',
                f'[逐笔JSON](failure_trace.json)；[返回总报告]({REPORT})。', '']
    (OUT / '11级打穿逐笔说明.md').write_text('\n'.join(failure))
    quality = ['# T071：数据质量与因果审计', '',
               f"候选情景{audit['uniqueEntries']}条，资金统计{len(results)}组。T070严格复现{audit['baseline']['matchedT070Entries']}条、逐字段差异0。", '',
               '输入全部SHA256、脚本哈希、计划哈希见manifest.json；SQLite只读且源清单逐项与T065一致。'
               '官方标签逐场核对；当前市场标签不进入信号，仅事后计分与已结算后的预算推进。', '',
               f"时间、信号复现、预算、价格和重复审计：`{audit['violations']}`。", '',
               'K线模拟延迟与下单延迟分开记录，保留原始收盘时间，原定到期后不沿用旧值。'
               '现货与盘口按已收到的数据回放，缺失不插值。D1同样要求新鲜Spot和精确本场Binance open。', '',
               '每秒网格和历史缓存无法重建真实毫秒级链路、排队和撤单；本轮不是实盘成交保证。'
               '日期删除只是描述性固定账本检查；多次使用历史切分存在选择偏差。', '',
               f'[返回总报告]({REPORT})。', '']
    (OUT / '数据质量与因果审计.md').write_text('\n'.join(quality))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-entries', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    parent = base.load(base.OUT / 'manifest.json')
    inputs = {str(p.relative_to(base.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in [PLAN, Path(__file__), base.OUT / 'manifest.json', base.OUT / 'market_coverage.json',
                        base.OUT / 'binance_official_1m.json.gz', base.OUT / 'spot_projection.json.gz',
                        base.OUT / 'official_label_arrivals.json', st.OUT / 'entries.json.gz',
                        st.OUT / 'sizing_ledgers.json.gz', Path(st.__file__), Path(base.__file__)]}
    con = sqlite3.connect(f'file:{base.ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    if [list(r) for r in con.execute('SELECT path,size,mtime_ns FROM source_files ORDER BY path')] != parent['sourceFiles']:
        raise RuntimeError('frozen source cache changed')
    labels = base.load(base.OUT / 'official_label_arrivals.json')['markets']
    cohort = base.load(base.OUT / 'market_coverage.json')
    markets = []
    for c in cohort:
        row = dict(con.execute('SELECT market_id,start_us,end_us,winner FROM markets WHERE market_id=?', (c['marketId'],)).fetchone())
        if row['winner'] != labels[row['market_id']]['winner']:
            raise RuntimeError('official label mismatch')
        row['label_available_us'] = c['labelAvailableUs']
        markets.append(row)
    if len(markets) != 3752 or any(b['start_us'] <= a['start_us'] for a, b in zip(markets, markets[1:])):
        raise RuntimeError('cohort mismatch')
    bars = base.load(base.OUT / 'binance_official_1m.json.gz')['bars']
    indicators = st.build_indicators(bars)
    opens = {b['open_us']: b['open'] for b in bars}
    crosses = base.load(base.OUT / 'spot_projection.json.gz')['markets']
    callback = make_callback(indicators, opens)
    if args.reuse_entries:
        cached = base.load(OUT / 'manifest.json')
        if cached['inputs'] != inputs or cached['version'] != VERSION:
            raise RuntimeError('cached inputs/version mismatch')
        entries, extraction = base.load(OUT / 'entries.json.gz'), cached['extraction']
    else:
        print('extracting T071 frozen candidates', len(markets), flush=True)
        entries, extraction, _ = base.extract_entries(con, markets, crosses, indicators, None, OUT,
                                                     (), include_builtin=False, signal_callback=callback)
        add_features(entries, indicators, opens, crosses)
        base.dump(OUT / 'entries.json.gz', entries)
    con.close()
    print('auditing candidates', len(entries), flush=True)
    audit = audit_entries(entries, callback, indicators, crosses)
    audit['baseline'] = baseline_check(entries)
    print('calculating finite accounts', flush=True)
    results, ledgers = analyze(entries)
    trace = failure_trace(markets, labels)
    manifest = {'version': VERSION, 'runDate': '2026-11-20', 'inputs': inputs, 'extraction': extraction,
                'settledMarkets': len(markets), 'barCount': len(bars), 'rules': RULES, 'publicationLagsMs': LAGS,
                'orderDelaysMs': [0,300,1000], 'decisionGridMs': 1000, 'newOutOfSampleData': False,
                'sourceFiles': parent['sourceFiles']}
    for filename, data in [('results.json', results), ('sizing_ledgers.json.gz', ledgers),
                           ('failure_trace.json', trace), ('manifest.json', manifest), ('audit.json', audit)]:
        base.dump(OUT / filename, data)
    render(results, trace, manifest, audit)
    print({'entries': len(entries), 'scenarios': len(results), 'audit': audit, 'output': str(OUT)}, flush=True)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""T068: ATR filters and normalized Spot-distance candidates, offline only."""
import argparse
from bisect import bisect_right
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import sqlite3

import analyze_martingale_candidates as base

OUT = base.ROOT / 'outputs/research_tests/t068_atr_candidates_20261118'
VERSION = 'atr-candidates-v1-causal-closed-bars'
NAMES = {
    'M3': '顺势恢复原对照', 'A1': 'M3+ATR中间波动', 'A2': 'M3+过滤高波动', 'A3': 'M3+过滤低波动',
    'A4': 'ATR距离0.5', 'A5': 'ATR距离1.0', 'A6': 'ATR距离1.5',
    'A7': 'ATR突破延续', 'A8': 'ATR急变后反转',
}
FILES = {k: f'{k}_{v}.md' for k, v in NAMES.items() if k != 'M3'}


def aggregate_fives(bars):
    result, bucket = [], []
    for b in bars:
        if bucket and (b['open_us'] != bucket[-1]['end_us'] or
                       b['open_us'] // (5 * base.MINUTE) != bucket[0]['open_us'] // (5 * base.MINUTE)):
            bucket = []
        bucket.append(b)
        if len(bucket) == 5 and bucket[0]['open_us'] % (5 * base.MINUTE) == 0:
            result.append({'open_us': bucket[0]['open_us'], 'end_us': b['end_us'],
                           'open': bucket[0]['open'], 'high': max(x['high'] for x in bucket),
                           'low': min(x['low'] for x in bucket), 'close': b['close']})
            bucket = []
    return result


def build_atr(bars, period=14, rank_window=60):
    times, values, trs, atrs, previous = [], [], [], [], None
    tr_sum = atr = None
    for b in bars:
        if previous is None or b['open_us'] != previous['end_us']:
            trs, atrs, tr_sum, atr = [], [], None, None
        tr = (max(b['high'] - b['low'], abs(b['high'] - previous['close']),
                  abs(b['low'] - previous['close'])) if previous else b['high'] - b['low'])
        trs.append(tr)
        if tr_sum is None:
            if len(trs) == period:
                tr_sum = sum(trs); atr = tr_sum / period
        else:
            atr = (atr * (period - 1) + tr) / period
        atrs.append(atr)
        rank = None
        usable = [x for x in atrs[-rank_window:] if x is not None]
        if atr is not None and usable:
            rank = sum(x <= atr + 1e-15 for x in usable) / len(usable)
        values.append({'barEndUs': b['end_us'], 'atr': atr, 'tr': tr, 'rank': rank,
                       'atrBps': 10000 * atr / b['close'] if atr and b['close'] else None,
                       'close': b['close'],
                       'return': math.log(b['close'] / previous['close']) if previous and previous['close'] > 0 else None,
                       'previousTrAtr': (trs[-2] / atrs[-2] if len(trs) >= 2 and atrs[-2] else None)})
        times.append(b['end_us']); previous = b
    return times, values


def add_signals(base_indicators, atr_indicators, atr5_indicators):
    times, original = base_indicators
    atimes, avals = atr_indicators
    fives, fvals = atr5_indicators
    values = deepcopy(original)
    for row in values:
        at = row['barEndUs']; j = bisect_right(atimes, at) - 1
        atr = avals[j] if j >= 0 and 0 <= at - atimes[j] < base.MINUTE else None
        k = bisect_right(fives, at) - 1
        atr5 = fvals[k] if k >= 0 and 0 <= at - fives[k] < 5 * base.MINUTE else None
        rank = atr['rank'] if atr else None
        m3 = row.get('M3')
        row['A1'] = m3 if m3 and rank is not None and .20 <= rank <= .80 else None
        row['A2'] = m3 if m3 and rank is not None and rank <= .80 else None
        row['A3'] = m3 if m3 and rank is not None and rank >= .20 else None
        row.update(atr=atr['atr'] if atr else None, tr=atr['tr'] if atr else None,
                   atrRank=rank, atrBps=atr['atrBps'] if atr else None,
                   previousTrAtr=atr['previousTrAtr'] if atr else None,
                   atr5=atr5['atr'] if atr5 else None,
                   atr5Bps=atr5['atrBps'] if atr5 else None,
                   atr5BarEndUs=atr5['barEndUs'] if atr5 else None,
                   **{'return': atr.get('return') if atr else None})
    previous = None
    for row in values:
        row['atrShockReverse'] = None
        if previous and previous.get('previousTrAtr') is not None and previous['previousTrAtr'] >= 2:
            if previous.get('return') is not None and row.get('return') is not None:
                if previous['return'] < 0 < row['return']:
                    row['atrShockReverse'] = 'UP'
                elif previous['return'] > 0 > row['return']:
                    row['atrShockReverse'] = 'DOWN'
        previous = row
    return times, values


def dynamic_signals(at, ind, xc, remaining, **kwargs):
    out = {}
    if ind is None or xc is None or ind.get('atr') is None or ind['atr'] <= 0 or xc[2] <= 0 or xc[3] <= 0:
        return out
    scale = ind['atr'] / xc[2]
    z = math.log(xc[2] / xc[3]) / scale
    for key, threshold in (('A4', .5), ('A5', 1.), ('A6', 1.5)):
        out[key] = 'UP' if z >= threshold else 'DOWN' if z <= -threshold else None
    rows = kwargs.get('cross_rows') or []
    row_times = [r[0] for r in rows]
    x60 = base.cross_at(rows, row_times, at - 60 * base.SECOND)
    if x60 is not None:
        move_z = math.log(xc[2] / x60[2]) / scale
        out['A7'] = 'UP' if move_z >= 1.0 else 'DOWN' if move_z <= -1.0 else None
    out['A8'] = ind.get('atrShockReverse')
    return out


def primary(row, odds='gross_le_050'):
    return row['delayMs'] == 300 and row['odds'] == odds and row['sizing'] == 'fixed'


def result_table(rows):
    text = '|策略|方向|分段|胜/总笔数|胜率|含费收益U|投入收益率|最大回撤U|最长连败|亏后再亏|\n|---|---|---|---|---|---|---|---|---|---|\n'
    for r in rows:
        text += (f"|{r['strategy']} {NAMES.get(r['strategy'], r['strategy'])}|{r['side']}|{r['split']}|"
                 f"{r['wins']}/{r['n']}|{base.pct(r['winRate'])}|{r['netPnl']:.3f}|{base.pct(r['roi'])}|"
                 f"{r['maxDrawdown']:.3f}|{r['maxConsecutiveLosses']}|{base.pct(r['lossAfterLoss'])}|\n")
    return text


def render(results, manifest, out):
    core = [r for r in results if primary(r)]
    text = '# ATR波动率与锚定距离候选验证（T068）\n\n'
    text += (f"本轮使用T065冻结的{manifest['settledMarkets']}个官方结算市场，时间为{manifest['firstMarket']}至{manifest['lastMarket']}，"
             '中间有采集缺口；没有修改纸上或实盘。ATR只作为波动率/距离尺度，方向仍由规则决定。\n\n'
             '主成交口径：剩余270–30秒、卖一0.30–0.50、1U含费预算、300ms延迟、盘口≤2秒、每场首个合格机会、持有官方结算。'
             '费用冻结为0.07*p*(1-p)，1U不等于实盘最低金额。\n\n')
    for title, split in (('全段固定金额','all'), ('验证段固定金额','validation'), ('末段固定金额','test')):
        text += f'## {title}\n\n' + result_table(sorted([r for r in core if r['split'] == split], key=lambda r: (r['strategy'], r['side'])))
    eligible = []
    for key in FILES:
        for side in ('UP', 'DOWN'):
            rr = [r for r in core if r['strategy'] == key and r['side'] == side and r['split'] in ('validation', 'test')]
            if len(rr) == 2 and all(r['n'] >= 100 and r['netPnl'] > 0 for r in rr): eligible.append(key + '/' + side)
    text += '\n## 结论\n\n'
    text += ('满足验证段/末段各≥100笔且固定金额均盈利：' + '、'.join(eligible) if eligible else
             '没有ATR候选满足验证段/末段各≥100笔且固定金额均盈利的初步门槛。') + '\n\n'
    text += ('ATR过滤后交易数减少时，必须同时看机会保留率和收益，不把较少的交易误报为胜率改善。A4–A8需要新鲜Spot，缺失不补造；'
             'ATR不等于方向预测，快速波动既可能延续也可能反转。\n\n')
    text += '## 2047U本金11级压力\n\n|策略|方向|笔数|净收益U|最大回撤U|最高注额U|打穿|本金停止|\n|---|---|---|---|---|---|---|---|\n'
    for r in sorted(results, key=lambda r: (r['strategy'], r['side'])):
        if r['strategy'] in NAMES and r['delayMs'] == 300 and r['odds'] == 'gross_le_050' and r['sizing'] == 'martin11_2047' and r['split'] == 'all':
            text += f"|{r['strategy']}|{r['side']}|{r['n']}|{r['netPnl']:.3f}|{r['maxDrawdown']:.3f}|{r['maxBudget']:.0f}|{r['capLossCycles']}|{r['rejections'].get('bankroll_stop', 0)}|\n"
    text += '\n## 文件\n\n'
    for key, filename in FILES.items(): text += f'- [{key} {NAMES[key]}]({filename})\n'
    text += '- [数据质量与审计](数据质量与审计.md)\n- [压力对照](压力对照.md)\n'
    text += '\n结果和逐笔数据保存在JSON/GZIP，复现命令为`python3 scripts/analysis/analyze_atr_candidates.py`。\n'
    (out / 'ATR波动率与锚定距离候选验证.md').write_text(text, encoding='utf-8')
    for key, filename in FILES.items():
        body = f'# {key} {NAMES[key]}\n\n完整规则与限制见[总报告](ATR波动率与锚定距离候选验证.md)及[冻结计划](../../../docs/ATR策略验证计划_20261118.md)。\n\n'
        body += result_table(sorted([r for r in core if r['strategy'] in (key, 'M3')], key=lambda r: (r['split'], r['side'], r['strategy'])))
        (out / filename).write_text(body, encoding='utf-8')
    pressure = '# ATR候选压力对照\n\n|规则|方向|场景|笔数|净收益U|\n|---|---|---|---|---|\n'
    for r in sorted(results, key=lambda r: (r['strategy'], r['side'], r['sizing'], r['delayMs'])):
        if r['strategy'] in FILES and r['split'] == 'test' and r['odds'] == 'gross_le_050' and ((r['sizing'] == 'fixed' and r['delayMs'] in (0,1000)) or (r['delayMs']==300 and r['sizing'] in ('fixed_fee2','fixed_settle300'))):
            label = f"延迟{r['delayMs']}ms" if r['sizing']=='fixed' else ('双倍费用' if r['sizing']=='fixed_fee2' else '结算+300秒')
            pressure += f"|{r['strategy']}|{r['side']}|{label}|{r['n']}|{r['netPnl']:.3f}|\n"
    (out / '压力对照.md').write_text(pressure, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-entries', action='store_true')
    args = parser.parse_args(); OUT.mkdir(parents=True, exist_ok=True)
    parent = base.load(base.OUT / 'manifest.json')
    coverage = base.load(base.OUT / 'market_coverage.json')
    labels = base.load(base.OUT / 'official_label_arrivals.json')['markets']
    con = sqlite3.connect(f'file:{base.ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro', uri=True); con.row_factory = sqlite3.Row
    sources = [list(r) for r in con.execute('SELECT path,size,mtime_ns FROM source_files ORDER BY path')]
    if sources != parent['sourceFiles']: raise RuntimeError('T065 source cache changed; refreeze cohort')
    markets = []
    for row in coverage:
        m = dict(con.execute('SELECT market_id,start_us,end_us,winner FROM markets WHERE market_id=?', (row['marketId'],)).fetchone())
        if m['winner'] != labels[m['market_id']]['winner']: raise RuntimeError('official label mismatch')
        m['label_available_us'] = row['labelAvailableUs']; markets.append(m)
    bars = base.load(base.OUT / 'binance_official_1m.json.gz')['bars']
    indicators = add_signals(base.build_indicators(bars), build_atr(bars), build_atr(aggregate_fives(bars)))
    crosses, cross_audit = base.project_cross(base.OUT)
    old = base.load(base.OUT / 'entries.json.gz')
    inputs = {'parentManifestSha256': hashlib.sha256((base.OUT / 'manifest.json').read_bytes()).hexdigest(),
              'baseCandlesSha256': hashlib.sha256((base.OUT / 'binance_official_1m.json.gz').read_bytes()).hexdigest(),
              'crossSourceFingerprint': cross_audit['sourceFingerprint']}
    if args.reuse_entries:
        cached = base.load(OUT / 'manifest.json')
        if cached['version'] != VERSION or cached['inputs'] != inputs: raise RuntimeError('entry cache mismatch')
        entries = base.load(OUT / 'entries.json.gz'); extraction = cached['extraction']
    else:
        entries, extraction, _ = base.extract_entries(con, markets, crosses, indicators, None, OUT,
                                                       ('M3', 'A1', 'A2', 'A3'), include_builtin=False,
                                                       signal_callback=dynamic_signals)
        base.dump(OUT / 'entries.json.gz', entries)
    con.close()
    results, _ = base.analyze(entries, [], OUT)
    base.dump(OUT / 'results.json', results)
    fields = ('marketId','side','odds','delayMs','decisionUs','executionUs','winner','priceCap','fixedFill','levels','decisionLevels')
    old_control = {tuple(str(e.get(k)) for k in fields) for e in old if e['strategy'] == 'M3'}
    new_control = {tuple(str(e.get(k)) for k in fields) for e in entries if e['strategy'] == 'M3'}
    keys = [(e['strategy'],e['marketId'],e['side'],e['odds'],e['delayMs']) for e in entries]
    violations = sum(not (e['bookReceivedUs'] <= e['executionUs'] and 0 <= e['executionUs']-e['bookSourceUs'] <= 2*base.SECOND and e['fixedFill']['cost'] <= 1+1e-9 and e['fixedFill']['worst'] <= e['priceCap']+1e-9) for e in entries)
    if len(keys) != len(set(keys)) or violations: raise RuntimeError('entry audit failed')
    manifest = {**parent,'version':VERSION,'parentVersion':parent['version'],'inputs':inputs,'candidates':NAMES,
                'extraction':extraction,'cross':cross_audit,'model':None,
                'rules':{**parent['rules'],'atrPeriod':14,'atrRankWindow':60,'zThresholds':[.5,1,1.5],'breakoutAtrMultiple':1,'shockAtrMultiple':2}}
    base.dump(OUT / 'manifest.json',manifest)
    base.dump(OUT / 'audit.json',{'entries':len(entries),'uniqueEntries':len(set(keys)),'timestampBudgetCapViolations':violations,'scenarios':len(results),'m3ControlEntriesOld':len(old_control),'m3ControlEntriesNew':len(new_control),'m3ControlIntersection':len(old_control & new_control)})
    render(results,manifest,OUT)
    (OUT/'数据质量与审计.md').write_text(f"# 数据质量与审计\n\n- 冻结市场：{len(markets)}。\n- 1分钟K线：{len(bars)}。\n- ATR指标只使用已完成K线；缺口会重置平滑。\n- M3控制成交集合与T065交集：{len(old_control & new_control)}；本轮共有{len(entries)}条唯一候选成交情景。\n- 时间/预算/盘口限价违规：{violations}。\n- A4–A8若无新鲜Spot则不成交；没有用未来K线或最终结算标签筛选入场。\n\n[返回总报告](ATR波动率与锚定距离候选验证.md)；机器审计见[audit.json](audit.json)。\n",encoding='utf-8')
    print({'markets':len(markets),'entries':len(entries),'scenarios':len(results),'out':str(OUT)},flush=True)


if __name__ == '__main__':
    main()

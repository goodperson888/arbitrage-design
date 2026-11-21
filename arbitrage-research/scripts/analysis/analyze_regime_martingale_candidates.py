#!/usr/bin/env python3
"""T067: frozen range RSI, trend recovery, and Bollinger re-entry candidates."""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import sqlite3

import analyze_martingale_candidates as base

OUT = base.ROOT / 'outputs/research_tests/t067_regime_martingale_candidates_20261118'
VERSION = 'regime-three-v1-causal-closed-bars'
NAMES = {'M2': '80/20回穿原对照', 'M9': '震荡80/20回穿',
         'R70': '70/30回穿原对照', 'M9S': '震荡70/30敏感性',
         'M3': '顺势恢复原对照', 'M10': '趋势强度+顺势恢复',
         'B0': '布林带收回原对照', 'M11': '震荡+布林带收回'}
PAIRS = [('M9', 'M2'), ('M9S', 'R70'), ('M10', 'M3'), ('M11', 'B0')]
FILES = {'M9': 'M9_震荡RSI回穿.md', 'M9S': 'M9S_七十三十敏感性.md',
         'M10': 'M10_趋势回撤恢复.md', 'M11': 'M11_震荡布林带收回.md'}


def aggregate_fives(bars):
    result, bucket = [], []
    for b in bars:
        if bucket and (b['open_us'] != bucket[-1]['end_us'] or
                       b['open_us'] // (5 * base.MINUTE) != bucket[0]['open_us'] // (5 * base.MINUTE)):
            bucket = []
        bucket.append(b)
        if len(bucket) == 5 and bucket[0]['open_us'] % (5 * base.MINUTE) == 0:
            result.append({'open_us': bucket[0]['open_us'], 'end_us': b['end_us'],
                           'open': bucket[0]['open'], 'high': max(r['high'] for r in bucket),
                           'low': min(r['low'] for r in bucket), 'close': b['close']})
            bucket = []
    return result


def build_adx(fives, period=14):
    """Wilder sums seed at period transitions, ADX at period DX values."""
    values, times, previous = [], [], None
    tr_seed, plus_seed, minus_seed, dx_seed = [], [], [], []
    tr_sum = plus_sum = minus_sum = adx = None
    for b in fives:
        if previous is None or b['open_us'] != previous['end_us']:
            tr_seed, plus_seed, minus_seed, dx_seed = [], [], [], []
            tr_sum = plus_sum = minus_sum = adx = None
        plus_di = minus_di = dx = None
        if previous is not None and b['open_us'] == previous['end_us']:
            up, down = b['high'] - previous['high'], previous['low'] - b['low']
            plus = up if up > down and up > 0 else 0.
            minus = down if down > up and down > 0 else 0.
            tr = max(b['high'] - b['low'], abs(b['high'] - previous['close']),
                     abs(b['low'] - previous['close']))
            if tr_sum is None:
                tr_seed.append(tr); plus_seed.append(plus); minus_seed.append(minus)
                if len(tr_seed) == period:
                    tr_sum, plus_sum, minus_sum = map(sum, (tr_seed, plus_seed, minus_seed))
            else:
                tr_sum = tr_sum - tr_sum / period + tr
                plus_sum = plus_sum - plus_sum / period + plus
                minus_sum = minus_sum - minus_sum / period + minus
            if tr_sum is not None:
                plus_di = 100 * plus_sum / tr_sum if tr_sum else 0.
                minus_di = 100 * minus_sum / tr_sum if tr_sum else 0.
                dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if plus_di + minus_di else 0.
                if adx is None:
                    dx_seed.append(dx)
                    if len(dx_seed) == period:
                        adx = sum(dx_seed) / period
                else:
                    adx = (adx * (period - 1) + dx) / period
        times.append(b['end_us'])
        values.append({'barEndUs': b['end_us'], 'adx': adx,
                       'plusDI': plus_di, 'minusDI': minus_di, 'dx': dx})
        previous = b
    return times, values


def bollinger(values, period=20):
    if len(values) < period:
        return None
    window = values[-period:]
    mid = math.fsum(window) / period
    sd = math.sqrt(math.fsum((v - mid) ** 2 for v in window) / period)
    return mid, mid - 2 * sd, mid + 2 * sd


def return_cross(previous, current, delta, high=80, low=20):
    if previous is None or current is None or delta is None:
        return None
    if previous >= high > current and delta < 0:
        return 'DOWN'
    if previous <= low < current and delta > 0:
        return 'UP'
    return None


def bb_return(previous_close, previous_band, close, band):
    if previous_band is None or band is None or previous_close is None:
        return None
    if not band[1] <= close <= band[2]:
        return None
    if previous_close < previous_band[1] and close > previous_close:
        return 'UP'
    if previous_close > previous_band[2] and close < previous_close:
        return 'DOWN'
    return None


def add_regime_signals(indicators, adx_indicators):
    times, original = indicators
    values = deepcopy(original)
    adx_times, adx_values = adx_indicators
    closes, previous_band = [], None
    previous = None
    for row in values:
        at = row['barEndUs']
        if previous is None or at != previous['barEndUs'] + base.MINUTE:
            closes, previous_band, previous = [], None, None
        delta = row['close'] - previous['close'] if previous else None
        r70 = return_cross(previous['rsi'] if previous else None, row['rsi'], delta, 70, 30)
        closes.append(row['close'])
        band = bollinger(closes)
        b0 = bb_return(previous['close'] if previous else None, previous_band, row['close'], band)
        j = bisect_right(adx_times, at) - 1
        a = adx_values[j] if j >= 0 and 0 <= at - adx_times[j] < 5 * base.MINUTE else None
        adx = a['adx'] if a else None
        is_range, is_trend = adx is not None and adx < 20, adx is not None and adx >= 25
        row.update(R70=r70, B0=b0, M9=row['M2'] if is_range else None,
                   M9S=r70 if is_range else None, M10=row['M3'] if is_trend else None,
                   M11=b0 if is_range else None, adx=adx,
                   adxBarEndUs=a['barEndUs'] if a else None, bb=band)
        previous, previous_band = row, band
    return times, values


def coverage_funnel(markets, indicators, entries, results):
    markets_with_signal = defaultdict(set)
    signal_seconds = Counter()
    for index, market in enumerate(markets):
        split = base.market_split(index, len(markets))
        for at in range(market['start_us'] + 30 * base.SECOND, market['end_us'] - 30 * base.SECOND + 1, base.SECOND):
            row = base.indicator_at(indicators, at)
            if row is None:
                continue
            for key in NAMES:
                side = row[key]
                if side is not None:
                    for segment in ('all', split):
                        k = (key, side, segment)
                        markets_with_signal[k].add(market['market_id']); signal_seconds[k] += 1
    qualified = Counter((e['strategy'], e['side'], e['split']) for e in entries
                        if e['delayMs'] == 300 and e['odds'] == 'gross_le_050')
    rows = []
    for result in results:
        if result['delayMs'] != 300 or result['odds'] != 'gross_le_050' or result['sizing'] != 'fixed':
            continue
        k = (result['strategy'], result['side'], result['split'])
        count = sum(v for (s, side, _), v in qualified.items() if (s, side) == k[:2]) if k[2] == 'all' else qualified[k]
        rows.append({'strategy': k[0], 'side': k[1], 'split': k[2],
                     'signalMarkets': len(markets_with_signal[k]), 'signalSeconds': signal_seconds[k],
                     'executableCandidateMarkets': count, 'actualTrades': result['n'],
                     'rejections': result['rejections']})
    return rows


def complete_results(entries, out):
    results, strata = base.analyze(entries, [], out)
    present = {(r['strategy'], r['side'], r['delayMs'], r['odds'], r['split'], r['sizing']) for r in results}
    for key in NAMES:
        for side in ('UP', 'DOWN'):
            for delay in (0, 300, 1000):
                for odds in ('gross_le_050', 'net_cost_le_050'):
                    for split in ('all', 'train', 'validation', 'test'):
                        scenarios = [('fixed', 1, 100., 60, 1.), ('martin3_100', 3, 100., 60, 1.),
                                     ('martin5_100', 5, 100., 60, 1.), ('martin11_100', 11, 100., 60, 1.),
                                     ('martin11_2047', 11, 2047., 60, 1.)]
                        if delay == 300:
                            scenarios += [('fixed_fee2', 1, 100., 60, 2.), ('fixed_settle300', 1, 100., 300, 1.)]
                        for label, rounds, bankroll, wait, mult in scenarios:
                            if (key, side, delay, odds, split, label) not in present:
                                summary, _ = base.replay([], rounds, bankroll, wait, mult)
                                summary.update(strategy=key, side=side, delayMs=delay, odds=odds,
                                               split=split, sizing=label, qualifiedSignals=0)
                                if label == 'fixed':
                                    summary['dailyBlockRoiCI95'] = None
                                results.append(summary)
    base.dump(out / 'results.json', results)
    return results, strata


def table(rows):
    text = '|规则|方向|分段|胜/笔数|胜率|含费收益U|投入收益率|最大回撤U|连败|亏后再亏|\n'
    text += '|---|---|---|---|---|---|---|---|---|---|\n'
    for r in rows:
        text += (f"|{r['strategy']} {NAMES[r['strategy']]}|{r['side']}|{r['split']}|{r['wins']}/{r['n']}|"
                 f"{base.pct(r['winRate'])}|{r['netPnl']:.3f}|{base.pct(r['roi'])}|{r['maxDrawdown']:.3f}|"
                 f"{r['maxConsecutiveLosses']}|{base.pct(r['lossAfterLoss'])}|\n")
    return text


def primary(row, odds='gross_le_050'):
    return row['delayMs'] == 300 and row['odds'] == odds and row['sizing'] == 'fixed'


def render_reports(results, funnel, manifest, out):
    core = [r for r in results if primary(r)]
    text = '# 行情过滤三类候选与有限马丁（T067）\n\n'
    text += (f"已完成三类固定规则及70/30预设敏感性；UP/DOWN分开。沿用{manifest['settledMarkets']}个市场，"
             f"{manifest['firstMarket']}至{manifest['lastMarket']}，有缺口，不是连续到11月。\n\n"
             '规则在结果前冻结，见[计划](../../../docs/马丁行情过滤三候选验证计划_20261118.md)。'
             'RSI/布林带只用已收盘1分钟K线；ADX只用完整已收盘5分钟OHLC，另有至少24小时预热。'
             'M9/M9S/M11要求ADX<20，M10要求ADX≥25。M2、R70、M3、B0为对应无ADX对照。\n\n'
             '主表：剩余270–30秒、决策卖一0.30–0.50、每笔1U含费预算、300ms延迟、盘口年龄≤2秒、持有官方结算。'
             '每场首个合格机会，首次发送失败不追单；下一笔需等实际收到结果且市场结束至少60秒。'
             '费用为冻结假设0.07*p*(1-p)，不是当前收费承诺。原M2/M3的成交轨迹已与T065逐条核对。\n\n'
             '前60%/中20%/末20%按全部市场时间切分；末段曾用于其他研究，不是未接触的未来样本。'
             '各方向独立账户；不可简单把两个方向的收益相加当作一个账户。\n\n'
             '## 本轮发现\n\n'
             '- 震荡80/20回穿整段实际仅UP1笔、DOWN3笔，末段没有成交；70/30放宽后仍然很少，不能凭个别赢单判断有效。\n'
             '- 趋势恢复的固定金额全段UP为3胜/6笔、+0.369U，DOWN为6胜/10笔、+2.308U；原M3分别为12胜/23笔、+2.138U与15胜/27笔、+5.732U。过滤减少机会和全段利润；DOWN胜率的提高还不足以确认增益。\n'
             '- 震荡布林带全段UP17胜/41笔、-0.054U，DOWN16胜/48笔、-11.533U。无ADX的B0 UP全段+5.880U，但验证段-3.376U、末段-0.006U，不能只挑全段正收益。\n'
             '- 趋势恢复的3/5/11级回放实际上最高只投到4U，样本没有覆盖更高倍投级；“未打穿”不等于11级已充分验证。\n\n'
             '## 全段固定金额对照\n\n')
    text += table(sorted([r for r in core if r['split'] == 'all'], key=lambda r: (r['strategy'], r['side'])))
    text += '\n## 末段固定金额对照\n\n'
    text += table(sorted([r for r in core if r['split'] == 'test'], key=lambda r: (r['strategy'], r['side'])))
    text += '\n## 验证段固定金额对照\n\n'
    text += table(sorted([r for r in core if r['split'] == 'validation'], key=lambda r: (r['strategy'], r['side'])))
    text += '\n## 含费成本≤0.50的严格末段\n\n'
    text += ('每档买价上限在发送前确定，使每档含费成本≤0.50，不是根据未来平均成交价筛选。'
             '不同上限产生独立首单与等待结算序列。\n\n')
    text += table(sorted([r for r in results if primary(r, 'net_cost_le_050') and r['split'] == 'test'], key=lambda r: (r['strategy'], r['side'])))
    text += '\n## 覆盖漏斗（全部日期、主成交口径）\n\n'
    text += '|规则|方向|有信号市场|可成交候选市场|等待结算后成交|\n|---|---|---|---|---|\n'
    for r in sorted([r for r in funnel if r['split'] == 'all'], key=lambda r: (r['strategy'], r['side'])):
        text += f"|{r['strategy']}|{r['side']}|{r['signalMarkets']}|{r['executableCandidateMarkets']}|{r['actualTrades']}|\n"
    text += '\n信号市场、候选市场、最终交易不是同一概念；同场多秒信号不作为多笔独立样本。信号窗口60秒不是持仓60秒。\n'
    text += '\n## 整段倍投压力（2047U本金、最多11单）\n\n'
    text += '|规则|方向|笔数|净收益U|最大回撤U|最高注额U|最长连败|11级打穿|本金停止|赢单但周期亏损|\n|---|---|---|---|---|---|---|---|---|---|\n'
    for r in sorted(results, key=lambda r: (r['strategy'], r['side'])):
        if r['strategy'] in FILES and r['delayMs'] == 300 and r['odds'] == 'gross_le_050' and r['sizing'] == 'martin11_2047' and r['split'] == 'all':
            text += f"|{r['strategy']}|{r['side']}|{r['n']}|{r['netPnl']:.3f}|{r['maxDrawdown']:.3f}|{r['maxBudget']:.0f}|{r['maxConsecutiveLosses']}|{r['capLossCycles']}|{r['rejections'].get('bankroll_stop', 0)}|{r['negativeWinningCycles']}|\n"
    qualified = []
    for key in FILES:
        for side in ('UP', 'DOWN'):
            rr = [r for r in core if r['strategy'] == key and r['side'] == side and r['split'] in ('validation', 'test')]
            if len(rr) == 2 and all(r['n'] >= 100 and r['netPnl'] > 0 for r in rr):
                qualified.append(key + '/' + side)
    text += '\n## 判断与局限\n\n'
    text += ('满足验证/末段各≥100笔且固定金额扣费均盈利的组：' + '、'.join(qualified) if qualified else
             '没有新增候选方向满足验证/末段各≥100笔且固定金额扣费均盈利的初步门槛。') + '\n\n'
    text += ('这是历史固定规则比较，不是普遍否定某个指标。正收益小样本不能升级实盘；过滤后减少交易也不等于提高总体收益。'
             '对照版本独立重新发现首单，收益差不是同一组成交单的逐笔配对因果效应。'
             '1U研究订单未应用实盘最低金额；历史最终K线不能证明实际发布延迟，且入场仅限已记录可成交盘口。'
             '0/1000ms、双倍费用、额外结算等待、100U本金与3/5/11单完整结果在results.json；'
             '逐笔和逐日数据在sizing_ledgers.json.gz，各策略分段结果如下。\n\n')
    text += '- [有限本金与延迟/费用压力对照](有限本金与压力对照.md)\n'
    text += '- [数据质量与因果审计](数据质量与因果审计.md)\n'
    for key, filename in FILES.items():
        text += f'- [{key} {NAMES[key]}]({filename})\n'
    text += ('\n复现：在arbitrage-research目录运行`python3 scripts/analysis/analyze_regime_martingale_candidates.py`；'
             '只重算资金结果加`--reuse-entries`。不刷新或覆盖T065/T066。\n')
    (out / '行情过滤三候选验证.md').write_text(text, encoding='utf-8')
    for key, control in PAIRS:
        body = f'# {key} {NAMES[key]}\n\n'
        body += '完整规则和局限见[总报告](行情过滤三候选验证.md)及[冻结计划](../../../docs/马丁行情过滤三候选验证计划_20261118.md)。\n\n'
        body += table(sorted([r for r in core if r['strategy'] in (key, control)], key=lambda r: (r['split'], r['side'], r['strategy'])))
        body += '\n胜率区间、逐日ROI区间和各注额/压力对照见results.json，未据这些分组调参。\n'
        (out / FILES[key]).write_text(body, encoding='utf-8')
    sizing = '# 有限本金与预设压力对照\n\n'
    sizing += '规则和局限见[总报告](行情过滤三候选验证.md)。先看固定金额，以下不用于倒推选择最赚钱的倍投级数。\n\n'
    sizing += '## 100U本金全段有限倍投\n\n'
    sizing += '|规则|方向|最多级数|实际笔数|净收益U|最大回撤U|最高注额U|级数打穿|本金停止|\n|---|---|---|---|---|---|---|---|---|\n'
    for r in sorted(results, key=lambda r: (r['strategy'], r['side'], r['sizing'])):
        if (r['strategy'] in FILES and r['delayMs'] == 300 and r['odds'] == 'gross_le_050'
                and r['split'] == 'all' and r['sizing'] in ('martin3_100', 'martin5_100', 'martin11_100')):
            level = r['sizing'].split('_')[0].replace('martin', '')
            sizing += f"|{r['strategy']}|{r['side']}|{level}|{r['n']}|{r['netPnl']:.3f}|{r['maxDrawdown']:.3f}|{r['maxBudget']:.0f}|{r['capLossCycles']}|{r['rejections'].get('bankroll_stop', 0)}|\n"
    sizing += ('\n布林带M11在2047U情景的11级倍投虽然历史盈利，但100U本金的11级情景UP在17笔后停止（-53.078U），'
               'DOWN在8笔后停止（-59.637U）；不能忽略资金约束。3/5级全段UP、DOWN均亏损。\n\n'
               '## 末段固定金额压力\n\n'
               '|规则|方向|场景|成交笔数|含费净收益U|\n|---|---|---|---|---|\n')
    for r in sorted(results, key=lambda r: (r['strategy'], r['side'], r['sizing'], r['delayMs'])):
        if (r['strategy'] in FILES and r['odds'] == 'gross_le_050' and r['split'] == 'test'
                and ((r['sizing'] == 'fixed' and r['delayMs'] in (0, 300, 1000))
                     or (r['delayMs'] == 300 and r['sizing'] in ('fixed_fee2', 'fixed_settle300')))):
            scene = f"延迟{r['delayMs']}ms" if r['sizing'] == 'fixed' else ('双倍费用' if r['sizing'] == 'fixed_fee2' else '结算至少+300秒')
            sizing += f"|{r['strategy']}|{r['side']}|{scene}|{r['n']}|{r['netPnl']:.3f}|\n"
    sizing += '\n不同延迟改变可成交首单和后续可用资金序列，不能把收益差全部解释成同一单的滑点；双倍费用保持原发送限价和候选列表。\n'
    (out / '有限本金与压力对照.md').write_text(sizing, encoding='utf-8')


def write_quality(manifest, indicators, markets, out):
    ready = Counter()
    for market in markets:
        row = base.indicator_at(indicators, market['start_us'] + 30 * base.SECOND)
        ready['rsi'] += int(row is not None and row['rsi'] is not None)
        ready['adx'] += int(row is not None and row['adx'] is not None)
        ready['bb'] += int(row is not None and row['bb'] is not None)
    audit = base.load(out / 'audit.json')
    text = '# 数据质量与因果审计（T067）\n\n'
    text += (f"- 冻结市场：{len(markets)}；首个检查点RSI可用{ready['rsi']}、ADX可用{ready['adx']}、布林带可用{ready['bb']}。\n"
             f"- 新补预热1分钟K线{manifest['warmup']['bars']}根，失败请求{len(manifest['warmup']['failedRequests'])}，预热内部缺口{manifest['warmup']['gaps']}。\n"
             f"- 原M2/M3对照有{audit['controlEntriesIdenticalToT065']}条成交情景与T065逐字段一致；覆盖方向、延迟、赔率上限，不代表独立场数。\n"
             f"- 共{audit['entries']}条候选成交情景、{audit['scenarios']}组资金统计；去重后{audit['uniqueEntries']}，时间/预算/限价违规{audit['timestampBudgetCapViolations']}。\n"
             '- SQLite只读；缓存source_files清单必须与T065一致，官方胜负标签逐场核对；父清单、原K线与预热K线记录SHA256。\n'
             '- 正式比较沿用T065实际结果接收时间。盘口含源时间和本机接收时间，决策/执行分别检查，指标只按已收盘时间生效。\n'
             '- 原K线六小时预热的RSI/EMA不变，额外长预热仅用于ADX；因此避免修改原指标种子导致对照漂移。\n'
             '- 0成交方向仍以0笔、胜率缺省明确列出，不当作100%或从报告消失。\n'
             '- 数据日期有缺口、20个有市场的北京时间日期；并未使用截至11月的新行情。历史末段曾被其他实验观察，不是未接触的未来样本。\n'
             '- 历史费用是假设，1U最低金额未做实盘门槛校验；已收盘K线的发布时间没有实际网络延迟日志。缺失盘口不补造。\n\n'
             '[返回总报告](行情过滤三候选验证.md)；[覆盖漏斗](signal_coverage_funnel.json)；[机器审计](audit.json)。\n')
    (out / '数据质量与因果审计.md').write_text(text, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-entries', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    parent = base.load(base.OUT / 'manifest.json')
    cohort = base.load(base.OUT / 'market_coverage.json')
    labels = base.load(base.OUT / 'official_label_arrivals.json')['markets']
    con = sqlite3.connect(f'file:{base.ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    sources = [list(r) for r in con.execute('SELECT path,size,mtime_ns FROM source_files ORDER BY path')]
    if sources != parent['sourceFiles']:
        raise RuntimeError('Frozen T065 normalized cache changed; comparison must be refrozen')
    markets = []
    for row in cohort:
        market = dict(con.execute('SELECT market_id,start_us,end_us,winner FROM markets WHERE market_id=?', (row['marketId'],)).fetchone())
        if market['winner'] != labels[market['market_id']]['winner']:
            raise RuntimeError('Official winner disagreement')
        market['label_available_us'] = row['labelAvailableUs']
        markets.append(market)
    if len(markets) != parent['settledMarkets']:
        raise RuntimeError('Frozen cohort mismatch')
    bars = base.load(base.OUT / 'binance_official_1m.json.gz')['bars']
    warm_dir = OUT / 'warmup'; warm_dir.mkdir(exist_ok=True)
    prefix, warm_audit = base.official_candles(markets[0]['start_us'] - 18 * 60 * base.MINUTE,
                                             bars[0]['open_us'], warm_dir)
    combined = {r['open_us']: r for r in prefix}
    combined.update({r['open_us']: r for r in bars})
    adx_bars = [combined[k] for k in sorted(combined)]
    adx_indicators = build_adx(aggregate_fives(adx_bars))
    indicators = add_regime_signals(base.build_indicators(bars), adx_indicators)
    first = base.indicator_at(indicators, markets[0]['start_us'] + 30 * base.SECOND)
    if not first or first['adx'] is None or warm_audit['failedRequests'] or warm_audit['gaps']:
        raise RuntimeError('ADX warmup is incomplete; do not silently reduce early-market coverage')
    inputs = {'parentManifestSha256': hashlib.sha256((base.OUT / 'manifest.json').read_bytes()).hexdigest(),
              'baseCandlesSha256': hashlib.sha256((base.OUT / 'binance_official_1m.json.gz').read_bytes()).hexdigest(),
              'warmCandlesSha256': hashlib.sha256((warm_dir / 'binance_official_1m.json.gz').read_bytes()).hexdigest()}
    if args.reuse_entries:
        cached = base.load(OUT / 'manifest.json')
        if cached['version'] != VERSION or cached['inputs'] != inputs:
            raise RuntimeError('Entry cache mismatch')
        entries, extraction = base.load(OUT / 'entries.json.gz'), cached['extraction']
    else:
        entries, extraction, _ = base.extract_entries(con, markets, {}, indicators, None, OUT, tuple(NAMES))
        for e in entries:
            row = base.indicator_at(indicators, e['decisionUs'])
            e.update(adx=row['adx'], adxBarEndUs=row['adxBarEndUs'], indicatorBarEndUs=row['barEndUs'], bb=row['bb'])
        base.dump(OUT / 'entries.json.gz', entries)
    con.close()
    old_entries = base.load(base.OUT / 'entries.json.gz')
    compare_fields = ('strategy', 'marketId', 'side', 'odds', 'delayMs', 'decisionUs', 'executionUs',
                      'winner', 'labelAvailableUs', 'priceCap', 'fixedFill', 'levels', 'decisionLevels')
    signature = lambda e: tuple(str(e[k]) for k in compare_fields)
    old_control = {signature(e) for e in old_entries if e['strategy'] in ('M2', 'M3')}
    new_control = {signature(e) for e in entries if e['strategy'] in ('M2', 'M3')}
    if old_control != new_control:
        raise RuntimeError('M2/M3 control execution regression')
    results, _ = complete_results(entries, OUT)
    keys = [(e['strategy'], e['marketId'], e['side'], e['odds'], e['delayMs']) for e in entries]
    violations = sum(not (e['bookReceivedUs'] <= e['executionUs']
                          and 0 <= e['executionUs'] - e['bookSourceUs'] <= 2 * base.SECOND
                          and e['indicatorBarEndUs'] <= e['decisionUs']
                          and (e['adxBarEndUs'] is None or e['adxBarEndUs'] <= e['decisionUs'])
                          and e['fixedFill']['cost'] <= 1 + 1e-9
                          and e['fixedFill']['worst'] <= e['priceCap'] + 1e-9) for e in entries)
    if violations or len(set(keys)) != len(entries):
        raise RuntimeError('Causal timestamp, budget, cap or unique-entry audit failed')
    if any(r['odds'] == 'net_cost_le_050' and r['feeMultiplier'] == 1 and r['negativeWinningCycles'] for r in results):
        raise RuntimeError('Net-odds invariant failed')
    funnel = coverage_funnel(markets, indicators, entries, results)
    base.dump(OUT / 'signal_coverage_funnel.json', funnel)
    base.dump(OUT / 'audit.json', {'entries': len(entries), 'uniqueEntries': len(set(keys)),
                                 'timestampBudgetCapViolations': violations, 'scenarios': len(results),
                                 'controlEntriesIdenticalToT065': len(new_control)})
    manifest = {**parent, 'version': VERSION, 'parentVersion': parent['version'], 'inputs': inputs,
                'candidates': NAMES, 'extraction': extraction, 'model': None, 'cross': None,
                'warmup': warm_audit, 'adxBars': len(adx_indicators[0]),
                'rules': {**parent['rules'], 'adxPeriod': 14, 'rangeADX': [0, 20],
                          'trendADXAtLeast': 25, 'bollingerPeriod': 20, 'bollingerPopulationStdMultiplier': 2,
                          'rsiPrimary': [80, 20], 'rsiSensitivity': [70, 30], 'signalValidSeconds': 60}}
    base.dump(OUT / 'manifest.json', manifest)
    render_reports(results, funnel, manifest, OUT)
    write_quality(manifest, indicators, markets, OUT)
    print({'markets': len(markets), 'entries': len(entries), 'scenarios': len(results), 'out': str(OUT)}, flush=True)


if __name__ == '__main__':
    main()

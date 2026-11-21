#!/usr/bin/env python3
"""T066: frozen persistent RSI crossing variants, offline only."""
import argparse
from copy import deepcopy
import sqlite3

import analyze_martingale_candidates as base

OUT = base.ROOT / 'outputs/research_tests/t066_rsi_persistent_crossings_20261118'
VERSION = 'persistent-crossings-v1'
NAMES = {'M7': '回穿后持续方向', 'M8': '回穿后持续方向、极值区暂停'}


def add_crossing_states(indicators):
    """Closed RSI bars only; opposite entry crossings switch the latent state."""
    times, original = indicators
    values = deepcopy(original)
    previous = previous_end = state = None
    for row in values:
        end, rsi = row['barEndUs'], row['rsi']
        if rsi is None or (previous_end is not None and end != previous_end + base.MINUTE):
            previous = state = None
        if rsi is not None and previous is not None:
            if previous >= 80 > rsi:
                state = 'DOWN'
            elif previous <= 20 < rsi:
                state = 'UP'
        row['M7'] = state
        row['M8'] = state if rsi is not None and 20 < rsi < 80 else None
        previous, previous_end = rsi, end
    return times[:], values


def result_table(rows):
    header = '|策略|方向|分段|胜/总笔数|胜率|含费净收益U|投入收益率|最大回撤U|最长连败|\n'
    header += '|---|---|---|---|---|---|---|---|---|\n'
    for r in rows:
        header += (f"|{r['strategy']}|{r['side']}|{r['split']}|{r['wins']}/{r['n']}|"
                   f"{base.pct(r['winRate'])}|{r['netPnl']:.3f}|{base.pct(r['roi'])}|"
                   f"{r['maxDrawdown']:.3f}|{r['maxConsecutiveLosses']}|\n")
    return header


def write_reports(results, manifest, out):
    prior = base.load(base.OUT / 'results.json')
    primary = lambda r: r['delayMs'] == 300 and r['odds'] == 'gross_le_050' and r['sizing'] == 'fixed'
    selected = [r for r in prior if primary(r) and r['strategy'] in ('M1', 'M2') and r['split'] == 'test']
    selected += [r for r in results if primary(r) and r['split'] == 'test']
    text = '# RSI回穿后持续方向与有限马丁（T066）\n\n'
    text += ('本轮仅离线验证，不改纸上/实盘。上一轮六项已完成首轮，不代表穷尽参数。\n\n'
             '- M7：1分钟Wilder RSI(14)由≥80降至<80开始买DOWN；由≤20升至>20开始买UP；其余穿越不改变方向，持续至相反的入场穿越。\n'
             '- M8：切换方式同M7，但RSI≤20或≥80时暂停新入场，保留方向；回到20–80再恢复。\n'
             '- 未观察到首次有效回穿前不猜初始方向；分钟缺口清空状态；只用已收盘K线。做空指买DOWN合约。\n'
             '- M1旧版在触及极值时切换，M2旧版只在回穿后60秒内有效且附带收盘方向条件，均不同于M7/M8。\n\n')
    text += (f"数据沿用T065冻结的{manifest['settledMarkets']}个官方结算市场：{manifest['firstMarket']}至{manifest['lastMarket']}，"
             '中间有缺口，非采集到11月。前60%/中20%/末20%按市场时间切分；末段曾用于其他研究，不是独立未来检验。\n\n'
             '剩余270–30秒、决策卖一0.30–0.50、1U含费预算、订单延迟300ms、盘口源/接收时间年龄≤2秒、每场首次合格机会，持有官方结算。'
             '使用实际收到结果的时间与结束+60秒的较晚者决定下次可交易时间；不因后验结果追入更好价。'
             '历史费用假设每份0.07*p*(1-p)，并非当前收费声明；1U不代表满足实盘最低金额。\n\n'
             '## 与旧版末段固定金额对照\n\n')
    text += result_table(selected)
    text += '\n## 新版跨段固定金额\n\n'
    text += result_table([r for r in results if primary(r) and r['split'] != 'all'])
    text += '\n## 发送前限制每档含费成本≤0.50\n\n'
    text += ('这组订单上限在决策时确定，严格于名义价≤0.50；不是事后剔除高成本交易。'
             '改变上限会改变首个入场机会和等待结算后的可交易序列，需独立回放。\n\n')
    text += result_table([r for r in results if r['delayMs'] == 300 and r['odds'] == 'net_cost_le_050'
                          and r['sizing'] == 'fixed' and r['split'] in ('validation', 'test')])
    text += '\n## 整段11单压力\n\n'
    text += '|策略|方向|笔数|净收益U|最大回撤U|最长连败|最大注额U|11单打穿|本金停止|\n|---|---|---|---|---|---|---|---|---|\n'
    for r in results:
        if r['delayMs'] == 300 and r['odds'] == 'gross_le_050' and r['sizing'] == 'martin11_2047' and r['split'] == 'all':
            text += f"|{r['strategy']}|{r['side']}|{r['n']}|{r['netPnl']:.3f}|{r['maxDrawdown']:.3f}|{r['maxConsecutiveLosses']}|{r['maxBudget']:.0f}|{r['capLossCycles']}|{r['rejections'].get('bankroll_stop', 0)}|\n"
    qualified = []
    for strategy in NAMES:
        for side in ('UP', 'DOWN'):
            rr = [r for r in results if primary(r) and r['strategy'] == strategy and r['side'] == side and r['split'] in ('validation', 'test')]
            if len(rr) == 2 and all(r['n'] >= 100 and r['netPnl'] > 0 for r in rr):
                qualified.append(strategy + '/' + side)
    text += '\n## 判断与限制\n\n'
    text += ('达到验证段/末段各≥100笔且固定金额净盈利的组：' + '、'.join(qualified) if qualified else
             '没有新版方向同时满足验证段/末段各≥100笔且固定金额净盈利的初步门槛。') + '\n'
    text += ('小样本盈利和某条倍投曲线盈利不能证明信号有优势；应先看固定金额跨段表现，再看连败、深度、有限本金。'
             'M7/M8各方向是独立账户，不可将两边收益简单相加。历史K线不证明实盘公布延迟；缺失盘口不补造。\n\n'
             '净含费成本≤0.50、0/1000ms延迟、双倍费用、+300秒结算等待及100U有限本金的完整结果在results.json，'
             '逐笔在sizing_ledgers.json.gz；各策略独立报告如下。\n\n'
             '- [M7 回穿后持续方向](M7_回穿后持续方向.md)\n'
             '- [M8 极值区暂停](M8_极值区暂停.md)\n'
             '- [T065六项首轮](../t065_martingale_candidates_20261118/六类低价策略与有限马丁验证.md)\n\n'
             '- [网络资料与下一步讨论](../../../docs/马丁策略网络资料与候选讨论_20261118.md)\n\n'
             '复现：在arbitrage-research目录运行`python3 scripts/analysis/analyze_rsi_persistent_crossings.py`；'
             '只重算资金结果可加`--reuse-entries`。该脚本拒绝与T065不同的缓存清单，不刷新或覆盖T065。\n')
    (out / 'RSI回穿持续方向验证.md').write_text(text, encoding='utf-8')
    for strategy, name in NAMES.items():
        filename = 'M7_回穿后持续方向.md' if strategy == 'M7' else 'M8_极值区暂停.md'
        body = f'# {strategy} {name}\n\n规则、数据、费用与局限见[总报告](RSI回穿持续方向验证.md)。\n\n'
        body += result_table([r for r in results if primary(r) and r['strategy'] == strategy])
        (out / filename).write_text(body, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-entries', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    prior_manifest = base.load(base.OUT / 'manifest.json')
    cohort = base.load(base.OUT / 'market_coverage.json')
    labels = base.load(base.OUT / 'official_label_arrivals.json')['markets']
    con = sqlite3.connect(f'file:{base.ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    sources = [list(r) for r in con.execute('SELECT path,size,mtime_ns FROM source_files ORDER BY path')]
    if sources != prior_manifest['sourceFiles']:
        raise RuntimeError('T065 normalized cache changed; freeze a new comparison cohort before replay')
    markets = []
    for row in cohort:
        m = dict(con.execute('SELECT market_id,start_us,end_us,winner FROM markets WHERE market_id=?', (row['marketId'],)).fetchone())
        if m['winner'] != labels[m['market_id']]['winner']:
            raise RuntimeError('Official winner mismatch')
        m['label_available_us'] = row['labelAvailableUs']
        markets.append(m)
    if len(markets) != prior_manifest['settledMarkets']:
        raise RuntimeError('Frozen cohort mismatch')
    indicators = add_crossing_states(base.build_indicators(base.load(base.OUT / 'binance_official_1m.json.gz')['bars']))
    if args.reuse_entries:
        cached = base.load(OUT / 'manifest.json')
        if cached['version'] != VERSION or cached['sourceFiles'] != sources:
            raise RuntimeError('Entry cache mismatch')
        entries = base.load(OUT / 'entries.json.gz')
        audit = cached['extraction']
    else:
        entries, audit, _ = base.extract_entries(con, markets, {}, indicators, None, OUT, ('M7', 'M8'))
    con.close()
    manifest = {**prior_manifest, 'version': VERSION, 'parentVersion': prior_manifest['version'],
                'extraction': audit, 'model': None, 'cross': None,
                'candidates': NAMES, 'parentOutput': str(base.OUT)}
    base.dump(OUT / 'manifest.json', manifest)
    results, _ = base.analyze(entries, [], OUT)
    keys = [(r['strategy'], r['side'], r['delayMs'], r['odds'], r['marketId']) for r in entries]
    violations = sum(not (r['bookReceivedUs'] <= r['executionUs']
                          and 0 <= r['executionUs'] - r['bookSourceUs'] <= 2 * base.SECOND
                          and r['fixedFill']['cost'] <= 1 + 1e-9
                          and r['fixedFill']['worst'] <= r['priceCap'] + 1e-9) for r in entries)
    if len(keys) != len(set(keys)) or violations:
        raise RuntimeError('Duplicate entry or timestamp/budget/cap invariant failed')
    base.dump(OUT / 'audit.json', {'entries': len(entries), 'uniqueEntries': len(set(keys)),
                                  'timestampBudgetCapViolations': violations, 'scenarios': len(results)})
    if any(r['odds'] == 'net_cost_le_050' and r['feeMultiplier'] == 1 and r['negativeWinningCycles'] for r in results):
        raise RuntimeError('Net-odds winning-cycle invariant failed')
    write_reports(results, manifest, OUT)
    print({'markets': len(markets), 'entries': len(entries), 'scenarios': len(results), 'out': str(OUT)}, flush=True)


if __name__ == '__main__':
    main()

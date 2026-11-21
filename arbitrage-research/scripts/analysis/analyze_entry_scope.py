#!/usr/bin/env python3
"""T074: paired entry-range ablations; frozen sources and offline accounts."""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3

import analyze_martingale_candidates as base
import analyze_supertrend_direction as st

OUT = base.ROOT / 'outputs/research_tests/t074_entry_scope_20261121'
PLAN = base.ROOT / 'docs/入场范围放宽对照计划_T074_20261121.md'
PROFILES = {
    'baseline': ('原条件', .30, 30, 270),
    'price_only': ('只取消0.30下限', 0., 30, 270),
    'time_only': ('只放开时间', .30, 0, 300),
    'price_time': ('取消下限和时间窗口', 0., 0, 300),
}
RULES = ('S1_n10', 'D1')
SIZINGS = (('fixed', 1, 100.), ('martin11_100', 11, 100.), ('martin11_2047', 11, 2047.))
ODDS = ('gross_le_050', 'net_cost_le_050')


def allows(profile, ask, remaining):
    _, floor, lo, hi = PROFILES[profile]
    return 0 < ask <= .5 + 1e-9 and ask >= floor - 1e-9 and remaining > 0 and lo <= remaining <= hi


def available(indicators, at, lag_ms):
    times, rows = indicators
    j = bisect_right(times, at - lag_ms * 1000) - 1
    if j >= 0 and 0 <= at - times[j] < base.MINUTE:
        return rows[j]
    return None


def signals(row, xc, anchor, remaining):
    if row is None:
        return {}
    result = {'S1_n10': row['supertrends']['n10']['flip']}
    if xc is not None and anchor is not None and remaining > 0:
        side = 'UP' if xc[2] > anchor else 'DOWN' if xc[2] < anchor else None
        score = st.distance_time_score(xc[2], anchor, row['supertrends']['primary']['atr'], remaining, side) if side else None
        if score is not None and score >= 1:
            result['D1'] = side
    return result


def extract_market(market, split, rows, xs, indicators, opens):
    books = {s: [r for r in rows if r['outcome'] == s] for s in ('UP', 'DOWN')}
    times = {s: [r['received_us'] for r in books[s]] for s in books}
    xtimes = [r[0] for r in xs]
    seen, entries, counts = set(), [], Counter()
    anchor = opens.get(market['start_us'])
    for at in range(market['start_us'], market['end_us'], base.SECOND):
        remaining = (market['end_us'] - at) / base.SECOND
        xc = base.cross_at(xs, xtimes, at)
        active = []
        for lag in (0, 1000):
            row = available(indicators, at, lag)
            for rule, side in signals(row, xc, anchor, remaining).items():
                if side:
                    active.append((rule, side, lag, row))
        if not active:
            continue
        for side in ('UP', 'DOWN'):
            selected = [a for a in active if a[1] == side]
            if not selected:
                continue
            decision = base.book_at(books[side], times[side], at)
            if decision is None:
                counts['decision_missing_or_stale_book'] += 1
                continue
            ask = float(decision['best_ask'])
            profiles = [p for p in PROFILES if allows(p, ask, remaining)]
            if not profiles:
                continue
            dlevels = [[float(p), float(q)] for p, q in json.loads(decision['asks_json'])]
            for delay in (300, 1000):
                execution = at + delay * 1000
                book = base.book_at(books[side], times[side], execution) if execution < market['end_us'] else None
                levels = [[float(p), float(q)] for p, q in json.loads(book['asks_json'])] if book else []
                for odds in ODDS:
                    cap = .5 if odds == 'gross_le_050' else base.probability_price_cap(.55)
                    planned = [r for r in dlevels if 0 < r[0] <= cap + 1e-10 and r[1] > 0]
                    if base.buy_budget(planned, 1., cap=cap) is None:
                        continue
                    actual = [r for r in levels if 0 < r[0] <= cap + 1e-10 and r[1] > 0]
                    fill = base.buy_budget(actual, 1., cap=cap) if book else None
                    for rule, _, lag, row in selected:
                        for profile in profiles:
                            flag = (rule, profile, side, lag, delay, odds)
                            if flag in seen:
                                continue
                            seen.add(flag)
                            if execution >= market['end_us']:
                                counts['execution_at_or_after_close'] += 1
                                continue
                            if book is None or fill is None:
                                counts['execution_missing_or_stale_book' if book is None else 'execution_insufficient_depth'] += 1
                                continue
                            atr = row['supertrends']['n10' if rule == 'S1_n10' else 'primary']['atr']
                            entries.append({
                                'strategy': rule, 'profile': profile, 'publicationLagMs': lag,
                                'marketId': market['market_id'], 'startUs': market['start_us'],
                                'endUs': market['end_us'], 'split': split, 'side': side,
                                'winner': market['winner'], 'won': side == market['winner'],
                                'labelAvailableUs': market['label_available_us'],
                                'decisionUs': at, 'executionUs': execution, 'remaining': remaining,
                                'delayMs': delay, 'odds': odds, 'levels': actual,
                                'decisionLevels': planned, 'priceCap': cap, 'fixedFill': fill,
                                'decisionAsk': ask, 'decisionBookSourceUs': decision['source_us'],
                                'decisionBookReceivedUs': decision['received_us'],
                                'bookSourceUs': book['source_us'], 'bookReceivedUs': book['received_us'],
                                'indicatorBarEndUs': row['barEndUs'],
                                'atr': atr, 'flip': row['supertrends']['n10']['flip'] if rule == 'S1_n10' else None,
                                'spotPrice': xc[2] if xc else None, 'binanceAnchorOpen': anchor,
                                'distanceTimeScore': st.distance_time_score(xc[2], anchor, atr, remaining, side)
                                if xc is not None and anchor is not None else None,
                                'spotSourceUs': xc[1] if xc else None,
                                'spotReceivedUs': xc[0] if xc else None,
                            })
    return entries, counts


def audit(entries):
    keys = set()
    for e in entries:
        assert allows(e['profile'], e['decisionAsk'], e['remaining'])
        assert e['startUs'] <= e['decisionUs'] <= e['executionUs'] < e['endUs']
        assert e['indicatorBarEndUs'] + e['publicationLagMs'] * 1000 <= e['decisionUs'] < e['indicatorBarEndUs'] + base.MINUTE
        for time_key, at_key in (('decisionBookSourceUs', 'decisionUs'), ('decisionBookReceivedUs', 'decisionUs'),
                                 ('bookSourceUs', 'executionUs'), ('bookReceivedUs', 'executionUs')):
            assert 0 <= e[at_key] - e[time_key] <= 2 * base.SECOND
        if e['strategy'] == 'D1':
            assert all(e[k] is not None and 0 <= e['decisionUs'] - e[k] <= 2 * base.SECOND
                       for k in ('spotSourceUs', 'spotReceivedUs'))
            assert e['distanceTimeScore'] is not None and e['distanceTimeScore'] >= 1
        else:
            assert e['flip'] == e['side']
        assert e['fixedFill']['worst'] <= e['priceCap'] + 1e-9
        assert e['fixedFill']['cost'] <= 1 + 1e-9
        key = tuple(e[k] for k in ('strategy', 'profile', 'marketId', 'side', 'publicationLagMs', 'delayMs', 'odds'))
        assert key not in keys
        keys.add(key)
    return {'entries': len(entries), 'timestampPriceBudgetDuplicateViolations': 0}


def summarize(entries):
    groups = defaultdict(list)
    for e in entries:
        groups[(e['strategy'], e['profile'], e['publicationLagMs'], e['delayMs'], e['odds'])].append(e)
    results, ledgers = [], {}
    for rule in RULES:
        for profile in PROFILES:
            for lag in (0, 1000):
                for delay in (300, 1000):
                    for odds in ODDS:
                        candidates = groups[(rule, profile, lag, delay, odds)]
                        for split in ('all', 'train', 'validation', 'test'):
                            selected = candidates if split == 'all' else [e for e in candidates if e['split'] == split]
                            for sizing, rounds, capital in SIZINGS:
                                summary, ledger = base.replay(selected, rounds, capital)
                                summary.update(strategy=rule, profile=profile, publicationLagMs=lag,
                                               delayMs=delay, odds=odds, split=split, side='BOTH', sizing=sizing,
                                               qualifiedSignals=len(selected))
                                results.append(summary)
                                key = '|'.join(map(str, (rule, profile, lag, delay, odds, split, sizing)))
                                ledgers[key] = ledger
    return results, ledgers


def reproduce(results):
    checked = []
    for rule, lag, folder in (
        ('S1_n10', 0, 't070_supertrend_direction_20261120'),
        ('D1', 1000, 't071_supertrend_robustness_20261120'),
    ):
        original = base.load(base.ROOT / 'outputs/research_tests' / folder / 'results.json')
        for r in results:
            if not (r['strategy'] == rule and r['profile'] == 'baseline' and r['publicationLagMs'] == lag):
                continue
            if rule == 'D1' and r['sizing'] == 'martin11_100':
                # T071 never published this capital scenario; calculate it here
                # without presenting it as an already existing reference.
                continue
            matches = [o for o in original if all(o.get(k) == r[k] for k in ('side', 'split', 'sizing', 'delayMs', 'odds'))
                       and o.get('strategy', o.get('rule')) == rule
                       and (rule != 'D1' or o.get('publicationLagMs') == lag)]
            if len(matches) != 1:
                raise RuntimeError(f'baseline match failure: {rule} {r["split"]} {r["sizing"]}: {len(matches)}')
            for k in ('n', 'wins', 'netPnl', 'spent', 'fees', 'maxConsecutiveLosses', 'maxDrawdown', 'maxBudget', 'capLossCycles', 'negativeWinningCycles'):
                if abs(r[k] - matches[0][k]) > 1e-8:
                    raise RuntimeError(f'baseline difference {rule} {k}: {r[k]} vs {matches[0][k]}')
            checked.append((rule, r['split'], r['sizing'], r['delayMs'], r['odds']))
    return {'matchingScenarios': len(checked), 'matchingFieldsPerScenario': 10}


def diagnostics(entries, ledgers):
    rows = []
    for rule in RULES:
        lookup = {(e['marketId'], e['side'], e['executionUs']): e for e in entries
                  if e['strategy'] == rule and e['profile'] == 'price_time'
                  and e['publicationLagMs'] == 1000 and e['delayMs'] == 300 and e['odds'] == 'gross_le_050'}
        groups = defaultdict(lambda: {'n': 0, 'wins': 0, 'netPnl': 0.})
        for trade in ledgers[f'{rule}|price_time|1000|300|gross_le_050|all|fixed']:
            e = lookup[(trade['marketId'], trade['side'], trade['executionUs'])]
            time_band = '开场30秒' if e['remaining'] > 270 else '最后30秒' if e['remaining'] < 30 else '原时间窗'
            price_band = '低于0.30' if e['decisionAsk'] < .3 else '0.30–0.50'
            group = groups[(time_band, price_band)]
            group['n'] += 1
            group['wins'] += int(trade['won'])
            group['netPnl'] += trade['pnl']
        for (time_band, price_band), metrics in groups.items():
            rows.append({'strategy': rule, 'timeBand': time_band, 'priceBand': price_band, **metrics})
    return rows


def render(results, check, diagnostic_rows):
    lines = ['# T074：入场价格下限与时间窗口放宽对照', '',
             '双向共享账户，规则为1分钟Supertrend ATR10×3真实翻转和D1距离时间。不是持续盲猜，不改变实盘。', '',
             '[冻结计划](../../../docs/入场范围放宽对照计划_T074_20261121.md)；[预选总清单](../../../docs/双向策略预选总清单_20261120.md)。', '',
             '使用原冻结3752个官方结算市场：北京时间2026-08-21 19:00至2026-10-03 01:50，20个活跃日期、有缺口，未加入新行情。60/20/20旧时间切分已被反复查看。',
             '盘口决策与执行源/接收年龄均≤2秒；每份最高0.50，逐档检查整笔预算深度。固定1U含旧冻结费用0.07×p×(1-p)，不代表当前费率或实盘最小订单。',
             '全时间为[start,end)，每秒观察，订单到达必须早于收盘；不把0元当可买价。翻转仍在原分钟边界后60秒过期。主表K线收盘后1秒可用、下单300ms；旧0ms只供复现。',
             '双向共用一条资金/倍投序列，持有到官方结算且结束后至少60秒才再用。无历史盘口/信号则跳过；大额每一级重查深度。回撤按结算权益，不含持仓盘中浮亏。', '',
             f'原条件复现：{check["matchingScenarios"]}组×{check["matchingFieldsPerScenario"]}项全部一致。D1原报告没有100U倍投账本，该项本轮新增，不计入旧结果复现。',
             '各时间分段独立回放并重置资金/级数；分段账本不等于全段账本直接切片。', '',
             '## 本轮结论', '',
             '- ATR10×3翻转可以保留为候选；放开下限和时间让机会增加，但固定金额全段与末段表现变弱，不支持直接用完全放宽版替换原版。',
             '- 只放开时间可以作为研究备选，原0.30下限仍应保留；增加的开场机会整体没有显示优势。',
             '- D1完全放宽版固定金额严重亏损、11级/2047U出现11级失败及后续资金不足；不支持为了加密交易而取消所有范围。',
             '- 如果继续研究翻转倍投，原条件与“含费每份≤0.50”的保守版本应并列。倍投累计收益不代替固定金额信号检验。',
             '- 历史仅49笔原翻转主账本，仍不能证明未来连亏上限；现有末段已多次查看，需冻结规则后看新行情。', '']
    for odds in ODDS:
        lines += ['## ' + ('名义价格不超过0.50' if odds == 'gross_le_050' else '含费每份成本不超过0.50'), '',
                  '|规则/范围|胜/笔数|胜率|固定净收益U|最长连亏|验证笔数/收益|末段笔数/收益|1秒订单末段笔数/收益|',
                  '|---|---:|---:|---:|---:|---:|---:|---:|']
        def pick(rule, profile, split='all', sizing='fixed', delay=300):
            return next(r for r in results if (r['strategy'], r['profile'], r['publicationLagMs'], r['delayMs'], r['odds'], r['split'], r['sizing'])
                        == (rule, profile, 1000, delay, odds, split, sizing))
        for rule in RULES:
            for profile, (name, *_rest) in PROFILES.items():
                r, v, t, stress = pick(rule, profile), pick(rule, profile, 'validation'), pick(rule, profile, 'test'), pick(rule, profile, 'test', delay=1000)
                wr = f'{r["winRate"]:.1%}' if r['winRate'] is not None else '—'
                lines.append(f'|{rule}/{name}|{r["wins"]}/{r["n"]}|{wr}|{r["netPnl"]:+.3f}|{r["maxConsecutiveLosses"]}|{v["n"]}/{v["netPnl"]:+.3f}|{t["n"]}/{t["netPnl"]:+.3f}|{stress["n"]}/{stress["netPnl"]:+.3f}|')
        lines += ['', '### 有限11级倍投', '',
                  '|规则/范围|本金U|笔数|净收益U|最大回撤U|最大单注U|最长连亏|11级失败轮|资金不足停止|赢后周期仍亏|深度拒绝|',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
        for rule in RULES:
            for profile, (name, *_rest) in PROFILES.items():
                for sizing in ('martin11_100', 'martin11_2047'):
                    r = pick(rule, profile, sizing=sizing)
                    rejected = sum(r['rejections'].get(k, 0) for k in ('insufficient_decision_depth', 'insufficient_full_stake_depth'))
                    lines.append(f'|{rule}/{name}|{r["startingBankroll"]:.0f}|{r["n"]}|{r["netPnl"]:+.3f}|{r["maxDrawdown"]:.3f}|{r["maxBudget"]:.0f}|{r["maxConsecutiveLosses"]}|{r["capLossCycles"]}|{r["rejections"].get("bankroll_stop", 0)}|{r["negativeWinningCycles"]}|{rejected}|')
        lines += ['']
    lines += ['## 两项都放开后的实际固定订单分组', '',
              '主口径：1秒K线公布、300ms订单、名义价≤0.50；按实际双向共享固定账本归组。是描述性拆解，不是事前新增过滤器的独立验证。', '',
              '|规则|时间|卖一价|胜/笔数|净收益U|', '|---|---|---|---:|---:|']
    for d in diagnostic_rows:
        lines.append(f'|{d["strategy"]}|{d["timeBand"]}|{d["priceBand"]}|{d["wins"]}/{d["n"]}|{d["netPnl"]:+.3f}|')
    lines += ['', 'D1尾段低价失败不能仅凭此表归因于Binance/Chainlink基准差异；还需逐笔对照真实官方参考价。没有把推测当作已证实原因。', '',
              '## 阅读边界', '',
              '每组独立寻找最早机会，放宽条件可能把原来稍后成交的机会提前替换，并不保证交易数单调增加。固定与倍投的大额深度拒绝也会改变实际成交集合。',
              '0.50名义成交加费用后成本可能超过0.50，故倍投某轮赢后整个周期仍可能亏；含费≤0.50为独立严格对照。没有第11级失败不等于已验证未来不会出现。',
              '完整结果见results.json；逐笔账户见sizing_ledgers.json.gz；候选及特征见entries.json.gz；来源与SHA256见manifest.json；审计见audit.json。',
              '未对所有范围/时序/费用事后挑最高收益宣称有效，未以旧历史后20%称新样本外。']
    (OUT / '入场范围放宽对照.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    source_paths = [base.OUT / name for name in ('manifest.json', 'market_coverage.json', 'official_label_arrivals.json', 'binance_official_1m.json.gz', 'spot_projection.json.gz')]
    source_paths += [Path(__file__).resolve(), PLAN,
                     Path(base.__file__), Path(st.__file__),
                     base.ROOT / 'outputs/research_tests/t070_supertrend_direction_20261120/results.json',
                     base.ROOT / 'outputs/research_tests/t071_supertrend_robustness_20261120/results.json']
    hashes = {str(p.relative_to(base.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    parent = base.load(base.OUT / 'manifest.json')
    con = sqlite3.connect(f'file:{base.ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    assert [list(r) for r in con.execute('SELECT path,size,mtime_ns FROM source_files ORDER BY path')] == parent['sourceFiles']
    cohort = base.load(base.OUT / 'market_coverage.json')
    labels = base.load(base.OUT / 'official_label_arrivals.json')['markets']
    bars = base.load(base.OUT / 'binance_official_1m.json.gz')['bars']
    indicators = st.build_indicators(bars)
    opens = {r['open_us']: r['open'] for r in bars}
    crosses = base.load(base.OUT / 'spot_projection.json.gz')['markets']
    entries, counts = [], Counter()
    for i, c in enumerate(cohort):
        m = dict(con.execute('SELECT market_id,start_us,end_us,winner FROM markets WHERE market_id=?', (c['marketId'],)).fetchone())
        assert m['winner'] == labels[m['market_id']]['winner']
        assert m['end_us'] - m['start_us'] == 5 * base.MINUTE and m['start_us'] % (5 * base.MINUTE) == 0
        m['label_available_us'] = c['labelAvailableUs']
        rows = [dict(r) for r in con.execute('SELECT source_us,received_us,outcome,best_ask,asks_json FROM snapshots WHERE market_id=? ORDER BY received_us', (m['market_id'],))]
        selected, rejected = extract_market(m, base.market_split(i, len(cohort)), rows, crosses.get(m['market_id'], []), indicators, opens)
        entries.extend(selected)
        counts.update(rejected)
        if (i + 1) % 200 == 0 or i + 1 == len(cohort):
            print(f'markets {i+1}/{len(cohort)} entries {len(entries)}', flush=True)
    con.close()
    checks = audit(entries)
    results, ledgers = summarize(entries)
    checks['baselineReproduction'] = reproduce(results)
    checks['cohortMarkets'] = len(cohort)
    for p in source_paths:
        assert hashlib.sha256(p.read_bytes()).hexdigest() == hashes[str(p.relative_to(base.ROOT))]
    base.dump(OUT / 'entries.json.gz', entries)
    base.dump(OUT / 'results.json', results)
    base.dump(OUT / 'sizing_ledgers.json.gz', ledgers)
    diagnostic_rows = diagnostics(entries, ledgers)
    base.dump(OUT / 'diagnostics.json', diagnostic_rows)
    base.dump(OUT / 'audit.json', {**checks, 'rejections': dict(counts)})
    base.dump(OUT / 'manifest.json', {'version': 't074-v1-frozen-range-ablation', 'runDate': '2026-11-21',
                                     'inputs': hashes, 'profiles': PROFILES, 'rules': RULES,
                                     'settledMarkets': len(cohort), 'publicationLagsMs': [0, 1000],
                                     'orderDelaysMs': [300, 1000], 'side': 'BOTH', 'newOutOfSampleData': False})
    render(results, checks['baselineReproduction'], diagnostic_rows)
    print(json.dumps({'audit': checks, 'output': str(OUT)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

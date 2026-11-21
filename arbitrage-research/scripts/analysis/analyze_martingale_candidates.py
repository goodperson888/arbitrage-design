#!/usr/bin/env python3
"""Six frozen low-price candidates, causal books, and finite-bankroll sizing.

Offline only. Generated datasets/results are separate from all live settings.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import zlib
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs/research_tests/t065_martingale_candidates_20261118'
VERSION = 'six-candidates-causal-v3-decision-limits'
SECOND = 1_000_000
MINUTE = 60 * SECOND
NAMES = {
    'M1': 'RSI持续反向', 'M2': 'RSI回穿确认', 'M3': '顺势回撤恢复',
    'M4': '开盘线突破失败', 'M5': 'Binance短动量与合约滞后', 'M6': '概率折价',
}


def dump(path, value):
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if str(path).endswith('.gz'):
        with gzip.open(path, 'wt', encoding='utf-8') as f:
            f.write(text)
    else:
        Path(path).write_text(text + '\n', encoding='utf-8')


def load(path):
    with gzip.open(path, 'rt', encoding='utf-8') if str(path).endswith('.gz') else open(path, encoding='utf-8') as f:
        return json.load(f)


def local(us):
    return datetime.fromtimestamp(us / SECOND, ZoneInfo('Asia/Shanghai')).isoformat()


def fingerprint(paths):
    records = [(str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(paths)]
    return hashlib.sha256(json.dumps(records).encode()).hexdigest(), records


def official_candles(start_us, end_us, out):
    """Download public closed 1m candles, bounded concurrency, reusable cache."""
    path = out / 'binance_official_1m.json.gz'
    start = (start_us // MINUTE - 360) * 60_000
    end = (end_us // MINUTE + 1) * 60_000
    if path.exists():
        cached = load(path)
        if cached['startMs'] <= start and cached['endMs'] >= end:
            return cached['bars'], cached['audit']
    windows = [(at, min(end - 1, at + 1000 * 60_000 - 1)) for at in range(start, end, 1000 * 60_000)]

    def fetch(window):
        query = urllib.parse.urlencode({'symbol': 'BTCUSDT', 'interval': '1m',
                                        'startTime': window[0], 'endTime': window[1], 'limit': 1000})
        error = None
        for attempt in range(2):
            try:
                with urllib.request.urlopen('https://data-api.binance.vision/api/v3/klines?' + query, timeout=20) as f:
                    rows = json.load(f)
                if not isinstance(rows, list):
                    raise ValueError('non-list public kline response')
                bars = [{'open_us': int(r[0]) * 1000, 'end_us': (int(r[0]) + 60_000) * 1000,
                         'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]),
                         'close': float(r[4])} for r in rows]
                return bars, None
            except Exception as exc:
                error = type(exc).__name__ + ': ' + str(exc)[:200]
                if attempt == 0:
                    time.sleep(.3)
        return [], {'startMs': window[0], 'endMs': window[1], 'error': error}

    bars, failures = {}, []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch, w) for w in windows]
        for i, future in enumerate(as_completed(futures), 1):
            values, error = future.result()
            bars.update({r['open_us']: r for r in values})
            if error:
                failures.append(error)
            if i % 10 == 0 or i == len(futures):
                print('official candles', i, '/', len(futures), 'bars', len(bars), flush=True)
    values = sorted(bars.values(), key=lambda r: r['open_us'])
    audit = {'source': 'Binance public /api/v3/klines BTCUSDT 1m', 'bars': len(values),
             'failedRequests': failures, 'requests': len(windows),
             'gaps': sum(b['open_us'] != a['end_us'] for a, b in zip(values, values[1:]))}
    if not values:
        raise RuntimeError('No official candles available; RSI cannot be honestly tested.')
    dump(path, {'startMs': start, 'endMs': end, 'bars': values, 'audit': audit})
    return values, audit


def settlement_arrivals(out):
    """Recover when an official result was actually received, not its end time."""
    paths = sorted((ROOT / 'data/live_monitor/events').glob('date=*/hour=*/part-*.ndjson*'))
    paths += sorted((ROOT / 'data/cross_market_validation/labels').glob('date=*/hour=*/part-*.ndjson*'))
    key, _ = fingerprint(paths)
    cached_path = out / 'official_label_arrivals.json'
    if cached_path.exists():
        cached = load(cached_path)
        if cached['sourceFingerprint'] == key:
            return cached['markets'], cached['audit']
    arrivals, audit = {}, Counter()
    for index, p in enumerate(paths, 1):
        try:
            with gzip.open(p, 'rt') if p.suffix == '.gz' else p.open() as f:
                for line in f:
                    if '"SETTLEMENT"' not in line and '"MARKET_LABEL"' not in line:
                        continue
                    try:
                        r = json.loads(line)
                        winner = (r.get('payload') or {}).get('winnerOutcome')
                        mid, at = r.get('marketId'), int(r.get('receivedTimeUs') or 0)
                        if winner not in ('UP', 'DOWN') or not mid or at <= 0:
                            continue
                        audit['officialLabelRows'] += 1
                        old = arrivals.get(mid)
                        if old and old['winner'] != winner:
                            audit['conflictingOfficialLabels'] += 1
                            raise RuntimeError('Conflicting official labels for ' + mid)
                        if old is None or at < old['availableUs']:
                            arrivals[mid] = {'availableUs': at, 'winner': winner}
                    except (ValueError, TypeError):
                        audit['invalidRows'] += 1
        except (OSError, EOFError, zlib.error):
            audit['damagedFiles'] += 1
        if index % 160 == 0 or index == len(paths):
            print('official result arrival scan', index, '/', len(paths), flush=True)
    result_audit = {**audit, 'markets': len(arrivals), 'sourceFingerprint': key}
    dump(cached_path, {'sourceFingerprint': key, 'markets': arrivals, 'audit': result_audit})
    return arrivals, result_audit


def build_indicators(bars):
    """Wilder RSI(14), closed 5m EMA12/26, trailing closed-minute volatility."""
    fives, bucket = [], []
    for b in bars:
        if bucket and (b['open_us'] != bucket[-1]['end_us'] or b['open_us'] // (5 * MINUTE) != bucket[0]['open_us'] // (5 * MINUTE)):
            bucket = []
        bucket.append(b)
        if len(bucket) == 5 and bucket[0]['open_us'] % (5 * MINUTE) == 0:
            fives.append((b['end_us'], b['close']))
            bucket = []
    five_times, trends = [], []
    segment, e12, e26, prev = [], None, None, None
    for at, close in fives:
        if prev is not None and at - prev != 5 * MINUTE:
            segment, e12, e26 = [], None, None
        segment.append(close)
        if len(segment) == 12:
            e12 = sum(segment[-12:]) / 12
        elif e12 is not None:
            e12 += 2 / 13 * (close - e12)
        if len(segment) == 26:
            e26 = sum(segment[-26:]) / 26
        elif e26 is not None:
            e26 += 2 / 27 * (close - e26)
        five_times.append(at)
        trends.append(None if e26 is None else ('UP' if e12 > e26 else 'DOWN' if e12 < e26 else None))
        prev = at

    times, values = [], []
    closes, changes, rsis = [], [], []
    gain = loss = previous_rsi = None
    state = None
    prev_end = None
    for b in bars:
        if prev_end is not None and b['open_us'] != prev_end:
            closes, changes, rsis = [], [], []
            gain = loss = previous_rsi = state = None
        delta = b['close'] - closes[-1] if closes else None
        closes.append(b['close'])
        rsi = None
        if delta is not None:
            changes.append(delta)
            if len(changes) == 14:
                gain = sum(max(x, 0) for x in changes) / 14
                loss = sum(max(-x, 0) for x in changes) / 14
            elif len(changes) > 14:
                gain = (gain * 13 + max(delta, 0)) / 14
                loss = (loss * 13 + max(-delta, 0)) / 14
            if gain is not None:
                rsi = 50. if gain == loss == 0 else 100. if loss == 0 else 100 - 100 / (1 + gain / loss)
        m2 = m3 = None
        if rsi is not None:
            if rsi >= 80:
                state = 'DOWN'
            elif rsi <= 20:
                state = 'UP'
            if previous_rsi is not None:
                if previous_rsi >= 80 > rsi and delta < 0:
                    m2 = 'DOWN'
                elif previous_rsi <= 20 < rsi and delta > 0:
                    m2 = 'UP'
                j = bisect_right(five_times, b['end_us']) - 1
                trend = trends[j] if j >= 0 and b['end_us'] - five_times[j] < 5 * MINUTE else None
                if trend == 'UP' and previous_rsi < 50 <= rsi and any(x <= 45 for x in rsis[-3:]) and delta > 0:
                    m3 = 'UP'
                elif trend == 'DOWN' and previous_rsi > 50 >= rsi and any(x >= 55 for x in rsis[-3:]) and delta < 0:
                    m3 = 'DOWN'
            previous_rsi = rsi
            rsis.append(rsi)
        sigma = None
        if len(closes) >= 61:
            returns = [math.log(v / u) for u, v in zip(closes[-61:-1], closes[-60:])]
            sigma = max(.00005, math.sqrt(sum(x * x for x in returns) / 60))
        times.append(b['end_us'])
        values.append({'rsi': rsi, 'M1': state, 'M2': m2, 'M3': m3, 'sigma': sigma,
                       'close': b['close'], 'barEndUs': b['end_us']})
        prev_end = b['end_us']
    return times, values


def indicator_at(indicators, at):
    times, values = indicators
    j = bisect_right(times, at) - 1
    return values[j] if j >= 0 and 0 <= at - times[j] < MINUTE else None


def project_cross(out):
    paths = sorted((ROOT / 'data/cross_market_validation/venue_snapshots').glob('date=*/hour=*/part-*.ndjson*'))
    key, sources = fingerprint(paths)
    path = out / 'spot_projection.json.gz'
    if path.exists():
        cached = load(path)
        if cached.get('sourceFingerprint') == key:
            return cached['markets'], cached['audit']
    markets = defaultdict(list)
    audit = Counter()
    damaged = []
    for index, p in enumerate(paths, 1):
        try:
            with gzip.open(p, 'rt') if p.suffix == '.gz' else p.open() as f:
                for line in f:
                    audit['rows'] += 1
                    try:
                        r = json.loads(line)
                        b = (r.get('features') or {}).get('binance') or {}
                        if not b.get('available') or b.get('sourceAgeMs') is None:
                            audit['unavailable'] += 1
                            continue
                        src, rec = int(r['sourceTimeUs']), int(r['receivedTimeUs'])
                        age = float(b['sourceAgeMs'])
                        btc_src = src - round(age * 1000)
                        if age < 0 or btc_src > rec:
                            audit['future_or_negative_age'] += 1
                            continue
                        if rec - btc_src > 2 * SECOND:
                            audit['stale'] += 1
                            continue
                        price, anchor = float(b['price']), float(b['startPrice'])
                        if not all(math.isfinite(x) and x > 0 for x in (price, anchor)):
                            audit['invalid_price'] += 1
                            continue
                        markets[str(r['marketId'])].append([rec, btc_src, price, anchor])
                        audit['accepted'] += 1
                    except (KeyError, TypeError, ValueError):
                        audit['invalid'] += 1
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
            damaged.append({'path': str(p.relative_to(ROOT)), 'error': type(exc).__name__})
        if index % 80 == 0 or index == len(paths):
            print('cross projection', index, '/', len(paths), 'fresh', audit['accepted'], flush=True)
    markets = {mid: sorted(rows, key=lambda x: x[0]) for mid, rows in markets.items()}
    result_audit = {**audit, 'files': len(paths), 'damagedFiles': damaged,
                    'marketCount': len(markets), 'sourceFingerprint': key}
    dump(path, {'sourceFingerprint': key, 'sources': sources, 'markets': markets, 'audit': result_audit})
    return markets, result_audit


def cross_at(rows, times, at):
    j = bisect_right(times, at) - 1
    if j < 0:
        return None
    r = rows[j]
    return r if 0 <= at - r[0] <= 2 * SECOND and 0 <= at - r[1] <= 2 * SECOND else None


def book_at(rows, times, at):
    j = bisect_right(times, at) - 1
    if j < 0:
        return None
    r = rows[j]
    # Reject future source clocks as well as stale received/source timestamps.
    if not (0 <= at - r['received_us'] <= 2 * SECOND and 0 <= at - r['source_us'] <= 2 * SECOND):
        return None
    return r if r['best_ask'] is not None else None


def buy_budget(levels, budget, fee_mult=1., cap=.5):
    remaining, quantity, principal, fee, worst = budget, 0., 0., 0., 0.
    for price, size in sorted((float(p), float(q)) for p, q in levels):
        if not (0 < price <= cap + 1e-10 and size > 0):
            continue
        unit_fee = .07 * fee_mult * price * (1 - price)
        take = math.floor((min(size, remaining / (price + unit_fee)) + 1e-10) * 100) / 100
        if take <= 0:
            continue
        quantity += take
        principal += take * price
        fee += take * unit_fee
        remaining -= take * (price + unit_fee)
        worst = price
        if remaining <= .01 + 1e-9:
            break
    if remaining > .01 + 1e-9 or quantity <= 0:
        return None
    return {'quantity': quantity, 'principal': principal, 'fee': fee, 'cost': principal + fee,
            'vwap': principal / quantity, 'effectivePrice': (principal + fee) / quantity, 'worst': worst}


def probability_price_cap(probability):
    """Known at decision time; enforces edge on every possible filled share."""
    lo, hi = 0., .5
    for _ in range(45):
        mid = (lo + hi) / 2
        if mid + .07 * mid * (1 - mid) <= probability - .05:
            lo = mid
        else:
            hi = mid
    return lo


def probability_feature(cross, indicator, remaining):
    if cross is None or indicator is None or indicator['sigma'] is None or remaining <= 0:
        return None
    z = math.log(cross[2] / cross[3]) / (indicator['sigma'] * math.sqrt(remaining / 60))
    return max(-8., min(8., z))


def predict(model, z):
    if not model or z is None:
        return None
    score = max(-30., min(30., model['intercept'] + model['slope'] * z))
    return 1 / (1 + math.exp(-score))


def fit_probability(markets, crosses, indicators, cutoff):
    rows = []
    for m in markets:
        if max(m['end_us'] + 60 * SECOND, m.get('label_available_us', 0)) >= cutoff:
            continue
        xs = crosses.get(m['market_id'], [])
        times = [x[0] for x in xs]
        items = []
        for remaining in (240, 180, 120, 90, 60, 30):
            at = m['end_us'] - remaining * SECOND
            z = probability_feature(cross_at(xs, times, at), indicator_at(indicators, at), remaining)
            if z is not None:
                items.append((z, int(m['winner'] == 'UP')))
        rows.extend((m['market_id'], z, y, 1 / len(items)) for z, y in items)
    if len({r[0] for r in rows}) < 100:
        return None, {'status': 'insufficient_train_markets', 'rows': len(rows), 'markets': len({r[0] for r in rows})}
    x = np.asarray([[1., r[1]] for r in rows]); y = np.asarray([r[2] for r in rows])
    weights = np.asarray([r[3] for r in rows])
    beta = np.asarray([0., 1.])
    for _ in range(40):
        # Explicit two-feature reductions avoid platform BLAS warnings and make
        # all numerical finiteness checks independent of matrix multiplication.
        p = 1 / (1 + np.exp(-np.clip(beta[0] + x[:, 1] * beta[1], -30, 30)))
        penalty = np.asarray([.01, 1.])
        residual = weights * (p - y)
        gradient = np.asarray([np.sum(residual), np.sum(residual * x[:, 1])]) + penalty * beta
        variance = weights * p * (1 - p)
        cross_sum = np.sum(variance * x[:, 1])
        hessian = np.asarray([[np.sum(variance) + penalty[0], cross_sum],
                              [cross_sum, np.sum(variance * x[:, 1] ** 2) + penalty[1]]])
        delta = np.linalg.solve(hessian, gradient)
        beta -= delta
        beta[1] = max(0., beta[1])
        if not np.all(np.isfinite(beta)):
            raise RuntimeError('Non-finite probability fit')
        if float(np.max(np.abs(delta))) < 1e-8:
            break
    model = {'intercept': float(beta[0]), 'slope': float(beta[1]), 'cutoffUs': cutoff,
             'features': 'log(spot/recorded_start)/(trailing_60_closed_1m_RMS_return*sqrt(remaining/60))',
             'trainingMarketWeight': 'one per market across checkpoints'}
    return model, {'status': 'fit', 'rows': len(rows), 'markets': len({r[0] for r in rows}), **model}


def market_split(index, count):
    return 'train' if index < int(count * .6) else 'validation' if index < int(count * .8) else 'test'


def extract_entries(con, markets, crosses, indicators, model, out,
                    indicator_signal_keys=('M1', 'M2', 'M3'), include_builtin=True,
                    signal_callback=None):
    entries = []
    audit = Counter()
    probability_observations = []
    coverage = []
    for index, m in enumerate(markets):
        split = market_split(index, len(markets))
        rows = [dict(r) for r in con.execute(
            'SELECT source_us,received_us,outcome,best_ask,asks_json FROM snapshots WHERE market_id=? ORDER BY received_us',
            (m['market_id'],))]
        books = {s: [r for r in rows if r['outcome'] == s] for s in ('UP', 'DOWN')}
        book_times = {s: [r['received_us'] for r in books[s]] for s in books}
        xs = crosses.get(m['market_id'], [])
        cross_times = [r[0] for r in xs]
        seen = set()
        market_coverage = Counter()
        for at in range(m['start_us'] + 30 * SECOND, m['end_us'] - 30 * SECOND + 1, SECOND):
            remaining = (m['end_us'] - at) / SECOND
            ind = indicator_at(indicators, at)
            xc = cross_at(xs, cross_times, at)
            market_coverage['rsiReadyGrid'] += int(ind is not None and ind['rsi'] is not None)
            market_coverage['freshSpotGrid'] += int(xc is not None)
            x3 = cross_at(xs, cross_times, at - 3 * SECOND)
            x15 = cross_at(xs, cross_times, at - 15 * SECOND)
            ret3 = 10000 * math.log(xc[2] / x3[2]) if xc is not None and x3 is not None else None
            ret15 = 10000 * math.log(xc[2] / x15[2]) if xc is not None and x15 is not None else None
            z = probability_feature(xc, ind, remaining)
            p_up = predict(model, z)
            if remaining in (240, 180, 120, 90, 60, 30) and p_up is not None:
                up = book_at(books['UP'], book_times['UP'], at)
                dn = book_at(books['DOWN'], book_times['DOWN'], at)
                market_p = None
                if up and dn:
                    a, b = float(up['best_ask']), float(dn['best_ask'])
                    market_p = a / (a + b) if a + b else None
                probability_observations.append({'marketId': m['market_id'], 'split': split,
                    'remaining': remaining, 'pUp': p_up, 'marketPUpAskNormalized': market_p,
                    'y': int(m['winner'] == 'UP')})
            signals = {k: ind[k] if ind else None for k in indicator_signal_keys}
            if include_builtin and xc is not None and ret3 is not None:
                j = bisect_right(cross_times, at - 30 * SECOND)
                k = bisect_right(cross_times, at)
                distances = [10000 * math.log(r[2] / r[3]) for r in xs[max(0, j - 1):k]
                             if at - 30 * SECOND <= r[0] <= at]
                d = 10000 * math.log(xc[2] / xc[3])
                signals['M4'] = ('DOWN' if d <= -.5 and ret3 < 0 and distances and max(distances) >= 2
                                 else 'UP' if d >= .5 and ret3 > 0 and distances and min(distances) <= -2 else None)
            if include_builtin:
                signals['M5'] = 'UP' if ret3 is not None and ret15 is not None and ret3 >= 1 and ret15 >= 1 else (
                    'DOWN' if ret3 is not None and ret15 is not None and ret3 <= -1 and ret15 <= -1 else None)
            if signal_callback is not None:
                signals.update(signal_callback(at, ind, xc, remaining,
                                               cross_rows=xs, ret3=ret3, ret15=ret15) or {})
            for side in ('UP', 'DOWN'):
                decision_book = book_at(books[side], book_times[side], at)
                if decision_book is None:
                    audit['decision_missing_or_stale_book'] += 1
                    continue
                ask = float(decision_book['best_ask'])
                market_coverage['freshBookGrid' + side] += 1
                if not .30 - 1e-9 <= ask <= .50 + 1e-9:
                    continue
                prior = book_at(books[side], book_times[side], at - 3 * SECOND)
                lag_ok = prior is not None and -.01 - 1e-9 <= ask - float(prior['best_ask']) <= .02 + 1e-9
                probability = p_up if side == 'UP' else 1 - p_up if p_up is not None else None
                active = [key for key, direction in signals.items() if direction == side and (key != 'M5' or lag_ok)]
                # The probability edge must be known at decision time, not obtained
                # from a cheaper/future execution book after the order was sent.
                probability_cap = probability_price_cap(probability) if probability is not None else 0.
                decision_levels = [[float(p), float(q)] for p, q in json.loads(decision_book['asks_json'])]
                decision_fill = buy_budget(decision_levels, 1., cap=probability_cap) if probability is not None else None
                if decision_fill and probability - decision_fill['effectivePrice'] >= .05 - 1e-9:
                    active.append('M6')
                if not active:
                    continue
                for delay_ms in (0, 300, 1000):
                    execution = at + delay_ms * 1000
                    book = book_at(books[side], book_times[side], execution)
                    levels = [[float(p), float(q)] for p, q in json.loads(book['asks_json'])] if book else []
                    for key in active:
                        for odds in ('gross_le_050', 'net_cost_le_050'):
                            flag = (key, side, delay_ms, odds)
                            if flag in seen:
                                continue
                            # Limits are fixed before sending. Net-odds orders
                            # cap every share at the fee-adjusted 0.50 boundary.
                            cap = probability_price_cap(.55) if odds == 'net_cost_le_050' else .5
                            if key == 'M6':
                                cap = min(cap, probability_cap)
                            planned_levels = [r for r in decision_levels if 0 < r[0] <= cap + 1e-10 and r[1] > 0]
                            if buy_budget(planned_levels, 1., cap=cap) is None:
                                continue
                            # One attempt per market: an execution miss is not
                            # silently replaced by a more favourable later book.
                            seen.add(flag)
                            if book is None:
                                audit['execution_missing_or_stale_book'] += 1
                                continue
                            key_levels = [r for r in levels if 0 < r[0] <= cap + 1e-10 and r[1] > 0]
                            key_fill = buy_budget(key_levels, 1., cap=cap)
                            if key_fill is None:
                                audit['execution_insufficient_depth'] += 1
                                continue
                            # A cheaper fill after sending remains a real fill;
                            # never remove it using a future minimum VWAP test.
                            entries.append({'strategy': key, 'marketId': m['market_id'], 'startUs': m['start_us'],
                                'endUs': m['end_us'], 'split': split, 'side': side, 'winner': m['winner'],
                                'labelAvailableUs': m.get('label_available_us'),
                                'decisionUs': at, 'executionUs': execution, 'remaining': remaining,
                                'delayMs': delay_ms, 'odds': odds, 'levels': key_levels, 'decisionLevels': planned_levels,
                                'priceCap': cap, 'fixedFill': key_fill,
                                'rsi': ind['rsi'] if ind else None, 'pOutcome': probability,
                                'spotReturn3Bps': ret3, 'spotReturn15Bps': ret15,
                                'bookReceivedUs': book['received_us'], 'bookSourceUs': book['source_us'],
                                'won': side == m['winner']})
        coverage.append({'marketId': m['market_id'], 'startUs': m['start_us'], 'endUs': m['end_us'],
                         'split': split, 'labelAvailableUs': m.get('label_available_us'),
                         'snapshotsUP': len(books['UP']), 'snapshotsDOWN': len(books['DOWN']), **market_coverage})
        if (index + 1) % 400 == 0 or index + 1 == len(markets):
            print('market replay', index + 1, '/', len(markets), 'entries', len(entries), flush=True)
    dump(out / 'entries.json.gz', entries)
    dump(out / 'probability_observations.json.gz', probability_observations)
    dump(out / 'market_coverage.json', coverage)
    return entries, dict(audit), probability_observations


def wilson(wins, n):
    if not n:
        return None
    z = 1.95996398454
    p = wins / n; den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [center - half, center + half]


def replay(entries, rounds=1, bankroll=100., settlement_delay=60, fee_mult=1.):
    """Only settled past outcomes affect the next budget; never scale a fill."""
    pnl = peak = drawdown = fees = spent = cycle_pnl = 0.
    available = step = streak = max_streak = wins = negative_win_cycles = cap_hits = cycles = 0
    losses_followed = losses_followed_by_loss = 0
    previous_won = None
    ledger, reasons, daily = [], Counter(), defaultdict(lambda: [0., 0., 0])
    max_budget = 0.
    for e in sorted(entries, key=lambda r: r['executionUs']):
        if e['executionUs'] < available:
            reasons['waiting_for_settlement'] += 1
            continue
        budget = float(2 ** step)
        if budget > bankroll + pnl + 1e-9:
            reasons['bankroll_stop'] += 1
            break
        planned = buy_budget(e.get('decisionLevels', e['levels']), budget, fee_mult)
        if planned is None:
            reasons['insufficient_decision_depth'] += 1
            continue
        fill = buy_budget(e['levels'], budget, fee_mult)
        if fill is None:
            reasons['insufficient_full_stake_depth'] += 1
            continue
        if fee_mult == 1. and e['odds'] == 'net_cost_le_050' and fill['effectivePrice'] > .50 + 1e-9:
            reasons['resized_price_constraint'] += 1
            continue
        if fee_mult == 1. and e['strategy'] == 'M6' and (e['pOutcome'] is None or e['pOutcome'] - fill['effectivePrice'] < .05 - 1e-9):
            reasons['resized_probability_edge'] += 1
            continue
        net = (fill['quantity'] if e['won'] else 0.) - fill['cost']
        pnl += net; cycle_pnl += net; spent += fill['cost']; fees += fill['fee']
        peak = max(peak, pnl); drawdown = max(drawdown, peak - pnl)
        max_budget = max(max_budget, budget)
        if previous_won is False:
            losses_followed += 1
            losses_followed_by_loss += int(not e['won'])
        previous_won = e['won']
        streak = 0 if e['won'] else streak + 1
        max_streak = max(max_streak, streak)
        wins += int(e['won'])
        available = max(e['endUs'] + settlement_delay * SECOND, e.get('labelAvailableUs') or 0)
        day = local(e['executionUs'])[:10]
        daily[day][0] += net; daily[day][1] += fill['cost']; daily[day][2] += 1
        ledger.append({'marketId': e['marketId'], 'entryTime': local(e['executionUs']),
                       'executionUs': e['executionUs'], 'side': e['side'], 'remaining': e['remaining'],
                       'round': step + 1, 'budget': budget, 'won': e['won'], **fill,
                       'pnl': net, 'cumulativePnl': pnl, 'cashAfterSettlement': bankroll + pnl,
                       'cyclePnlAtTrade': cycle_pnl})
        cap_loss = not e['won'] and step + 1 >= rounds
        if e['won'] or cap_loss:
            cycles += 1
            negative_win_cycles += int(e['won'] and cycle_pnl < -1e-8)
            cap_hits += int(cap_loss and rounds > 1)
            step = 0; cycle_pnl = 0.
        else:
            step += 1
    n = len(ledger)
    prices = [r['vwap'] for r in ledger]
    summary = {'n': n, 'wins': wins, 'losses': n - wins, 'winRate': wins / n if n else None,
               'winRateCI95': wilson(wins, n), 'netPnl': pnl, 'spent': spent, 'fees': fees,
               'roi': pnl / spent if spent else None, 'maxDrawdown': drawdown,
               'maxConsecutiveLosses': max_streak, 'maxBudget': max_budget,
               'lossFollowCount': losses_followed,
               'lossAfterLoss': losses_followed_by_loss / losses_followed if losses_followed else None,
               'closedCycles': cycles, 'negativeWinningCycles': negative_win_cycles,
               'capLossCycles': cap_hits, 'unfinishedCycleTrades': step, 'unfinishedCyclePnl': cycle_pnl,
               'startingBankroll': bankroll, 'endingBankroll': bankroll + pnl,
               'settlementDelaySeconds': settlement_delay, 'feeMultiplier': fee_mult,
               'rejections': dict(reasons), 'averageVwap': sum(prices) / n if n else None,
               'daily': dict(daily)}
    return summary, ledger


def bootstrap_daily_ci(daily):
    if len(daily) < 3:
        return None
    pairs = list(daily.values()); rng = random.Random(20261118)
    values = []
    for _ in range(1200):
        sample = [rng.choice(pairs) for _ in pairs]
        total = sum(r[1] for r in sample)
        values.append(sum(r[0] for r in sample) / total if total else 0.)
    values.sort()
    return [values[30], values[1169]]


def probability_scores(rows):
    result = []
    for split in ('train', 'validation', 'test'):
        selected = [r for r in rows if r['split'] == split]
        comparable = [r for r in selected if r['marketPUpAskNormalized'] is not None]
        buckets = []
        for lo, hi in ((0., .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.000001)):
            group = [r for r in selected if lo <= r['pUp'] < hi]
            buckets.append({'range': [lo, min(hi, 1.)], 'n': len(group),
                            'predicted': sum(r['pUp'] for r in group) / len(group) if group else None,
                            'actualUp': sum(r['y'] for r in group) / len(group) if group else None})
        result.append({'split': split, 'observations': len(selected),
                       'markets': len({r['marketId'] for r in selected}),
                       'accuracy': sum((r['pUp'] >= .5) == bool(r['y']) for r in selected) / len(selected) if selected else None,
                       'brier': sum((r['pUp'] - r['y']) ** 2 for r in selected) / len(selected) if selected else None,
                       'comparableObservations': len(comparable),
                       'modelBrierComparable': sum((r['pUp'] - r['y']) ** 2 for r in comparable) / len(comparable) if comparable else None,
                       'askNormalizedBrierComparable': sum((r['marketPUpAskNormalized'] - r['y']) ** 2 for r in comparable) / len(comparable) if comparable else None,
                       'calibration': buckets})
    return result


def analyze(entries, observations, out):
    groups = defaultdict(list)
    for e in entries:
        groups[(e['strategy'], e['side'], e['delayMs'], e['odds'])].append(e)
    results, ledgers, strata = [], {}, []
    for (strategy, side, delay, odds), rows in sorted(groups.items()):
        for split in ('all', 'train', 'validation', 'test'):
            selected = rows if split == 'all' else [r for r in rows if r['split'] == split]
            scenarios = [('fixed', 1, 100., 60, 1.), ('martin3_100', 3, 100., 60, 1.),
                         ('martin5_100', 5, 100., 60, 1.), ('martin11_100', 11, 100., 60, 1.),
                         ('martin11_2047', 11, 2047., 60, 1.)]
            if delay == 300:
                scenarios += [('fixed_fee2', 1, 100., 60, 2.), ('fixed_settle300', 1, 100., 300, 1.)]
            for label, rounds, bankroll, wait, mult in scenarios:
                summary, ledger = replay(selected, rounds, bankroll, wait, mult)
                summary.update(strategy=strategy, side=side, delayMs=delay, odds=odds,
                               split=split, sizing=label, qualifiedSignals=len(selected))
                if label == 'fixed':
                    summary['dailyBlockRoiCI95'] = bootstrap_daily_ci(summary['daily'])
                results.append(summary)
                if delay == 300 and split in ('all', 'test'):
                    ledgers['|'.join((strategy, side, odds, split, label))] = ledger
                if delay == 300 and label == 'fixed':
                    actual_times = {r['executionUs'] for r in ledger}
                    # These are post-selection descriptions, not independent
                    # strategies that can claim the same trade count as a rescan.
                    executed = [r for r in selected if r['executionUs'] in actual_times]
                    for dimension, bands in [('price', ((.30, .40), (.40, .45), (.45, .500001))),
                                             ('remaining', ((30, 90), (90, 180), (180, 270.000001)))]:
                        for lo, hi in bands:
                            subset = [r for r in executed if lo <= (r['fixedFill']['vwap'] if dimension == 'price' else r['remaining']) < hi]
                            summary2, _ = replay(subset)
                            strata.append({'strategy': strategy, 'side': side, 'odds': odds, 'split': split,
                                           'dimension': dimension, 'band': [lo, hi], 'metrics': summary2})
    dump(out / 'results.json', results)
    dump(out / 'sizing_ledgers.json.gz', ledgers)
    dump(out / 'descriptive_strata.json', strata)
    dump(out / 'probability_scores.json', probability_scores(observations))
    return results, strata


def fmt(x, digits=3):
    return '—' if x is None else f'{x:.{digits}f}'


def pct(x):
    return '—' if x is None else f'{x:.1%}'


def table(rows, fields):
    lines = ['| ' + ' | '.join(label for label, _ in fields) + ' |',
             '|' + '|'.join('---' for _ in fields) + '|']
    for r in rows:
        lines.append('| ' + ' | '.join(str(fn(r)) for _, fn in fields) + ' |')
    return lines


def write_reports(results, strata, manifest, out):
    fields = [('方向', lambda r: r['side']), ('分段', lambda r: r['split']),
              ('单数', lambda r: r['n']), ('胜/负', lambda r: f"{r['wins']}/{r['losses']}"),
              ('胜率', lambda r: pct(r['winRate'])), ('含费净收益U', lambda r: fmt(r['netPnl'])),
              ('投入收益率', lambda r: pct(r['roi'])), ('最大回撤U', lambda r: fmt(r['maxDrawdown'])),
              ('最大连败', lambda r: r['maxConsecutiveLosses'])]
    possible = []
    for key in NAMES:
        for side in ('UP', 'DOWN'):
            check = [r for r in results if r['strategy'] == key and r['side'] == side
                     and r['delayMs'] == 300 and r['odds'] == 'gross_le_050' and r['sizing'] == 'fixed'
                     and r['split'] in ('validation', 'test')]
            if len(check) == 2 and all(r['n'] >= 100 and r['netPnl'] > 0 for r in check):
                possible.append(key + ' ' + side)
    verdict = ('没有任何方向同时达到“验证段与末段各至少100笔、固定金额扣费均盈利”的初步研究门槛；本轮不升级实盘。'
               if not possible else '达到后续前瞻研究初步门槛：' + '、'.join(possible) + '；仍不代表可升级实盘。')
    overview = ['# 六类低价策略与有限马丁验证（T065）', '',
        '本轮不改实盘。本报告中的策略是冻结规则的离线候选，不是已验证可盈利的交易建议。', '',
        f"- 数据北京时间：{manifest['firstMarket']} 至 {manifest['lastMarket']}。",
        f"- 官方结算市场：{manifest['settledMarkets']}；Binance官方1分钟K线：{manifest['candles']['bars']}根。",
        f"- 时间切分：前60%探索、之后20%验证、最后20%末段检查；末段开始 {manifest['testStart']}。",
        '- 最后20%并非从未在其他实验接触的数据；本轮未用它调参。数据覆盖20个日期，中间有明显缺口，并非连续运行到11月。',
        '- 主表：1U含费预算、300ms订单延迟、剩余270–30秒、决策卖一0.30–0.50、最高成交档≤0.50、持有结算。下单后更便宜的成交保留。',
        '- 费用为冻结历史假设0.07*p*(1-p)，不是当前平台收费承诺。每笔重新逐档吃盘口。',
        '- 每方向独立账户；不把UP+DOWN相加当作一条可同时运行的单账户曲线。', '',
        '## 本轮结论', '',
        verdict,
        '评估以固定金额的跨段净收益、足够样本和有限倍投的资金压力共同判断，不能只挑最高收益小组。',
        'RSI持续反向在整段时间顺序回放中出现足以打穿11单的亏损序列；低于0.50只改变赔率，不消除连败。',
        'RSI回穿、顺势恢复和突破失败的部分盈利小组样本极少。Binance短动量在中间段与末段出现方向变化。',
        '概率模型的整体方向准确率不能直接转化为便宜合约的交易优势；同覆盖盘口参考概率优于该模型。', '',
        '## 六项末段固定金额结果', '']
    main = [r for r in results if r['delayMs'] == 300 and r['odds'] == 'gross_le_050' and r['split'] == 'test' and r['sizing'] == 'fixed']
    overview += table(main, [('策略', lambda r: r['strategy'] + ' ' + NAMES[r['strategy']])] + fields)
    overview += ['', '## RSI原始想法的整段11单压力结果', '',
                 '初始2047U，1U起投，至多11单；遇本金不足即停止。不能用末段恰好盈利替代整段风险。', '']
    overview += table([r for r in results if r['strategy'] == 'M1' and r['delayMs'] == 300
                       and r['odds'] == 'gross_le_050' and r['split'] == 'all' and r['sizing'] == 'martin11_2047'],
                      [('方向', lambda r: r['side']), ('成交单数', lambda r: r['n']),
                       ('净收益U', lambda r: fmt(r['netPnl'])), ('最大回撤U', lambda r: fmt(r['maxDrawdown'])),
                       ('最长连败', lambda r: r['maxConsecutiveLosses']), ('11单打穿次数', lambda r: r['capLossCycles']),
                       ('资金停止', lambda r: r['rejections'].get('bankroll_stop', 0))])
    overview += ['', '## 每项独立归档', '']
    ledgers = load(out / 'sizing_ledgers.json.gz')
    for key, name in NAMES.items():
        filename = key + '_' + name + '.md'
        overview.append(f'- [{key} {name}]({filename})')
        subset = [r for r in results if r['strategy'] == key and r['delayMs'] == 300]
        lines = [f'# {key} {name}', '', '[返回总览](六类低价策略与有限马丁验证.md)', '',
                 '[冻结规则与执行计划](../../../docs/马丁候选策略验证计划_20261118.md)', '',
                 '## 结论定位', '',
                 '先比较验证段和末段的固定金额收益；再看倍投是否通过增加最大单注和回撤换来历史利润。',
                 'M1/M6的长连败必须保留在结论中；M2–M5的小样本盈利不升级为成熟策略。', '',
                 '## 固定金额结果', '',
                 '主口径0.30–0.50，1U含费预算；先看验证和末段，不能只看全部样本。', '']
        lines += table([r for r in subset if r['sizing'] == 'fixed' and r['odds'] == 'gross_le_050'], fields)
        lines += ['', '## 含费每份成本≤0.50的净赔率对照', '',
                  '此口径另行寻找首个合格信号，含费成本决定胜时净赔率是否至少1:1。', '']
        lines += table([r for r in subset if r['sizing'] == 'fixed' and r['odds'] == 'net_cost_le_050'], fields)
        lines += ['', '## 末段资金管理对照', '',
                  '100U账户无法完整覆盖1→1024的11单；2047U情景单列。每次赢复位，达到级数且输也复位。', '']
        lines += table([r for r in subset if r['split'] == 'test' and r['odds'] == 'gross_le_050'],
                       [('方向', lambda r: r['side']), ('资金管理', lambda r: r['sizing']),
                        ('单数', lambda r: r['n']), ('净收益U', lambda r: fmt(r['netPnl'])),
                        ('ROI', lambda r: pct(r['roi'])), ('最大回撤U', lambda r: fmt(r['maxDrawdown'])),
                        ('最高单注U', lambda r: fmt(r['maxBudget'])), ('打穿次数', lambda r: r['capLossCycles']),
                        ('本金停止', lambda r: r['rejections'].get('bankroll_stop', 0)),
                        ('赢后仍未回本周期', lambda r: r['negativeWinningCycles'])])
        lines += ['', '## 整段与分段11单压力对照', '',
                  '各分段独立从2047U开始；整段从头按时间连续运行，遇资金不足停止。M6探索段包含拟合，只作压力描述。', '']
        lines += table([r for r in subset if r['odds'] == 'gross_le_050' and r['sizing'] == 'martin11_2047'],
                       [('方向', lambda r: r['side']), ('分段', lambda r: r['split']),
                        ('单数', lambda r: r['n']), ('净收益U', lambda r: fmt(r['netPnl'])),
                        ('最大回撤U', lambda r: fmt(r['maxDrawdown'])), ('最长连败', lambda r: r['maxConsecutiveLosses']),
                        ('打穿次数', lambda r: r['capLossCycles']), ('本金停止', lambda r: r['rejections'].get('bankroll_stop', 0))])
        runs = []
        for side in ('UP', 'DOWN'):
            ledger = ledgers.get('|'.join((key, side, 'gross_le_050', 'all', 'fixed')), [])
            run = []
            for trade in ledger + [{'won': True}]:
                if trade['won']:
                    if run:
                        runs.append({'side': side, 'n': len(run), 'first': run[0]['entryTime'],
                                     'last': run[-1]['entryTime'], 'loss': sum(x['pnl'] for x in run)})
                        run = []
                else:
                    run.append(trade)
        lines += ['', '## 固定金额失败序列示例', '', '按实际可执行交易顺序取最长的三个亏损序列；时间间隔不表示中间一直有数据。', '']
        lines += table(sorted(runs, key=lambda x: x['n'], reverse=True)[:3],
                       [('方向', lambda r: r['side']), ('连续亏损笔数', lambda r: r['n']),
                        ('首次入场北京时间', lambda r: r['first']), ('末次入场北京时间', lambda r: r['last']),
                        ('累计亏损U', lambda r: fmt(r['loss']))])
        lines += ['', '## 延迟与成本压力', '']
        pressure = [r for r in results if r['strategy'] == key and r['split'] == 'test' and r['odds'] == 'gross_le_050'
                    and r['sizing'] in ('fixed', 'fixed_fee2', 'fixed_settle300')]
        lines += table(pressure, [('方向', lambda r: r['side']), ('情景', lambda r: r['sizing']),
                                 ('订单延迟ms', lambda r: r['delayMs']), ('单数', lambda r: r['n']),
                                 ('净收益U', lambda r: fmt(r['netPnl'])), ('ROI', lambda r: pct(r['roi']))])
        lines += ['', '## 末段价格和时间描述分层', '',
                  '这是首个信号策略成交后的分层，不是每个区间重新搜索；仅用于提出下一轮假设。', '']
        selected = [r for r in strata if r['strategy'] == key and r['split'] == 'test' and r['odds'] == 'gross_le_050']
        lines += table(selected, [('方向', lambda r: r['side']), ('维度', lambda r: r['dimension']),
                                 ('区间', lambda r: str(r['band'])), ('单数', lambda r: r['metrics']['n']),
                                 ('胜率', lambda r: pct(r['metrics']['winRate'])),
                                 ('净收益U', lambda r: fmt(r['metrics']['netPnl']))])
        lines += ['', '## 限制与下一步', '',
                  '- 少于30笔为小样本；少于100笔不升级为实盘硬条件。即使超过100笔也不代表已证明有效。',
                  '- 每秒决策网格不能验证毫秒级领先；延迟结果是历史快照重放，不是实际订单确认延迟。',
                  '- 盘口可见深度不保证真实成交；没有虚构缺失深度或订单排队。',
                  '- 每场只评估首个合格候选；账户仍等待结算时跳过该场，不搜同场稍后机会；首次订单未完整成交也不追单。',
                  '- 净赔率对照在下单前就把每档最高价限制到含费成本0.50；不事后删除价格变差的成交。费用加倍压力保持原入场名单，不再次优化。',
                  '- RSI状态和信号只由当时已收盘K线更新，盘口同时检查源时间和接收时间。历史K线无法证明当时网络发布延迟。',
                  '- 必须等官方结算标签实际收到、且结束至少60秒后才复位或倍投；不能在场次结束时提前知道结果。',
                  '- M6探索段含模型拟合数据，不可解读为可实时获得的样本外收益。',
                  '- M4/M6使用记录中的Binance开盘参考价，不替代Chainlink官方结算锚点。',
                  '- 可观测历史中断后的下一笔继续沿用资金状态；不代表中断期间没有其他机会或亏损。',
                  '- 全部策略和参数均须在新日期前瞻复验，不能用本轮最佳组再次自证。', '',
                  '## 重现', '', '```sh', 'cd arbitrage-research',
                  'python3 scripts/analysis/analyze_martingale_candidates.py', '```', '',
                  '逐单见`entries.json.gz`和`sizing_ledgers.json.gz`；完整指标见`results.json`，数据审计见`manifest.json`。']
        (out / filename).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    scores = load(out / 'probability_scores.json')
    overview += ['', '## 概率模型准确性', '',
                 '概率评分按固定剩余时间观察，同场多次观察有关联，不能把观察数当成独立市场数。', '']
    overview += table(scores, [('分段', lambda r: r['split']), ('市场数', lambda r: r['markets']),
                              ('观察数', lambda r: r['observations']), ('方向准确率', lambda r: pct(r['accuracy'])),
                              ('Brier越低越好', lambda r: fmt(r['brier'], 4)),
                              ('同覆盖模型Brier', lambda r: fmt(r['modelBrierComparable'], 4)),
                              ('同覆盖双边卖一归一Brier', lambda r: fmt(r['askNormalizedBrierComparable'], 4))])
    overview += ['', '双边卖一归一只是盘口参考概率，不是可成交中间价。概率准确率高不等于交易净收益高。', '',
                 '## 资金风险与覆盖', '',
                 '- 主回放每单含费预算1U；不执行当前实盘2.5U最低金额，因此是研究假设。',
                 '- 11单1、2、4…1024需要最多2047U，不是11U。没有出现长连败不代表以后不会出现。',
                 '- 马丁与固定金额会因金额、深度、本金和等待结算筛出不同交易，不能只拿最终收益比较。',
                 '- 结算等待取“实际官方结果接收时间”和“结束+60秒”的较晚者；收盘即知输赢的结果没有用于本报告。',
                 '- 校验、拒绝原因、逐日ROI区间和校准分桶保存在JSON；分层与极小样本不作为成熟结论。',
                 '- 数据：[数据质量与覆盖审计](数据质量与覆盖审计.md)。',
                 '- 计划：[冻结参数与验证标准](../../../docs/马丁候选策略验证计划_20261118.md)。']
    (out / '六类低价策略与有限马丁验证.md').write_text('\n'.join(overview) + '\n', encoding='utf-8')


def write_quality(manifest, out):
    coverage = load(out / 'market_coverage.json')
    daily = Counter(local(r['startUs'])[:10] for r in coverage)
    eligible = Counter()
    for r in coverage:
        eligible['withSnapshots'] += int(r['snapshotsUP'] > 0 and r['snapshotsDOWN'] > 0)
        eligible['withFreshUPBook'] += int(r.get('freshBookGridUP', 0) > 0)
        eligible['withFreshDOWNBook'] += int(r.get('freshBookGridDOWN', 0) > 0)
        eligible['withRSI'] += int(r.get('rsiReadyGrid', 0) > 0)
        eligible['withFreshSpot'] += int(r.get('freshSpotGrid', 0) > 0)
    waits = sorted((r['labelAvailableUs'] - r['endUs']) / SECOND for r in coverage)
    stats = {'markets': len(coverage), 'daily': dict(sorted(daily.items())), **eligible,
             'officialResultDelayMedianSeconds': waits[len(waits) // 2],
             'officialResultDelayP90Seconds': waits[int(len(waits) * .9)],
             'officialResultDelayMaxSeconds': max(waits)}
    dump(out / 'coverage_summary.json', stats)
    lines = ['# T065 数据质量与覆盖审计', '',
             '[返回总览](六类低价策略与有限马丁验证.md)', '',
             f"- 官方标签市场：{len(coverage)}；日期数：{len(daily)}；盘口事件来源文件：{len(manifest['sourceFiles'])}。",
             f"- 双方向都有盘口记录的市场：{eligible['withSnapshots']}；至少一个有效决策时点有UP盘口：{eligible['withFreshUPBook']}，DOWN：{eligible['withFreshDOWNBook']}。",
             f"- 能计算收盘RSI的市场：{eligible['withRSI']}；至少有一个新鲜Spot决策观察的市场：{eligible['withFreshSpot']}。",
             f"- 官方结果到达延迟：中位数{fmt(stats['officialResultDelayMedianSeconds'],1)}秒，P90 {fmt(stats['officialResultDelayP90Seconds'],1)}秒，最大{fmt(stats['officialResultDelayMaxSeconds'],1)}秒。",
             '- 结果到达延迟是本地收到官方标签的延迟，不是平台链上赎回到账时间。资金回放仍是近似执行模型。',
             '- 历史公开1分钟K线只在收盘后使用；历史接口不提供当时客户端的接收时延。',
             '- 原始数据有缺口；未补出缺失的Polymarket盘口，也未把只含成交价的历史数据当作可成交深度。', '',
             '## 按北京时间日期的官方标签市场数', '']
    lines += table([{'date': d, 'n': n} for d, n in sorted(daily.items())], [('日期', lambda r: r['date']), ('场数', lambda r: r['n'])])
    c = manifest['cross']
    lines += ['', '## 跨市场快照清洗', '',
              f"读取{c['rows']}行；新鲜有效{c['accepted']}行；源时间领先/负年龄{c['future_or_negative_age']}行；陈旧{c['stale']}行；缺失/格式不合格{c['unavailable']+c['invalid']}行。", '',
              '## 损坏或截断文件', '',
              '仅保留能成功读取并通过时间检查的行；损坏后的未知部分不补值，不保证整个文件完整。', '']
    lines += table(c['damagedFiles'], [('相对路径', lambda r: r['path']), ('异常类型', lambda r: r['error'])])
    lines += ['', '## 限制', '',
              '- 数据最晚到2026年10月3日，没有把11月18日当作数据截止日期。',
              '- 一分钟K线补录解决RSI历史覆盖，不会补齐当时缺失的预测市场盘口或跨市场到达时序。',
              '- 单向独立账户、1U含费、全额深度成交、无自动赎回网络延迟；不能把研究结果直接等同于当前实盘。',
              '- 所有输入文件元数据、冻结规则、概率模型和异常计数见manifest.json；原始数据未删除。']
    (out / '数据质量与覆盖审计.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--reuse-entries', action='store_true')
    args = parser.parse_args(); out = args.out; out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / 'src'))
    from arb_research.experiments.live_strategy_cache import connect_cache, refresh_event_cache
    refresh_con = connect_cache(ROOT / 'data/derived/live_strategy_cache.sqlite3')
    refresh = refresh_event_cache(refresh_con, ROOT / 'data/live_monitor/events')
    refresh_con.close()
    print('event cache', json.dumps(refresh), flush=True)
    con = sqlite3.connect(f'file:{ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    markets = [dict(r) for r in con.execute("SELECT market_id,start_us,end_us,winner FROM markets WHERE winner IN ('UP','DOWN') AND end_us-start_us=300000000 ORDER BY start_us")]
    arrivals, arrival_audit = settlement_arrivals(out)
    no_arrival = []
    for m in markets:
        label = arrivals.get(m['market_id'])
        if label is None:
            no_arrival.append(m['market_id'])
        elif label['winner'] != m['winner']:
            raise RuntimeError('Cache/official label disagreement: ' + m['market_id'])
        else:
            m['label_available_us'] = label['availableUs']
    # Do not invent an availability timestamp for missing official records.
    markets = [m for m in markets if m.get('label_available_us')]
    if len(markets) < 100:
        raise RuntimeError('Insufficient official settled 5m markets')
    cutoff, test = markets[int(.6 * len(markets))]['start_us'], markets[int(.8 * len(markets))]['start_us']
    candles, candle_audit = official_candles(markets[0]['start_us'], markets[-1]['end_us'], out)
    indicators = build_indicators(candles)
    crosses, cross_audit = project_cross(out)
    model, model_audit = fit_probability(markets, crosses, indicators, cutoff)
    if args.reuse_entries:
        old = load(out / 'manifest.json')
        if (old['version'] != VERSION or old['settledMarkets'] != len(markets)
                or old['cross']['sourceFingerprint'] != cross_audit['sourceFingerprint']
                or old['officialLabels']['sourceFingerprint'] != arrival_audit['sourceFingerprint']):
            raise RuntimeError('Entry cache mismatch; rerun without --reuse-entries')
        entries = load(out / 'entries.json.gz')
        observations = load(out / 'probability_observations.json.gz')
        extraction_audit = old['extraction']
    else:
        entries, extraction_audit, observations = extract_entries(con, markets, crosses, indicators, model, out)
    source_rows = [list(r) for r in con.execute('SELECT path,size,mtime_ns FROM source_files ORDER BY path')]
    con.close()
    manifest = {'version': VERSION, 'runDate': '2026-11-18', 'settledMarkets': len(markets),
                'firstMarket': local(markets[0]['start_us']), 'lastMarket': local(markets[-1]['end_us']),
                'validationStart': local(cutoff), 'testStart': local(test),
                'candles': candle_audit, 'cross': cross_audit, 'model': model_audit,
                'officialLabels': {**arrival_audit, 'excludedMissingArrival': no_arrival},
                'eventCacheRefresh': refresh,
                'extraction': extraction_audit, 'sourceFiles': source_rows,
                'rules': {'remaining': [30, 270], 'priceFloor': .30, 'priceCap': .50, 'budgetIncludesFees': True,
                          'feeRate': .07, 'quantityStep': .01, 'maxAgeSeconds': 2,
                          'latenciesMs': [0, 300, 1000], 'split': [.6, .2, .2],
                          'settlementDelaySeconds': [60, 300], 'noLiveChanges': True}}
    dump(out / 'manifest.json', manifest)
    results, strata = analyze(entries, observations, out)
    for r in results:
        if r['odds'] == 'net_cost_le_050' and r['feeMultiplier'] == 1. and r['negativeWinningCycles']:
            raise RuntimeError('Net-odds bankroll invariant failed')
    write_reports(results, strata, manifest, out)
    write_quality(manifest, out)
    print(json.dumps({'markets': len(markets), 'entries': len(entries), 'scenarios': len(results),
                      'out': str(out), 'model': model_audit}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

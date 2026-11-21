#!/usr/bin/env python3
"""Compare 60s, 120s and 240s BTC realized volatility around entries."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
import random
import statistics
import zlib

ROOT = Path(__file__).resolve().parents[2]
ROWS_PATH = ROOT / 'outputs/research_tests/t061_loss_sizing_volatility_20261001/diagnostic_rows.json'
OUT = ROOT / 'outputs/research_tests/t063_long_volatility_20261003'


def iso(us):
    return datetime.fromtimestamp(us / 1e6, timezone.utc).isoformat()


def num(x):
    try:
        y = float(x)
        return y if math.isfinite(y) else None
    except (TypeError, ValueError):
        return None


def grid(xs, at_us, seconds):
    xs = sorted(xs, key=lambda x: x['received_us'])
    times = [x['received_us'] for x in xs]
    out = []
    for i in range(seconds + 1):
        at = at_us - (seconds - i) * 1_000_000
        j = bisect_right(times, at) - 1
        if j < 0:
            out.append(None)
            continue
        r = xs[j]
        out.append(r['price'] if r['price'] is not None and 0 <= at - r['source_us'] <= 3_000_000
                   and 0 <= at - r['received_us'] <= 3_000_000 else None)
    return out


def realized(values):
    pairs = [(a, b) for a, b in zip(values, values[1:]) if a and b and a > 0 and b > 0]
    if len(pairs) < 0.8 * (len(values) - 1):
        return None
    return math.sqrt(sum((math.log(b / a) * 10000) ** 2 for a, b in pairs))


def stats(rs):
    if not rs:
        return {'n': 0, 'win_rate': None, 'pnl_per_share': None}
    return {'n': len(rs), 'win_rate': sum(r['won'] for r in rs) / len(rs),
            'pnl_per_share': statistics.mean(r['pnl'] for r in rs)}


def block_ci(rs):
    byday = defaultdict(list)
    for r in rs:
        byday[r['date']].append(r['pnl'])
    blocks = list(byday.values())
    if len(blocks) < 3:
        return None
    rng = random.Random(20261003)
    vals = []
    for _ in range(1000):
        sample = [v for _ in blocks for v in rng.choice(blocks)]
        vals.append(statistics.mean(sample))
    vals.sort()
    return [vals[25], vals[975]]


def compact(x):
    if not x or x.get('n', 0) == 0:
        return '0/—/—'
    return f"{x['n']}/{x['win_rate'] * 100:.1f}%/{x['pnl_per_share']:+.4f}"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = json.loads(ROWS_PATH.read_text())
    targets = {r['market_id'] for r in rows}
    bounds = {}
    for r in rows:
        lo, hi = bounds.get(r['market_id'], (r['execution_us'], r['end_us']))
        bounds[r['market_id']] = (min(lo, r['execution_us']), max(hi, r['end_us']))
    projected = defaultdict(list)
    audit = {'files': 0, 'lines': 0, 'invalid_json': 0, 'damaged_files': []}
    paths = sorted((ROOT / 'data/cross_market_validation/venue_snapshots').glob('date=*/hour=*/part-*.ndjson*'))
    for idx, path in enumerate(paths, 1):
        audit['files'] += 1
        try:
            opener = gzip.open if path.suffix == '.gz' else open
            with opener(path, 'rt') as f:
                for line in f:
                    audit['lines'] += 1
                    try:
                        raw = json.loads(line)
                    except (ValueError, TypeError):
                        audit['invalid_json'] += 1
                        continue
                    mid = raw.get('marketId')
                    if mid not in targets:
                        continue
                    rec = int(raw.get('receivedTimeUs') or 0)
                    src = int(raw.get('sourceTimeUs') or 0)
                    if not rec or not src:
                        continue
                    lo, hi = bounds[mid]
                    if rec < lo - 245_000_000 or rec > hi:
                        continue
                    fs = raw.get('features') or {}
                    b = fs.get('binance') or {}
                    age = num(b.get('sourceAgeMs'))
                    valid = bool(b.get('available')) and age is not None and 0 <= age <= 2000
                    projected[mid].append({'source_us': src - int(age * 1000) if age is not None else 0,
                        'received_us': rec, 'price': num(b.get('price')) if valid else None})
        except (OSError, EOFError, zlib.error) as exc:
            audit['damaged_files'].append({'path': str(path.relative_to(ROOT)), 'error': str(exc)})
        if idx % 40 == 0:
            print('files', idx, '/', len(paths), 'rows', sum(map(len, projected.values())), flush=True)
    for xs in projected.values():
        xs.sort(key=lambda x: x['received_us'])

    for r in rows:
        t = r['execution_us']
        xs = [x for x in projected.get(r['market_id'], []) if x['received_us'] <= t and x['source_us'] <= t]
        for period in (60, 120, 240):
            values = grid(xs, t, period)
            clean = [x for x in values if x is not None]
            r[f'btc_rv{period}_bps'] = realized(values)
            r[f'btc_cov{period}'] = len(clean) / len(values)
            r[f'btc_range{period}_bps'] = ((max(clean) - min(clean)) / statistics.mean(clean) * 10000
                                            if len(clean) >= int(0.8 * len(values)) else None)

    audit['feature_coverage'] = {k: sum(r.get(k) is not None for r in rows)
                                 for k in ('btc_rv60_bps', 'btc_rv120_bps', 'btc_rv240_bps',
                                           'btc_range60_bps', 'btc_range120_bps', 'btc_range240_bps')}
    audit['cross_files'] = len(paths)
    (OUT / 'audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    (OUT / 'diagnostic_rows.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')

    results = {}
    for strategy in sorted({r['strategy'] for r in rows}):
        group = sorted([r for r in rows if r['strategy'] == strategy], key=lambda r: r['end_us'])
        split = sorted({r['end_us'] for r in group})[int(len({r['end_us'] for r in group}) * 0.7)]
        train = [r for r in group if r['end_us'] < split]
        valid = [r for r in group if r['end_us'] >= split]
        fields = {}
        for field in ('btc_rv60_bps', 'btc_rv120_bps', 'btc_rv240_bps',
                      'btc_range60_bps', 'btc_range120_bps', 'btc_range240_bps'):
            values = [r[field] for r in train if r.get(field) is not None]
            if len(values) < 20:
                fields[field] = {'available': sum(r.get(field) is not None for r in group), 'insufficient': True}
                continue
            threshold = sorted(values)[int((len(values) - 1) * 0.9)]
            def selected(xs, high):
                return [r for r in xs if r.get(field) is not None and (r[field] >= threshold) == high]
            high_all, low_all = selected(group, True), selected(group, False)
            high_valid, low_valid = selected(valid, True), selected(valid, False)
            fields[field] = {'threshold_train_p90': threshold,
                'available': sum(r.get(field) is not None for r in group),
                'all_high': stats(high_all), 'all_low': stats(low_all),
                'validation_high': stats(high_valid), 'validation_low': stats(low_valid),
                'validation_high_day_ci95': block_ci(high_valid), 'validation_low_day_ci95': block_ci(low_valid)}
            tertile = sorted(values)[int((len(values) - 1) * 2 / 3)]
            th, tl = selected(valid, True), selected(valid, False)
            # Keep a second, less sparse cut for deciding whether a longer
            # period is worth shadow monitoring.
            fields[field]['tercile_train_p67'] = tertile
            fields[field]['tercile_validation_high'] = stats([r for r in valid if r.get(field) is not None and r[field] >= tertile])
            fields[field]['tercile_validation_low'] = stats([r for r in valid if r.get(field) is not None and r[field] < tertile])
        results[strategy] = {'n': len(group), 'split_utc': iso(split), 'train_n': len(train),
                             'validation_n': len(valid), 'fields': fields}
    output = {'model': 'long-btc-realized-vol-v1', 'results': results, 'audit': audit}
    (OUT / 'results.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')

    lines = ['# 更大周期 BTC 波动率验证', '',
        '在同一批策略成交上，用入场前 BTC 现货 60/120/240 秒逐秒价格计算实现波动；每个策略前70%按时间拟定90分位阈值，后30%只做留后验证。',
        '波动率是入场前特征，不使用入场后价格；240秒覆盖不足的记录保留为缺失，不当成低波动。', '',
        '| 策略 | 指标 | 训练P90 | 后30%高波动 n/胜率/每份 | 后30%低波动 n/胜率/每份 | 高波动日块95%区间 |',
        '|---|---|---:|---|---|---|']
    for strategy, result in results.items():
        for field, x in result['fields'].items():
            if x.get('insufficient') or 'validation_high' not in x:
                continue
            h, l = x['validation_high'], x['validation_low']
            lines.append(f"| {strategy} | {field} | {x['threshold_train_p90']:.3f} | {compact(h)} | {compact(l)} | {x['validation_high_day_ci95']} |")
    lines += ['', '## 训练段三分位（减少极端高波动样本过少）', '',
        '| 策略 | 指标 | 训练P67 | 后30%高波动 n/胜率/每份 | 后30%低波动 n/胜率/每份 |',
        '|---|---|---:|---|---|']
    for strategy, result in results.items():
        if 'trade_down_80_85_90_10_q10_size10' not in strategy and 'trade_up_80_85_90_10_q10_size10' not in strategy:
            continue
        for field, x in result['fields'].items():
            if x.get('insufficient') or 'tercile_train_p67' not in x:
                continue
            lines.append(f"| {strategy} | {field} | {x['tercile_train_p67']:.3f} | {compact(x['tercile_validation_high'])} | {compact(x['tercile_validation_low'])} |")
    lines += ['', '## 解释与限制', '',
        '- 只有当更大周期在多个方向、多个新增日期中稳定拉开高低波动表现，才值得做仓位缩放或硬过滤；单个分组的胜率差不能直接当成因果。',
        '- 240秒在5分钟市场里接近整场生命周期，覆盖通常比60/120秒差；它更适合作为“本场已经剧烈波动”的背景标签，而非精细入场信号。',
        '- 本轮仍是历史回放，不改变主策略配置。', '',
        '```bash', 'PYTHONPATH=src python3 scripts/analysis/analyze_long_volatility.py', '```', '']
    (OUT / '更大周期BTC波动率验证.md').write_text('\n'.join(lines))
    print('done', OUT, flush=True)


if __name__ == '__main__':
    main()

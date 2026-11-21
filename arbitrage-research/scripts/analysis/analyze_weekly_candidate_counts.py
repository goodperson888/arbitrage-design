#!/usr/bin/env python3
"""Count frozen candidate ledgers by Asia/Shanghai Monday-to-Sunday week."""
import gzip
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs/research_tests/t075_weekly_candidate_counts_20261121'
PARENT = ROOT / 'outputs/research_tests'
TZ = ZoneInfo('Asia/Shanghai')
T071 = PARENT / 't071_supertrend_robustness_20261120'
T074 = PARENT / 't074_entry_scope_20261121'
COHORT = PARENT / 't065_martingale_candidates_20261118/market_coverage.json'
VERSIONS = (
    ('flip_net', 'ATR10×3翻转，含费≤0.50', T074, 'S1_n10|baseline|1000|300|net_cost_le_050|all|{sizing}', 37, 37),
    ('flip_gross', 'ATR10×3翻转，名义≤0.50', T074, 'S1_n10|baseline|1000|300|gross_le_050|all|{sizing}', 49, 49),
    ('D1', 'D1原版', T074, 'D1|baseline|1000|300|gross_le_050|all|{sizing}', 51, 50),
    ('S3', 'S3 ATR14×3', T071, 'S3|1000|BOTH|{sizing}', 27, 27),
)


def load(path):
    with gzip.open(path, 'rt', encoding='utf-8') if path.suffix == '.gz' else path.open(encoding='utf-8') as stream:
        return json.load(stream)


def local_date(microseconds):
    return datetime.fromtimestamp(microseconds / 1_000_000, TZ).date()


def monday(day):
    return day - timedelta(days=day.weekday())


def main():
    assert monday(datetime(2026, 8, 23).date()).isoformat() == '2026-08-17'
    assert monday(datetime(2026, 8, 24).date()).isoformat() == '2026-08-24'
    inputs = [COHORT, T071 / 'sizing_ledgers.json.gz', T074 / 'sizing_ledgers.json.gz', Path(__file__).resolve()]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    cohort = load(COHORT)
    market_ids = {m['marketId'] for m in cohort}
    assert len(market_ids) == len(cohort) == 3752
    counts = Counter(monday(local_date(m['startUs'])) for m in cohort)
    days = {}
    for m in cohort:
        day = local_date(m['startUs'])
        days.setdefault(monday(day), set()).add(day)
    assert len(set.union(*days.values())) == 20
    first = min(local_date(m['startUs']) for m in cohort)
    last = max(local_date(m['endUs']) for m in cohort)
    weeks = []
    day = monday(first)
    while day <= monday(last):
        weeks.append(day)
        day += timedelta(days=7)
    ledger_sources = {T071: load(T071 / 'sizing_ledgers.json.gz'), T074: load(T074 / 'sizing_ledgers.json.gz')}
    by_week = {day: {'weekStart': day.isoformat(), 'weekEnd': (day + timedelta(days=6)).isoformat(),
                     'recordedDates': len(days.get(day, set())), 'settledMarkets': counts[day],
                     'dateCoverageIs24Hours': False, 'fixed': {}, 'martin11_2047': {}} for day in weeks}
    totals = {'fixed': {}, 'martin11_2047': {}}
    for identifier, name, folder, template, n_fixed, n_martin in VERSIONS:
        for sizing, expected in (('fixed', n_fixed), ('martin11_2047', n_martin)):
            rows = ledger_sources[folder][template.format(sizing=sizing)]
            assert len(rows) == expected
            assert all(r['marketId'] in market_ids for r in rows)
            assert len({(r['marketId'], r['side'], r['executionUs']) for r in rows}) == len(rows)
            actual = Counter(monday(local_date(r['executionUs'])) for r in rows)
            assert set(actual).issubset(set(weeks))
            for week in weeks:
                by_week[week][sizing][identifier] = actual[week]
            totals[sizing][identifier] = len(rows)
            assert sum(by_week[w][sizing][identifier] for w in weeks) == expected
    report = ['# T075：四个候选版本每周实际成交次数', '',
              '按北京时间、周一至周日自然周分组，以实际下单执行时间归周。直接统计已有完整账本，不重跑或重置每周资金状态，没有加入新数据。', '',
              '范围：2026-08-21 19:00至2026-10-03 01:50，3752个已结算市场、20个有数据日期，存在长时间断档。K线公布延迟1秒、订单延迟300ms；各版本原入场条件、等待官方结算、盘口/深度限制不变。',
              '有数据日期数不等于24小时完整采集；已结算市场数不证明每个市场盘口全程完整。零覆盖周写“无数据”，不能解释为策略零机会。', '',
              '## 固定每笔1U含历史冻结手续费', '',
              '|自然周（北京时间）|有数据日期数|已结算市场数|翻转含费≤0.50|翻转名义≤0.50|D1原版|S3|',
              '|---|---:|---:|---:|---:|---:|---:|']
    for week, row in by_week.items():
        values = [str(row['fixed'][v[0]]) if row['settledMarkets'] else '无数据' for v in VERSIONS]
        report.append(f'|{row["weekStart"]}—{row["weekEnd"]}|{row["recordedDates"]}|{row["settledMarkets"]}|' + '|'.join(values) + '|')
    report.append('|合计|20|3752|' + '|'.join(str(totals['fixed'][v[0]]) for v in VERSIONS) + '|')
    report += ['', '## 11级倍投、2047U起始资金', '',
               '采用跨周连续的原倍投账本，并非每周归1U重新开始。大额深度不足等会改变实际成交笔数；这张表仅统计频率，不重复推算收益。', '',
               '|自然周|翻转含费≤0.50|翻转名义≤0.50|D1原版|S3|', '|---|---:|---:|---:|---:|']
    for row in by_week.values():
        values = [str(row['martin11_2047'][v[0]]) if row['settledMarkets'] else '无数据' for v in VERSIONS]
        report.append(f'|{row["weekStart"]}—{row["weekEnd"]}|' + '|'.join(values) + '|')
    report.append('|合计|' + '|'.join(str(totals['martin11_2047'][v[0]]) for v in VERSIONS) + '|')
    report += ['', '## 解释与使用边界', '',
               '- 这是历史实际成交频率，不是未来每周承诺。三周无数据不能拿来计算正常运行频率；首尾周也只有部分日期。即使7个日期都有数据，也不代表每分钟都有新鲜盘口/Spot。',
               '- 四列为独立运行的候选账本，交易可能重叠，不能相加作为组合每周交易次数；多策略同时运行的资金冲突尚未联合回放。',
               '- 翻转的两个版本属于同一信号家族；S3是D1加趋势过滤的独立对照。',
               '- 原始结果是旧历史费用口径，不证明当下市场最低订单和费率。这里不修改交易或采集配置。', '',
               '[T074入场范围对照](../t074_entry_scope_20261121/入场范围放宽对照.md)；[双向预选总清单](../../../docs/双向策略预选总清单_20261120.md)。']
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / '四个候选策略每周成交次数.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    result = {'timezone': 'Asia/Shanghai', 'weekStartsOn': 'Monday', 'weeks': list(by_week.values()),
              'totals': totals, 'newBacktest': False, 'uniqueSettledMarkets': len(cohort),
              'versions': [{'id': v[0], 'name': v[1], 'sourceLedger': str((v[2] / 'sizing_ledgers.json.gz').relative_to(ROOT)), 'keyTemplate': v[3]} for v in VERSIONS]}
    (OUT / 'weekly_counts.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == hashes[str(p.relative_to(ROOT))] for p in inputs)
    (OUT / 'manifest.json').write_text(json.dumps({'runDate': '2026-11-21', 'version': 't075-v1-ledger-week-count', 'inputs': hashes, 'weeklyTotalsMatchSource': True}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()

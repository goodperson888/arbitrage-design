#!/usr/bin/env python3
"""Frozen-cache sizing stress and causal pre-entry failure diagnostics (research only)."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
from statistics import mean, median, pstdev
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from arb_research.experiments.live_strategy_cache import load_cases

MODEL = 'loss-sizing-depth-v1-received-and-source-gated'
BASE = [f'trade_{side}_80_85_90_{end}_q10_size10' for end in (30, 10) for side in ('up', 'down')]
FIELDS = {
    'range15': '合约卖一15秒极差', 'rv30': '合约卖一30秒实现波动（价格点）',
    'btc_rv60_bps': 'BTC现货60秒实现波动（bps）',
    'btc_range60_bps': 'BTC现货60秒高低幅（bps）',
    'momentum5': '合约卖一5秒动量', 'signal_delay_s': '成交信号接收延迟（秒）',
    'depth_cap': '限价内可买份数', 'trade_imbalance10': '近10秒双方主动买量方向不平衡',
    'btc_distance_bps': 'BTC相对本场开盘同向距离（bps）',
    'btc_return15_bps': 'BTC近15秒同向涨跌（bps）',
    'btc_book_imbalance': 'BTC盘口同向不平衡', 'book_source_age_s': '入场盘口源时间年龄（秒）',
}

def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')

def iso(us):
    return datetime.fromtimestamp(us/1e6, timezone.utc).isoformat()

def number(x):
    try:
        y = float(x)
        return y if math.isfinite(y) else None
    except (TypeError, ValueError):
        return None

def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    pos = (len(xs)-1)*q
    lo = int(pos)
    return xs[lo] + (xs[min(lo+1, len(xs)-1)]-xs[lo])*(pos-lo)

def stats(rows):
    n = len(rows)
    if not n:
        return {'n': 0, 'wins': 0, 'losses': 0, 'win_rate': None, 'pnl_per_share': None, 'total_10': 0, 'ci95': None}
    w = sum(r['won'] for r in rows)
    p = w/n
    z = 1.95996398454
    c = (p+z*z/(2*n))/(1+z*z/n)
    h = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return {'n': n, 'wins': w, 'losses': n-w, 'win_rate': p,
            'pnl_per_share': mean(r['pnl'] for r in rows), 'total_10': sum(r['pnl']*10 for r in rows),
            'ci95': [c-h, c+h]}

def grid(rows, end, seconds, field, max_age=2):
    """Arrival-time grid; excludes messages whose source time is in the future."""
    rows = sorted(rows, key=lambda r: r['received_us'])
    times = [r['received_us'] for r in rows]
    result = []
    for i in range(seconds+1):
        at = end-(seconds-i)*1_000_000
        j = bisect_right(times, at)-1
        if j < 0:
            result.append(None)
            continue
        r = rows[j]
        result.append(number(r.get(field)) if 0 <= at-r['source_us'] <= max_age*1e6 and at-r['received_us'] <= max_age*1e6 else None)
    return result

def rv(vals, log=False):
    pairs = [(a,b) for a,b in zip(vals, vals[1:]) if a is not None and b is not None and a>0 and b>0]
    if len(pairs) < .8*(len(vals)-1):
        return None
    diffs = [math.log(b/a)*10000 if log else b-a for a,b in pairs]
    return math.sqrt(sum(x*x for x in diffs))

def consume(levels, quantity, cap):
    remaining, cost, fee = quantity, 0., 0.
    for p,s in sorted(levels):
        if p > cap+1e-12:
            break
        take = min(remaining, s)
        cost += take*p
        fee += take*.07*p*(1-p)
        remaining -= take
        if remaining <= 1e-8:
            return cost+fee
    return None

def extract(con, row):
    t, end, mid, side = row['execution_us'], row['end_us'], row['market_id'], row['outcome']
    history = [dict(x) for x in con.execute(
        'SELECT source_us,received_us,best_ask FROM snapshots WHERE market_id=? AND outcome=? AND source_us BETWEEN ? AND ? AND received_us<=? ORDER BY source_us,received_us',
        (mid, side, t-63_000_000, t, t))]
    own = con.execute('SELECT source_us,received_us,best_ask,asks_json FROM snapshots WHERE market_id=? AND outcome=? AND received_us<=? ORDER BY received_us DESC LIMIT 1', (mid,side,t)).fetchone()
    levels = [[float(p),float(s)] for p,s in json.loads(own['asks_json'])] if own else []
    v = grid(history,t,60,'best_ask')
    clean15 = [x for x in v[-16:] if x is not None]
    result = {'levels': levels, 'range15': max(clean15)-min(clean15) if len(clean15)>=13 else None,
              'rv30': rv(v[-31:]), 'momentum5': v[-1]-v[-6] if v[-1] is not None and v[-6] is not None else None,
              'poly_pre_grid_coverage': sum(x is not None for x in v)/len(v),
              'book_source_age_s': (t-own['source_us'])/1e6 if own else None,
              'depth_cap': sum(s for p,s in levels if p<=row['cap']+1e-12),
              'signal_delay_s': (t-row['signal_us'])/1e6}
    flow = dict(con.execute("SELECT outcome,SUM(CAST(size AS REAL)) FROM trades WHERE market_id=? AND side='BUY' AND source_us BETWEEN ? AND ? AND received_us<=? GROUP BY outcome",(mid,t-10_000_000,t,t)).fetchall())
    volume = sum(flow.values())
    result['trade_imbalance10'] = (2*flow.get(side,0)/volume-1) if volume else None
    post = [dict(x) for x in con.execute('SELECT source_us,received_us,best_ask FROM snapshots WHERE market_id=? AND outcome=? AND source_us BETWEEN ? AND ? AND received_us<=? ORDER BY source_us,received_us', (mid,side,t-2_000_000,end,end))]
    seconds = min(100, max(0,int((end-t)/1e6)))
    path = grid(post,t+seconds*1_000_000,seconds,'best_ask')
    clean = [x for x in path if x is not None]
    result['post_ask_coverage'] = len(clean)/len(path)
    result['post_ask_min'] = min(clean) if clean else None
    result['post_ask_max'] = max(clean) if clean else None
    result['post_drop05_seconds'] = next((i for i,x in enumerate(path) if x is not None and x<=row['entry_vwap']-.05),None)
    result['post_below_half_seconds'] = next((i for i,x in enumerate(path) if x is not None and x<.5),None)
    result['post_bounce05'] = any(x>=row['entry_vwap']+.05 for x in clean)
    return result

def stream_cross(targets, bounds, out):
    projected = defaultdict(list)
    audit = {'files':0, 'lines':0, 'invalid_json':0, 'damaged_files':[], 'missing_timestamps':0}
    paths = sorted((ROOT/'data/cross_market_validation/venue_snapshots').glob('date=*/hour=*/part-*.ndjson*'))
    for index,p in enumerate(paths):
        audit['files'] += 1
        try:
            with (gzip.open(p,'rt') if p.suffix=='.gz' else p.open()) as f:
                for line in f:
                    audit['lines'] += 1
                    try:
                        r=json.loads(line)
                    except (ValueError, TypeError):
                        audit['invalid_json']+=1
                        continue
                    mid=r.get('marketId')
                    if mid not in targets:
                        continue
                    src, rec = int(r.get('sourceTimeUs') or 0), int(r.get('receivedTimeUs') or 0)
                    if not src or not rec:
                        audit['missing_timestamps']+=1
                        continue
                    if not bounds[mid][0]-65e6 <= rec <= bounds[mid][1]:
                        continue
                    fs=r.get('features') or {}
                    b=fs.get('binance') or {}
                    d=fs.get('deribit') or {}
                    age=number(b.get('sourceAgeMs'))
                    valid=bool(b.get('available')) and age is not None and 0<=age<=2000
                    projected[mid].append({'source_us':src,'received_us':rec,
                        'btc_source_us':src-int(age*1000) if age is not None else 0,
                        'price': number(b.get('price')) if valid else None,
                        'distance':number(b.get('returnFromStart')) if valid else None,
                        'return15':number(b.get('return15s')) if valid else None,
                        'book':number(b.get('bookImbalance')) if valid else None,
                        'dvol':number(d.get('dvol')), 'atmIv':number(d.get('atmIv'))})
        except (OSError, EOFError, zlib.error) as exc:
            audit['damaged_files'].append({'path':str(p.relative_to(ROOT)), 'error':str(exc)})
        if index%40==0:
            print('cross files',index+1,'/',len(paths),'projected',sum(map(len,projected.values())),flush=True)
    for xs in projected.values():
        xs.sort(key=lambda r:r['received_us'])
    dump(out/'cross_projection.json',projected)
    dump(out/'cross_quality.json',audit)
    return projected,audit

def add_cross(row, xs):
    t=row['execution_us']
    xs=[r for r in xs if r['received_us']<=t and r['source_us']<=t and r['received_us']>=t-65e6]
    gated=[dict(r,source_us=r['btc_source_us']) for r in xs]
    vals=grid(gated,t,60,'price',max_age=3)
    row['btc_rv60_bps']=rv(vals,True)
    clean=[v for v in vals if v is not None]
    row['btc_grid_coverage']=len(clean)/len(vals)
    row['btc_range60_bps']=(max(clean)-min(clean))/mean(clean)*10000 if len(clean)>=49 else None
    latest=xs[-1] if xs and t-xs[-1]['received_us']<=5e6 else None
    sign=1 if row['outcome']=='UP' else -1
    for target,source,scale in [('btc_distance_bps','distance',10000),('btc_return15_bps','return15',10000),('btc_book_imbalance','book',1)]:
        row[target]=latest[source]*sign*scale if latest and latest[source] is not None and 0<=t-latest['btc_source_us']<=5e6 else None
    row['dvol_observed']=latest.get('dvol') if latest else None
    row['atm_iv_observed']=latest.get('atmIv') if latest else None

def sizing(rows, rule, bankroll=1000., cap_mult=None, depth=True, delay=60):
    cash, peak, drawdown, ddpct = bankroll,bankroll,0.,0.
    profit, turnover, maxcost, maxqty = 0.,0.,0.,0.
    step, losses, maxlosses, available = 0,0,0,0
    skipped=Counter()
    ledger=[]
    cycle=0.
    unrecovered, closedcycles = 0,0
    required_bankroll=0.
    for r in rows:
        if r['execution_us']<available:
            skipped['unresolved_previous']+=1
            continue
        mult=1 if rule=='fixed' else 2**step
        if cap_mult is not None:
            mult=min(mult,cap_mult)
        quantity=10*mult
        cost=consume(r['levels'],quantity,r['cap']) if depth else quantity*(r['entry_vwap']+r['fee_per_share'])
        if cost is None:
            skipped['insufficient_depth']+=1
            continue
        required_bankroll=max(required_bankroll,cost-profit)
        if cost>cash+1e-8:
            skipped['bankroll_stop']+=1
            break
        pnl=quantity*int(r['won'])-cost
        cash+=pnl
        profit+=pnl
        cycle+=pnl
        turnover+=cost
        maxcost=max(maxcost,cost)
        maxqty=max(maxqty,quantity)
        peak=max(peak,cash)
        drawdown=max(drawdown,peak-cash)
        ddpct=max(ddpct,(peak-cash)/peak)
        losses=0 if r['won'] else losses+1
        maxlosses=max(maxlosses,losses)
        available=r['end_us']+int(delay*1e6)
        ledger.append({'market':r['market_id'],'entry':iso(r['execution_us']),'quantity':quantity,'cost':cost,'won':r['won'],'pnl':pnl,'equity':cash})
        close=r['won'] or (cap_mult is not None and mult>=cap_mult)
        if close:
            closedcycles+=1
            unrecovered+=int(cycle<0)
            cycle=0.
            step=0
        elif rule!='fixed':
            step+=1
    return {'rule':rule,'bankroll':bankroll,'cap_multiple':cap_mult,'depth_replay':depth,'settlement_delay_s':delay,
        'n':len(ledger),'profit':profit,'max_drawdown':drawdown,'max_drawdown_pct':ddpct,
        'turnover':turnover,'profit_per_cost':profit/turnover if turnover else None,
        'max_order_cost':maxcost,'max_quantity':maxqty,'minimum_initial_capital_on_this_path':max(0,required_bankroll),
        'max_loss_streak':maxlosses,'skipped':dict(skipped),'closed_cycles':closedcycles,'negative_closed_cycles':unrecovered,
        'ledger':ledger}

def volatility_sizing(rows, threshold_bps=3.5, high_quantity=5, bankroll=1000., delay=60):
    """Shadow sizing rule: halve or skip orders when BTC 60s RV is high.

    The threshold is fixed before reading the validation output. Missing BTC
    volatility keeps the normal 10-share size so this remains a sizing shadow,
    rather than silently becoming a data-availability filter.
    """
    cash, peak, drawdown, ddpct = bankroll, bankroll, 0., 0.
    profit, turnover, maxcost, maxqty = 0., 0., 0., 0.
    available = 0
    skipped = Counter()
    ledger = []
    high_count = 0
    for r in rows:
        if r['execution_us'] < available:
            skipped['unresolved_previous'] += 1
            continue
        high = r.get('btc_rv60_bps') is not None and r['btc_rv60_bps'] >= threshold_bps
        if high:
            high_count += 1
        quantity = high_quantity if high else 10
        if quantity == 0:
            skipped['high_volatility_skip'] += 1
            continue
        cost = consume(r['levels'], quantity, r['cap'])
        if cost is None:
            skipped['insufficient_depth'] += 1
            continue
        if cost > cash + 1e-8:
            skipped['bankroll_stop'] += 1
            break
        pnl = quantity * int(r['won']) - cost
        cash += pnl
        profit += pnl
        turnover += cost
        maxcost = max(maxcost, cost)
        maxqty = max(maxqty, quantity)
        peak = max(peak, cash)
        drawdown = max(drawdown, peak - cash)
        ddpct = max(ddpct, (peak - cash) / peak)
        available = r['end_us'] + int(delay * 1e6)
        ledger.append({'market': r['market_id'], 'entry': iso(r['execution_us']),
                       'quantity': quantity, 'cost': cost, 'won': r['won'],
                       'pnl': pnl, 'equity': cash, 'high_volatility': high})
    return {'rule': 'volatility_shadow', 'vol_threshold_bps': threshold_bps,
        'high_vol_quantity': high_quantity, 'bankroll': bankroll,
        'depth_replay': True, 'settlement_delay_s': delay, 'n': len(ledger),
        'profit': profit, 'max_drawdown': drawdown, 'max_drawdown_pct': ddpct,
        'turnover': turnover, 'profit_per_cost': profit / turnover if turnover else None,
        'max_order_cost': maxcost, 'max_quantity': maxqty,
        'high_vol_fills': high_count, 'skipped': dict(skipped), 'ledger': ledger}

def block_ci(rows):
    days=defaultdict(list)
    for r in rows:
        days[r['date']].append(r['pnl'])
    groups=list(days.values())
    if len(groups)<3:
        return None
    rng=random.Random(20261001)
    values=[]
    for _ in range(1000):
        sample=[x for _ in groups for x in rng.choice(groups)]
        values.append(mean(sample))
    return [quantile(values,.025),quantile(values,.975)]

def analyze_group(rows):
    rows=sorted(rows,key=lambda r:r['execution_us'])
    allstats=stats(rows)
    split=sorted(set(r['end_us'] for r in rows))[int(len(set(r['end_us'] for r in rows))*.7)]
    train=[r for r in rows if r['end_us']<split]
    valid=[r for r in rows if r['end_us']>=split]
    partitions={'all':rows,'train':train,'validation':valid}
    features=[]
    for field,label in FIELDS.items():
        values=[r[field] for r in train if r.get(field) is not None]
        if len(values)<20:
            features.append({'field':field,'label':label,'available':sum(r.get(field) is not None for r in rows),'insufficient_train':True})
            continue
        cuts=[quantile(values,1/3),quantile(values,2/3)]
        groups=[]
        for splitname,items in partitions.items():
            for tag in ('low','mid','high','missing'):
                selected=[r for r in items if ('missing' if r.get(field) is None else 'low' if r[field]<=cuts[0] else 'mid' if r[field]<=cuts[1] else 'high')==tag]
                groups.append({'split':splitname,'bucket':tag,**stats(selected)})
        features.append({'field':field,'label':label,'cuts_train_only':cuts,'available':sum(r.get(field) is not None for r in rows),'groups':groups,
            'success_median':median([r[field] for r in rows if r['won'] and r.get(field) is not None]) if any(r['won'] and r.get(field) is not None for r in rows) else None,
            'failure_median':median([r[field] for r in rows if not r['won'] and r.get(field) is not None]) if any(not r['won'] and r.get(field) is not None for r in rows) else None})
    flags=[]
    # Fixed hypotheses. High-volatility cutoff is learnt only on the early segment.
    for field in ('range15','rv30','btc_rv60_bps'):
        xs=[r[field] for r in train if r.get(field) is not None]
        if len(xs)>=20:
            threshold=quantile(xs,.9)
            flags.append((field+'_high',field,threshold,'ge'))
    flags.extend([('momentum5_reverse','momentum5',-.01,'le'),('btc_reverse','btc_return15_bps',0.,'lt'),('signal_delay_over2s','signal_delay_s',2.,'gt')])
    flag_stats=[]
    for name,field,threshold,op in flags:
        def check(r):
            v=r.get(field)
            return v is not None and {'ge':v>=threshold,'le':v<=threshold,'lt':v<threshold,'gt':v>threshold}[op]
        for r in rows:
            if check(r):
                r.setdefault('flags',[]).append(name)
        bysplit={}
        for splitname,rs in partitions.items():
            known=[r for r in rs if r.get(field) is not None]
            marked=[r for r in known if check(r)]
            kept=[r for r in known if not check(r)]
            bysplit[splitname]={'matched_baseline':stats(known),'flagged':stats(marked),'kept':stats(kept),
                'flagged_failures':sum(not r['won'] for r in marked),'flagged_winners':sum(r['won'] for r in marked),
                'missing':len(rs)-len(known),'kept_day_block_ci':block_ci(kept)}
        flag_stats.append({'name':name,'field':field,'threshold':threshold,'comparison':op,'splits':bysplit})
    transitions={}
    for won,label in [(True,'after_win'),(False,'after_loss')]:
        pairs=[b for a,b in zip(rows,rows[1:]) if a['won']==won and b['execution_us']>=a['end_us']+60e6]
        transitions[label]=stats(pairs)
    seq=0;maxseq=0
    for r in rows:
        seq=0 if r['won'] else seq+1
        maxseq=max(maxseq,seq)
    tests=[]
    for delay in (60,300):
        for rule,cap in [('fixed',None),('double',2),('double',4),('double',8),('double',None)]:
            tests.append(sizing(rows,rule,cap_mult=cap,delay=delay))
    for capital in (100,500):
        for rule,cap in [('fixed',None),('double',8),('double',None)]:
            tests.append(sizing(rows,rule,bankroll=capital,cap_mult=cap))
    # Unbounded linear-price calculation is explicitly a counterfactual, not execution evidence.
    tests.append(sizing(rows,'double',bankroll=1e12,depth=False))
    # Pre-registered shadow checks: fixed 3.5 bps BTC 60s RV threshold,
    # using half-size or skipping only the high-volatility entries.
    tests.append(volatility_sizing(rows, threshold_bps=3.5, high_quantity=5))
    tests.append(volatility_sizing(rows, threshold_bps=3.5, high_quantity=0))
    daily=[{'date':d,**stats([r for r in rows if r['date']==d])} for d in sorted({r['date'] for r in rows})]
    return {'stats':allstats,'split_utc':iso(split),'train':stats(train),'validation':stats(valid),
            'day_block_mean_pnl_ci95':block_ci(rows),'max_loss_streak_raw':maxseq,
            'transitions':transitions,'features':features,'flags':flag_stats,'sizing':tests,'daily':daily}

def fmt(v,d=4):
    return '—' if v is None else f'{v:.{d}f}'

def pct(v):
    return '—' if v is None else f'{v*100:.1f}%'

def report(out, groups, audit, rows):
    lines=['# 失败倍投、波动与逐笔失败诊断', '',
        '本报告冻结当前缓存；仅历史研究。每个策略、方向独立记账；不把同市场不同策略当成独立机会。', '',
        '## 数据与方法', '',
        f"- 数据范围（UTC）：{iso(min(r['execution_us'] for r in rows))} 至 {iso(max(r['execution_us'] for r in rows))}；缓存已结算市场：{audit['settled_markets']}。",
        '- 使用现有策略哈希精确匹配，避免把同名旧版本重复累计。0.80–0.85及0.81–0.82在这些缓存中是主动成交信号价格带；实际VWAP可低于下限。前轮将其直接称作实际入场价格带不严谨，本报告保留原策略作可比对照。',
        '- 信号≥10份；基准10份；最坏成交价≤信号带上限；持有到官方结算。费用逐档0.07×p×(1−p)，参照[官方费用文档](https://docs.polymarket.com/trading/fees)。历史逐市场费率未重新取证。',
        '- 新诊断特征同时要求source/received时间≤观测时刻；1秒采样，合约源时间年龄≤2秒，BTC≤3秒；至少80%窗口有效才计算波动。高低幅与实现波动分开。',
        '- 各策略按市场结束时间前70%/后30%切分；所有分位点在前70%固定。后30%是回顾性留后验证，这批历史曾被探索，不能冒充全新独立样本外。胜率给Wilson区间，均值给按UTC日期分块bootstrap区间。',
        '- 倍投首笔10份，输后×2，赢后回10；封顶2/4/8倍时，封顶那笔无论输赢均回10。每个方向同一时刻最多一笔，默认假设结束后60秒结果和资金可用，另测300秒。缓存缺实际结算接收时间，这仍是模拟假设。',
        '- 资金1000U主情景，100/500U敏感性；资金不足立即停止；深度不足跳过本市场并保留当前倍数。按原10份策略首个信号重放加仓，不重新搜寻该市场后续入场。',
        '- 最大回撤按结算后权益计算，不含持仓浮亏；最大单笔成本是本模型单仓资金占用。盘口静态覆盖不证明实际能成交，未加入额外网络延迟、市场冲击和部分成交。',
        '- 入场后仅查看本方向卖一报价路径，不把它称作可卖出价或可实现MFE/MAE；不据此自动止损。异动是关联或事后现象，不能证明每笔损失的原因。',
        f"- 跨源文件：{audit['cross']['files']}；损坏/截断文件：{len(audit['cross']['damaged_files'])}；坏JSON行：{audit['cross']['invalid_json']}。保留损坏前完整行，缺失不视为正常。DVOL/ATM IV缺独立可靠新鲜度保证，只列覆盖而不作为硬筛选。",'',
        '## 固定数量与失败倍投（1000U、结束后60秒可用）','',
        '| 策略 | 规则 | 成交 | 净收益U | 最大回撤U | 最大单笔成本U | 最大份数 | 深度跳过 | 资金停止 |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,g in groups.items():
        for t in g['sizing']:
            if t.get('vol_threshold_bps') is not None or t['bankroll']!=1000 or t['settlement_delay_s']!=60 or not t['depth_replay']:
                continue
            label='固定10份' if t['rule']=='fixed' else f"失败×2 / {'不封顶' if t['cap_multiple'] is None else str(t['cap_multiple'])+'倍封顶'}"
            lines.append(f"| {name} | {label} | {t['n']} | {t['profit']:+.2f} | {t['max_drawdown']:.2f} | {t['max_order_cost']:.2f} | {t['max_quantity']} | {t['skipped'].get('insufficient_depth',0)} | {t['skipped'].get('bankroll_stop',0)} |")
    lines += ['', '## 高波动影子仓位（BTC 60秒实现波动 ≥3.5bps）', '',
        '阈值在本轮回测前固定；缺少BTC波动值的记录按正常10份处理。该表只用于风险敏感性验证，不自动改线上规则。', '',
        '| 策略 | 情景 | 成交 | 净收益U | 最大回撤U | 最大单笔成本U | 最大份数 | 高波动记录 | 高波动跳过 |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,g in groups.items():
        for t in g['sizing']:
            if t.get('vol_threshold_bps') != 3.5 or t['bankroll'] != 1000 or t['settlement_delay_s'] != 60:
                continue
            label = '高波动半仓5份' if t['high_vol_quantity'] == 5 else '高波动跳过'
            lines.append(f"| {name} | {label} | {t['n']} | {t['profit']:+.2f} | {t['max_drawdown']:.2f} | {t['max_order_cost']:.2f} | {t['max_quantity']} | {t['high_vol_fills']} | {t['skipped'].get('high_volatility_skip', 0)} |")
    lines+=['','赢一次并不等于回本。例如价格0.82、费率0.07：10份输掉8.30332U；随后20份赢3.39336U，两笔仍净亏4.90996U。想靠下一笔一次补回前亏并赚一笔基础利润，约需58.94份，远超过20份。','',
        '## 策略分项、留后验证与波动率','']
    for name,g in groups.items():
        lines += [f'### {name}','',f"全样本：{g['stats']['n']}笔，胜率{pct(g['stats']['win_rate'])}，每份{fmt(g['stats']['pnl_per_share'])}；最大连续失败{g['max_loss_streak_raw']}。",
            f"留后起点：{g['split_utc']}；后30%：{g['validation']['n']}笔，胜率{pct(g['validation']['win_rate'])}，每份{fmt(g['validation']['pnl_per_share'])}。按日均值95%区间：{g['day_block_mean_pnl_ci95']}。",
            f"前笔失败后：{g['transitions']['after_loss']['n']}笔，胜率{pct(g['transitions']['after_loss']['win_rate'])}，每份{fmt(g['transitions']['after_loss']['pnl_per_share'])}；前笔成功后：{g['transitions']['after_win']['n']}笔，胜率{pct(g['transitions']['after_win']['win_rate'])}，每份{fmt(g['transitions']['after_win']['pnl_per_share'])}。",'',
            '| 波动指标（前70%分位界线） | 组 | 全样本 n/胜率/每份 | 后30% n/胜率/每份 |','|---|---|---|---|']
        for f in g['features']:
            if f['field'] not in ('range15','rv30','btc_rv60_bps','btc_range60_bps') or f.get('insufficient_train'):
                continue
            for bucket in ('low','mid','high','missing'):
                a=next(x for x in f['groups'] if x['split']=='all' and x['bucket']==bucket)
                b=next(x for x in f['groups'] if x['split']=='validation' and x['bucket']==bucket)
                lines.append(f"| {f['label']} ({fmt(f['cuts_train_only'][0])}/{fmt(f['cuts_train_only'][1])}) | {bucket} | {a['n']}/{pct(a['win_rate'])}/{fmt(a['pnl_per_share'])} | {b['n']}/{pct(b['win_rate'])}/{fmt(b['pnl_per_share'])} |")
        lines+=['','| 排除异动（仅入场前） | 后30%有效基线 n/每份 | 保留 n/每份 | 排除失败 | 误删成功 | 缺失 |','|---|---|---|---:|---:|---:|']
        for f in g['flags']:
            v=f['splits']['validation'];a=v['matched_baseline'];b=v['kept']
            lines.append(f"| {f['name']}（{f['comparison']} {fmt(f['threshold'])}） | {a['n']}/{fmt(a['pnl_per_share'])} | {b['n']}/{fmt(b['pnl_per_share'])} | {v['flagged_failures']} | {v['flagged_winners']} | {v['missing']} |")
    lines+=['','## 附件与复现','',
        '- [逐笔失败明细](失败逐笔明细.md)：每笔时间、策略、报价路径、入场前异动与缺失字段。',
        '- [完整结果](results.json)：每个资金与结算延迟场景、逐笔权益账本、分组胜率区间、分日收益。',
        '- [特征记录](diagnostic_rows.json)：成功和失败均保留，便于对照；[数据质量](audit.json)。',
        '- 本次没有改变监控、纸上入场或实盘规则。', '',
        '```bash','PYTHONPATH=src python3 scripts/analysis/analyze_loss_sizing_volatility.py','```','']
    (out/'失败倍投与波动验证.md').write_text('\n'.join(lines))
    detail=['# 逐笔失败明细','', '时间为UTC。异动阈值仅用前70%拟定；训练段标记属于拟合内描述。没有异动标签不等于没有风险。卖一路径只描述报价，不能推导可卖出收益。', '',
            '| 策略 | 市场 | 入场UTC | 留后验证 | 入场价 | 延迟秒 | 合约15秒幅 | BTC60秒RV bps | 前5秒动量 | 入场前标签 | 报价跌5点秒 | 跌破0.5秒 | 先曾涨5点 | 后续覆盖 |',
            '|---|---|---|---|---:|---:|---:|---:|---:|---|---:|---:|---|---:|']
    for r in sorted(rows,key=lambda r:(r['execution_us'],r['strategy'])):
        if r['won']:
            continue
        detail.append(f"| {r['strategy']} | {r['market_id']} | {iso(r['execution_us'])} | {iso(r['end_us'])>=groups[r['strategy']]['split_utc']} | {fmt(r['entry_vwap'])} | {fmt(r['signal_delay_s'],2)} | {fmt(r['range15'])} | {fmt(r['btc_rv60_bps'])} | {fmt(r['momentum5'])} | {', '.join(r.get('flags',[])) or '未检出所测标签'} | {r['post_drop05_seconds']} | {r['post_below_half_seconds']} | {r['post_bounce05']} | {pct(r['post_ask_coverage'])} |")
    (out/'失败逐笔明细.md').write_text('\n'.join(detail)+'\n')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,default=ROOT/'outputs/research_tests/t061_loss_sizing_volatility_20261001')
    parser.add_argument('--reuse-extraction',action='store_true')
    args=parser.parse_args();out=args.out;out.mkdir(parents=True,exist_ok=True)
    if args.reuse_extraction:
        rows=json.loads((out/'diagnostic_rows.json').read_text())
        audit=json.loads((out/'audit.json').read_text())
        for r in rows:r.pop('flags',None)
    else:
        con=sqlite3.connect(f'file:{ROOT}/data/derived/live_strategy_cache.sqlite3?mode=ro',uri=True)
        con.row_factory=sqlite3.Row
        con.execute('BEGIN')
        specs={c.case_id:(c.case_hash,float(c.price_cap)) for c in load_cases(ROOT/'configs/research/live_strategy_cases_v1.json') if c.case_id in BASE}
        for side in ('up','down'):
            name=f'band081_082_{side}_90_10'
            found=con.execute('select case_hash from case_specs where case_id=?',(name,)).fetchall()
            if len(found)==1:specs[name]=(found[0][0],.82)
        rows=[];audit={'model':MODEL,'created_utc':datetime.now(timezone.utc).isoformat(),'cases':specs,
                     'settled_markets':con.execute("select count(*) from markets where winner in ('UP','DOWN')").fetchone()[0]}
        memo={}
        for name,(h,cap) in specs.items():
            base=con.execute('SELECT r.*,m.end_us FROM case_results r JOIN markets m USING(market_id) WHERE case_hash=? AND filled=1 ORDER BY execution_us',(h,)).fetchall()
            for raw in base:
                r=dict(raw)
                for k in ('entry_vwap','fee_per_share','pnl_per_share'):r[k]=float(r[k])
                r.update(strategy=name,cap=cap,won=r['outcome']==r['winner'],pnl=r['pnl_per_share'],date=iso(r['execution_us'])[:10])
                key=(r['market_id'],r['outcome'],r['execution_us'],cap)
                if key not in memo:memo[key]=extract(con,r)
                r.update(memo[key]);rows.append(r)
            print(name,'fills',len(base),'unique extracted',len(memo),flush=True)
        con.close()
        bounds={}
        for r in rows:
            lo,hi=bounds.get(r['market_id'],(r['execution_us'],r['end_us']))
            bounds[r['market_id']]=(min(lo,r['execution_us']),max(hi,r['end_us']))
        dump(out/'poly_extraction.json',rows)
        projected,audit['cross']=stream_cross(set(bounds),bounds,out)
        for r in rows:add_cross(r,projected.get(r['market_id'],[]))
        audit['feature_coverage']={k:sum(r.get(k) is not None for r in rows) for k in list(FIELDS)+['dvol_observed','atm_iv_observed']}
        audit['unique_market_outcome_entries']=len({(r['market_id'],r['outcome'],r['execution_us']) for r in rows})
        audit['actual_entry_below_signal_lower']={n:sum(r['entry_vwap']<(.81 if n.startswith('band') else .8) for r in rows if r['strategy']==n) for n in specs}
        dump(out/'diagnostic_rows.json',rows);dump(out/'audit.json',audit)
    groups={}
    for name in sorted({r['strategy'] for r in rows}):
        groups[name]=analyze_group([r for r in rows if r['strategy']==name])
    dump(out/'results.json',{'model':MODEL,'audit':audit,'groups':groups})
    dump(out/'diagnostic_rows.json',rows)
    report(out,groups,audit,rows)
    print('done',out,flush=True)

if __name__=='__main__':
    main()

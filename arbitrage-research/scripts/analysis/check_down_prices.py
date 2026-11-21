#!/usr/bin/env python3
"""
检查9个市场中 DOWN token 的价格分布，找出为什么没有跌概率 70-88% 的机会。
"""
import json
from datetime import datetime

# 9个市场的slug列表
markets = [
    "btc-updown-5m-1787220300",
    "btc-updown-5m-1787220000", 
    "btc-updown-5m-1787219700",
    "btc-updown-5m-1787219100",
    "btc-updown-5m-1787218800",
    "btc-updown-5m-1787218500",
    "btc-updown-5m-1787218200",
    "btc-updown-5m-1787217900",
    "btc-updown-5m-1787217600"
]

for slug in markets:
    part1_file = f"{slug}_part1.json"
    part2_file = f"{slug}_part2.json"
    
    try:
        with open(part1_file, 'r') as f:
            part1 = json.load(f)
        with open(part2_file, 'r') as f:
            part2 = json.load(f)
        
        # 合并快照
        all_snapshots = part1['snapshots'] + part2['snapshots']
        snapshot_dict = {s['time']: s for s in all_snapshots}
        merged_snapshots = sorted(snapshot_dict.values(), key=lambda x: x['time'])
        
        # 检查最后3分钟内 DOWN token 的价格
        end_time = datetime.fromisoformat(part1['market']['end_time'].replace('Z', '+00:00'))
        start_window = end_time.timestamp() - 180
        
        down_prices_3min = []
        for snapshot in merged_snapshots:
            snapshot_time = datetime.fromisoformat(snapshot['time'].replace('Z', '+00:00:00'))
            
            if snapshot_time.timestamp() < start_window:
                continue
            
            if snapshot['orderbook_down'] and snapshot['orderbook_down'].get('asks'):
                down_prices_3min.append(snapshot['orderbook_down']['asks'][0]['price'])
        
        if down_prices_3min:
            min_price = min(down_prices_3min)
            max_price = max(down_prices_3min)
            in_70_88 = len([p for p in down_prices_3min if 0.70 <= p <= 0.88])
            in_60_80 = len([p for p in down_prices_3min if 0.60 <= p <= 0.80])
            in_50_90 = len([p for p in down_prices_3min if 0.50 <= p <= 0.90])
            
            print(f"{slug}:")
            print(f"  DOWN价格范围: {min_price:.2f} - {max_price:.2f}")
            print(f"  在70-88范围: {in_70_88}/{len(down_prices_3min)}")
            print(f"  在60-80范围: {in_60_80}/{len(down_prices_3min)}")
            print(f"  在50-90范围: {in_50_90}/{len(down_prices_3min)}")
        else:
            print(f"{slug}: 无 DOWN 价格数据")
        
        print()
        
    except Exception as e:
        print(f"{slug}: 错误 - {e}\n")
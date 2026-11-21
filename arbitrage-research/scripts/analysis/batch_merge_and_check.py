#!/usr/bin/env python3
"""
批量合并10个市场的两部分数据，并检查最后3分钟内的价格范围。
"""
import json
from datetime import datetime

# 10个市场的slug列表
markets = [
    "btc-updown-5m-1787220300",
    "btc-updown-5m-1787220000", 
    "btc-updown-5m-1787219700",
    "btc-updown-5m-1787219400",
    "btc-updown-5m-1787219100",
    "btc-updown-5m-1787218800",
    "btc-updown-5m-1787218500",
    "btc-updown-5m-1787218200",
    "btc-updown-5m-1787217900",
    "btc-updown-5m-1787217600"
]

print("开始批量合并和检查数据...\n")

valid_markets = []

for slug in markets:
    part1_file = f"{slug}_part1.json"
    part2_file = f"{slug}_part2.json"
    
    try:
        # 读取两部分数据
        with open(part1_file, 'r') as f:
            part1 = json.load(f)
        
        with open(part2_file, 'r') as f:
            part2 = json.load(f)
        
        # 合并快照
        all_snapshots = part1['snapshots'] + part2['snapshots']
        
        # 去重（按时间）
        snapshot_dict = {s['time']: s for s in all_snapshots}
        merged_snapshots = sorted(snapshot_dict.values(), key=lambda x: x['time'])
        
        # 检查最后3分钟的价格范围
        end_time = datetime.fromisoformat(part1['market']['end_time'].replace('Z', '+00:00'))
        start_window = end_time.timestamp() - 180
        
        last_3min_prices = []
        for snapshot in merged_snapshots:
            snapshot_time = datetime.fromisoformat(snapshot['time'].replace('Z', '+00:00'))
            
            if snapshot_time.timestamp() < start_window:
                continue
            
            if snapshot['orderbook_up'] and snapshot['orderbook_up'].get('asks'):
                last_3min_prices.append(snapshot['orderbook_up']['asks'][0]['price'])
            if snapshot['orderbook_up'] and snapshot['orderbook_up'].get('bids'):
                last_3min_prices.append(snapshot['orderbook_up']['bids'][0]['price'])
        
        if last_3min_prices:
            min_price = min(last_3min_prices)
            max_price = max(last_3min_prices)
            in_range = len([p for p in last_3min_prices if 0.70 <= p <= 0.88])
            
            print(f"{slug}:")
            print(f"  合并快照数: {len(merged_snapshots)}")
            print(f"  最后3分钟价格范围: {min_price:.2f} - {max_price:.2f}")
            print(f"  在0.70-0.88范围内: {in_range}/{len(last_3min_prices)}")
            
            if in_range > 0:
                print(f"  ✓ 符合条件")
                valid_markets.append(slug)
            else:
                print(f"  ✗ 不符合条件")
        else:
            print(f"{slug}: 无最后3分钟数据")
        
        print()
        
    except Exception as e:
        print(f"{slug}: 错误 - {e}\n")

print(f"\n符合条件的市场数: {len(valid_markets)}")
print(f"符合条件的市场: {valid_markets}")
#!/usr/bin/env python3
"""
合并 snap_26 的两部分数据，并验证最后 3 分钟的价格范围。
"""
import json

# 读取两部分数据
with open('snap_26_part1.json', 'r') as f:
    part1 = json.load(f)

with open('snap_26_offset.json', 'r') as f:
    part2 = json.load(f)

# 合并快照
all_snapshots = part1['snapshots'] + part2['snapshots']

# 去重（按时间）
snapshot_dict = {s['time']: s for s in all_snapshots}
merged_snapshots = sorted(snapshot_dict.values(), key=lambda x: x['time'])

# 创建合并后的数据
merged_data = {
    'market': part1['market'],
    'snapshots': merged_snapshots
}

# 保存合并后的数据
with open('snap_26_merged.json', 'w') as f:
    json.dump(merged_data, f, indent=2)

print(f'合并完成:')
print(f'  第一部分: {len(part1["snapshots"])} 个快照')
print(f'  第二部分: {len(part2["snapshots"])} 个快照')
print(f'  合并后: {len(merged_snapshots)} 个快照（去重后）')
print(f'  保存到: snap_26_merged.json')

# 验证最后 3 分钟的价格范围
from datetime import datetime

end_time = datetime.fromisoformat(merged_data['market']['end_time'].replace('Z', '+00:00'))
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
    print(f'\n最后 3 分钟价格范围: {min_price:.2f} - {max_price:.2f}')
    print(f'在 0.70-0.88 范围内: {in_range}/{len(last_3min_prices)}')
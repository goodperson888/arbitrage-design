#!/usr/bin/env python3
"""
检查市场的最后 3 分钟内是否有价格在 0.70-0.88 范围内。
"""
import json
from datetime import datetime

def check_last_3min_price_range(snapshots_file: str):
    """检查市场在最后 3 分钟内的价格范围"""
    with open(snapshots_file, "r") as f:
        data = json.load(f)
    
    snapshots = data["snapshots"]
    market_end_time = datetime.fromisoformat(data["market"]["end_time"].replace("Z", "+00:00"))
    start_window = market_end_time.timestamp() - 180  # 最后 3 分钟
    
    last_3min_prices = []
    for snapshot in snapshots:
        snapshot_time = datetime.fromisoformat(snapshot["time"].replace("Z", "+00:00"))
        
        # 只看最后 3 分钟
        if snapshot_time.timestamp() < start_window:
            continue
        
        if snapshot["orderbook_up"] and snapshot["orderbook_up"].get("asks"):
            last_3min_prices.append(snapshot["orderbook_up"]["asks"][0]["price"])
        if snapshot["orderbook_up"] and snapshot["orderbook_up"].get("bids"):
            last_3min_prices.append(snapshot["orderbook_up"]["bids"][0]["price"])
    
    if last_3min_prices:
        min_price = min(last_3min_prices)
        max_price = max(last_3min_prices)
        in_range = len([p for p in last_3min_prices if 0.70 <= p <= 0.88])
        return {
            "min": min_price,
            "max": max_price,
            "count": len(last_3min_prices),
            "in_range": in_range
        }
    else:
        return None

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        result = check_last_3min_price_range(sys.argv[1])
        if result:
            print(f"最后 3 分钟价格范围: {result['min']:.2f} - {result['max']:.2f}")
            print(f"在 0.70-0.88 范围内: {result['in_range']}/{result['count']}")
        else:
            print("无数据")
    else:
        # 检查已下载的所有市场
        for i in range(17, 27):
            file = f"snap_{i}.json"
            try:
                result = check_last_3min_price_range(file)
                if result:
                    if result['in_range'] > 0:
                        print(f"✓ {file}: {result['min']:.2f} - {result['max']:.2f}, {result['in_range']}/{result['count']} in range")
                    else:
                        print(f"  {file}: {result['min']:.2f} - {result['max']:.2f}, {result['in_range']}/{result['count']} in range")
            except Exception as e:
                print(f"  {file}: 错误 - {e}")
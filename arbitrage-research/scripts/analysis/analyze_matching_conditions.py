#!/usr/bin/env python3
"""
分析 5 分钟市场数据，检查符合策略条件的时刻：
- 在最后 2-3 分钟内
- 价格在 0.70-0.88 范围内
"""
import json
from datetime import datetime
from pathlib import Path

def parse_timestamp_to_us(timestamp_str: str) -> int:
    """将 ISO 时间戳转换为微秒"""
    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000)

def analyze_data():
    events_file = Path("btc_5m_full.ndjson")
    
    # 按市场分组
    markets = {}
    
    with open(events_file, "r") as f:
        for line in f:
            event = json.loads(line)
            
            if event["eventType"] == "MARKET_DEFINITION":
                market_id = event["marketId"]
                markets[market_id] = {
                    "event_end_us": int(event["payload"]["eventEndUs"]),
                    "quotes": []
                }
            elif event["eventType"] == "PRICE_QUOTE":
                market_id = event["marketId"]
                if market_id in markets:
                    best_ask = float(event["payload"]["bestAsk"])
                    source_time_us = event["sourceTimeUs"]
                    markets[market_id]["quotes"].append({
                        "time_us": source_time_us,
                        "price": best_ask
                    })
    
    # 分析每个市场
    print(f"分析了 {len(markets)} 个市场\n")
    
    total_quotes = 0
    total_in_2min_range = 0
    total_in_3min_range = 0
    total_in_price_range = 0
    total_matching_2min = 0
    total_matching_3min = 0
    
    for market_id, data in markets.items():
        end_time_us = data["event_end_us"]
        quotes = data["quotes"]
        
        in_2min = 0
        in_3min = 0
        in_price = 0
        matching_2min = 0
        matching_3min = 0
        
        for quote in quotes:
            time_us = quote["time_us"]
            price = quote["price"]
            remaining_us = end_time_us - time_us
            remaining_seconds = remaining_us / 1_000_000
            
            total_quotes += 1
            
            # 检查时间窗口
            if remaining_seconds <= 120:  # 2 分钟内
                in_2min += 1
            if remaining_seconds <= 180:  # 3 分钟内
                in_3min += 1
            
            # 检查价格范围
            if 0.70 <= price <= 0.88:
                in_price += 1
                
                # 检查是否同时满足时间窗口
                if remaining_seconds <= 120:
                    matching_2min += 1
                if remaining_seconds <= 180:
                    matching_3min += 1
        
        total_in_2min_range += in_2min
        total_in_3min_range += in_3min
        total_in_price_range += in_price
        total_matching_2min += matching_2min
        total_matching_3min += matching_3min
        
        print(f"{market_id}:")
        print(f"  总报价数: {len(quotes)}")
        print(f"  最后 2 分钟内: {in_2min}")
        print(f"  最后 3 分钟内: {in_3min}")
        print(f"  价格在 0.70-0.88: {in_price}")
        print(f"  ✓ 2 分钟内且价格在范围内: {matching_2min}")
        print(f"  ✓ 3 分钟内且价格在范围内: {matching_3min}")
        print()
    
    print("总计:")
    print(f"  总报价数: {total_quotes}")
    print(f"  最后 2 分钟内: {total_in_2min_range}")
    print(f"  最后 3 分钟内: {total_in_3min_range}")
    print(f"  价格在 0.70-0.88: {total_in_price_range}")
    print(f"  ✓ 2 分钟内且价格在范围内: {total_matching_2min}")
    print(f"  ✓ 3 分钟内且价格在范围内: {total_matching_3min}")

if __name__ == "__main__":
    analyze_data()
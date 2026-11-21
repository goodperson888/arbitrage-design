#!/usr/bin/env python3
"""
分析 NDJSON 数据的价格范围，找出符合策略条件的市场。
"""
import json

def analyze_price_range(ndjson_file: str):
    """分析价格范围"""
    prices = []
    market_prices = {}
    
    with open(ndjson_file, "r") as f:
        for line in f:
            event = json.loads(line)
            if event["eventType"] == "PRICE_QUOTE":
                best_ask = float(event["payload"]["bestAsk"])
                best_bid = float(event["payload"]["bestBid"])
                market_id = event["marketId"]
                
                if market_id not in market_prices:
                    market_prices[market_id] = []
                market_prices[market_id].append(best_ask)
                market_prices[market_id].append(best_bid)
                prices.extend([best_ask, best_bid])
    
    print(f"总价格点数: {len(prices)}")
    print(f"价格范围: {min(prices):.2f} - {max(prices):.2f}")
    print(f"平均价格: {sum(prices)/len(prices):.2f}")
    
    # 统计在 0.70-0.88 范围内的价格
    in_range = [p for p in prices if 0.70 <= p <= 0.88]
    print(f"在 0.70-0.88 范围内的价格点: {len(in_range)} / {len(prices)} ({len(in_range)/len(prices)*100:.1f}%)")
    
    # 按市场统计
    print("\n各市场价格范围:")
    for market_id, m_prices in market_prices.items():
        print(f"  {market_id}: {min(m_prices):.2f} - {max(m_prices):.2f}")
        in_range_count = len([p for p in m_prices if 0.70 <= p <= 0.88])
        if in_range_count > 0:
            print(f"    ✓ 有 {in_range_count} 个价格点在 0.70-0.88 范围内")
        else:
            print(f"    ✗ 无价格点在 0.70-0.88 范围内")

if __name__ == "__main__":
    analyze_price_range("btc_5m_15markets.ndjson")
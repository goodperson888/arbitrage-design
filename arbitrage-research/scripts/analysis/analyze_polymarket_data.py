#!/usr/bin/env python3
"""
基于现有示例数据的Polymarket功能验证
分析示例数据中包含的市场特征
"""
import sys
sys.path.insert(0, "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/src")

from arb_research.io import read_events
from datetime import datetime, timezone, timedelta
from collections import defaultdict

def analyze_polymarket_data():
    """分析现有的Polymarket示例数据"""
    print("=== Polymarket示例数据分析 ===")
    
    # 读取示例数据
    from pathlib import Path
    events_file = Path("/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/examples/prediction_events.ndjson")
    events = list(read_events(events_file))
    
    print(f"总事件数: {len(events)}")
    
    # 按市场分组分析
    markets = defaultdict(list)
    for event in events:
        market_id = event.market_id
        markets[market_id].append(event)
    
    print(f"市场数量: {len(markets)}")
    
    # 分析每个市场
    for market_id, market_events in markets.items():
        print(f"\n=== 市场: {market_id} ===")
        print(f"事件数量: {len(market_events)}")
        
        # 分析事件类型
        event_types = [e.event_type for e in market_events]
        print(f"事件类型: {set(event_types)}")
        
        # 分析时间范围
        source_times = [e.source_time_us for e in market_events if e.source_time_us]
        if source_times:
            start_time = min(source_times) / 1_000_000
            end_time = max(source_times) / 1_000_000
            duration_seconds = (end_time - start_time)
            duration_minutes = duration_seconds / 60
            
            print(f"开始时间: {datetime.fromtimestamp(start_time, tz=timezone.utc)}")
            print(f"结束时间: {datetime.fromtimestamp(end_time, tz=timezone.utc)}")
            print(f"持续时间: {duration_seconds:.1f}秒 ({duration_minutes:.1f}分钟)")
        
        # 分析价格报价
        price_quotes = [e for e in market_events if e.event_type == "PRICE_QUOTE"]
        if price_quotes:
            print(f"价格报价数量: {len(price_quotes)}")
            
            # 分析价格范围
            prices = []
            for quote in price_quotes:
                if quote.payload.get("bestBid"):
                    prices.append(float(quote.payload["bestBid"]))
                if quote.payload.get("bestAsk"):
                    prices.append(float(quote.payload["bestAsk"]))
            
            if prices:
                print(f"价格范围: {min(prices):.3f} - {max(prices):.3f}")
                print(f"平均价格: {sum(prices)/len(prices):.3f}")
        
        # 分析结算
        settlements = [e for e in market_events if e.event_type == "SETTLEMENT"]
        if settlements:
            print(f"结算事件: {len(settlements)}")
            for settlement in settlements:
                print(f"  获胜结果: {settlement.payload.get('winnerOutcome')}")
                print(f"  结算价格: {settlement.payload.get('settlementPrice')}")

def simulate_short_term_markets():
    """模拟短期预测市场的特征"""
    print("\n=== 模拟短期预测市场特征 ===")
    
    # 你需要的时间周期
    time_periods = {
        "5分钟": timedelta(minutes=5),
        "15分钟": timedelta(minutes=15),
        "30分钟": timedelta(minutes=30),
        "1小时": timedelta(hours=1),
    }
    
    print("目标时间周期:")
    for period, duration in time_periods.items():
        print(f"  {period}: {duration.total_seconds()}秒")
    
    print("\nPolymarket结算特征:")
    print("  - 使用Chainlink 30/60秒TWAP")
    print("  - 减少价格操纵风险")
    print("  - 结算规则透明可验证")
    
    print("\n与你的套利策略匹配度:")
    print("  - S1跨平台互补结果套利: 高匹配 (结算规则一致)")
    print("  - S2同平台完整份额套利: 高匹配 (同一平台规则)")
    print("  - S3跨平台基准价覆盖套利: 高匹配 (共享Chainlink数据)")
    print("  - S4 Maker成交后对冲: 高匹配 (可靠的订单簿)")

if __name__ == "__main__":
    analyze_polymarket_data()
    simulate_short_term_markets()
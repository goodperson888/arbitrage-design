#!/usr/bin/env python3
"""
手动数据导入和分析工具
支持手动输入或从其他来源导入Polymarket数据
"""
import sys
sys.path.insert(0, "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/src")

from pathlib import Path
import json
from datetime import datetime, timezone
from decimal import Decimal
from arb_research.io import write_events
from arb_research.models import MarketEvent

def create_sample_btc_eth_markets():
    """创建模拟的BTC/ETH短期预测市场数据"""
    print("=== 创建模拟BTC/ETH短期预测市场数据 ===")
    
    # 模拟多个5分钟周期的BTC预测市场
    base_time = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    
    events = []
    
    # 创建10个模拟市场，每个市场包含不同的事件
    for i in range(10):
        market_id = f"btc-5m-{i+1}"
        start_time = base_time + i * 300_000_000  # 每5分钟一个市场
        end_time = start_time + 300_000_000  # 5分钟周期
        
        # 市场定义
        events.append(MarketEvent(
            schema_version=1,
            event_id=f"evt-def-{i*4}",
            event_type="MARKET_DEFINITION",
            venue="POLYMARKET",
            market_id=market_id,
            source_time_us=start_time,
            received_time_us=start_time + 1000,
            payload={
                "underlying": "BTC/USD",
                "eventStartUs": str(start_time),
                "eventEndUs": str(end_time),
                "outcomes": ["UP", "DOWN"]
            }
        ))
        
        # 模拟价格波动 (0.70-0.88范围)
        initial_price = 0.75 + (i % 10) * 0.01  # 0.75-0.84
        
        # 添加几个价格报价
        for j in range(5):
            quote_time = start_time + (j + 1) * 60_000_000  # 每分钟一个报价
            price = initial_price + (j * 0.01)  # 价格逐渐上升
            
            events.append(MarketEvent(
                schema_version=1,
                event_id=f"evt-quote-{i*4 + j}",
                event_type="PRICE_QUOTE",
                venue="POLYMARKET",
                market_id=market_id,
                source_time_us=quote_time,
                received_time_us=quote_time + 1000,
                payload={
                    "outcome": "UP",
                    "bestBid": str(price - 0.01),
                    "bestBidSize": "100",
                    "bestAsk": str(price),
                    "bestAskSize": "50"
                }
            ))
        
        # 结算事件
        winner = "UP" if i % 2 == 0 else "DOWN"  # 交替获胜
        events.append(MarketEvent(
            schema_version=1,
            event_id=f"evt-settle-{i*4 + 3}",
            event_type="SETTLEMENT",
            venue="POLYMARKET",
            market_id=market_id,
            source_time_us=end_time + 10_000_000,
            received_time_us=end_time + 10_001_000,
            payload={
                "winnerOutcome": winner,
                "settlementPrice": "1"
            }
        ))
    
    return events

def import_from_json(json_file: Path):
    """从JSON文件导入市场数据"""
    print(f"从JSON文件导入数据: {json_file}")
    
    with json_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
    
    events = []
    for item in data:
        # 根据JSON结构转换为MarketEvent
        # 这里需要根据实际JSON格式调整
        pass
    
    return events

def main():
    output_dir = Path("/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/data")
    output_dir.mkdir(exist_ok=True)
    
    # 创建模拟数据
    events = create_sample_btc_eth_markets()
    
    # 保存为标准格式
    output_file = output_dir / "btc_eth_5min_sample.ndjson"
    write_events(output_file, events)
    
    print(f"\n✅ 模拟数据已创建: {output_file}")
    print(f"  事件数量: {len(events)}")
    print(f"  市场数量: 10")
    print(f"  时间周期: 5分钟")
    print(f"  价格范围: 0.70-0.88")
    
    # 统计信息
    market_defs = [e for e in events if e.event_type == "MARKET_DEFINITION"]
    price_quotes = [e for e in events if e.event_type == "PRICE_QUOTE"]
    settlements = [e for e in events if e.event_type == "SETTLEMENT"]
    
    print(f"\n数据统计:")
    print(f"  市场定义: {len(market_defs)}")
    print(f"  价格报价: {len(price_quotes)}")
    print(f"  结算事件: {len(settlements)}")
    
    return output_file

if __name__ == "__main__":
    output_file = main()
    print(f"\n可以使用此文件进行策略验证:")
    print(f"python3 validate_strategy_with_real_data.py {output_file}")
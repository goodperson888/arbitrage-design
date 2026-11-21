#!/usr/bin/env python3
"""
Polymarket数据验证 - 使用代理连接
"""
import sys
sys.path.insert(0, "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/src")

from arb_research.connectors.polymarket_backfill import PolymarketBackfill

def main():
    print("=== Polymarket数据验证 ===")
    
    # 使用代理
    proxy = "http://127.0.0.1:7890"
    
    try:
        with PolymarketBackfill(proxy=proxy) as backfill:
            # 获取市场列表
            print("获取市场列表...")
            markets = backfill.get_markets(limit=50, closed=True)
            
            print(f"找到 {len(markets)} 个市场")
            
            # 筛选包含BTC、ETH、Bitcoin、Ethereum的市场
            crypto_markets = []
            for market in markets:
                question_lower = market.question.lower()
                if any(keyword in question_lower for keyword in ['btc', 'bitcoin', 'eth', 'ethereum', 'bitcoin price', 'ethereum price']):
                    crypto_markets.append(market)
            
            print(f"\n找到 {len(crypto_markets)} 个加密货币相关市场:")
            
            for i, market in enumerate(crypto_markets[:15], 1):  # 显示前15个
                print(f"{i}. {market.question}")
                print(f"   结束时间: {market.end_date}")
                print(f"   结果: {market.outcomes}")
                print()
            
            # 分析时间框架
            print("=== 分析时间框架 ===")
            from datetime import datetime, timezone, timedelta
            
            time_categories = {
                "5分钟": timedelta(minutes=5),
                "15分钟": timedelta(minutes=15), 
                "30分钟": timedelta(minutes=30),
                "1小时": timedelta(hours=1),
            }
            
            for market in crypto_markets[:10]:
                try:
                    end_date = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                    print(f"市场: {market.question[:50]}...")
                    print(f"结束时间: {end_date}")
                    
                    # 检查是否有明确的持续时间信息
                    if market.description:
                        print(f"描述: {market.description[:100]}...")
                    print()
                except Exception as e:
                    print(f"解析时间错误: {e}")
                    
    except Exception as e:
        print(f"连接错误: {e}")
        print("尝试其他连接方式...")

if __name__ == "__main__":
    main()
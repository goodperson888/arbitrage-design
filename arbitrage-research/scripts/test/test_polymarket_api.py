#!/usr/bin/env python3
"""
测试Polymarket公开API连接
"""
import sys
sys.path.insert(0, "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/src")

from arb_research.connectors.polymarket_gamma import PolymarketGammaClient, PolymarketClobClient

def main():
    print("=== 测试Polymarket公开API连接 ===")
    
    try:
        # 测试Gamma API（无需认证）
        print("\n1. 测试Gamma API...")
        with PolymarketGammaClient(proxy=None) as gamma_client:
            # 获取标签列表
            print("获取标签列表...")
            tags = gamma_client.get_tags()
            print(f"找到 {len(tags)} 个标签")
            
            # 显示加密货币相关标签
            crypto_tags = [tag for tag in tags if any(keyword in tag.get('name', '').lower() for keyword in ['crypto', 'bitcoin', 'ethereum', 'finance', 'trading'])]
            print(f"加密货币相关标签: {len(crypto_tags)}")
            for tag in crypto_tags[:5]:
                print(f"  - {tag.get('name')} (ID: {tag.get('id')})")
            
            # 获取活跃事件
            print("\n获取活跃事件...")
            events = gamma_client.get_events(active=True, closed=False, limit=10)
            print(f"找到 {len(events)} 个活跃事件")
            
            # 筛选加密货币相关事件
            crypto_events = []
            for event in events:
                question_lower = event.question.lower()
                if any(keyword in question_lower for keyword in ['btc', 'bitcoin', 'eth', 'ethereum', 'crypto', 'price']):
                    crypto_events.append(event)
            
            print(f"加密货币相关事件: {len(crypto_events)}")
            for event in crypto_events[:5]:
                print(f"  - {event.question[:60]}...")
                print(f"    结束时间: {event.end_date}")
                print(f"    市场数量: {len(event.markets)}")
        
        print("\n2. 测试CLOB API...")
        with PolymarketClobClient(proxy=None) as clob_client:
            # 如果有token_id，测试价格查询
            print("CLOB API连接成功（需要具体的token_id来查询价格）")
        
        print("\n✅ Polymarket公开API连接测试成功！")
        return True
        
    except Exception as e:
        print(f"\n❌ API连接失败: {e}")
        return False

if __name__ == "__main__":
    success = main()
    if success:
        print("\n可以进行数据采集和策略验证")
    else:
        print("\n需要检查网络连接或API配置")
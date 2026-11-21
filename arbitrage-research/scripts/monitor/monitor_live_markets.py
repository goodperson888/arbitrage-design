#!/usr/bin/env python3
"""
实时监控 Polymarket BTC 5 分钟市场，记录价格变化并检测策略触发条件。
"""
import requests
import json
import os
import time
from datetime import datetime
from pathlib import Path

API_KEY = os.environ["POLYHISTORICAL_API_KEY"]
BASE_URL = "https://api.polyhistorical.com/v1"
PROXY = "http://127.0.0.1:7890"

DATA_DIR = Path("live_monitor_data")
DATA_DIR.mkdir(exist_ok=True)

# 策略参数
DOWN_THRESHOLD = 0.70  # DOWN 价格达到此值时触发

def fetch_markets(coin="BTC", market_type="5m", resolved=False, limit=10):
    """获取最新的市场列表"""
    params = {"coin": coin, "limit": limit, "resolved": resolved}
    if market_type:
        params["market_type"] = market_type
    
    headers = {"X-API-Key": API_KEY}
    proxies = {"http": PROXY, "https": PROXY}
    
    try:
        response = requests.get(f"{BASE_URL}/markets", params=params, headers=headers, proxies=proxies)
        response.raise_for_status()
        return response.json()["markets"]
    except Exception as e:
        print(f"获取市场列表失败: {e}")
        return []

def fetch_snapshots(slug, limit=100):
    """获取市场的快照数据"""
    params = {"include_orderbook": True, "limit": limit}
    headers = {"X-API-Key": API_KEY}
    proxies = {"http": PROXY, "https": PROXY}
    
    try:
        response = requests.get(f"{BASE_URL}/markets/{slug}/snapshots", params=params, headers=headers, proxies=proxies)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"获取快照失败 {slug}: {e}")
        return None

def check_strategy(market_data):
    """检查策略是否触发"""
    if not market_data or 'snapshots' not in market_data:
        return None
    
    snapshots = market_data['snapshots']
    if not snapshots:
        return None
    
    # 获取最新的 DOWN 价格
    latest_snapshot = snapshots[-1]
    latest_down_price = latest_snapshot.get('price_down', 0)
    
    # 检查是否有任何时刻达到阈值
    down_prices = [s.get('price_down', 0) for s in snapshots]
    max_down = max(down_prices) if down_prices else 0
    
    return {
        'latest_down': latest_down_price,
        'max_down': max_down,
        'triggered': max_down >= DOWN_THRESHOLD
    }

def monitor_market(slug, market_info):
    """监控单个市场"""
    print(f"\n{'='*60}")
    print(f"开始监控: {slug}")
    print(f"开始时间: {market_info['start_time']}")
    print(f"结束时间: {market_info['end_time']}")
    print(f"{'='*60}")
    
    market_file = DATA_DIR / f"{slug}.json"
    all_snapshots = []
    
    while True:
        # 获取最新快照
        data = fetch_snapshots(slug, limit=100)
        if not data:
            print(f"获取快照失败，等待 5 秒...")
            time.sleep(5)
            continue
        
        # 保存快照
        all_snapshots.extend(data.get('snapshots', []))
        
        # 检查策略
        strategy_result = check_strategy(data)
        if strategy_result:
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] DOWN 价格: {strategy_result['latest_down']:.2f} (最高: {strategy_result['max_down']:.2f})", end="")
            if strategy_result['triggered']:
                print(" ✓ 触发!")
            else:
                print()
        
        # 保存数据
        market_file.write_text(json.dumps({
            'market': market_info,
            'snapshots': all_snapshots,
            'updated_at': datetime.now().isoformat()
        }, indent=2))
        
        # 检查市场是否已结算
        if market_info.get('is_resolved'):
            print(f"\n市场已结算: {market_info.get('winner', 'Unknown')}")
            break
        
        # 等待
        time.sleep(10)

def main():
    print("=== Polymarket BTC 5 分钟市场实时监控 ===\n")
    
    while True:
        # 获取最新的未结算市场
        markets = fetch_markets(coin="BTC", market_type="5m", resolved=False, limit=5)
        
        if not markets:
            print("没有找到未结算的市场，等待 30 秒...")
            time.sleep(30)
            continue
        
        print(f"找到 {len(markets)} 个未结算市场:")
        for m in markets:
            print(f"  - {m['slug']}: {m['start_time']} -> {m['end_time']}")
        
        # 监控第一个市场
        target_market = markets[0]
        monitor_market(target_market['slug'], target_market)
        
        # 等待一段时间后继续
        print("\n等待 60 秒后继续监控...")
        time.sleep(60)

if __name__ == "__main__":
    main()

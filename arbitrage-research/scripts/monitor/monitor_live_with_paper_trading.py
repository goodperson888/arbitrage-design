#!/usr/bin/env python3
"""
实时监控 Polymarket BTC 5 分钟市场，记录数据并进行纸上交易统计。
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

# 纸上交易记录
PAPER_TRADES_FILE = DATA_DIR / "paper_trades.json"

# 策略参数
DOWN_THRESHOLD = 0.70  # DOWN 价格达到此值时触发买入
POSITION_SIZE = 100  # 每次交易金额（美元）

def load_paper_trades():
    """加载纸上交易记录"""
    if PAPER_TRADES_FILE.exists():
        with open(PAPER_TRADES_FILE) as f:
            return json.load(f)
    return []

def save_paper_trades(trades):
    """保存纸上交易记录"""
    with open(PAPER_TRADES_FILE, 'w') as f:
        json.dump(trades, f, indent=2)

def add_paper_trade(trade_type, market_slug, entry_price, exit_price=None, pnl=None):
    """添加一笔纸上交易"""
    trades = load_paper_trades()
    
    trade = {
        'timestamp': datetime.now().isoformat(),
        'type': trade_type,  # 'BUY_DOWN' or 'SELL'
        'market_slug': market_slug,
        'entry_price': entry_price,
        'exit_price': exit_price,
        'pnl': pnl,
        'status': 'OPEN' if exit_price is None else 'CLOSED'
    }
    
    trades.append(trade)
    save_paper_trades(trades)
    return trade

def close_paper_trade(market_slug, exit_price, winner):
    """平仓一笔纸上交易"""
    trades = load_paper_trades()
    
    for trade in trades:
        if trade['market_slug'] == market_slug and trade['status'] == 'OPEN':
            trade['exit_price'] = exit_price
            trade['status'] = 'CLOSED'
            
            # 计算 PnL
            if trade['type'] == 'BUY_DOWN':
                if winner == 'Down':
                    trade['pnl'] = POSITION_SIZE * (1 - trade['entry_price'])
                else:
                    trade['pnl'] = -POSITION_SIZE * trade['entry_price']
            
            trade['closed_at'] = datetime.now().isoformat()
            break
    
    save_paper_trades(trades)

def calculate_statistics():
    """计算纸上交易统计"""
    trades = load_paper_trades()
    
    closed_trades = [t for t in trades if t['status'] == 'CLOSED']
    open_trades = [t for t in trades if t['status'] == 'OPEN']
    
    total_trades = len(closed_trades)
    winning_trades = len([t for t in closed_trades if t['pnl'] > 0])
    losing_trades = len([t for t in closed_trades if t['pnl'] < 0])
    
    total_pnl = sum(t['pnl'] for t in closed_trades)
    win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0
    
    stats = {
        'total_trades': total_trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'total_pnl': total_pnl,
        'win_rate': win_rate,
        'open_positions': len(open_trades)
    }
    
    return stats

def print_statistics():
    """打印统计信息"""
    stats = calculate_statistics()
    
    print(f"\n{'='*60}")
    print(f"纸上交易统计")
    print(f"{'='*60}")
    print(f"总交易次数: {stats['total_trades']}")
    print(f"盈利次数: {stats['winning_trades']}")
    print(f"亏损次数: {stats['losing_trades']}")
    print(f"胜率: {stats['win_rate']:.1f}%")
    print(f"总盈亏: ${stats['total_pnl']:.2f}")
    print(f"当前持仓: {stats['open_positions']}")
    print(f"{'='*60}\n")

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

def monitor_market(slug, market_info):
    """监控单个市场"""
    print(f"\n{'='*60}")
    print(f"开始监控: {slug}")
    print(f"开始时间: {market_info['start_time']}")
    print(f"结束时间: {market_info['end_time']}")
    print(f"{'='*60}")
    
    market_file = DATA_DIR / f"{slug}.json"
    all_snapshots = []
    trade_opened = False
    
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
        snapshots = data.get('snapshots', [])
        if snapshots:
            latest_snapshot = snapshots[-1]
            latest_down_price = latest_snapshot.get('price_down', 0)
            down_prices = [s.get('price_down', 0) for s in snapshots]
            max_down = max(down_prices) if down_prices else 0
            
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] DOWN 价格: {latest_down_price:.2f} (最高: {max_down:.2f})", end="")
            
            # 检查是否触发买入
            if max_down >= DOWN_THRESHOLD and not trade_opened:
                print(" ✓ 触发买入!")
                add_paper_trade('BUY_DOWN', slug, latest_down_price)
                trade_opened = True
                print(f"  纸上交易: 买入 DOWN @ {latest_down_price:.2f}")
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
            winner = market_info.get('winner', 'Unknown')
            print(f"\n市场已结算: {winner}")
            
            # 平仓
            if trade_opened:
                exit_price = 1.0 if winner == 'Down' else 0.0
                close_paper_trade(slug, exit_price, winner)
                print(f"  平仓: DOWN @ {exit_price:.2f}")
            
            # 打印统计
            print_statistics()
            break
        
        # 等待
        time.sleep(10)

def main():
    print("=== Polymarket BTC 5 分钟市场实时监控 + 纸上交易 ===\n")
    
    # 初始化纸上交易文件
    if not PAPER_TRADES_FILE.exists():
        save_paper_trades([])
    
    print_statistics()
    
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

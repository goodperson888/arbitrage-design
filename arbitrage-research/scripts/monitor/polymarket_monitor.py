#!/usr/bin/env python3
"""
Polymarket BTC 5 分钟市场实时监控
结合浏览器页面监控和 API 数据收集，实现自动切换下一轮和纸上交易统计
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
MARKET_LOG_FILE = DATA_DIR / "market_log.txt"

# 策略参数
DOWN_THRESHOLD = 0.70  # DOWN 价格达到此值时触发买入
POSITION_SIZE = 100  # 每次交易金额（美元）

def log_message(message):
    """记录日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}\n"
    print(log_line.strip())
    with open(MARKET_LOG_FILE, 'a') as f:
        f.write(log_line)

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

def add_paper_trade(trade_type, market_slug, entry_price, btc_price=None):
    """添加一笔纸上交易"""
    trades = load_paper_trades()
    
    trade = {
        'timestamp': datetime.now().isoformat(),
        'type': trade_type,
        'market_slug': market_slug,
        'entry_price': entry_price,
        'btc_price': btc_price,
        'exit_price': None,
        'pnl': None,
        'status': 'OPEN'
    }
    
    trades.append(trade)
    save_paper_trades(trades)
    log_message(f"📊 纸上交易: {trade_type} {market_slug} @ {entry_price:.2f} (BTC: ${btc_price})")
    return trade

def close_paper_trade(market_slug, exit_price, winner, btc_price_end=None):
    """平仓一笔纸上交易"""
    trades = load_paper_trades()
    
    for trade in trades:
        if trade['market_slug'] == market_slug and trade['status'] == 'OPEN':
            trade['exit_price'] = exit_price
            trade['status'] = 'CLOSED'
            trade['btc_price_end'] = btc_price_end
            
            if trade['type'] == 'BUY_DOWN':
                if winner == 'Down':
                    trade['pnl'] = POSITION_SIZE * (1 - trade['entry_price'])
                else:
                    trade['pnl'] = -POSITION_SIZE * trade['entry_price']
            
            trade['closed_at'] = datetime.now().isoformat()
            log_message(f"✅ 平仓: {market_slug} @ {exit_price:.2f}, PnL: ${trade['pnl']:.2f}, 赢家: {winner}")
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
    
    return {
        'total_trades': total_trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'total_pnl': total_pnl,
        'win_rate': win_rate,
        'open_positions': len(open_trades)
    }

def print_statistics():
    """打印统计信息"""
    stats = calculate_statistics()
    
    log_message(f"📈 统计: 总交易={stats['total_trades']}, 盈利={stats['winning_trades']}, 亏损={stats['losing_trades']}, 胜率={stats['win_rate']:.1f}%, 总盈亏=${stats['total_pnl']:.2f}, 持仓={stats['open_positions']}")

def fetch_market(slug):
    """获取市场信息"""
    headers = {"X-API-Key": API_KEY}
    proxies = {"http": PROXY, "https": PROXY}
    
    try:
        response = requests.get(f"{BASE_URL}/markets/{slug}", headers=headers, proxies=proxies)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        log_message(f"❌ 获取市场信息失败 {slug}: {e}")
        return None

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
        log_message(f"❌ 获取快照失败 {slug}: {e}")
        return None

def get_latest_unresolved_market():
    """获取最新的未结算市场"""
    headers = {"X-API-Key": API_KEY}
    proxies = {"http": PROXY, "https": PROXY}
    
    try:
        response = requests.get(f"{BASE_URL}/markets", params={"coin": "BTC", "market_type": "5m", "resolved": False, "limit": 1}, headers=headers, proxies=proxies)
        response.raise_for_status()
        data = response.json()
        if data["markets"]:
            return data["markets"][0]
    except Exception as e:
        log_message(f"❌ 获取最新市场失败: {e}")
    
    return None

def monitor_market(slug):
    """监控单个市场"""
    log_message(f"🚀 开始监控: {slug}")
    
    market_file = DATA_DIR / f"{slug}.json"
    all_snapshots = []
    trade_opened = False
    
    # 获取初始市场信息
    market_info = fetch_market(slug)
    if not market_info:
        log_message("❌ 无法获取市场信息，跳过")
        return
    
    start_time = market_info['start_time']
    end_time = market_info['end_time']
    btc_start = market_info['coin_price_start']
    
    log_message(f"⏰ 时间: {start_time} -> {end_time}")
    log_message(f"💰 BTC 起始价格: ${btc_start}")
    
    while True:
        # 获取市场信息
        market_info = fetch_market(slug)
        if not market_info:
            log_message("⏳ 获取市场信息失败，等待 10 秒...")
            time.sleep(10)
            continue
        
        # 获取快照
        data = fetch_snapshots(slug, limit=100)
        if not data:
            log_message("⏳ 获取快照失败，等待 10 秒...")
            time.sleep(10)
            continue
        
        # 保存快照
        all_snapshots.extend(data.get('snapshots', []))
        
        # 检查策略
        snapshots = data.get('snapshots', [])
        if snapshots:
            latest_snapshot = snapshots[-1]
            latest_down_price = latest_snapshot.get('price_down', 0)
            latest_up_price = latest_snapshot.get('price_up', 0)
            latest_btc_price = latest_snapshot.get('coin_price', btc_start)
            
            down_prices = [s.get('price_down', 0) for s in snapshots]
            max_down = max(down_prices) if down_prices else 0
            
            log_message(f"📊 UP: {latest_up_price:.2f} | DOWN: {latest_down_price:.2f} (最高: {max_down:.2f}) | BTC: ${latest_btc_price}")
            
            # 检查是否触发买入
            if max_down >= DOWN_THRESHOLD and not trade_opened:
                add_paper_trade('BUY_DOWN', slug, latest_down_price, latest_btc_price)
                trade_opened = True
        
        # 保存数据
        market_file.write_text(json.dumps({
            'market': market_info,
            'snapshots': all_snapshots,
            'updated_at': datetime.now().isoformat()
        }, indent=2))
        
        # 检查市场是否已结算
        if market_info.get('is_resolved'):
            winner = market_info.get('winner', 'Unknown')
            btc_end = market_info.get('coin_price_end', 0)
            btc_change = ((btc_end - btc_start) / btc_start) * 100 if btc_start > 0 else 0
            
            log_message(f"🏁 市场已结算: {winner}")
            log_message(f"💰 BTC: ${btc_start} -> ${btc_end} ({btc_change:+.2f}%)")
            
            # 平仓
            if trade_opened:
                exit_price = 1.0 if winner == 'Down' else 0.0
                close_paper_trade(slug, exit_price, winner, btc_end)
            
            # 打印统计
            print_statistics()
            break
        
        # 等待
        time.sleep(10)

def main():
    log_message("🎯 === Polymarket BTC 5 分钟市场实时监控开始 ===")
    
    # 初始化纸上交易文件
    if not PAPER_TRADES_FILE.exists():
        save_paper_trades([])
    
    print_statistics()
    
    while True:
        # 获取最新的未结算市场
        latest_market = get_latest_unresolved_market()
        
        if not latest_market:
            log_message("⏳ 没有找到未结算的市场，等待 30 秒...")
            time.sleep(30)
            continue
        
        slug = latest_market['slug']
        start_time = latest_market['start_time']
        end_time = latest_market['end_time']
        
        log_message(f"📍 找到最新市场: {slug}")
        log_message(f"⏰ 时间: {start_time} -> {end_time}")
        
        # 监控这个市场
        monitor_market(slug)
        
        # 等待一段时间后继续
        log_message("⏳ 等待 60 秒后继续监控下一个市场...")
        time.sleep(60)

if __name__ == "__main__":
    main()

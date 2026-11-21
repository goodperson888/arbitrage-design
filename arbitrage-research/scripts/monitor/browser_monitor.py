#!/usr/bin/env python3
"""
基于浏览器 MCP 的 Polymarket BTC 5 分钟市场实时监控
通过浏览器操作实现自动切换下一轮和纸上交易统计
"""
import json
import time
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("live_monitor_data")
DATA_DIR.mkdir(exist_ok=True)

# 纸上交易记录
PAPER_TRADES_FILE = DATA_DIR / "paper_trades.json"
MARKET_LOG_FILE = DATA_DIR / "browser_monitor_log.txt"

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

def main():
    log_message("🎯 === Polymarket BTC 5 分钟市场浏览器监控开始 ===")
    log_message("📝 注意：此脚本需要配合 MCP 浏览器工具使用")
    log_message("🔄 监控逻辑：")
    log_message("   1. 每 30 秒检查页面状态")
    log_message("   2. 记录 UP/DOWN 价格")
    log_message("   3. 当 DOWN 价格达到 0.70 时执行纸上交易")
    log_message("   4. 当市场结算后，点击下一轮")
    log_message("   5. 统计胜率和盈亏")
    
    # 初始化纸上交易文件
    if not PAPER_TRADES_FILE.exists():
        save_paper_trades([])
    
    print_statistics()
    
    log_message("⏳ 等待用户手动操作浏览器页面...")
    log_message("💡 提示：请在浏览器中保持 Polymarket 页面打开")
    log_message("📊 数据将记录到: " + str(DATA_DIR))
    
    # 监控循环
    while True:
        log_message("⏰ 监控中... (每 30 秒检查一次)")
        time.sleep(30)

if __name__ == "__main__":
    main()
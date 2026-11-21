#!/usr/bin/env python3
"""
分析9个市场数据的UP/DOWN胜率。
"""
import sys
from pathlib import Path
from decimal import Decimal

# 添加项目路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / "src"))

from arb_research.io import read_events
from arb_research.strategy import LastWindowThresholdConfig, LastWindowThresholdStrategy
from arb_research.backtest import run_backtest

def analyze_up_down_performance():
    """分析UP/DOWN的胜率"""
    events_file = project_root / "btc_5m_9markets_full.ndjson"
    
    print(f"加载数据: {events_file}")
    events = list(read_events(events_file))
    print(f"加载了 {len(events)} 个事件\n")
    
    # 测试最佳参数组合
    strategy_config = LastWindowThresholdConfig(
        strategy_id="prediction_last_window_threshold",
        version=1,
        window_seconds=180,  # 3分钟
        threshold_price=Decimal("0.80"),
        max_entry_price=Decimal("0.88"),
        order_size=Decimal("1"),
        min_ask_size=Decimal("1"),
        fee_model="polymarket_crypto_taker",
        fee_rate=Decimal("0.0001"),
        allowed_outcomes=("UP", "DOWN"),
        one_trade_per_market=True,
    )
    
    strategy = LastWindowThresholdStrategy(strategy_config)
    backtest_result = run_backtest(events, strategy)
    
    print(f"总交易数: {len(backtest_result.fills)}")
    print(f"已结算交易数: {len([f for f in backtest_result.fills if f.settled])}")
    
    # 分离UP和DOWN交易
    up_fills = [f for f in backtest_result.fills if f.outcome == "UP"]
    down_fills = [f for f in backtest_result.fills if f.outcome == "DOWN"]
    
    print(f"\nUP交易: {len(up_fills)} 笔")
    if up_fills:
        up_wins = [f for f in up_fills if f.won]
        up_win_rate = len(up_wins) / len(up_fills) if up_fills else 0
        up_total_pnl = sum([f.net_pnl for f in up_fills if f.net_pnl])
        print(f"  胜: {len(up_wins)} 笔")
        print(f"  负: {len(up_fills) - len(up_wins)} 笔")
        print(f"  胜率: {up_win_rate:.2%}")
        print(f"  总盈亏: {up_total_pnl:.4f}")
        
        for i, fill in enumerate(up_fills):
            print(f"   {i+1}. {fill.market_id} | 入场价: {fill.entry_price:.3f} | {'胜' if fill.won else '负'} | 盈亏: {fill.net_pnl:.4f}")
    
    print(f"\nDOWN交易: {len(down_fills)} 笔")
    if down_fills:
        down_wins = [f for f in down_fills if f.won]
        down_win_rate = len(down_wins) / len(down_fills) if down_fills else 0
        down_total_pnl = sum([f.net_pnl for f in down_fills if f.net_pnl])
        print(f"  胜: {len(down_wins)} 笔")
        print(f"  负: {len(down_fills) - len(down_wins)} 笔")
        print(f"  胜率: {down_win_rate:.2%}")
        print(f"  总盈亏: {down_total_pnl:.4f}")
        
        for i, fill in enumerate(down_fills):
            print(f"  {i+1}. {fill.market_id} | 入场价: {fill.entry_price:.3f} | {'胜' if fill.won else '负'} | 盈亏: {fill.net_pnl:.4f}")
    
    print(f"\n整体胜率: {backtest_result.metrics.get('winRate', 'N/A')}")
    print(f"总盈亏: {backtest_result.metrics.get('netPnlPerShare', 'N/A')}")

if __name__ == "__main__":
    analyze_up_down_performance()
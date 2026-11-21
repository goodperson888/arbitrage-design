#!/usr/bin/env python3
"""
修改策略逻辑：当市场认为涨跌概率在70-88%时买入。
即：UP价格0.70-0.88 或 DOWN价格0.70-88
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

def analyze_bidirectional_strategy():
    """分析双向策略（当涨跌概率在70-88%时买入）"""
    events_file = project_root / "btc_5m_9markets_full.ndjson"
    
    print(f"加载数据: {events_file}")
    events = list(read_events(events_file))
    print(f"加载了 {len(events)} 个事件\n")
    
    # 修改策略：当 UP 或 DOWN 价格在 0.70-0.88 时都买入
    # 修改配置为使用 threshold_price=0.70，但允许 DOWN 价格也在范围内
    # 这需要修改策略逻辑：检查 (UP >= 0.70 and UP <= 0.88) or (DOWN >= 0.70 and DOWN <= 0.88)
    
    # 临时方案：分别测试两个价格区间
    print("方案1：只买价格在 0.70-0.88 的方向")
    strategy_config = LastWindowThresholdConfig(
        strategy_id="prediction_last_window_threshold",
        version=1,
        window_seconds=180,
        threshold_price=Decimal("0.70"),
        max_entry_price=Decimal("0.88"),
        order_size=Decimal("1"),
        min_ask_size=Decimal("1"),
        fee_model="polymarket_crypto_taker",
        fee_rate=Decimal("0.0001"),
        allowed_outcomes=("UP", "DOWN"),
        one_trade_per_market=False,
    )
    
    strategy = LastWindowThresholdStrategy(strategy_config)
    backtest_result = run_backtest(events, strategy)
    
    up_fills = [f for f in backtest_result.fills if f.outcome == "UP"]
    down_fills = [f for f in backtest_result.fills if f.outcome == "DOWN"]
    
    print(f"UP交易: {len(up_fills)} 笔，胜率: {len([f for f in up_fills if f.won])/len(up_fills) if up_fills else 0:.2%}")
    print(f"DOWN交易: {len(down_fills)} 笔，胜率: {len([f for f in down_fills if f.won])/len(down_fills) if down_fills else 0:.2%}")
    
    print(f"\n总交易数: {len(backtest_result.fills)}")
    print(f"总盈亏: {sum([f.net_pnl for f in backtest_result.fills if f.net_pnl]):.4f}")
    
    # 显示最佳交易
    profitable_fills = [f for f in backtest_result.fills if f.net_pnl and f.net_pnl > 0]
    print(f"\n盈利交易: {len(profitable_fills)} 笔")
    for fill in profitable_fills[:10]:
        print(f"  {fill.market_id} | {fill.outcome} | 入场价: {fill.entry_price:.3f} | 盈亏: {fill.net_pnl:.4f}")

if __name__ == "__main__":
    analyze_bidirectional_strategy()
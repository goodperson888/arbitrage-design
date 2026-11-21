#!/usr/bin/env python3
"""
正确分析双向策略：涨概率70-88%买UP，跌概率70-88%买DOWN
"""
import sys
from pathlib import Path
from decimal import Decimal

# 添加项目路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / "src"))

from arb_research.io import read_events
from arb_research.backtest import run_backtest
from arb_research.models import MarketDefinition, MarketEvent, Quote, Fill
from arb_research.orderbook import OrderBookState

def custom_strategy_bidirectional(events, window_seconds, up_min, up_max, down_min, down_max, order_size):
    """自定义双向策略：UP在0.70-0.88买，DOWN在0.70-0.88买"""
    states = {}
    fills = []
    
    for event in sorted(events, key=lambda item: (item.source_time_us, item.received_time_us)):
        state = states.setdefault(event.market_id, MarketReplayState())
        
        if event.event_type == "MARKET_DEFINITION":
            state.definition = MarketDefinition.from_event(event)
            continue
        
        if event.event_type in {"PRICE_QUOTE", "ORDERBOOK_SNAPSHOT", "ORDERBOOK_DELTA"}:
            state.book.apply(event)
            if state.definition is None:
                continue
            
            # 检查时间窗口
            remaining_us = state.definition.event_end_us - event.source_time_us
            if remaining_us < 0:
                continue
            if remaining_us > window_seconds * 1_000_000:
                continue
            
            # 检查 UP 方向
            up_quote = state.book.quote("UP")
            if up_quote and up_quote.best_ask:
                if up_min <= up_quote.best_ask <= up_max:
                    quantity = min(order_size, up_quote.best_ask_size or Decimal("1"))
                    fill = Fill(
                        venue=state.definition.venue,
                        market_id=state.definition.market_id,
                        outcome="UP",
                        source_time_us=event.source_time_us,
                        remaining_seconds=Decimal(remaining_us) / Decimal("1000000"),
                        entry_price=up_quote.best_ask,
                        quantity=quantity,
                        fee=Decimal("0.0000144"),
                        gross_cost=up_quote.best_ask * quantity,
                    )
                    state.fills.append(fill)
                    fills.append(fill)
            
            # 检查 DOWN 方向
            down_quote = state.book.quote("DOWN")
            if down_quote and down_quote.best_ask:
                if down_min <= down_quote.best_ask <= down_max:
                    quantity = min(order_size, down_quote.best_ask_size or Decimal("1"))
                    fill = Fill(
                        venue=state.definition.venue,
                        market_id=state.definition.market_id,
                        outcome="DOWN",
                        source_time_us=event.source_time_us,
                        remaining_seconds=Decimal(remaining_us) / Decimal("1000000"),
                        entry_price=down_quote.best_ask,
                        quantity=quantity,
                        fee=Decimal("0.0000144"),
                        gross_cost=down_quote.best_ask * quantity,
                    )
                    state.fills.append(fill)
                    fills.append(fill)
        
        if event.event_type == "SETTLEMENT":
            winner = event.payload.get("winnerOutcome")
            if winner is None:
                continue
            for fill in state.fills:
                if fill.winner_outcome is not None:
                    continue
                fill.winner_outcome = str(winner)
                fill.payout = fill.quantity if fill.outcome == winner else Decimal("0")
                fill.net_pnl = fill.payout - fill.gross_cost
    
    return fills

class MarketReplayState:
    def __init__(self):
        self.definition = None
        self.book = OrderBookState()
        self.fills = []

def analyze_correct_bidirectional():
    """正确分析双向策略"""
    events_file = project_root / "btc_5m_9markets_full.ndjson"
    
    print(f"加载数据: {events_file}")
    events = list(read_events(events_file))
    print(f"加载了 {len(events)} 个事件\n")
    
    # 测试：最后3分钟，涨概率70-88%买UP，跌概率70-88%买DOWN
    # 即：UP价格0.70-0.88买UP，DOWN价格0.70-0.88买DOWN（UP价格0.12-0.30）
    fills = custom_strategy_bidirectional(
        events, 
        window_seconds=180,
        up_min=Decimal("0.70"),
        up_max=Decimal("0.88"),
        down_min=Decimal("0.70"),
        down_max=Decimal("0.88"),
        order_size=Decimal("1")
    )
    
    print(f"总交易数: {len(fills)}")
    
    # 结算
    for fill in fills:
        if fill.winner_outcome is None:
            continue
        fill.payout = fill.quantity if fill.outcome == fill.winner_outcome else Decimal("0")
        fill.net_pnl = fill.payout - fill.gross_cost
    
    up_fills = [f for f in fills if f.outcome == "UP"]
    down_fills = [f for f in fills if f.outcome == "DOWN"]
    
    print(f"\nUP交易（涨概率70-88%）: {len(up_fills)} 笔")
    if up_fills:
        up_wins = [f for f in up_fills if f.won]
        up_win_rate = len(up_wins) / len(up_fills) if up_fills else 0
        up_total_pnl = sum([f.net_pnl for f in up_fills])
        print(f"  胜: {len(up_wins)} 笔")
        print(f"  负: {len(up_fills) - len(up_wins)} 笔")
        print(f"  胜率: {up_win_rate:.2%}")
        print(f"  总盈亏: {up_total_pnl:.4f}")
    
    print(f"\nDOWN交易（跌概率70-88%）: {len(down_fills)} 笔")
    if down_fills:
        down_wins = [f for f in down_fills if f.won]
        down_win_rate = len(down_wins) / len(down_fills) if down_fills else 0
        down_total_pnl = sum([f.net_pnl for f in down_fills])
        print(f"  胜: {len(down_wins)} 笔")
        print(f"  负: {len(down_fills) - len(down_wins)} 笔")
        print(f"  胜率: {down_win_rate:.2%}")
        print(f"  总盈亏: {down_total_pnl:.4f}")
    
    total_pnl = sum([f.net_pnl for f in fills])
    print(f"\n总盈亏: {total_pnl:.4f}")
    print(f"平均每笔盈亏: {total_pnl / len(fills) if fills else 0:.4f}")

if __name__ == "__main__":
    analyze_correct_bidirectional()
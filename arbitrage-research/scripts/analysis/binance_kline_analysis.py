#!/usr/bin/env python3
"""
获取币安 BTC 5 分钟 K 线数据并分析涨跌预测
"""
import requests
import json
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("live_monitor_data")
DATA_DIR.mkdir(exist_ok=True)

def fetch_binance_klines(symbol="BTCUSDT", interval="5m", limit=100):
    """获取币安 K 线数据"""
    url = f"https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"获取 K 线数据失败: {e}")
        return None

def analyze_klines(klines):
    """分析 K 线数据"""
    if not klines:
        return None
    
    analysis = []
    
    for kline in klines:
        # K 线数据格式: [open_time, open, high, low, close, volume, close_time, ...]
        open_time = kline[0]
        open_price = float(kline[1])
        high_price = float(kline[2])
        low_price = float(kline[3])
        close_price = float(kline[4])
        volume = float(kline[5])
        
        # 计算涨跌
        change = close_price - open_price
        change_pct = (change / open_price) * 100 if open_price > 0 else 0
        
        # 计算涨跌方向
        direction = "UP" if change > 0 else "DOWN"
        
        analysis.append({
            'open_time': open_time,
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'change': change,
            'change_pct': change_pct,
            'direction': direction,
            'volume': volume
        })
    
    return analysis

def calculate_indicators(analysis):
    """计算技术指标"""
    if len(analysis) < 10:
        return None
    
    # 简单移动平均
    closes = [k['close'] for k in analysis]
    sma_5 = sum(closes[-5:]) / 5
    sma_10 = sum(closes[-10:]) / 10
    
    # RSI (简化版)
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        if change > 0:
            gains.append(change)
        else:
            losses.append(abs(change))
    
    avg_gain = sum(gains[-14:]) / 14 if gains else 0
    avg_loss = sum(losses[-14:]) / 14 if losses else 0
    
    if avg_loss == 0:
        rsi = 100
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
    
    # 动量
    momentum = closes[-1] - closes[-5]
    
    return {
        'sma_5': sma_5,
        'sma_10': sma_10,
        'rsi': rsi,
        'momentum': momentum,
        'current_price': closes[-1]
    }

def predict_direction(indicators):
    """基于指标预测涨跌方向"""
    if not indicators:
        return "UNKNOWN"
    
    sma_5 = indicators['sma_5']
    sma_10 = indicators['sma_10']
    rsi = indicators['rsi']
    momentum = indicators['momentum']
    current_price = indicators['current_price']
    
    # 简单预测逻辑
    signals = []
    
    # 趋势信号
    if sma_5 > sma_10:
        signals.append("UP")
    else:
        signals.append("DOWN")
    
    # 动量信号
    if momentum > 0:
        signals.append("UP")
    else:
        signals.append("DOWN")
    
    # RSI 信号
    if rsi > 70:
        signals.append("DOWN")  # 超买，可能下跌
    elif rsi < 30:
        signals.append("UP")    # 超卖，可能上涨
    
    # 统计信号
    up_signals = signals.count("UP")
    down_signals = signals.count("DOWN")
    
    if up_signals > down_signals:
        return "UP"
    elif down_signals > up_signals:
        return "DOWN"
    else:
        return "NEUTRAL"

def main():
    print("=== 币安 BTC 5 分钟 K 线分析 ===\n")
    
    # 获取 K 线数据
    print("获取币安 K 线数据...")
    klines = fetch_binance_klines()
    
    if not klines:
        print("❌ 获取 K 线数据失败")
        return
    
    print(f"✓ 获取到 {len(klines)} 根 K 线数据\n")
    
    # 分析 K 线
    analysis = analyze_klines(klines)
    
    # 计算指标
    indicators = calculate_indicators(analysis)
    
    if indicators:
        print("=== 技术指标 ===")
        print(f"当前价格: ${indicators['current_price']:.2f}")
        print(f"SMA 5: ${indicators['sma_5']:.2f}")
        print(f"SMA 10: ${indicators['sma_10']:.2f}")
        print(f"RSI: {indicators['rsi']:.2f}")
        print(f"动量: ${indicators['momentum']:.2f}")
        print()
        
        # 预测方向
        prediction = predict_direction(indicators)
        print(f"=== 预测结果 ===")
        print(f"下一个 5 分钟预测: {prediction}")
    
    # 保存数据
    with open(DATA_DIR / "binance_klines.json", 'w') as f:
        json.dump(analysis, f, indent=2)
    
    print(f"\n✓ 数据已保存到: {DATA_DIR / 'binance_klines.json'}")

if __name__ == "__main__":
    main()
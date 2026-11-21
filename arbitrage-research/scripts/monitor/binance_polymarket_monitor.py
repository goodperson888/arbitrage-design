#!/usr/bin/env python3
"""
结合币安 K 线预测和 Polymarket 纸上交易的实时监控脚本
"""
import requests
import json
import os
import time
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("live_monitor_data")
DATA_DIR.mkdir(exist_ok=True)

# 纸上交易记录
PAPER_TRADES_FILE = DATA_DIR / "binance_polymarket_trades.json"
MARKET_LOG_FILE = DATA_DIR / "binance_polymarket_log.txt"

# 策略参数
POSITION_SIZE = 100  # 每次交易金额（美元）

# API 配置
BINANCE_URL = "https://api.binance.com/api/v3/klines"
POLYMARKET_API_KEY = os.environ["POLYHISTORICAL_API_KEY"]
POLYMARKET_BASE_URL = "https://api.polyhistorical.com/v1"
PROXY = "http://127.0.0.1:7890"

def log_message(message):
    """记录日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}\n"
    print(log_line.strip())
    with open(MARKET_LOG_FILE, 'a') as f:
        f.write(log_line)

def fetch_binance_klines(symbol="BTCUSDT", interval="5m", limit=20):
    """获取币安 K 线数据"""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    proxies = {"http": PROXY, "https": POLYMARKET_BASE_URL}
    
    try:
        response = requests.get(BINANCE_URL, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        log_message(f"❌ 获取币安 K 线失败: {e}")
        return None

def analyze_klines_prediction(klines):
    """基于 K 线预测涨跌"""
    if not klines or len(klines) < 5:
        return "UNKNOWN"
    
    closes = [float(k[4]) for k in klines]
    
    # 简单预测策略
    sma_5 = sum(closes[-5:]) / 5
    current_price = closes[-1]
    
    if current_price > sma_5:
        return "UP"
    else:
        return "DOWN"

def get_polymarket_market():
    """获取最新的 Polymarket BTC 5 分钟市场"""
    headers = {"X-API-Key": POLYMARKET_API_KEY}
    proxies = {"http": POLYMARKET_BASE_URL, "https": POLYMARKET_BASE_URL}
    
    try:
        response = requests.get(f"{POLYMARKET_BASE_URL}/markets", 
                           params={"coin": "BTC", "market_type": "5m", "resolved": False, "limit": 1}, 
                           headers=headers, proxies=proxies)
        response.raise_for_status()
        data = response.json()
        if data["markets"]:
            return data["markets"][0]
    except Exception as e:
        log_message(f"❌ 获取 Polymarket 市场失败: {e}")
    
    return None

def main():
    log_message("🎯 === 币安 K 线 + Polymarket 实时监控开始 ===")
    log_message("📊 策略：基于币安 K 线预测涨跌，在 Polymarket 执行纸上交易")
    
    cycle_count = 0
    
    while True:
        cycle_count += 1
        log_message(f"🔄 第 {cycle_count} 轮监控")
        
        # 获取币安 K 线
        klines = fetch_binance_klines()
        
        if klines:
            prediction = analyze_klines_prediction(klines)
            current_price = float(klines[-1][4])
            
            log_message(f"📈 币安预测: {prediction} (当前价格: ${current_price:.2f})")
            
            # 获取 Polymarket 市场
            market = get_polymarket_market()
            
            if market:
                slug = market['slug']
                start_time = market['start_time']
                end_time = market['end_time']
                
                log_message(f"🎯 Polymarket 市场: {slug}")
                log_message(f"⏰ 时间: {start_time} -> {end_time}")
                
                # 根据预测记录策略
                if prediction == "UP":
                    log_message(f"📊 纸上交易: 买入 UP (基于币安预测)")
                else:
                    log_message(f"📊 纸上交易: 买入 DOWN (基于币安预测)")
        
        else:
            log_message("⏳ 无法获取币安 K 线，等待 30 秒...")
        
        # 等待 5 分钟
        log_message("⏳ 等待 5 分钟...")
        time.sleep(300)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
先测试Binance连接作为对比，验证网络/代理是否正常
"""
import sys
sys.path.insert(0, "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/src")

from arb_research.connectors.binance_backfill import BinanceBackfill

def main():
    print("=== 测试Binance连接 ===")
    
    try:
        with BinanceBackfill(proxy=None) as backfill:
            # 获取BTCUSDT的K线数据
            print("获取BTCUSDT 1分钟K线数据...")
            klines = backfill.get_klines(symbol="BTCUSDT", interval="1m", limit=5)
            
            print(f"成功获取 {len(klines)} 条K线数据")
            for kline in klines[:3]:
                print(f"时间: {kline.open_time}, 收盘价: {kline.close_price}")
                
    except Exception as e:
        print(f"Binance连接失败: {e}")
        print("尝试使用代理...")
        
        try:
            with BinanceBackfill(proxy="http://127.0.0.1:7890") as backfill:
                klines = backfill.get_klines(symbol="BTCUSDT", interval="1m", limit=5)
                print(f"代理连接成功！获取 {len(klines)} 条K线数据")
                for kline in klines[:3]:
                    print(f"时间: {kline.open_time}, 收盘价: {kline.close_price}")
        except Exception as e2:
            print(f"代理连接也失败: {e2}")

if __name__ == "__main__":
    main()
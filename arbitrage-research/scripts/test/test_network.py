#!/usr/bin/env python3
"""
Polymarket数据验证 - 尝试不同的网络连接方式
"""
import sys
sys.path.insert(0, "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/src")

def test_network_connection():
    """测试不同的网络连接方式"""
    print("=== 测试网络连接 ===")
    
    import httpx
    
    # 方式1: 不使用代理
    print("\n1. 尝试不使用代理连接...")
    try:
        client = httpx.Client(timeout=10)
        response = client.get("https://api.polymarket.com/markets?limit=5")
        print(f"成功！状态码: {response.status_code}")
        print(f"响应内容: {response.text[:200]}")
        return True
    except Exception as e:
        print(f"失败: {e}")
    
    # 方式2: 使用本地代理
    print("\n2. 尝试使用本地代理 (http://127.0.0.1:7890)...")
    try:
        client = httpx.Client(timeout=10, proxy="http://127.0.0.1:7890")
        response = client.get("https://api.polymarket.com/markets?limit=5")
        print(f"成功！状态码: {response.status_code}")
        print(f"响应内容: {response.text[:200]}")
        return True
    except Exception as e:
        print(f"失败: {e}")
    
    # 方式3: 使用socks5代理
    print("\n3. 尝试使用socks5代理 (socks5://127.0.0.1:7890)...")
    try:
        client = httpx.Client(timeout=10, proxy="socks5://127.0.0.1:7890")
        response = client.get("https://api.polymarket.com/markets?limit=5")
        print(f"成功！状态码: {response.status_code}")
        print(f"响应内容: {response.text[:200]}")
        return True
    except Exception as e:
        print(f"失败: {e}")
    
    return False

if __name__ == "__main__":
    success = test_network_connection()
    if success:
        print("\n网络连接成功，可以进行数据采集")
    else:
        print("\n所有网络连接方式都失败，需要检查网络配置")
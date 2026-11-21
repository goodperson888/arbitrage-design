#!/usr/bin/env python3
"""
网络代理配置助手
"""
import os
import sys

def setup_proxy():
    """设置代理环境变量"""
    proxy_url = "http://127.0.0.1:7890"
    
    os.environ['HTTP_PROXY'] = proxy_url
    os.environ['HTTPS_PROXY'] = proxy_url
    os.environ['ALL_PROXY'] = proxy_url
    
    print(f"代理已设置: {proxy_url}")
    print(f"HTTP_PROXY: {os.environ.get('HTTP_PROXY')}")
    print(f"HTTPS_PROXY: {os.environ.get('HTTPS_PROXY')}")
    print(f"ALL_PROXY: {os.environ.get('ALL_PROXY')}")

def test_connection():
    """测试连接"""
    try:
        import httpx
        
        # 使用代理创建客户端
        client = httpx.Client(
            proxy="http://127.0.0.1:7890",
            timeout=30,
            verify=False  # 跳过SSL验证
        )
        
        # 测试Polymarket API
        response = client.get("https://api.polymarket.com/events?limit=1")
        print(f"\n✅ 连接成功！状态码: {response.status_code}")
        print(f"响应: {response.text[:200]}")
        
        client.close()
        return True
        
    except Exception as e:
        print(f"\n❌ 连接失败: {e}")
        return False

if __name__ == "__main__":
    print("=== 网络代理配置 ===")
    setup_proxy()
    test_connection()
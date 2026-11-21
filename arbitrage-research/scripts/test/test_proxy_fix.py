#!/usr/bin/env python3
"""
使用代理连接Polymarket API测试
"""
import os
import sys

# 设置代理环境变量
os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7890'
os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:7890'
os.environ['ALL_PROXY'] = 'http://127.0.0.1:7890'

print("代理环境变量已设置:")
print(f"HTTP_PROXY: {os.environ.get('HTTP_PROXY')}")
print(f"HTTPS_PROXY: {os.environ.get('HTTPS_PROXY')}")

# 测试连接
try:
    import httpx
    
    print("\n测试Polymarket API连接...")
    
    # 使用代理创建客户端
    client = httpx.Client(
        proxy="http://127.0.0.1:7890",
        timeout=30,
        verify=False  # 跳过SSL验证
    )
    
    # 测试获取事件
    response = client.get("https://api.polymarket.com/events?limit=5")
    print(f"连接成功！状态码: {response.status_code}")
    print(f"响应内容: {response.text[:500]}")
    
    client.close()
    
except Exception as e:
    print(f"连接失败: {e}")
    print("尝试其他方法...")
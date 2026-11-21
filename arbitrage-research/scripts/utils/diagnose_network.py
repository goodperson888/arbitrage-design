#!/usr/bin/env python3
"""
网络诊断脚本 - 检查当前网络配置
"""
import sys
import socket
import os

def check_dns_resolution():
    """检查DNS解析"""
    print("=== DNS解析检查 ===")
    test_domains = [
        "api.polymarket.com",
        "gamma-api.polymarket.com",
        "clob.polymarket.com",
        "www.google.com"
    ]
    
    for domain in test_domains:
        try:
            ip = socket.gethostbyname(domain)
            print(f"✅ {domain} -> {ip}")
        except Exception as e:
            print(f"❌ {domain} -> {e}")

def check_proxy_settings():
    """检查代理环境变量"""
    print("\n=== 代理环境变量检查 ===")
    proxy_vars = ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']
    
    for var in proxy_vars:
        value = os.environ.get(var)
        if value:
            print(f"✅ {var}={value}")
        else:
            print(f"❌ {var} 未设置")

def check_system_dns():
    """检查系统DNS配置"""
    print("\n=== 系统DNS配置 ===")
    try:
        import subprocess
        result = subprocess.run(['scutil', '--dns'], capture_output=True, text=True)
        print(result.stdout[:500])  # 显示前500字符
    except:
        print("无法获取系统DNS配置")

def test_curl_proxy():
    """测试curl使用代理"""
    print("\n=== curl代理测试 ===")
    try:
        import subprocess
        # 测试不用代理
        result = subprocess.run(['curl', '-I', 'https://www.google.com'], 
                              capture_output=True, text=True, timeout=5)
        print(f"✅ curl直连google: {result.returncode == 0}")
        
        # 测试使用代理
        result = subprocess.run(['curl', '-x', 'http://127.0.0.1:7890', '-I', 'https://api.polymarket.com'], 
                              capture_output=True, text=True, timeout=5)
        print(f"✅ curl代理连接polymarket: {result.returncode == 0}")
        if result.returncode != 0:
            print(f"错误信息: {result.stderr[:200]}")
    except Exception as e:
        print(f"❌ curl测试失败: {e}")

if __name__ == "__main__":
    check_dns_resolution()
    check_proxy_settings()
    check_system_dns()
    test_curl_proxy()
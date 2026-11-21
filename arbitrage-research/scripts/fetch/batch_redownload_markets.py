#!/usr/bin/env python3
"""批量下载完整的 5 分钟市场快照。"""
import json
from pathlib import Path

from fetch_polyhistorical_data import fetch_all_snapshots

# 读取市场列表
with open("btc_5m_markets.json", "r") as f:
    markets_data = json.load(f)

markets = markets_data["markets"]

# 选择前 10 个市场进行测试（避免 API 限速）
selected_markets = markets[:10]

print(f"开始重新下载 {len(selected_markets)} 个市场...")

for i, market in enumerate(selected_markets):
    slug = market["slug"]
    output_file = Path(f"{slug}_full.json")
    
    print(f"\n[{i+1}/{len(selected_markets)}] 下载 {slug}")
    
    snapshots = fetch_all_snapshots(slug, include_orderbook=True)
    output_file.write_text(
        json.dumps({"market": market, "snapshots": snapshots}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  ✓ {len(snapshots)} 个快照 -> {output_file}")

print(f"\n批量下载完成！")

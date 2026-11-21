#!/usr/bin/env python3
"""将本地可用的连续市场数据转换为 NDJSON，不按策略信号筛样本。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict


def best_level(orderbook: Dict, side: str) -> Dict | None:
    levels = orderbook.get(side) or []
    if not levels:
        return None
    selector = min if side == "asks" else max
    return selector(levels, key=lambda level: float(level["price"]))

def parse_timestamp_to_us(timestamp_str: str) -> int:
    """将 ISO 时间戳转换为微秒"""
    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000)

def convert_market_to_ndjson(market: Dict, snapshots_data: Dict) -> List[Dict]:
    """将单个市场的数据转换为 NDJSON 事件"""
    events = []
    market_id = market["slug"]
    event_id_counter = 0
    
    # MARKET_DEFINITION
    def_event = {
        "eventId": f"{market_id}-def-{event_id_counter}",
        "eventType": "MARKET_DEFINITION",
        "marketId": market_id,
        "payload": {
            "eventEndUs": str(parse_timestamp_to_us(market["end_time"])),
            "eventStartUs": str(parse_timestamp_to_us(market["start_time"])),
            "outcomes": ["UP", "DOWN"],
            "underlying": f"{market['coin']}/USD"
        },
        "receivedTimeUs": parse_timestamp_to_us(market["start_time"]),
        "schemaVersion": 1,
        "source": {},
        "sourceTimeUs": parse_timestamp_to_us(market["start_time"]),
        "venue": "POLYMARKET"
    }
    events.append(def_event)
    event_id_counter += 1
    
    # 保留全部快照数据（不过滤时间窗口），在回测引擎中应用过滤
    snapshots = snapshots_data["snapshots"]
    for snapshot in snapshots:
        timestamp_us = parse_timestamp_to_us(snapshot["time"])
        
        # UP token 的 PRICE_QUOTE
        if snapshot["orderbook_up"]:
            ask = best_level(snapshot["orderbook_up"], "asks")
            bid = best_level(snapshot["orderbook_up"], "bids")
            best_ask = ask["price"] if ask else snapshot["price_up"]
            best_ask_size = ask["size"] if ask else 0
            best_bid = bid["price"] if bid else snapshot["price_up"]
            best_bid_size = bid["size"] if bid else 0
            
            up_quote = {
                "eventId": f"{market_id}-quote-{event_id_counter}",
                "eventType": "PRICE_QUOTE",
                "marketId": market_id,
                "payload": {
                    "bestAsk": str(best_ask),
                    "bestAskSize": str(best_ask_size),
                    "bestBid": str(best_bid),
                    "bestBidSize": str(best_bid_size),
                    "outcome": "UP"
                },
                "receivedTimeUs": timestamp_us,
                "schemaVersion": 1,
                "source": {},
                "sourceTimeUs": timestamp_us,
                "venue": "POLYMARKET"
            }
            events.append(up_quote)
            event_id_counter += 1
        
        # DOWN token 的 PRICE_QUOTE
        if snapshot["orderbook_down"]:
            ask = best_level(snapshot["orderbook_down"], "asks")
            bid = best_level(snapshot["orderbook_down"], "bids")
            best_ask = ask["price"] if ask else snapshot["price_down"]
            best_ask_size = ask["size"] if ask else 0
            best_bid = bid["price"] if bid else snapshot["price_down"]
            best_bid_size = bid["size"] if bid else 0
            
            down_quote = {
                "eventId": f"{market_id}-quote-{event_id_counter}",
                "eventType": "PRICE_QUOTE",
                "marketId": market_id,
                "payload": {
                    "bestAsk": str(best_ask),
                    "bestAskSize": str(best_ask_size),
                    "bestBid": str(best_bid),
                    "bestBidSize": str(best_bid_size),
                    "outcome": "DOWN"
                },
                "receivedTimeUs": timestamp_us,
                "schemaVersion": 1,
                "source": {},
                "sourceTimeUs": timestamp_us,
                "venue": "POLYMARKET"
            }
            events.append(down_quote)
            event_id_counter += 1
    
    # SETTLEMENT (仅对已解决的市场)
    if market["is_resolved"]:
        settle_event = {
            "eventId": f"{market_id}-settle-{event_id_counter}",
            "eventType": "SETTLEMENT",
            "marketId": market_id,
            "payload": {
                "settlementPrice": "1",
                "winnerOutcome": market["winner"].upper()
            },
            "receivedTimeUs": parse_timestamp_to_us(market["resolved_at"]),
            "schemaVersion": 1,
            "source": {},
            "sourceTimeUs": parse_timestamp_to_us(market["resolved_at"]),
            "venue": "POLYMARKET"
        }
        events.append(settle_event)
    
    return events

def main():
    # 读取市场列表
    with open("btc_5m_markets.json", "r") as f:
        markets_data = json.load(f)
    
    markets = markets_data["markets"]
    
    full_slugs = {
        path.name.removesuffix("_full.json")
        for path in Path(".").glob("btc-updown-5m-*_full.json")
    }
    legacy_slugs = {
        path.name.removesuffix("_part1.json")
        for path in Path(".").glob("btc-updown-5m-*_part1.json")
        if Path(path.name.replace("_part1.json", "_part2.json")).exists()
    }
    available_markets = full_slugs or legacy_slugs
    
    all_events = []
    
    for market in markets:
        slug = market["slug"]
        if slug not in available_markets:
            continue
        
        full_file = Path(f"{slug}_full.json")
        part1_file = Path(f"{slug}_part1.json")
        part2_file = Path(f"{slug}_part2.json")
        
        print(f"处理市场: {slug}")
        
        if full_file.exists():
            with full_file.open("r", encoding="utf-8") as f:
                full_data = json.load(f)
            all_snapshots = full_data["snapshots"]
            market_metadata = full_data["market"]
        else:
            # 兼容旧数据；旧的两页数据可能不覆盖结算前末段，正式
            # 验证仍会被 validate-last-window 的覆盖门禁拒绝。
            with part1_file.open("r", encoding="utf-8") as f:
                part1 = json.load(f)
            with part2_file.open("r", encoding="utf-8") as f:
                part2 = json.load(f)
            all_snapshots = part1["snapshots"] + part2["snapshots"]
            market_metadata = part1["market"]
        snapshot_dict = {s['time']: s for s in all_snapshots}
        merged_snapshots = sorted(snapshot_dict.values(), key=lambda x: x['time'])
        
        # 创建合并后的数据结构
        merged_data = {
            'market': market_metadata,
            'snapshots': merged_snapshots
        }
        
        # 转换为 NDJSON
        events = convert_market_to_ndjson(market, merged_data)
        all_events.extend(events)
        print(f"  转换为 {len(events)} 个事件")
    
    # 按 receivedTimeUs 排序
    all_events.sort(key=lambda x: x["receivedTimeUs"])
    
    # 写入 NDJSON 文件
    output_file = "btc_5m_available_markets_full.ndjson"
    with open(output_file, "w") as f:
        for event in all_events:
            f.write(json.dumps(event) + "\n")
    
    print(f"\n完成！处理了 {len(available_markets)} 个未按信号筛选的市场，共写入 {len(all_events)} 个事件到 {output_file}")

if __name__ == "__main__":
    main()

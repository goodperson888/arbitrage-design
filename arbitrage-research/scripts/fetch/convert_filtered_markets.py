#!/usr/bin/env python3
"""
将符合条件的 5 分钟市场（价格在 0.70-0.88 范围内）转换为 NDJSON 格式。
保留全部快照数据，在回测引擎中应用价格过滤和时间窗口过滤。
"""
import json
from datetime import datetime
from typing import List, Dict

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
        "eventId": f"evt-def-{event_id_counter}",
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
            best_ask = snapshot["orderbook_up"]["asks"][0]["price"] if snapshot["orderbook_up"]["asks"] else snapshot["price_up"]
            best_ask_size = snapshot["orderbook_up"]["asks"][0]["size"] if snapshot["orderbook_up"]["asks"] else 0
            best_bid = snapshot["orderbook_up"]["bids"][0]["price"] if snapshot["orderbook_up"]["bids"] else snapshot["price_up"]
            best_bid_size = snapshot["orderbook_up"]["bids"][0]["size"] if snapshot["orderbook_up"]["bids"] else 0
            
            up_quote = {
                "eventId": f"evt-quote-{event_id_counter}",
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
            best_ask = snapshot["orderbook_down"]["asks"][0]["price"] if snapshot["orderbook_down"]["asks"] else snapshot["price_down"]
            best_ask_size = snapshot["orderbook_down"]["asks"][0]["size"] if snapshot["orderbook_down"]["asks"] else 0
            best_bid = snapshot["orderbook_down"]["bids"][0]["price"] if snapshot["orderbook_down"]["bids"] else snapshot["price_down"]
            best_bid_size = snapshot["orderbook_down"]["bids"][0]["size"] if snapshot["orderbook_down"]["bids"] else 0
            
            down_quote = {
                "eventId": f"evt-quote-{event_id_counter}",
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
            "eventId": f"evt-settle-{event_id_counter}",
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
    print(f"读取到 {len(markets)} 个市场")
    
    all_events = []
    
    # 只处理符合条件的市场（价格在 0.70-0.88 范围内）
    snapshot_files = {
        "btc-updown-5m-1787215500": "snap_17.json",  # 893/2000 个价格点在范围内
        "btc-updown-5m-1787215200": "snap_18.json",  # 79/2000 个价格点在范围内
        "btc-updown-5m-1787214300": "snap_21.json",  # 2/2000 个价格点在范围内
        "btc-updown-5m-1787213400": "snap_24.json",  # 690/1865 个价格点在范围内
        "btc-updown-5m-1787213100": "snap_25.json"   # 251/2000 个价格点在范围内
    }
    
    processed_count = 0
    for market in markets:
        slug = market["slug"]
        if slug not in snapshot_files:
            continue
        
        print(f"处理市场 {processed_count + 1}: {slug} (winner: {market['winner']})")
        
        # 读取快照数据
        with open(snapshot_files[slug], "r") as f:
            snapshots_data = json.load(f)
        
        snapshots = snapshots_data["snapshots"]
        print(f"  读取到 {len(snapshots)} 个快照")
        
        # 转换为 NDJSON
        events = convert_market_to_ndjson(market, snapshots_data)
        all_events.extend(events)
        print(f"  转换为 {len(events)} 个事件")
        processed_count += 1
    
    # 按 receivedTimeUs 排序
    all_events.sort(key=lambda x: x["receivedTimeUs"])
    
    # 写入 NDJSON 文件
    output_file = "btc_5m_full.ndjson"
    with open(output_file, "w") as f:
        for event in all_events:
            f.write(json.dumps(event) + "\n")
    
    print(f"\n完成！处理了 {processed_count} 个符合条件的市场，共写入 {len(all_events)} 个事件到 {output_file}")

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
将 PolyHistorical API 的 JSON 数据转换为项目 NDJSON 格式。
批量处理多个市场，保留最后 3 分钟的数据用于参数扫实验。
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
    
    # 过滤快照：保留最后 3 分钟的数据（可以同时测试 2 分钟和 3 分钟策略）
    end_time = datetime.fromisoformat(market["end_time"].replace("Z", "+00:00"))
    start_window = end_time.timestamp() - 180  # 最后 3 分钟
    
    snapshots = snapshots_data["snapshots"]
    for snapshot in snapshots:
        snapshot_time = datetime.fromisoformat(snapshot["time"].replace("Z", "+00:00"))
        
        # 只保留最后 3 分钟的快照
        if snapshot_time.timestamp() < start_window:
            continue
        
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
    
    # 处理已下载的快照文件（15 个市场）
    snapshot_files = {
        "btc-updown-5m-1787220300": "snap_1.json",
        "btc-updown-5m-1787220000": "snap_2.json",
        "btc-updown-5m-1787219700": "snap_3.json",
        "btc-updown-5m-1787219400": "snap_4.json",
        "btc-updown-5m-1787219100": "snap_5.json",
        "btc-updown-5m-1787218800": "snap_6.json",
        "btc-updown-5m-1787218500": "snap_7.json",
        "btc-updown-5m-1787218200": "snap_8.json",
        "btc-updown-5m-1787217900": "snap_9.json",
        "btc-updown-5m-1787217600": "snap_10.json",
        "btc-updown-5m-1787217300": "snap_11.json",
        "btc-updown-5m-1787217000": "snap_12.json",
        "btc-updown-5m-1787216700": "snap_13.json",
        "btc-updown-5m-1787216400": "snap_14.json",
        "btc-updown-5m-1787216100": "snap_15.json"
    }
    
    processed_count = 0
    for market in markets:
        slug = market["slug"]
        if slug not in snapshot_files:
            continue
        
        print(f"处理市场 {processed_count + 1}: {slug}")
        
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
    output_file = "btc_5m_15markets.ndjson"
    with open(output_file, "w") as f:
        for event in all_events:
            f.write(json.dumps(event) + "\n")
    
    print(f"\n完成！处理了 {processed_count} 个市场，共写入 {len(all_events)} 个事件到 {output_file}")

if __name__ == "__main__":
    main()
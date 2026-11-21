#!/usr/bin/env python3
"""
从 PolyHistorical API 获取 BTC/ETH 短期市场数据并转换为项目 NDJSON 格式。
"""
from __future__ import annotations

import requests
import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
import time

API_KEY = os.environ.get("POLYHISTORICAL_API_KEY")
BASE_URL = "https://api.polyhistorical.com/v1"
PROXIES = None


def api_headers() -> Dict[str, str]:
    if not API_KEY:
        raise RuntimeError("Set POLYHISTORICAL_API_KEY before fetching data")
    return {"X-API-Key": API_KEY}

def fetch_markets(coin: str, market_type: str = None, resolved: bool = True, limit: int = 50) -> List[Dict]:
    """获取市场列表"""
    params = {"coin": coin, "limit": limit}
    if market_type:
        params["market_type"] = market_type
    if resolved is not None:
        params["resolved"] = resolved
    
    headers = api_headers()
    response = requests.get(
        f"{BASE_URL}/markets",
        params=params,
        headers=headers,
        proxies=PROXIES,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["markets"]

def fetch_snapshots(
    slug: str,
    include_orderbook: bool = True,
    limit: int = 1000,
    offset: int = 0,
) -> Dict:
    """获取市场的订单簿快照"""
    params = {"include_orderbook": include_orderbook, "limit": limit, "offset": offset}
    headers = api_headers()
    response = requests.get(
        f"{BASE_URL}/markets/{slug}/snapshots",
        params=params,
        headers=headers,
        proxies=PROXIES,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def fetch_all_snapshots(
    slug: str,
    include_orderbook: bool = True,
    page_size: int = 1000,
    max_pages: int = 100,
    request_delay_seconds: float = 1.05,
) -> List[Dict]:
    """Fetch every snapshot page and de-duplicate overlapping API pages."""
    snapshots_by_time: Dict[str, Dict] = {}
    offset = 0
    for _ in range(max_pages):
        page = fetch_snapshots(slug, include_orderbook, page_size, offset)
        snapshots = page.get("snapshots") or []
        before = len(snapshots_by_time)
        for snapshot in snapshots:
            snapshots_by_time[snapshot["time"]] = snapshot
        if len(snapshots) < page_size:
            break
        if len(snapshots_by_time) == before:
            raise RuntimeError(f"snapshot pagination made no progress at offset {offset}")
        offset += page_size
        if request_delay_seconds > 0:
            time.sleep(request_delay_seconds)
    else:
        raise RuntimeError(f"snapshot pagination exceeded {max_pages} pages")

    return sorted(snapshots_by_time.values(), key=lambda item: item["time"])

def parse_timestamp_to_us(timestamp_str: str) -> int:
    """将 ISO 时间戳转换为微秒"""
    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000)


def best_level(orderbook: Dict, side: str) -> Dict | None:
    levels = orderbook.get(side) or []
    if not levels:
        return None
    selector = min if side == "asks" else max
    return selector(levels, key=lambda level: float(level["price"]))

def convert_to_ndjson(market: Dict, snapshots: List[Dict]) -> List[Dict]:
    """将 PolyHistorical 数据转换为项目 NDJSON 格式"""
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
    
    # 过滤快照：只保留最后 2-3 分钟的数据
    end_time = datetime.fromisoformat(market["end_time"].replace("Z", "+00:00"))
    start_window = end_time.timestamp() - 180  # 最后 3 分钟
    
    for snapshot in snapshots:
        snapshot_time = datetime.fromisoformat(snapshot["time"].replace("Z", "+00:00"))
        
        # 只保留最后 3 分钟的快照
        if snapshot_time.timestamp() < start_window:
            continue
        
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

def fetch_and_convert_coin(
    coin: str,
    market_types: List[str],
    output_file: str,
    market_limit: int = 50,
    request_delay_seconds: float = 1.05,
):
    """获取指定币种的数据并转换为 NDJSON"""
    all_events = []
    
    for market_type in market_types:
        print(f"获取 {coin} {market_type} 市场列表...")
        markets = fetch_markets(
            coin=coin,
            market_type=market_type,
            resolved=True,
            limit=market_limit,
        )
        print(f"找到 {len(markets)} 个已解决的 {coin} {market_type} 市场")
        
        for i, market in enumerate(markets):
            print(f"处理市场 {i+1}/{len(markets)}: {market['slug']}")
            try:
                snapshots = fetch_all_snapshots(
                    market["slug"],
                    include_orderbook=True,
                    request_delay_seconds=request_delay_seconds,
                )
                print(f"  获取到 {len(snapshots)} 个快照")
                
                events = convert_to_ndjson(market, snapshots)
                all_events.extend(events)
                print(f"  转换为 {len(events)} 个事件")
                
                # API 限速：免费版 60 req/min
                time.sleep(request_delay_seconds)
            except Exception as e:
                print(f"  错误: {e}")
                continue
    
    # 按 receivedTimeUs 排序
    all_events.sort(key=lambda x: x["receivedTimeUs"])
    
    # 写入 NDJSON 文件
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for event in all_events:
            f.write(json.dumps(event) + "\n")
    
    print(f"完成！共写入 {len(all_events)} 个事件到 {output_file}")

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch complete resolved PolyHistorical markets")
    parser.add_argument("--coin", default="BTC")
    parser.add_argument("--market-types", default="5m", help="Comma-separated market types")
    parser.add_argument("--market-limit", type=int, default=50)
    parser.add_argument("--request-delay-seconds", type=float, default=1.05)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    fetch_and_convert_coin(
        coin=args.coin.upper(),
        market_types=[item.strip() for item in args.market_types.split(",") if item.strip()],
        output_file=args.out,
        market_limit=args.market_limit,
        request_delay_seconds=args.request_delay_seconds,
    )


if __name__ == "__main__":
    main()

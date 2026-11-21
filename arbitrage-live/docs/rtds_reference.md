# Polymarket RTDS 只读参考行情

RTDS（Real-Time Data Service）是可选的公开 WebSocket 参考流，不是订单通道。它在本项目中始终与 Market WebSocket、CLOB 订单接口分离：断线、陈旧或异常只会让参考值不可用，不会绕过行情新鲜度门禁，也不会打开下单。

## 地址与开关

- 地址：`wss://ws-live-data.polymarket.com`
- 开关：`POLYMARKET_RTDS_NETWORK_ENABLED=1`
- 默认值：关闭。RTDS 是额外的参考价链路，不是 Polymarket 下单或策略硬门槛；默认关闭可以避免程序在用户不知情时建立额外公网连接，也避免把预言机参考价误当成盘口/结算源。没有显式开关时 adapter 直接抛出 `ReadOnlyNetworkDisabled`。
- runner 启动时若开关打开，会额外启动 RTDS 只读循环；Market WS 和 Binance Spot 仍需各自的只读开关。

## 订阅与心跳

官方原始 RTDS 订阅使用字符串过滤器。Binance 主题示例为 `crypto_prices`，Chainlink 主题为 `crypto_prices_chainlink`；本适配器也支持 Chainlink TWAP 的 SDK 风格主题并发送 JSON 字符串过滤器。应用层心跳每 5 秒发送原始文本 `PING`，不是订单请求。

```json
{
  "action": "subscribe",
  "subscriptions": [
    {"topic": "crypto_prices", "type": "update", "filters": "btcusdt"},
    {"topic": "crypto_prices_chainlink", "type": "*", "filters": "{\"symbol\":\"btc/usd\"}"}
  ]
}
```

收到的值统一为：`source`、`topic`、`symbol`、`value`（Decimal）、`source_time`（UTC datetime）。Chainlink TWAP 额外带 `window_seconds`。runner 仅将这些消息记录为 `POLYMARKET_RTDS / REFERENCE_PRICE`，不会触发 FAK 构造或订单状态机。

## 与其他 Polymarket 链路的区别

| 链路 | 用途 | 是否认证 | 是否下单 |
| --- | --- | --- | --- |
| Market WebSocket | 盘口、成交、市场事件 | 否 | 否 |
| RTDS | Binance/Chainlink 参考价、TWAP | 否 | 否 |
| CLOB REST/SDK | 签名订单、查询、撤单、成交 | 是 | 受 rollout 闸门控制 |
| UMA/官方结算 | 最终市场结果 | 读取官方结果 | 否 |

RTDS 没有在本项目中被当作固定延迟保证；页面展示的是最近收到时间和连接状态。生产策略以 Polymarket Market WS 盘口和成交为主，Binance Spot 5 分钟 K 线与 Chainlink/TWAP 都只做可选对照，不参与当前策略硬门禁。

## 官方文档

- [Polymarket Real-Time Data](https://docs.polymarket.com/market-data/realtime-data)
- [Chainlink 30/60 秒 TWAP](https://docs.polymarket.com/market-data/chainlink-twap)
- [Market WebSocket](https://docs.polymarket.com/api-reference/wss/market)
- [Trading overview / CLOB](https://docs.polymarket.com/trading/overview)
- [Market resolution / UMA](https://docs.polymarket.com/concepts/resolution)

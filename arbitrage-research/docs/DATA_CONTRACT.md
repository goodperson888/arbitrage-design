# Market Event Contract

研究系统统一读取 `MarketEventEnvelopeV1`。每行 NDJSON 是一个事件。

## 必填字段

```json
{
  "schemaVersion": 1,
  "eventType": "PRICE_QUOTE",
  "venue": "POLYMARKET",
  "marketId": "btc-updown-5m-1700000000",
  "sourceTimeUs": "1700000180000000",
  "receivedTimeUs": "1700000180012000",
  "payload": {}
}
```

关键原则：

- `sourceTimeUs` 是平台或数据源时间；
- `receivedTimeUs` 是本机采集时间；
- 金额、价格、数量使用十进制字符串；
- 原始数据另存，标准事件用于回放和回测；
- 平台规则、手续费和结算结果也必须作为事件进入数据集。

## 第一版回测器支持的事件

### MARKET_DEFINITION

```json
{
  "eventType": "MARKET_DEFINITION",
  "payload": {
    "underlying": "BTC/USD",
    "eventStartUs": "1700000000000000",
    "eventEndUs": "1700000300000000",
    "outcomes": ["UP", "DOWN"]
  }
}
```

### PRICE_QUOTE

```json
{
  "eventType": "PRICE_QUOTE",
  "outcome": "UP",
  "payload": {
    "bestBid": "0.79",
    "bestBidSize": "120",
    "bestAsk": "0.80",
    "bestAskSize": "80"
  }
}
```

### ORDERBOOK_SNAPSHOT

```json
{
  "eventType": "ORDERBOOK_SNAPSHOT",
  "payload": {
    "outcomes": [
      {
        "outcome": "UP",
        "bids": [["0.79", "120"]],
        "asks": [["0.80", "80"]]
      }
    ]
  }
}
```

### SETTLEMENT

```json
{
  "eventType": "SETTLEMENT",
  "payload": {
    "winnerOutcome": "UP",
    "settlementPrice": "1"
  }
}
```

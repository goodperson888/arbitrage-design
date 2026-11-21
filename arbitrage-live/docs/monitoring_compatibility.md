# 实盘兼容记录器与研究纸上监控

`live_trader.monitoring.MonitoringRecorder` 复用研究监控的 envelope 字段：

```json
{
  "schemaVersion": 1,
  "eventId": "uuid",
  "eventType": "ORDER_REQUEST",
  "venue": "POLYMARKET",
  "marketId": "btc-5m-...",
  "outcome": "DOWN",
  "sourceTimeUs": 1700000000000000,
  "receivedTimeUs": 1700000000100000,
  "paperOnly": false,
  "liveMode": "observe",
  "payload": {}
}
```

## 分区与磁盘策略

实盘默认目录是 `data/live_trading`，流目录为：

```text
raw/ events/ signals/ orders/ fills/ settlements/ health/ sessions/
  date=YYYY-MM-DD/hour=HH/part-001.ndjson[.gz]
```

- `raw`：gzip 压缩、按 UTC 小时分区、内存有界异步队列；队列满时丢弃 raw，并增加 `rawDropped`，绝不阻塞关键下单路径。
- `events/signals/health`：同样异步写入，便于采集持续运行。
- `orders/fills/settlements/sessions`：关键流，SQLite 与 NDJSON 同步追加；任一写入失败会令 `acceptingOrders=false`。关键流不丢弃。
- 不在实盘目录无限保存逐条未压缩 raw；应按小时压缩，并由 `data_usage` 观察目录大小和丢弃计数。

查看状态：

```bash
./ops/data_usage.sh data/live_trading
```

`iter_partitioned_events()` 可读取 `.ndjson` 与 `.ndjson.gz`，因此研究导出/回放工具可以复用同一 envelope；但实盘 SQLite 和纸上 SQLite 永不共用。已有研究历史目录保持不变。

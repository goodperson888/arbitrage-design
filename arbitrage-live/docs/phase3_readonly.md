# 第三阶段：真实行情只读链路与 CLOB 配置边界

第三阶段定义公开行情边界和连接门禁。所有命令默认是 observe；`canary`/`live` 在 checked-in 配置下仍由安全 transport 拒绝，真实 CLOB transport 的实现和 mock 测试见第四阶段。

## 只读行情

- `PolymarketReadOnlyAdapter` 使用公开 market WebSocket 订阅 token 的市场/成交事件，不包含下单、撤单或认证字段。
- `BinanceSpotReadOnlyAdapter` 使用公开 `BTCUSDT@kline_1m` WebSocket；`rolling_binance_return()` 用最近五根 1 分钟 K 线计算 5 分钟收益。
- `ReadOnlyConnectionSession` 记录连接、重连次数、最后消息时间、最后心跳时间和断线原因。
- 心跳失败、消息超过 freshness 阈值或断线时，状态进入 `STALE`/`DISCONNECTED`，`can_process_new_signal` 为 false。
- 事件时间与本机时间的偏差超过 `max_clock_skew` 时同样进入 `STALE`，禁止新信号。
- 真实网络连接需要显式设置 `POLYMARKET_READONLY_NETWORK_ENABLED=1` 或 `BINANCE_READONLY_NETWORK_ENABLED=1`，否则连接器直接拒绝；本项目验证不主动连接公网。

可选 RTDS 参考链路见 [`rtds_reference.md`](rtds_reference.md)：设置 `POLYMARKET_RTDS_NETWORK_ENABLED=1` 才会订阅 Polymarket RTDS 的 Binance/Chainlink/Chainlink-TWAP 价格。它只写 `REFERENCE_PRICE` 事件供页面和诊断使用，不参与下单门禁；默认关闭。

## CLOB 签名配置

`ClobSigningConfig` 只保存 chain、signature type、funder 和环境变量名；真实 transport 的 SDK 适配器在 `clob_transport.py`，默认仍受三重闸门保护。

- 私钥/API key 只允许环境变量（后续可接 macOS Keychain provider），不允许文件路径。
- `POLYMARKET_ORDER_ENABLED` 未显式设为 `1` 时，submit 直接抛出 `LiveOrderDisabled`。
- 即使显式设为 `1`，如果配置开关或 preflight 未通过，仍抛出 `LiveOrderDisabled`。
- 任何日志或 repr 都必须使用脱敏后的凭据摘要。

## Replay 与 preflight

`ReadOnlyReplay` 将 Polymarket/Binance 录制消息规范化为统一事件，订单尝试数固定为 0。运行：

```bash
PYTHONPATH=src python3 -m live_trader.cli preflight
```

检查项包括订单开关关闭、observe 模式、禁止私钥文件和 transport 尚未批准。检查失败时命令返回非零码；默认命令不会发送订单请求。

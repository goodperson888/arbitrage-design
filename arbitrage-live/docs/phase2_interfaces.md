# 第二阶段接口与状态机

本阶段仍是安全骨架，所有网络 transport 默认关闭；适配器接口只定义边界和规范化格式，不创建 WebSocket 或 CLOB 客户端。

## 行情适配器

- `PolymarketWebSocketAdapter`：`connect(subscriptions)`、异步 `messages()`、`close()`。
- `BinanceSpotWebSocketAdapter`：`connect(symbol)`、异步 `messages()`、`close()`。
- `normalize_polymarket_message()` 将主动成交规范化为 Decimal 价格/数量、UTC 时间、market/token/BUY 或 SELL。
- `normalize_binance_kline()` 将 1 分钟 K 线规范化为 Decimal OHLC，可计算滚动收益。
- `rolling_binance_return()` 要求至少 5 根 1 分钟 K 线，按最早开盘到最新收盘计算 Spot 5 分钟滚动收益；该值仅作为记录与回放特征，不作为 DOWN 入场硬门槛。
- `DisabledWebSocketAdapter` 永远拒绝连接；后续接入真实 WS 必须单独评审并通过 preflight。

## 市场发现

`discover_btc_5m()` 只接受 active、未 closed、问题文本包含 BTC 和 5m、持续时间正好 300 秒、同时存在 UP/DOWN token 的市场。实盘只使用 `MarketDefinition.down_token_id`。

## FAK 构造

`build_fak_request()` 对卖盘按价格排序，逐档吃单，跳过 0.85 以上价格；数量按 `quantity_step` 向下取整，按 `polymarket_crypto_taker` 费用重算总成本，确保不超过 5U。深度不足 2.5U 时抛出 `FAKRejected`，不生成请求。每个请求有确定性的 `pm-...` client order id，禁止自动补单。

## 订单状态机

订单状态保存在独立 SQLite 表中，状态包括：`intent → submitting → acknowledged/partial/filled/canceled/rejected`，以及 `unknown` 和 `disabled`。

- 超时或连接异常进入 `unknown`，暂停该市场；禁止盲目重试。
- 进程重启从 SQLite 恢复所有非终态订单；未知订单必须先通过官方对账结果恢复。
- 终态不能回退；非法状态转换会抛出 `InvalidOrderTransition`。
- `DisabledOrderTransport` 不读密钥、不发请求，任何 submit/cancel/fetch 都直接拒绝。

## 启用真实 transport 的前置条件

必须另行实现并审查带认证的 Polymarket CLOB transport，凭据只允许来自环境变量或 macOS Keychain；完成重复提交、超时、未知状态、重启恢复、部分成交、撤单、对账和急停测试，并由人工显式开启。当前代码没有该 transport。

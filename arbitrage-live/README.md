# Polymarket BTC 5-minute live executor

This is the isolated executor for the confirmed BTC 5-minute DOWN strategy. It contains the strategy/risk/ledger, read-only market adapters, compatible event recorder, and optional CLOB transport. Real order transport remains **disabled by default** and is only reachable after the explicit rollout gates in `docs/phase4_clob_transport.md`.

## Run observe

From this directory:

```bash
PYTHONPATH=src python3 -m live_trader.cli observe
```

或者使用封装命令：

```bash
./ops/start_observe.sh
```

运行单元测试（当前骨架不依赖第三方测试库）：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

直接运行 `observe` 仍是一次性的只读健康会话；`canary/live` 的长连接入口统一走 `run`，在缺少显式 rollout 开关或凭据时对订单 fail-closed，但仍会保留只读行情和诊断运行。

## Run the network runner

行情→策略→FAK→订单状态机已经由 `run` 命令串起来；默认仍 fail-closed。页面启动时只需选择模式，内部实盘开关会自动派生；凭据和阻断原因见 [`docs/live_run.md`](docs/live_run.md)。

```bash
./ops/start_live.sh
```

命令行高级用法仍可显式设置 rollout 开关和 `POLYMARKET_PRIVATE_KEY`，不会读取仓库内的密钥文件；普通操作建议使用本地控制台。

## Safety parameters

- 页面可选择 DOWN only、UP only 或 UP+DOWN；默认仍为 DOWN。选择 UP+DOWN 时两边都按同一套主动成交、VWAP、动量和盘口门禁评估，但单市场仍最多成交一笔。
- Remaining 90–30 seconds; Binance Spot BTCUSDT 5-minute return is recorded as a reference feature, not an entry gate.
- The official DOWN active BUY threshold is dynamic: `max(10 shares, planned order quantity × 1.5)` by default. The DOWN sell price must rise at least 0.01 over the prior 5 seconds and must not reverse down by 0.01.
- Expected and reconciled FAK VWAP must be 0.80–0.85 and the worst executable price must be at most 0.85.
- One FAK intent, fee-inclusive budget at most 5U, effective fill at least 2.5U.
- Production CLOB V2 sizing fetches the current market's dynamic fee curve and uses pUSD collateral; it never falls back to the research fixed fee if the lookup fails.
- One order reservation per market; daily halt after the configured number of losing settlements or configured cumulative loss (defaults: two losing settlements or 10U, either first). These limits are editable in the dashboard as “每日亏损笔数上限” and “每日累计亏损上限（U）”; only confirmed settlements count and the risk day is Asia/Shanghai.
- New order events include backend microsecond timestamps and submit latency; the dashboard’s “行情动态” shows FAK requests, CLOB responses, rejects, and reconciliation outcomes. The page refresh is display-only and is not part of execution timing.
- Raw events use a bounded asynchronous writer. Order/fill/rejection/settlement events use the critical append-only ledger.

## Before canary is enabled

See [`docs/canary_preflight.md`](docs/canary_preflight.md). A separately reviewed transport, authenticated reconciliation, explicit canary limits, and failure-injection tests are required. Do not place credentials in this repository.

第二阶段接口和状态机说明见 [`docs/phase2_interfaces.md`](docs/phase2_interfaces.md)。当前 `live` 参数会记录一次 live-mode 会话并打印 transport disabled；不会发送订单。

第三阶段只读行情链路、连接门禁、凭据脱敏和 CLOB 配置边界见 [`docs/phase3_readonly.md`](docs/phase3_readonly.md)。

```bash
PYTHONPATH=src python3 -m live_trader.cli preflight
```

查看实盘记录目录、raw 有界队列和丢弃数：

```bash
./ops/data_usage.sh data/live_trading
```

启动只读可视化控制台（默认 `8766`，避免和研究项目的 `8765` 冲突）：

```bash
./ops/start_dashboard.sh data/live_trading
```

打开 `http://127.0.0.1:8766/`。页面显示连接健康、策略门槛、信号/订单/成交/结算数量、最近事件和磁盘队列，并提供运行模式、高频记录和新订单提交的安全控制；页面没有单笔下单按钮。

控制台启动脚本优先使用项目固定的 `.venv/bin/python`，避免工具缓存环境变更后丢失 `eth-account` 或 SDK。安装方式为 `python3 -m venv .venv`，再执行 `.venv/bin/python -m pip install -e '.[live]'`；仍支持用 `ARBITRAGE_PYTHON_BIN` 显式覆盖。安装依赖与启动控制台均不会自动启动交易。

订单诊断区会区分“订单未发送”（准备/签名失败，无需对账）与“订单状态未知”（提交后没有确定回执，必须对账）。前者释放无成交的风险占用，后者仍阻断新订单。修复代码不会自动清除历史未知订单。盘口滑点只允许在限价内跨档，默认不超过 0.85；上限内不足最低 2.5U 时直接拦截，详见 [`docs/phase4_clob_transport.md`](docs/phase4_clob_transport.md)。

页面的“成交诊断”会显示最近记录窗口内的策略评估、条件满足、拒绝、订单请求/响应和实际 fill，并按次数列出未成交原因。旁边的“运行与停止诊断”显示最后心跳、本次运行时长和停止/阻断原因；如果进程异常退出或心跳超过 10 秒未更新，会明确标记心跳超时。这样可以区分“没有触发机会”“盘口/策略门禁拒绝”“订单通道拒绝”和“runner 已停止”。

页面顶部的“Polymarket 实时行情”卡片展示当前 DOWN 买一/卖一、最新主动成交、剩余秒数和 Binance Spot 5 分钟收益。它来自公开 Market WebSocket 的只读事件；CLOB 订单接口状态会单独显示，默认关闭。

控制台也会显示一条可选的“RTDS 参考行情”：Polymarket 的 RTDS 可提供 Binance crypto price、Chainlink spot reference 和 30/60 秒 Chainlink TWAP。它只用于核对参考价/延迟和故障诊断，不替代 Market WebSocket，也不参与策略下单门禁。RTDS 默认关闭；只有在本机显式设置 `POLYMARKET_RTDS_NETWORK_ENABLED=1` 后，`run` 才会建立只读连接。页面会把它与 Market WS、Binance Spot 和 CLOB 身份/订单链路分开显示。

三条链路的职责不同：Market WebSocket 是 Polymarket 盘口、成交和市场生命周期；RTDS 是 Polymarket 转发的外部/预言机参考价；CLOB REST/SDK 是经过签名的订单接口。结算结果仍以官方市场结算/UMA Resolution 为准，RTDS 不是结算源。

控制台现在也包含“实盘设置”面板：可填写 Polymarket Funder、签名类型、私钥、代理和运行模式；模式会自动管理内部订单/行情通道。钱包登录优先选择 `0 · EOA`，软件通过 Polymarket CLOB 自动派生 L2 凭据；私钥和派生凭据只保存到 macOS Keychain。点击“配置并派生身份”后可点击“验证身份（不下单）”，整个身份配置不依赖 Polygon RPC。普通配置保存在 `data/live_trading/ui_settings.json`，不会把私钥写入磁盘或子进程命令行。详细流程见 [`docs/live_run.md`](docs/live_run.md) 的“可视化配置与启动”。

页面可以调整单笔下单额度（默认 5U，含手续费）、主动成交最低份额（默认 10 份）和主动成交倍数（默认 1.5）。系统按单笔额度和实时盘口自动计算计划买入份数，触发门槛为 `max(最低份额, 本次计划买入份数 × 倍数)`。这些值只作用于之后启动的 runner，不会改写历史回测数据。

新安装默认使用 `observe`，页面会自动打开两个只读行情连接；订单通道只有选择 `canary/live` 并通过启动前检查后才会进入受控路径。

runner 启动后可在页面分别暂停/开启高频记录（供回放/回测）和新订单提交，也可以使用“全部停止”结束整个 runner。暂停高频记录不会删除历史文件，关键订单、成交、结算账本仍会保留。

高频记录暂停后，后台仍会接收行情并维护有界的 `runtime_state.json` 最新状态，因此页面不会冻结；该文件只覆盖最新摘要，不属于回测事件流。默认降采样和信号合并规则见 [`docs/recording_policy.md`](docs/recording_policy.md) 与 [`config/recording_policy.json`](config/recording_policy.json)。

`observe` 会记录每秒特征快照、纸上成交和官方结算，页面可以直接看到胜/负；`canary/live` 还会异步记录 CLOB 的真实成交明细。详细的字段、丢弃监控和回测边界见 [`docs/live_run.md`](docs/live_run.md) 的“记录内容与回测用途”。

实盘兼容记录器按 `date=YYYY-MM-DD/hour=HH` 分区；`raw` 默认 gzip 压缩且通过有界异步队列写入，允许丢弃并显示计数，避免无限增长。`orders`、`fills`、`settlements`、`sessions` 先写 SQLite 再同步追加 NDJSON，失败即停止新订单。实盘目录与研究纸上监控的目录、数据库完全分开；研究历史数据不会被删除或改写。

live 与 research 使用同一版化事件 envelope（`schemaVersion/eventId/eventType/venue/marketId/outcome/sourceTimeUs/receivedTimeUs/payload`，live 另标 `paperOnly=false/liveMode`），因此可以使用同一导出/回放工具；两者只共享 schema，不共享数据目录、SQLite 数据库或事实样本。

第四阶段可选 CLOB SDK transport、L1/L2 凭据和 FAK/查询/撤销接口见 [`docs/phase4_clob_transport.md`](docs/phase4_clob_transport.md)。检查入库配置仍将 `order_enabled` 设为 `false`；页面只有在 canary/live 认证预检全部通过后才会为本次进程临时打开通道，observe 永远不发送订单。

RTDS 连接、订阅格式、心跳和规范化字段见 [`docs/rtds_reference.md`](docs/rtds_reference.md)。

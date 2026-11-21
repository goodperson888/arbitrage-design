# 实盘启动与阻断说明

## 当前结论

8765 端口是 `arbitrage-research` 的纸上研究监控，不是实盘订单进程，也不会占用 Polymarket 下单锁。实盘项目使用独立目录 `arbitrage-live/data/live_trading`；可视化页面使用 8766 端口。

当前仓库默认仍然关闭真实订单。页面只让用户选择 `observe`、`canary` 或 `live`；订单通道的内部开关会由模式自动派生，仍必须通过账户、凭据和风险预检：

1. 选择 `live`（或先用 `canary`）；
2. 运行时有有效的 Polymarket 钱包私钥 `POLYMARKET_PRIVATE_KEY`；
3. Polymarket 身份通过 Funder、签名类型和私钥向 CLOB 派生；Polygon RPC 仅为可选链上读取配置，不再是身份识别前置条件。

市场行情的两个只读连接由页面自动打开：`POLYMARKET_READONLY_NETWORK_ENABLED=1`、`BINANCE_READONLY_NETWORK_ENABLED=1`。它们只打开行情连接，不会单独打开下单。

## 推荐启动方式

### 可视化配置与启动（推荐）

先启动本地控制台：

```bash
cd /Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-live
./ops/start_dashboard.sh data/live_trading
```

浏览器打开 `http://127.0.0.1:8766/`，在“实盘设置”里填写 Polymarket Funder、私钥和签名类型：

页面顶部的实时行情卡片只读显示 DOWN 买一/卖一、最新主动成交、剩余秒数和 Binance 5 分钟收益；它来自公开 Market WebSocket，不等于已连接 CLOB 下单通道。CLOB 订单 transport 的状态会单独显示，默认关闭。

1. `observe` 只读采集不需要私钥、钱包或身份配置。要做 canary/live，再填写 Funder 和订单签名私钥。
2. 钱包登录优先选择 `0 · EOA`，钱包公开地址与 Funder 完全一致；Magic 邮箱 Proxy 选择 1，Safe/deposit wallet 按实际情况选择 2/3。
3. 点击“配置并派生身份”。页面通过 Polymarket CLOB `create_or_derive_api_key()` 派生 L2 凭据，不调用 Polygon RPC 猜测账户类型；私钥和派生凭据只写入 macOS Keychain。
4. 点击“验证身份（不下单）”检查 CLOB 身份、余额和账户状态；它不会创建或提交订单。
5. 点击“启动前检查”确认当前模式，再点击“启动当前模式”。页面启动的是独立 `run` 子进程；“全部停止”只停止该子进程，不会影响 8765 研究监控。

页面不再展示 `CLOB transport`、`允许实盘`、`订单开关`、`我已确认预检` 等内部复选框。`CLOB transport` 是 Polymarket 订单 API 的连接实现，不是行情连接；`canary` 是受风险限额和预检约束的受限真实下单模式。选择非 `observe` 模式并点击启动确认，即视为明确开启这条受控路径。

订单签名私钥必须是严格的 `0x` 加 64 个十六进制字符（共 32 字节、总长度 66）。界面会在发送前做格式检查；如果钱包复制结果只有 64 个字符，只在本地输入框前补 `0x`，不要把私钥发到聊天或写进文件。EOA（signature type 0）还要求该私钥对应地址与 Funder 完全一致。

启动后页面提供三个运行控制：

1. **高频记录（供回放/回测）暂停/开启**：暂停高频 `raw/events/signals` 写入，但网络监控仍运行；`orders/fills/settlements` 关键账本不会因暂停而丢失。默认开启，适合临时降低磁盘写入量。这里的“回放/回测”指用实盘目录中的同 schema 事件做事后分析，不是把数据写进研究历史库。
2. **新订单提交暂停/开启**：这是运行中的安全暂停开关，不是第二个运行模式。运行模式决定权限上限（observe/canary/live），此开关只控制当前 runner 是否继续提交新订单；暂停时仍接收行情，已有订单继续查询成交、对账和结算。`observe` 模式始终不允许真实下单。
3. **全部停止**：停止当前 runner 子进程，行情、策略和记录一起停止；不会删除已有数据。

页面不提供单笔下单按钮，真实订单仍受代码内风险限额约束。若 Keychain 不可用，保存或预检会明确报错，不会退回到明文文件。

页面里的交易方向、单笔下单额度和主动成交设置只影响之后启动的 runner。方向可选 DOWN、UP 或 UP+DOWN，默认 DOWN；UP+DOWN 会评估两边但单市场仍最多成交一笔。默认单笔含手续费额度为 `5U`，不能低于最低有效成交金额 `2.5U`。主动成交门槛默认是 `max(10份, 本次计划买入份数×1.5)`：系统先按选中方向的实时卖盘、价格、手续费和单笔额度构造 FAK 计划，再判断刚收到的官方主动 BUY 是否足够大。修改会写入 `ui_settings.json`，不会修改历史事件、纸上交易记录或回测结果。

### 记录内容与回测用途

- `observe` 会把每次行情、盘口、Binance 参考价、策略接受/拒绝和 `PAPER_FILL` 写入实盘目录；市场结束后通过 Gamma 的已结算结果写入 `SETTLEMENT`，因此页面可以统计纸上胜负。
- 每个市场最多写一条 `MARKET_SNAPSHOT_1S`/秒，包含剩余秒数、方向价格、5 秒前价格、买卖价差、前十档深度、可选 Binance 5 分钟收益和主动成交方向；入场前5秒要求对应方向卖价相对5秒前上涨至少 `0.01`，并禁止逆向回落达到 `0.01`。Binance 仅作参考展示，不是当前策略或实盘预检的硬门禁。
- `canary/live` 在真实订单状态为 partial/filled 后异步查询 CLOB 成交明细，写入关键 `fills` 账本；查询失败只记录健康事件，不阻塞下单主循环。
- `orders`、`fills`、`settlements`、`sessions` 是同步关键账本；`raw/events/signals/health` 使用有界异步队列。页面上的 `raw 丢弃` 应长期保持 0，否则该时段的逐事件回测需要标记为不完整。

因此，observe 适合验证触发条件、价格/时间窗口、盘口和跨市场特征；只有 canary/live 才能验证真实成交价、部分成交、拒单和滑点。两种模式共享事件格式，但 `paperOnly=true` 会明确标出纸上记录。

先使用项目已安装的 Python 3.12 运行时（系统 Python 3.9 不一定有 `py-clob-client-v2`）：

```bash
cd /Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-live
export POLYMARKET_PRIVATE_KEY='只在当前终端临时设置，不写文件'
export LIVE_MODE=live
export LIVE_TRANSPORT_ENABLED=1
export LIVE_TRADING_ENABLE=1
export POLYMARKET_ORDER_ENABLED=1
export POLYMARKET_PREFLIGHT_APPROVED=1
./ops/start_live.sh
```

如果只想先发一笔 canary，把页面模式设为 `canary`。脚本不会把私钥写入仓库，也不会把它打印到日志。Polymarket 的 L2 API 凭据可由官方 SDK 根据 L1 私钥启动时派生；若已有 L2 凭据，也可额外设置 `POLYMARKET_API_KEY`、`POLYMARKET_API_SECRET`、`POLYMARKET_API_PASSPHRASE`。

## 看到“另一个进程不让我实盘”时

先看启动输出中的 `BLOCKED ...`。常见情况是：

- `read_only_mode`：没有设置 `LIVE_MODE=live/canary`；
- `config_transport_on`、`order_switch_on`、`live_switch_on` 或 `transport_review_gate`：如果从命令行手动运行，说明内部 rollout 环境变量不完整；页面启动会按模式自动生成这些值；
- `credentials_present`：当前终端没有 `POLYMARKET_PRIVATE_KEY`；
- `POLYMARKET_READONLY_NETWORK_ENABLED`：只读行情 WebSocket 没打开。

`8765` 研究进程可以继续运行；它和实盘 runner 使用不同的数据目录、SQLite 和端口，不会形成订单锁。若要单独查看实盘状态，另开终端运行：

```bash
./ops/start_dashboard.sh data/live_trading
```

浏览器打开 `http://127.0.0.1:8766/`。页面没有下单按钮，只显示连接、信号、订单、成交、结算和磁盘队列。

页面的“成交诊断”统计最近记录窗口内的策略评估、条件满足、拒绝、订单请求/响应和真实 fill，并按次数列出未成交原因。“运行与停止诊断”显示最后心跳、本次运行时长以及停止/阻断原因；若没有正常停止事件但心跳超过 10 秒未更新，会标记为心跳超时，帮助区分 runner 异常退出、预检阻断和策略没有触发。canary/live 在预检失败时仍保持只读行情监控，不会静默退出或发送订单。

## 真实订单前的最后检查

- 先确认私钥对应的是实际持有 Polymarket 资金的 Polygon 钱包；此前聊天中出现的字符串不能默认当作钱包私钥使用。
- 首次建议 `LIVE_MODE=canary`，确认 FAK 回报、SQLite 订单状态和成交记录后再切 `live`。
- 发生提交超时会进入 `UNKNOWN` 并暂停该市场，程序不会自动重试；必须先对账。
- 每市场只保留一笔订单，单笔含费额度由页面的“单笔下单额度”控制、有效成交至少 2.5U，逐档计算的预计及实际成交 VWAP 为 0.80–0.85 且最坏入场价不超过 0.85；官方主动 DOWN BUY 必须达到动态门槛；入场前5秒要求 DOWN 价格同方向上涨至少 `0.01`，并禁止逆向回落达到 `0.01`；每日亏损笔数和累计亏损 U 数可在页面配置，任一达到即自动停单（默认两笔或 10U）。
- `raw` 事件使用有界异步队列和 gzip；订单、成交、结算同步写入关键账本。记录不会把消息无限堆在内存中。

# 通用实时概率计算器

这是一个与研究回测、Polymarket 实盘执行器分开的只读组件。它不读取客户历史文件，也不训练客户专属模型；每个市场只使用当前市场的开盘价、实时价格、剩余时间、当前市场高低幅和最近几十秒的滚动波动/动量计算实时方向概率。

本版本只接入 Binance Spot 一个外部行情源，使用一个组合 WebSocket 同时接收 `@aggTrade`、`@bookTicker` 和 `@depth5@100ms`。盘口、主动成交和短周期价格变化是通用事实指标，不包含任何客户专属主策略阈值，也不执行下单。

概率是通用随机波动模型的估计，不是官方结算保证。默认只输出信号，不下单、不保存私钥。网页默认开启轻量事实记录：每个市场每10秒最多写一行紧凑快照，不保存每条 WebSocket 消息。

## 安装与启动

需要 Python 3.9+；连接 Binance WebSocket 时安装可选依赖：

```bash
cd /Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-realtime
python3 -m pip install -e '.[live]'
```

启动本地实时网页：

```bash
./ops/start_web.sh
```

打开 `http://127.0.0.1:8787`。网页只连接本地服务；后台服务负责 Binance Spot
WebSocket、自动重连和实时概率计算。页面刷新不会中断行情连接。网页会同时展示 1/3/5/10 秒价格变化、盘口不平衡、微观价格偏移、买卖价差、60 秒主动成交倾向、主动买占比、10 秒成交速度、买一卖一数量和盘口年龄，并显示轻量记录行数。也可以直接运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m realtime_probability.web
```

停止本地网页服务：在运行 `start_web.sh` 的终端按 `Ctrl+C`。如果终端已经关闭，
可用下面的命令只结束监听 `8787` 的网页进程（不会停止研究采集或实盘程序）：

```bash
PID="$(lsof -tiTCP:8787 -sTCP:LISTEN || true)"
if [ -n "$PID" ]; then kill "$PID"; fi
```

直接使用 Binance BTCUSDT 5 分钟实时流（只读）：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m realtime_probability.cli \
  --mode binance --symbol BTCUSDT --interval-seconds 300
```

也可以使用封装脚本：

```bash
./ops/start_binance.sh
```

通过 `REALTIME_SYMBOL` 和 `REALTIME_INTERVAL_SECONDS` 覆盖默认的交易对和市场周期。
Binance 直连模式按 UTC 时间对齐周期；如果客户平台有自己的开盘/收盘定义，应使用
`--mode stdin` 传入平台实际的 `market_start`、`market_end`，避免参考周期错位。

也可以把已有监控器规范化后的实时快照通过标准输入传入：

```bash
cat snapshots.ndjson | PYTHONDONTWRITEBYTECODE=1 \
  python3 -m realtime_probability.cli --mode stdin
```

输入字段为 `market_id`、`market_start`、`market_end`、`price`，时间使用 Unix 秒；可选 `open_price`、`observed_at`、`source_age_ms`、`best_bid`、`best_ask`、`best_bid_size`、`best_ask_size`、`book_imbalance`、`trade_side` 和 `trade_quantity`。输出是一行 JSON，除 `probability_up`、`probability_down`、`session_high`、`session_low`、`range_bps`、`distance_from_open_bps`、`data_quality` 和 `entry_allowed` 外，还包含 1/3/5/10 秒收益、盘口新鲜度与不平衡、微观价格偏移、价差、60 秒主动成交倾向、主动买占比、成交笔数和 10 秒成交速度。命令行模式只有在传入 `--history-path` 时才写轻量记录。

`entry_allowed` 只是通用数据门禁（默认至少收到 3 个样本、数据不陈旧、方向概率至少 55%），不代表任何平台的实际下单许可；价格、盘口、余额和风控应由上层交易程序单独判断。

字段阅读口径：`return_*_bps` 和 `distance_from_open_bps` 为价格变化（正值偏 UP、负值偏 DOWN）；`book_imbalance` 为 `(买一数量−卖一数量)/(买一数量+卖一数量)`；`microprice_edge_bps` 为按一档数量加权的微观价格相对中间价偏移；`trade_imbalance_60s` 为最近60秒主动成交量净值/总量；`taker_buy_ratio_60s` 为主动买量占比。盘口年龄超过2秒会标记 `book_stale`；“—”表示尚未收到对应事件或窗口没有样本，不等于0。

## 设计边界

- 只维护当前活跃市场的有界滚动队列，不加载历史训练数据；可选的轻量记录只保存稀疏事实快照，不保存训练模型。
- 当前市场高低幅在线更新，时间复杂度 O(1)；内存随配置的滚动窗口上限增长，不随客户历史总量增长。
- `source_age_ms` 超过 `stale_after_seconds` 时会标记为 `stale` 并禁止入场建议；盘口自身超过该时长会单独标记为 `book_stale`，只作为事实提示，不会改变通用概率模型。
- 结果是“基于当前参考价格的实时理论概率”。若交易平台使用 Chainlink 等不同结算源，应在上层另行显示参考源差异。
- 该组件永远不包含订单、钱包、私钥或 CLOB 传输逻辑。
- 网页默认将稀疏事实快照写入 `data/realtime_facts.ndjson`，默认每10秒每个市场最多一行；记录按 `market_id` 保留，可与后续官方结算标签按市场 ID 连接，计算因子命中率。它不会自行假造胜负标签。
- 网页默认使用 Binance Spot 的 `@aggTrade`、`@bookTicker` 和 `@depth5@100ms` 组合流；`@aggTrade` 比原始 `@trade` 消息量更小。需要代理时设置
  `REALTIME_BINANCE_PROXY=http://127.0.0.1:7890`。

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

# Gate BTC 5分钟直连监控

## 目的与定位

该采集器用于把 Gate 的 BTC 5分钟涨跌市场作为 Polymarket 的辅助对照源，研究两边价格变化、方向和领先/滞后关系。它不修改 Polymarket 主监控策略，不登录 Gate、不下单，也不读取账户资料。

Polymarket 仍是主要高精度数据源。Gate 采集器由 Node 直接连接官方 WebSocket，仪表盘和纸上信号实时使用消息，磁盘为节省空间只保存10秒聚合，不能替代完整逐笔盘口回放。

## 当前运行方式

```bash
/Users/laochen/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  scripts/monitor/run_gate_direct_monitor.mjs \
  --snapshot-seconds 10 \
  --max-levels 3
```

不需要打开 Gate 页面，也不启动 Chrome。当前5分钟市场的 condition ID 和 UP/DOWN token ID 从正在运行的 Polymarket 状态文件读取；它们与 Gate 订阅标识一致。Gate 价格、成交及最终价格仍来自 Gate 自己的官方 WebSocket。

若 Polymarket 主监控或跨市场采集也掉线，三者的完整恢复命令和状态检查见项目根目录 [README](../README.md) 的“三个实时采集器的完整启动/恢复命令”。Gate 进程应在 Polymarket 主监控恢复并更新 `data/live_monitor/state/latest.json` 后启动；启动后会继续写入现有 `data/gate_monitor/` 分区。

## “实时展示、每10秒聚合落盘”的准确含义

采集器直接连接 Gate 官方 `wss://prediction-ws.gateio.ws/...` 长连接，不请求或渲染 Gate 网页。收到消息后会立即解析：

- `predict.poly.orderbook`：直接更新当前所选方向卖盘；买盘用互补价格换算另一方向卖盘，并明确标为推断值；
- `predict.poly.trade`：字段 `tid` 是 outcome token ID，可直接映射 UP/DOWN；`BUY` 解释为主动买入该方向，`SELL` 再互补换算为主动买入相反方向。若极少数消息的 token 暂时无法映射，才回退到与当时两边价格做最近匹配，并明确标为推断；
- `predict.poly.price_to_beat_final_price`：订阅当前市场并暂留刚结束市场的订阅，用官方目标价和最终价判断 Gate 胜出方向；
- WebSocket 断线会指数退避重连并重新订阅，不使用网页文本兜底。

每个10秒窗口最多保存一条 `GATE_10S_AGGREGATE_SNAPSHOT`：

- 当前市场编号、开始时间、结束时间、剩余秒数；
- UP、DOWN 最后一次观察到的卖一价和数量；
- 最多三档卖盘深度；
- 该10秒窗口的开盘价、最高价、最低价、收盘价和观察次数；
- 该10秒窗口按 UP、DOWN 分开的成交笔数、数量、VWAP、最高/最低/最后成交价；
- WebSocket 数据的本机接收时间；
- 已确认时的 Gate 官方胜出方向。

仪表盘状态文件每1秒覆盖一次，最多展示最近20笔成交；这些逐笔成交不会追加到历史文件。内存中的成交去重 ID 最多1万个，已结束超过30分钟的市场上下文会清理。原始 WebSocket 帧不会永久追加写盘。纸上信号按实时观察到的卖一价触发，无需等待10秒窗口结束。

## 纸上信号

采集器同时保留一个粗粒度 Gate 纸上观察标签：

- UP、DOWN 都检查；
- 买入价 `0.80–0.85`；
- 剩余 `90–30` 秒；
- 每个市场、每个方向最多记录一次；
- 数量为1份，仅按观察到的卖一价估算。

该标签没有模拟真实排队、部分成交、滑点和已核实手续费，字段中会标为粗略成交质量，不能直接与 Polymarket 的盘口成交回测等同。Gate 的胜负通过官方目标价 `price_to_beat` 与最终价 `final_price` 判断；直连器会保留刚结束市场的结算频道订阅。实际收益分析只应使用已结算记录。

## 数据目录

```text
data/gate_monitor/
├── snapshots/date=YYYY-MM-DD/hour=HH/part-001.ndjson.gz  # 10秒聚合快照
├── paper/date=YYYY-MM-DD/hour=HH/part-001.ndjson.gz      # 粗粒度纸上信号及结算
└── state/
    ├── latest.json                    # 最新可读状态
    └── runtime.json                   # 运行参数与计数
```

每条记录是独立 gzip 成员，程序运行中也能安全读取已完成记录。两个状态文件覆盖更新，不会无限增长。按10秒一条计算，每天每条流最多约8,640条；成交只是每个窗口的一小段统计字段，不保存17万条之类的原始事件，通常仍是每天数MB量级。长期运行仍应定期查看目录大小并按研究周期归档。

本机 `http://127.0.0.1:8765/` 仪表盘会读取 `latest.json` 和 `runtime.json`，在 Polymarket 统计下方单独展示 Gate 当前价格、信号数、未结持仓、胜负、毛收益，以及最近的 Gate 入场和官方结算。Gate 总计之外还会分别显示“买涨 UP”和“买跌 DOWN”的信号、未结、胜负、胜率和毛收益。两家平台的成交质量和收益口径不同，因此不会合并成同一总胜率。

## 可用于后续验证的关系

累计足够多的同步市场后，可以按统一时间轴计算：

- Gate 价格变化是否领先 Polymarket，领先多少秒；
- Gate 先到达 `0.75–0.80`、`0.80–0.85` 或 `0.85–0.88` 后，Polymarket 是否同向跟随；
- UP 与 DOWN 的领先性是否对称；
- 两个平台方向一致、相反时，Polymarket 原策略胜率和净收益如何变化；
- Gate 三档深度、价差或快速反转能否解释 Polymarket 的失败样本。

分析时必须使用信号时刻以前最后一份 Gate 快照，不能读取未来数据。历史落盘是10秒聚合，因此跨平台领先时间应主要按10秒分箱解释；实时页面虽更快，但不能据此把历史研究声称为逐笔因果验证。

## 交接检查

下一个 AI 或研究人员应先检查：

1. `state/latest.json` 的 `generatedAtUs` 是否每秒更新；
2. `service.transport` 是否为 `DIRECT_WEBSOCKET`、`browserRequired` 是否为 `false`；
3. 当前市场的 UP、DOWN 价格与同步行情是否一致；
4. gzip 文件能否解压并持续增加记录；
5. 只有已结算市场才进入胜率和盈亏统计。

直连运行文件：`scripts/monitor/run_gate_direct_monitor.mjs`  
公共解析器及旧版网页诊断工具：`scripts/monitor/run_gate_low_frequency_monitor.mjs`  
单元测试：`tests/node/gate_low_frequency_monitor.test.mjs`

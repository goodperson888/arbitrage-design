# BTC 5分钟研究信号实时监控与纸上交易

## 用途

该监控器独立于原策略运行，不修改 `configs/strategies/last_2m_threshold.json`。它通过 Polymarket 官方公开 Gamma API 和 Market WebSocket 收集当前 BTC 5分钟市场的实时盘口、成交和结算，并对已归档的研究信号做纸上交易。

监控器没有钱包、私钥、API交易凭证或真实下单路径。

2026-11-21新增：[候选策略并行纸上验证](候选策略并行纸上验证.md)。原页面保留，下列原规则不变；新Tab独立运行六候选（含A5/A6）和十二本账户，跟随`./arb-monitor start paper`开启。悬浮或点击策略标题可查看详细开仓逻辑。

## 当前 UP / DOWN 纸上策略（实时观察口径）

- 方向：UP、DOWN 都监控，但两方向分别统计；同一市场仍最多一笔纸上成交。
- 触发：官方 Market WebSocket 对应方向的真实主动买入事件（side=BUY），单笔不少于10份；单纯报价不触发。
- 实际入场：10份逐档成交后的VWAP在0.80–0.85观察区间。
- 时间：距离结算90–30秒。
- 动量：入场前约5秒，对应方向最优卖价上涨至少0.01；没有足够历史盘口时不触发。
- 纸上数量：10份。
- 模拟执行：读取对应方向卖盘，以0.85为限价逐档消耗；必须完整成交10份。
- 数据新鲜度：盘口接收时间不得超过2秒。
- 盘口稳定性、跨市场方向一致性、VWAP子区间和其他时间窗口只记录为可选回测字段，不作为实时监控的硬过滤。
- 每个市场最多一笔纸上成交；UP 和 DOWN 使用同一市场级互斥限制。
- 0.99提前退出目前不在实时纸上监控中，仅由离线回测单独模拟。
- 手续费：`0.07 × price × (1 - price)`。

看到历史成交只会生成信号。缺少盘口、盘口陈旧或0.85以内深度不足会记录条件失败，不会伪造纸上成交。新的组合条件由离线回测读取已记录的事件进行比较。

此前短暂运行过的严格v2信号仍保留在日志中，但不再作为当前实时策略；后续报告按`strategyId`分开，不能与基础观察口径合并评价。

旧的 DOWN-only 运行记录仍保留在离线报告中；当前页面使用新的策略 ID，UP、DOWN 的统计只来自这一套同规则候选，不与旧记录混合。

配置文件：`configs/research/live_paper_monitor_v1.json`。

结算接口失败时按“每个市场独立”的指数退避重试：默认结算轮询间隔为 5 秒，随后为
10、20、40、80、160、320、640、1280、2560 秒，单个市场最长退避为 3600 秒（1 小时）。
成功读到官方结算标签后立即清除该市场的退避状态；失败只写入健康日志并保留待重试，不会删除行情或纸上账本数据。

## 启动

推荐从项目根目录使用封装控制器：

```bash
./arb-monitor start paper   # Polymarket 主监控
./arb-monitor status        # 查看会话和状态更新时间
./arb-monitor stop paper    # 停止但保留 data/ 数据
```

三个采集器一起启动、重启、进入日志和恢复掉线的短命令，统一记录在项目根目录 [README](../README.md) 的“三个实时采集器的完整启动/恢复命令”小节。控制器使用独立 `screen` 会话，关闭当前终端不会自动杀掉监控。

```bash
PYTHONPATH=src python3 scripts/monitor/run_research_paper_monitor.py \
  --proxy http://127.0.0.1:7890 \
  --port 8765 \
  --duration-hours 24
```

省略 `--duration-hours` 会持续运行，按 Ctrl-C 可正常停止。重新启动会继续读取原有纸上账本，不会覆盖历史文件。

本地页面：`http://127.0.0.1:8765`。

跨市场和 Gate 需要分别启动，完整恢复命令（含当前本机 Python/Node 路径）统一维护在项目根目录 [README](../README.md) 的“三个实时采集器的完整启动/恢复命令”小节。掉线恢复时不要删除 `data/`，直接重新执行对应命令即可。

## 数据目录

默认目录为 `data/live_monitor`：

```text
data/live_monitor/
├── raw/date=YYYY-MM-DD/hour=HH/part-001.ndjson.gz
├── events/date=YYYY-MM-DD/hour=HH/part-NNN.ndjson.gz
├── signals/date=YYYY-MM-DD/hour=HH/part-NNN.ndjson.gz
├── paper/date=YYYY-MM-DD/hour=HH/part-001.ndjson
├── health/date=YYYY-MM-DD/hour=HH/part-001.ndjson
├── sessions/date=YYYY-MM-DD/hour=HH/part-001.ndjson
└── state/latest.json
```

- `raw`：gzip追加式官方消息采样；所有成交保留，价格增量每100毫秒最多一条，完整盘口每1秒最多一条。
- `events`：符合项目数据契约的市场定义、成交、250毫秒十档盘口快照和结算事件。
- `signals`：每笔 UP、DOWN 成交的条件检查、触发结果和未成交原因。
- `paper`：纸上成交、0.99提前退出与官方结算的事件账本。
- `health`：断线和接口错误。
- `sessions`：每次启动的完整参数、数据源和纸上交易声明。
- `state/latest.json`：页面当前状态缓存，不作为回测事实源。

所有事实流都是追加写入，按UTC日期和小时分区。程序不会重写已经记录的事件。

自2026-11-21起，`events`与`signals`也采用gzip无损压缩，记录频率和字段保持。每次重启新建压缩分片，小体积账本保留原格式。`./arb-monitor compact`预览、`./arb-monitor compact --apply`校验并压缩旧明文分区。页面显示磁盘余量和低空间告警；详情见[日志压缩与磁盘管理](日志压缩与磁盘管理.md)。

## 导出用于回测

导出标准市场事件：

```bash
python3 scripts/utils/export_live_monitor_stream.py \
  --data-dir data/live_monitor \
  --stream events \
  --date-from 2026-08-21 \
  --date-to 2026-08-22 \
  --out data/live_exports/btc_5m_2026-08-21_22.ndjson
```

导出信号或纸上账本时，将 `--stream` 改为 `signals` 或 `paper`。导出器拒绝覆盖已有输出，并生成包含输入文件、行数、去重数和 SHA-256 的 manifest。

标准事件导出后可以直接交给现有回放、质量检查和最后窗口验证工具。

## 增量验证不同价格和秒数

日常比较“最后多少秒、什么价格、买 UP 还是 DOWN、买多少份”时，优先使用增量验证器，不必反复导出和全量解析原始事件：

```bash
PYTHONPATH=src python3 scripts/analysis/run_incremental_live_strategy_validation.py
```

- 行情索引：`data/derived/live_strategy_cache.sqlite3`；
- 策略组合：`configs/research/live_strategy_cases_v1.json`；
- 中文报告：`outputs/live_incremental_strategy_validation/实时策略增量验证.md`；
- 汇总及逐笔成交：同目录的 JSON/CSV 与 `incremental_strategy_fills.csv`。

缓存根据事件文件大小和修改时间跳过没有变化的小时分区，并按事件唯一键去重。只对已结算市场出最终胜率和盈亏；相同策略的既有市场结果不会重复计算。逐笔成交的 `execution_us` 是本机实际收到盘口的时间，可安全地向前关联跨市场采集的每秒快照。

当前实现的成交语义是：`quote` 要求窗口内连续盘口覆盖并在价格区间内完整成交；`buy_trade` 先看到满足条件的主动买入，再使用当时本机已经收到的最近盘口逐档吃单。两者都默认要求计划数量完整成交，手续费计入盈亏。若未来改成挂单、允许部分成交、引入排队位置或新的滑点模型，这属于底层成交语义变化，旧策略结果不得混用。

## 监控重点

纸上运行一段时间后，应优先比较：

- 合格信号数、实际纸上成交数和未成交率；
- UP、DOWN 各自的机会数、胜率和净收益，禁止只看合并值；
- 不同盘口深度下的VWAP；
- 信号时间到本机接收时间的延迟；
- 45–60秒、5–45秒和全部60–5秒的独立表现；
- 0.99提前退出的机会率、完整成交率、退出后若继续持有到结算的反事实结果；
- Spot/Futures/Hyperliquid三方同向与未同向样本的胜率、盈亏和机会率；
- 最新100、300、500个市场的滚动胜率和净收益；
- 原策略全部交易与带研究标签子集的差异。

在累计至少300个完整市场以前，纸上结果不应自动升级为真实策略参数。

领先性初测可运行：

```bash
python3 scripts/analysis/analyze_rtds_lead_lag.py \
  --min-remaining 5 --max-remaining 60 --move-threshold-bps 0.25
```

该脚本同时报告相关性滞后和外部价格变动后1、2、3、5秒的Polymarket方向命中率。它是探索性统计，必须看样本数并做样本外验证，不能把同秒相关直接称为可交易领先。

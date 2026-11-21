# Arbitrage Research

预测市场 5 分钟 BTC UP/DOWN 的数据采集、可回放回测和纸上验证项目。它不负责真实下单；实盘执行仍由同级的 `mexc-polymarket-arb` 项目负责。

## 快速开始

在本目录执行：

```bash
./arb-monitor start
./arb-monitor status
```

页面：<http://127.0.0.1:8765>。停止或重启：

```bash
./arb-monitor stop
./arb-monitor restart paper
```

15 分钟跨市场采集可以单独控制：

```bash
./arb-monitor start cross15
./arb-monitor stop cross15
```

`cross15` 同步保存15分钟 Polymarket 每秒压缩盘口摘要和官方成交事件，可用于后续价格区间、10份VWAP、最坏成交价和深度回测；仍然只采集、不下单。

完整命令、screen 管理、回测 CLI、数据抓取和故障排查统一见 [运行与回测命令手册](docs/运行与回测命令.md)。

## 研究入口

- [研究测试总索引](docs/研究测试总索引.md)：全部历史回测、实时验证和策略报告。
- [历史回测统计汇总](docs/历史回测统计汇总_中文版.md)：价格、时间、UP/DOWN、失败原因等历史结果。
- [实时策略增量验证](outputs/live_incremental_strategy_validation/实时策略增量验证.md)：新增已结算市场的快速比较。
- [Binance / Hyperliquid / Deribit 与 5 分钟市场关系](docs/币安_Hyperliquid_Deribit与5分钟市场预测关系.md)：跨市场特征、领先性和因果边界。
- [实时纸上监控说明](docs/live_paper_monitor.md)：Polymarket 纸上账本和数据结构。
- [日志压缩与磁盘管理](docs/日志压缩与磁盘管理.md)：无损压缩、历史日志整理和页面磁盘余量提醒。
- [候选策略并行纸上验证](docs/候选策略并行纸上验证.md)：六候选（含A5/A6）、十二本独立虚拟账户；标题悬浮查看开仓逻辑，随`start paper`启用。
- [Gate 低频监控说明](docs/gate_low_frequency_monitor.md)：Gate WebSocket、10 秒聚合和磁盘边界。
- [15 分钟市场验证方案](docs/15分钟市场验证方案.md)：独立采集、不可拼接 5 分钟市场和低磁盘方案。
- [实盘自动交易方案](docs/实盘自动交易方案.md)：真实执行器的隔离目录、订单语义、风控和数据账本；当前仅方案评审。
- [实盘上线前预演](docs/PreMortem-polymarket-live-execution-20260824.md)：重复下单、滑点、断线、策略失效和密钥边界风险。
- [归档研究：末段入场](experiments/archive/btc_5m_last_window_entry_20260821/REPORT.md)。

## 当前默认观察口径

实时监控配置为 [`configs/research/live_paper_monitor_v1.json`](configs/research/live_paper_monitor_v1.json)：

- 当前纸上监控对 UP、DOWN 使用同一套规则并分开统计；每市场最多一笔纸上成交。
- 实际成交 VWAP 约 `0.80–0.85`。
- 剩余时间 `90–30 秒`。
- 最少观察到的主动成交量为 10 份；盘口陈旧或深度不足则不模拟成交。
- 入场前约5秒对应方向最优卖价同方向上涨至少 0.01；动量历史不足则不模拟成交。
- 当前监控不把跨市场指标设为硬过滤器；它们作为入场前快照标签，供后续回测比较。
- `0.99` 提前退出只在离线回测中模拟，不代表实时监控会实际卖出。

离线策略组合另见 [`configs/research/live_strategy_cases_v1.json`](configs/research/live_strategy_cases_v1.json)，因此修改一个测试策略不会覆盖当前纸上观察口径。

## 系统做什么

1. 采集 Polymarket 官方 WebSocket/Gamma/结算信息，并保存可回放事件。
2. 采集 Binance Spot/Futures、Hyperliquid、Deribit 和 RTDS/Gamma 的对齐特征。
3. 以真实入场时点以前的盘口、成交和外部快照模拟成交，不读取未来数据。
4. 市场结算后使用官方标签计算胜率、净收益、机会率和覆盖质量。
5. 将每个策略的参数、执行模型版本和结果写入独立报告，支持增量复算。

## 数据与回测原则

- `data/` 是事实数据区，重启服务时不要删除。
- `state/latest.json` 只供页面展示，不作为回测事实源。
- 原始高频消息会抽样、压缩并按小时分区；内存队列按时间和条数裁剪，不会无限增长。
- 成交模拟规则（IOC、部分成交、逐档吃单、滑点、盘口陈旧、时间基准）属于执行模型；修改语义必须升级 `EXECUTION_MODEL_VERSION`。
- 覆盖不完整或 `provisional=true` 的报告只能用于调试，不能当正式胜率结论。
- 历史回测必须区分 `best_ask`、`trade`、盘口深度等数据等级，不能混合解释。

数据能力分级：L1=K线+结算，L2=历史成交+结算，L3=买一卖一，L4=完整订单簿，L5=L4+本地接收时间与订单反馈。等级越高，越接近真实成交，但数据量和维护成本也越高。

## 项目结构

```text
configs/        策略参数和数据源配置
src/            统一事件模型、回放、回测库
scripts/        fetch / monitor / analysis / validation 命令
data/           本地原始、聚合、索引和状态数据（不进 Git）
outputs/        已生成的回测结果和中文报告
docs/           运行手册、研究规范、跨市场说明
experiments/    可复现实验及归档报告
tests/          单元测试和数据契约测试
```

## 维护边界

本项目只做研究、采集、回放、回测和纸上交易；真实资金、钱包、私钥和交易执行不在这里。研究进程即使崩溃，也不应影响 `mexc-polymarket-arb` 的实盘链路。

通用、无历史依赖的实时概率计算器已独立放在同级目录 [`../arbitrage-realtime`](../arbitrage-realtime/README.md)；实盘执行器仍独立放在 [`../arbitrage-live`](../arbitrage-live/README.md)。

# 项目目录结构

## 根目录

```
arbitrage-research/
├── configs/          # 配置文件
├── contracts/        # 智能合约相关
├── docs/            # 文档
├── examples/        # 示例代码
├── experiments/     # 实验运行记录
├── scripts/         # 脚本工具（按功能分类）
├── src/             # 核心源代码
├── tests/           # 测试代码
├── data/            # 数据文件（按类型分类）
├── .gitignore
├── pyproject.toml
└── README.md
```

## scripts/ 目录结构

```
scripts/
├── analysis/        # 数据分析脚本
│   ├── batch_merge_and_check.py          # 批量合并和检查市场数据
│   ├── binance_analysis.py              # 币安数据分析
│   ├── binance_kline_analysis.py        # 币安K线分析
│   ├── check_down_prices.py             # 检查DOWN token价格
│   ├── check_last_3min.py                # 检查最后3分钟价格
│   ├── check_polymarket_data.py         # 检查Polymarket数据
│   ├── check_polymarket_direct.py       # 直接检查Polymarket（无代理）
│   ├── analyze_bidirectional.py         # 双向策略分析
│   ├── analyze_both_directions.py       # 双向方向分析
│   ├── analyze_correct_bidirectional.py # 正确双向分析
│   ├── analyze_matching_conditions.py   # 匹配条件分析
│   ├── analyze_polymarket_data.py       # Polymarket数据分析
│   ├── analyze_price_range.py           # 价格范围分析
│   └── analyze_up_down_performance.py   # UP/DOWN表现分析
│
├── experiments/     # 实验运行脚本
│   ├── run_btc_5m_experiment.py         # 运行BTC 5分钟实验
│   ├── run_fixed_test.py                # 运行固定测试
│   ├── run_full_experiment.py           # 运行完整实验
│   └── run_test_experiment.py           # 运行测试实验
│
├── fetch/          # 数据获取脚本
│   ├── batch_redownload_markets.py      # 批量重新下载市场数据
│   ├── convert_9markets_to_ndjson.py    # 转换9个市场为NDJSON
│   ├── convert_filtered_markets.py      # 转换过滤的市场
│   ├── convert_polyhistorical_to_ndjson.py # 转换Polyhistorical为NDJSON
│   ├── fetch_polyhistorical_data.py    # 获取Polyhistorical数据
│   └── merge_snap_26.py                 # 合并快照数据
│
├── monitor/        # 监控脚本
│   ├── auto_5min_monitor.py             # 自动5分钟监控
│   ├── binance_polymarket_monitor.py   # 币安-Polymarket监控
│   ├── browser_monitor.py               # 浏览器监控
│   ├── monitor_live_markets.py          # 实时市场监控
│   ├── monitor_live_with_paper_trading.py # 实时监控+纸面交易
│   ├── polymarket_monitor.py            # Polymarket监控
│   └── smart_monitor.py                 # 智能监控
│
├── test/           # 测试脚本
│   ├── test_binance_connection.py      # 测试币安连接
│   ├── test_network.py                  # 测试网络
│   ├── test_polymarket_api.py           # 测试Polymarket API
│   └── test_proxy_fix.py                # 测试代理修复
│
├── utils/          # 工具脚本
│   ├── create_manual_data.py            # 创建手动数据
│   ├── diagnose_network.py              # 网络诊断
│   └── setup_proxy.py                   # 设置代理
│
└── validation/     # 验证脚本
    ├── validate_btc_eth_strategy.py    # 验证BTC/ETH策略
    ├── validate_strategy.py             # 验证策略
    └── verify_polymarket.py             # 验证Polymarket
```

## data/ 目录结构

```
data/
├── binance/        # 币安相关数据
│   └── binance_analysis.json
├── markets/        # 市场数据
├── monitor/        # 监控数据
│   ├── binance_analysis.json
│   ├── market_log.txt
│   ├── paper_trades.json
│   └── test_binance.ndjson
├── raw_snapshots/  # 原始快照数据
└── btc_eth_5min_sample.ndjson  # BTC/ETH 5分钟样本数据
```

## 使用说明

### 数据获取
- 从 Polymarket 获取历史数据：`scripts/fetch/fetch_polyhistorical_data.py`
- 批量下载市场数据：`scripts/fetch/batch_redownload_markets.py`

### 数据分析
- 分析市场数据：`scripts/analysis/analyze_bidirectional.py`
- 检查价格范围：`scripts/analysis/check_last_3min.py`
- 币安数据分析：`scripts/analysis/binance_analysis.py`

### 策略验证
- 验证策略：`scripts/validation/validate_strategy.py`
- 验证 BTC/ETH 策略：`scripts/validation/validate_btc_eth_strategy.py`

### 实时监控
- 实时市场监控：`scripts/monitor/monitor_live_markets.py`
- 币安-Polymarket 监控：`scripts/monitor/binance_polymarket_monitor.py`
- 纸面交易监控：`scripts/monitor/monitor_live_with_paper_trading.py`

### 实验运行
- 运行完整实验：`scripts/experiments/run_full_experiment.py`
- 运行BTC 5分钟实验：`scripts/experiments/run_btc_5m_experiment.py`

### 测试工具
- 测试币安连接：`scripts/test/test_binance_connection.py`
- 测试网络：`scripts/test/test_network.py`
- 测试代理：`scripts/test/test_proxy_fix.py`
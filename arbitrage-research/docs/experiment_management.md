# 实验管理系统使用指南

## 概述

工程化的实验管理系统用于系统性地运行参数扫描回测，确保结果可复现、可追溯、不会混乱。

## 架构设计

```
experiments/
├── configs/           # 实验配置文件
├── results/           # 实验结果目录
│   └── experiment_{id}/  # 每个实验的独立目录
│       ├── experiment_config.json
│       ├── experiment_result.json
│       └── results_summary.csv
├── archive/           # 归档的实验
└── experiment_index.json  # 实验索引
```

## 核心模块

### 1. ParameterSpace - 参数空间定义
定义参数搜索空间，支持默认和测试模式。

### 2. ExperimentConfig - 实验配置
每个实验的唯一配置，包含参数空间、数据源等信息。

### 3. ExperimentResult - 实验结果
汇总所有参数组合的运行结果，支持统计分析。

### 4. ExperimentManager - 实验管理器
管理实验生命周期：创建、运行、查询、归档、删除。

### 5. ParameterSweep - 参数扫描器
执行具体的参数扫描逻辑。

## 使用方法

### 1. 创建实验

```bash
# 使用默认参数空间（完整扫描）
python -m arb_research experiment create \
  --name "parameter_sweep_full" \
  --description "完整参数扫描测试" \
  --events data/events.ndjson \
  --experiments-dir experiments

# 使用测试参数空间（快速验证）
python -m arb_research experiment create \
  --name "parameter_sweep_test" \
  --description "快速测试参数扫描" \
  --events data/events.ndjson \
  --experiments-dir experiments \
  --test-params
```

### 2. 运行实验

```bash
python -m arb_research experiment run \
  --experiment-id <experiment_id> \
  --experiments-dir experiments
```

### 3. 列出实验

```bash
# 列出所有实验
python -m arb_research experiment list \
  --experiments-dir experiments

# 按状态过滤
python -m arb_research experiment list \
  --experiments-dir experiments \
  --status completed
```

### 4. 查看实验结果

```bash
python -m arb_research experiment show \
  --experiment-id <experiment_id> \
  --experiments-dir experiments \
  --top 10
```

## 参数空间说明

### 默认参数空间
- `window_seconds`: [120, 180] - 2分钟 vs 3分钟
- `threshold_price`: [0.70-0.88] - 入场价格范围，步长0.01
- `max_entry_price`: [0.82, 0.85, 0.88] - 最大入场价格
- `order_size`: [1, 5, 10] - 订单大小
- `min_ask_size`: [1, 5, 10] - 最小流动性
- `fee_rate`: [0.07] - Polymarket 7%手续费

总组合数：约 1500+ 个参数组合

### 测试参数空间
- `window_seconds`: [120] - 仅2分钟
- `threshold_price`: [0.70, 0.80, 0.85] - 仅3个阈值
- `max_entry_price`: [0.82] - 仅一个最大入场价
- `order_size`: [1] - 仅一个订单大小
- `min_ask_size`: [1] - 仅一个最小流动性
- `fee_rate`: [0.07] - Polymarket 7%手续费

总组合数：3 个参数组合（快速验证）

## 输出结果

每个实验会生成以下文件：

1. `experiment_config.json` - 实验配置
2. `experiment_result.json` - 完整结果（JSON格式）
3. `results_summary.csv` - 结果摘要（CSV格式，便于分析）

## 最佳实践

1. **先用测试参数验证**：先使用 `--test-params` 快速验证逻辑
2. **命名规范**：使用描述性的实验名称，如 `parameter_sweep_2min_70-80`
3. **定期归档**：完成的实验及时归档，保持工作区整洁
4. **版本控制**：重要的实验配置应纳入版本控制
5. **数据分析**：使用 `results_summary.csv` 进行进一步分析

## 扩展功能

### 自定义参数空间

```python
from arb_research.experiments import ExperimentManager, ParameterSpace
from decimal import Decimal
from pathlib import Path

manager = ExperimentManager(Path("experiments"))

# 自定义参数空间
custom_space = ParameterSpace(
    window_seconds=[120, 180, 240],
    threshold_price=[Decimal(str(x/100)) for x in range(70, 90)],
    max_entry_price=[Decimal("0.80"), Decimal("0.85"), Decimal("0.90")],
    order_size=[Decimal("1"), Decimal("10")],
    min_ask_size=[Decimal("1"), Decimal("5")],
    fee_rate=[Decimal("0.07")],
)

config = manager.create_experiment(
    experiment_name="custom_sweep",
    description="自定义参数空间",
    strategy_type="prediction_last_window_threshold",
    events_file=Path("data/events.ndjson"),
    parameter_space=custom_space,
)
```

### 比较多个实验

```python
from arb_research.experiments import ExperimentManager

manager = ExperimentManager(Path("experiments"))

comparisons = manager.compare_experiments([
    "exp_id_1",
    "exp_id_2", 
    "exp_id_3"
])

print(comparisons)
```

## 常见问题

### Q: 如何恢复已删除的实验？
A: 已删除的实验无法恢复，建议使用归档功能而非删除。

### Q: 参数扫描时间太长怎么办？
A: 使用 `--test-params` 先验证逻辑，或者缩小参数空间范围。

### Q: 如何分析大量实验结果？
A: 使用生成的 `results_summary.csv` 文件，可用 DuckDB、Excel 等工具分析。

### Q: 实验可以重复运行吗？
A: 可以，但会覆盖之前的结果。如需保留，请先复制实验目录。
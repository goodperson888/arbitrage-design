#!/usr/bin/env python3
"""
快速开始示例：使用实验管理系统进行参数扫描
"""
from pathlib import Path
from arb_research.experiments import ExperimentManager, ParameterSpace
from decimal import Decimal

def main():
    # 设置路径
    base_dir = Path("/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research")
    experiments_dir = base_dir / "experiments"
    
    # 假设有一个事件数据文件
    events_file = base_dir / "data" / "events.ndjson"
    
    # 创建实验管理器
    manager = ExperimentManager(experiments_dir)
    
    # 选项1: 使用默认参数空间创建实验
    print("=== 选项1: 使用默认参数空间 ===")
    config_default = manager.create_experiment(
        experiment_name="parameter_sweep_default",
        description="使用默认参数空间的完整扫描",
        strategy_type="prediction_last_window_threshold",
        events_file=events_file,
        metadata={"notes": "默认参数空间，包含约1500+个组合"},
    )
    
    print(f"创建实验: {config_default.experiment_name}")
    print(f"实验ID: {config_default.experiment_id}")
    print(f"参数组合数: {config_default.parameter_space.total_combinations()}")
    print(f"配置文件: {config_default.get_experiment_dir()}")
    
    # 选项2: 使用测试参数空间创建实验（快速验证）
    print("\n=== 选项2: 使用测试参数空间 ===")
    config_test = manager.create_experiment(
        experiment_name="parameter_sweep_test",
        description="使用测试参数空间的快速验证",
        strategy_type="prediction_last_window_threshold",
        events_file=events_file,
        use_test_params=True,  # 使用简化的参数空间
        metadata={"notes": "测试参数空间，仅3个组合"},
    )
    
    print(f"创建实验: {config_test.experiment_name}")
    print(f"实验ID: {config_test.experiment_id}")
    print(f"参数组合数: {config_test.parameter_space.total_combinations()}")
    print(f"配置文件: {config_test.get_experiment_dir()}")
    
    # 选项3: 自定义参数空间
    print("\n=== 选项3: 自定义参数空间 ===")
    custom_space = ParameterSpace(
        window_seconds=[120, 180],  # 2分钟和3分钟
        threshold_price=[Decimal(str(x/100)) for x in range(75, 86)],  # 0.75-0.85
        max_entry_price=[Decimal("0.80"), Decimal("0.85")],  # 两个最大入场价
        order_size=[Decimal("1"), Decimal("5")],  # 两个订单大小
        min_ask_size=[Decimal("1")],  # 一个最小流动性
        fee_rate=[Decimal("0.07")],  # Polymarket 7%手续费
    )
    
    config_custom = manager.create_experiment(
        experiment_name="parameter_sweep_custom",
        description="自定义参数空间的针对性扫描",
        strategy_type="prediction_last_window_threshold",
        events_file=events_file,
        parameter_space=custom_space,
        metadata={"notes": "自定义参数空间，针对特定范围"},
    )
    
    print(f"创建实验: {config_custom.experiment_name}")
    print(f"实验ID: {config_custom.experiment_id}")
    print(f"参数组合数: {config_custom.parameter_space.total_combinations()}")
    print(f"配置文件: {config_custom.get_experiment_dir()}")
    
    # 列出所有实验
    print("\n=== 所有实验列表 ===")
    experiments = manager.list_experiments()
    for exp in experiments:
        print(f"- {exp['experiment_name']} (ID: {exp['experiment_id'][:8]}, Status: {exp['status']})")
    
    print("\n=== 下一步 ===")
    print("使用以下命令运行实验:")
    print(f"python -m arb_research experiment run --experiment-id {config_test.experiment_id} --experiments-dir {experiments_dir}")
    print("\n或者查看实验结果:")
    print(f"python -m arb_research experiment show --experiment-id <experiment_id> --experiments-dir {experiments_dir}")

if __name__ == "__main__":
    main()
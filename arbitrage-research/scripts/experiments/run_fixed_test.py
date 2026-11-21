#!/usr/bin/env python3
"""
重新创建测试实验，使用正确的数据文件路径
"""
from pathlib import Path
import sys

# 添加src到路径
sys.path.insert(0, "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/src")

from arb_research.experiments import ExperimentManager

def main():
    # 设置路径
    base_dir = Path("/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research")
    experiments_dir = base_dir / "experiments"
    
    # 使用正确的数据文件路径
    events_file = base_dir / "examples" / "prediction_events.ndjson"
    
    # 创建实验管理器
    manager = ExperimentManager(experiments_dir)
    
    # 创建测试实验（3个参数组合）
    print("创建测试实验...")
    config = manager.create_experiment(
        experiment_name="parameter_sweep_test_fixed",
        description="修复路径后的测试实验",
        strategy_type="prediction_last_window_threshold",
        events_file=events_file,
        use_test_params=True,  # 使用简化的参数空间
        metadata={"notes": "测试参数空间，仅3个组合"},
    )
    
    print(f"创建实验: {config.experiment_name}")
    print(f"实验ID: {config.experiment_id}")
    print(f"参数组合数: {config.parameter_space.total_combinations()}")
    print(f"数据文件: {config.events_file}")
    print(f"配置文件: {config.get_experiment_dir()}")
    
    # 运行测试实验
    print(f"\n开始运行测试实验...")
    
    def progress_callback(current, total, params):
        print(f"进度: {current}/{total} ({current/total*100:.1f}%) - 参数: {params}")
    
    result = manager.run_experiment(config.experiment_id, progress_callback)
    
    print(f"\n测试实验完成!")
    print(f"总运行次数: {result.total_runs}")
    print(f"成功: {result.successful_runs}")
    print(f"失败: {result.failed_runs}")
    
    if result.summary_statistics:
        print(f"\n统计摘要:")
        print(f"  平均胜率: {result.summary_statistics.get('avg_win_rate', 'N/A')}")
        print(f"  最大胜率: {result.summary_statistics.get('max_win_rate', 'N/A')}")
        print(f"  平均净PnL: {result.summary_statistics.get('avg_net_pnl_per_share', 'N/A')}")
        print(f"  最大净PnL: {result.summary_statistics.get('max_net_pnl_per_share', 'N/A')}")
    
    # 如果测试成功，创建完整参数扫描实验
    if result.successful_runs > 0:
        print(f"\n测试成功！准备创建完整参数扫描实验...")
        
        full_config = manager.create_experiment(
            experiment_name="parameter_sweep_full_2min_70_88",
            description="完整参数扫描：最后2-3分钟，70-88价格范围",
            strategy_type="prediction_last_window_threshold",
            events_file=events_file,
            use_test_params=False,  # 使用完整的参数空间
            metadata={"notes": "完整参数空间，针对最后2-3分钟和70-88价格范围"},
        )
        
        print(f"完整实验创建: {full_config.experiment_name}")
        print(f"实验ID: {full_config.experiment_id}")
        print(f"参数组合数: {full_config.parameter_space.total_combinations()}")
        print(f"\n使用以下命令运行完整实验:")
        print(f"python3 run_full_experiment.py {full_config.experiment_id}")

if __name__ == "__main__":
    main()
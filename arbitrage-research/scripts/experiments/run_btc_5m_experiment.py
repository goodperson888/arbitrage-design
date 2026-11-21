#!/usr/bin/env python3
"""
使用 5 分钟市场的真实数据运行参数扫实验。
对比最后 2 分钟 vs 最后 3 分钟进场，价格在 0.70-0.88 范围内的策略。
"""
import sys
from decimal import Decimal
from pathlib import Path

# 添加项目路径
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root / "src"))

from arb_research.experiments.experiment_manager import ExperimentManager
from arb_research.experiments.experiment_config import ParameterSpace

def main():
    # 设置路径
    base_dir = project_root / "experiments"
    events_file = project_root / "data" / "markets" / "btc_5m_9markets_full.ndjson"
    
    print(f"使用数据文件: {events_file}")
    print(f"实验目录: {base_dir}")
    
    # 创建实验管理器
    manager = ExperimentManager(base_dir)
    
    # 创建自定义参数空间（专注 2 分钟 vs 3 分钟对比）
    parameter_space = ParameterSpace(
        window_seconds=[120, 180],  # 2分钟 vs 3分钟
        threshold_price=[Decimal("0.70"), Decimal("0.75"), Decimal("0.80"), Decimal("0.85")],  # 关键价格点
        max_entry_price=[Decimal("0.88")],  # 你的策略上限
        order_size=[Decimal("1")],  # 固定订单大小
        min_ask_size=[Decimal("1")],  # 最小流动性
        fee_rate=[Decimal("0.0001")],  # Polymarket 0.01% fee (1个基点)
    )
    
    print(f"参数空间: {parameter_space.total_combinations()} 个组合")
    print(f"  window_seconds: {parameter_space.window_seconds}")
    print(f"  threshold_price: {[str(p) for p in parameter_space.threshold_price]}")
    print(f"  max_entry_price: {[str(p) for p in parameter_space.max_entry_price]}")
    
    # 创建实验
    experiment_name = "btc_5m_9markets_correct_fee"
    description = "使用 5 分钟市场真实数据对比 2 分钟 vs 3 分钟进场策略（价格 0.70-0.88）"
    
    config = manager.create_experiment(
        experiment_name=experiment_name,
        description=description,
        strategy_type="prediction_last_window_threshold",
        events_file=events_file,
        parameter_space=parameter_space,
        metadata={"data_type": "real", "market_type": "5m", "markets_count": 5}
    )
    
    print(f"\n创建实验: {config.experiment_id}")
    print(f"实验名称: {experiment_name}")
    
    # 运行实验
    print("\n开始运行参数扫实验...")
    result = manager.run_experiment(config.experiment_id)
    
    print(f"\n实验完成！")
    print(f"总组合数: {result.total_runs}")
    print(f"成功组合数: {result.successful_runs}")
    print(f"结果文件: {config.get_experiment_dir() / 'experiment_result.json'}")
    
    # 显示最佳参数组合
    from arb_research.experiments.parameter_sweep import ParameterSweep
    top_combinations = ParameterSweep(config)
    top_combinations.result = result
    top_runs = top_combinations.get_top_performers(n=5)
    if top_runs:
        print("\n最佳参数组合:")
        for i, run in enumerate(top_runs, 1):
            print(f"  {i}. {run.parameters} | {run.metrics}")

if __name__ == "__main__":
    main()

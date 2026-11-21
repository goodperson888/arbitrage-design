#!/usr/bin/env python3
"""
运行完整参数扫描实验
"""
from pathlib import Path
import sys

# 添加src到路径
sys.path.insert(0, "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/src")

from arb_research.experiments import ExperimentManager

def main():
    if len(sys.argv) < 2:
        print("使用方法: python3 run_full_experiment.py <experiment_id>")
        sys.exit(1)
    
    experiment_id = sys.argv[1]
    
    # 设置路径
    experiments_dir = Path("/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/experiments")
    
    # 创建实验管理器
    manager = ExperimentManager(experiments_dir)
    
    # 运行完整参数扫描实验
    print(f"开始运行完整参数扫描实验: {experiment_id}")
    print("这将测试1026个参数组合，可能需要一些时间...")
    
    def progress_callback(current, total, params):
        if current % 50 == 0 or current == total - 1:  # 每50次或最后显示进度
            print(f"进度: {current+1}/{total} ({(current+1)/total*100:.1f}%)")
    
    result = manager.run_experiment(experiment_id, progress_callback)
    
    print(f"\n完整参数扫描完成!")
    print(f"总运行次数: {result.total_runs}")
    print(f"成功: {result.successful_runs}")
    print(f"失败: {result.failed_runs}")
    
    if result.summary_statistics:
        print(f"\n统计摘要:")
        print(f"  平均胜率: {result.summary_statistics.get('avg_win_rate', 'N/A')}")
        print(f"  最大胜率: {result.summary_statistics.get('max_win_rate', 'N/A')}")
        print(f"  平均净PnL: {result.summary_statistics.get('avg_net_pnl_per_share', 'N/A')}")
        print(f"  最大净PnL: {result.summary_statistics.get('max_net_pnl_per_share', 'N/A')}")
        print(f"  总成交次数: {result.summary_statistics.get('total_fills', 'N/A')}")
    
    # 获取最佳表现者
    from arb_research.experiments import ParameterSweep
    config = manager.get_experiment_config(experiment_id)
    sweep = ParameterSweep(config)
    sweep.result = result
    
    top_performers = sweep.get_top_performers(n=10)
    
    print(f"\n=== 最佳10个参数组合 ===")
    print(f"{'排名':<6} {'窗口(秒)':<10} {'阈值':<10} {'最大入场':<10} {'胜率':<10} {'净PnL':<12}")
    print("-" * 65)
    
    for i, run in enumerate(top_performers, 1):
        print(f"{i:<6} {run.parameters.get('window_seconds'):<10} "
              f"{str(run.parameters.get('threshold_price')):<10} "
              f"{str(run.parameters.get('max_entry_price')):<10} "
              f"{run.metrics.get('winRate', 'N/A'):<10} "
              f"{run.metrics.get('netPnlPerShare', 'N/A'):<12}")
    
    # 获取参数分析
    analysis = sweep.get_parameter_analysis()
    print(f"\n=== 参数性能分析 ===")
    for key, value in analysis.items():
        print(f"{key}: {value}")

if __name__ == "__main__":
    main()
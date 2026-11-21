#!/usr/bin/env python3
"""
运行测试实验验证系统
"""
from pathlib import Path
import sys

# 添加src到路径
sys.path.insert(0, "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/src")

from arb_research.experiments import ExperimentManager

def main():
    # 设置路径
    experiments_dir = Path("/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/experiments")
    
    # 创建实验管理器
    manager = ExperimentManager(experiments_dir)
    
    # 运行测试实验（3个参数组合）
    test_experiment_id = "3a17dbaa-3f8f-412b-af75-95ad07b7018d"
    
    print(f"开始运行测试实验: {test_experiment_id}")
    
    def progress_callback(current, total, params):
        print(f"进度: {current}/{total} ({current/total*100:.1f}%) - 参数: {params}")
    
    result = manager.run_experiment(test_experiment_id, progress_callback)
    
    print(f"\n实验完成!")
    print(f"总运行次数: {result.total_runs}")
    print(f"成功: {result.successful_runs}")
    print(f"失败: {result.failed_runs}")
    
    if result.summary_statistics:
        print(f"\n统计摘要:")
        print(f"  平均胜率: {result.summary_statistics.get('avg_win_rate', 'N/A')}")
        print(f"  最大胜率: {result.summary_statistics.get('max_win_rate', 'N/A')}")
        print(f"  平均净PnL: {result.summary_statistics.get('avg_net_pnl_per_share', 'N/A')}")
        print(f"  最大净PnL: {result.summary_statistics.get('max_net_pnl_per_share', 'N/A')}")

if __name__ == "__main__":
    main()
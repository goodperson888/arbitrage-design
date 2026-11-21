#!/usr/bin/env python3
"""
使用新创建的BTC/ETH模拟数据进行策略验证
"""
import sys
sys.path.insert(0, "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research/src")

from pathlib import Path
from arb_research.experiments import ExperimentManager, ParameterSpace
from decimal import Decimal

def main():
    print("=== 使用BTC/ETH模拟数据进行策略验证 ===")
    
    # 设置路径
    base_dir = Path("/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research")
    experiments_dir = base_dir / "experiments"
    events_file = base_dir / "data" / "btc_eth_5min_sample.ndjson"
    
    # 检查数据文件是否存在
    if not events_file.exists():
        print(f"数据文件不存在: {events_file}")
        return
    
    # 创建实验管理器
    manager = ExperimentManager(experiments_dir)
    
    # 定义针对2-3分钟、70-88价格范围的参数空间
    target_space = ParameterSpace(
        window_seconds=[120, 180],  # 2分钟、3分钟
        threshold_price=[Decimal(str(x/100)) for x in range(70, 89)],  # 0.70-0.88
        max_entry_price=[Decimal("0.82"), Decimal("0.85"), Decimal("0.88")],  # 最大入场价
        order_size=[Decimal("1"), Decimal("5")],  # 订单大小
        min_ask_size=[Decimal("1")],  # 最小流动性
        fee_rate=[Decimal("0.07")],  # Polymarket 7%手续费
    )
    
    print(f"参数组合数: {target_space.total_combinations()}")
    print(f"时间窗口: 2分钟(120秒)、3分钟(180秒)")
    print(f"价格范围: 0.70-0.88")
    print(f"最大入场价: 0.82, 0.85, 0.88")
    print(f"数据文件: {events_file}")
    
    # 创建专门的实验
    config = manager.create_experiment(
        experiment_name="btc_eth_5min_strategy_validation",
        description="使用BTC/ETH模拟数据验证2-3分钟、70-88价格范围策略",
        strategy_type="prediction_last_window_threshold",
        events_file=events_file,
        parameter_space=target_space,
        metadata={
            "focus": "2-3分钟、70-88价格范围",
            "data_source": "simulated_btc_eth_5min",
            "notes": "验证胜率和盈利情况"
        },
    )
    
    print(f"\n创建实验: {config.experiment_name}")
    print(f"实验ID: {config.experiment_id}")
    print(f"配置文件: {config.get_experiment_dir()}")
    
    # 运行实验
    print(f"\n开始运行参数扫描...")
    
    def progress_callback(current, total, params):
        if current % 50 == 0 or current == total - 1:
            print(f"进度: {current+1}/{total} ({(current+1)/total*100:.1f}%)")
    
    result = manager.run_experiment(config.experiment_id, progress_callback)
    
    print(f"\n=== 策略验证结果 ===")
    print(f"总参数组合: {result.total_runs}")
    print(f"有成交: {result.successful_runs}")
    print(f"无成交: {result.failed_runs}")
    
    if result.summary_statistics:
        print(f"\n整体统计:")
        print(f"  平均胜率: {result.summary_statistics.get('avg_win_rate', 'N/A')}")
        print(f"  最大胜率: {result.summary_statistics.get('max_win_rate', 'N/A')}")
        print(f"  平均净PnL: {result.summary_statistics.get('avg_net_pnl_per_share', 'N/A')}")
        print(f"  最大净PnL: {result.summary_statistics.get('max_net_pnl_per_share', 'N/A')}")
        print(f"  总成交次数: {result.summary_statistics.get('total_fills', 'N/A')}")
    
    # 获取最佳表现者
    from arb_research.experiments import ParameterSweep
    sweep = ParameterSweep(config)
    sweep.result = result
    
    top_performers = sweep.get_top_performers(n=10)
    
    print(f"\n=== 最佳10个参数组合 ===")
    print(f"{'排名':<6} {'窗口(秒)':<10} {'阈值':<10} {'最大入场':<10} {'胜率':<10} {'净PnL':<12} {'成交次数':<8}")
    print("-" * 75)
    
    for i, run in enumerate(top_performers, 1):
        print(f"{i:<6} {run.parameters.get('window_seconds'):<10} "
              f"{str(run.parameters.get('threshold_price')):<10} "
              f"{str(run.parameters.get('max_entry_price')):<10} "
              f"{run.metrics.get('winRate', 'N/A'):<10} "
              f"{run.metrics.get('netPnlPerShare', 'N/A'):<12} "
              f"{run.fills_count:<8}")
    
    # 按时间窗口分析
    analysis = sweep.get_parameter_analysis()
    print(f"\n=== 时间窗口性能分析 ===")
    for key, value in analysis.items():
        if key.startswith("window_"):
            print(f"{key}: {value}")
    
    # 按阈值价格分析
    print(f"\n=== 阈值价格性能分析 ===")
    for key, value in analysis.items():
        if key.startswith("threshold_"):
            print(f"{key}: {value}")
    
    # 盈利分析
    profitable_runs = [r for r in result.runs if r.metrics and Decimal(r.metrics.get('netPnlPerShare', '0')) > 0]
    losing_runs = [r for r in result.runs if r.metrics and Decimal(r.metrics.get('netPnlPerShare', '0')) < 0]
    
    print(f"\n=== 盈利分析 ===")
    print(f"盈利参数组合: {len(profitable_runs)} ({len(profitable_runs)/result.total_runs*100:.1f}%)")
    print(f"亏损参数组合: {len(losing_runs)} ({len(losing_runs)/result.total_runs*100:.1f}%)")
    
    if profitable_runs:
        avg_profit = sum(Decimal(r.metrics.get('netPnlPerShare', '0')) for r in profitable_runs) / len(profitable_runs)
        print(f"平均盈利: {avg_profit}")
        print(f"最佳盈利: {max(Decimal(r.metrics.get('netPnlPerShare', '0')) for r in profitable_runs)}")
    
    if losing_runs:
        avg_loss = sum(Decimal(r.metrics.get('netPnlPerShare', '0')) for r in losing_runs) / len(losing_runs)
        print(f"平均亏损: {avg_loss}")
        print(f"最大亏损: {min(Decimal(r.metrics.get('netPnlPerShare', '0')) for r in losing_runs)}")

if __name__ == "__main__":
    main()
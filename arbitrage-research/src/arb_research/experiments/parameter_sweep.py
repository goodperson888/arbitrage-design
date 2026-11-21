from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any

from ..backtest import build_metrics, run_backtest
from ..io import read_events
from ..strategy import LastWindowThresholdConfig, LastWindowThresholdStrategy
from .experiment_config import ExperimentConfig
from .experiment_result import ExperimentResult, SingleRunResult


class ParameterSweep:
    """参数扫描器，用于系统性测试不同参数组合."""
    
    def __init__(self, experiment_config: ExperimentConfig):
        self.config = experiment_config
        self.result = ExperimentResult(
            experiment_id=experiment_config.experiment_id,
            experiment_name=experiment_config.experiment_name,
            total_runs=0,
            successful_runs=0,
            failed_runs=0,
            runs=[],
        )
    
    def run_sweep(self, progress_callback: Any | None = None) -> ExperimentResult:
        """运行完整的参数扫描."""
        print(f"Starting parameter sweep for experiment: {self.config.experiment_name}")
        print(f"Total parameter combinations: {self.config.parameter_space.total_combinations()}")
        
        # 生成所有参数组合
        parameter_combinations = self.config.parameter_space.generate_combinations()
        
        # 读取事件数据（只读一次）
        print(f"Loading events from: {self.config.events_file}")
        events = list(read_events(self.config.events_file))
        print(f"Loaded {len(events)} events")
        
        # 遍历所有参数组合
        for i, params in enumerate(parameter_combinations):
            run_id = str(uuid.uuid4())[:8]
            
            if progress_callback:
                progress_callback(i, len(parameter_combinations), params)
            
            print(f"\n[{i+1}/{len(parameter_combinations)}] Testing parameters: {params}")
            
            try:
                # 运行单次回测
                run_result = self._run_single_backtest(events, params, run_id)
                self.result.add_run(run_result)
                
                print(f"  Win rate: {run_result.metrics.get('winRate', 'N/A')}")
                print(f"  Net PnL per share: {run_result.metrics.get('netPnlPerShare', 'N/A')}")
                print(f"  Fills: {run_result.fills_count}")
                print(f"  Execution time: {run_result.execution_time_ms:.2f}ms")
                
                direction_metrics = run_result.metrics.get("byOutcome", {})
                for outcome in ("UP", "DOWN"):
                    metrics = direction_metrics.get(outcome, {})
                    print(
                        f"  {outcome} trades: {metrics.get('settledFillCount', 0)}, "
                        f"wins: {metrics.get('winCount', 0)}, "
                        f"win rate: {metrics.get('winRate', '0')}"
                    )

            except Exception as e:
                print(f"  Error in run {run_id}: {e}")
                # 添加失败的运行记录
                failed_run = SingleRunResult(
                    run_id=run_id,
                    parameters=params,
                    metrics={"error": str(e)},
                    fills_count=0,
                    execution_time_ms=0,
                )
                self.result.add_run(failed_run)
        
        print(f"\nParameter sweep completed!")
        print(f"Total runs: {self.result.total_runs}")
        print(f"Successful: {self.result.successful_runs}")
        print(f"Failed: {self.result.failed_runs}")
        
        return self.result
    
    def _run_single_backtest(
        self, 
        events: list, 
        params: dict[str, Any], 
        run_id: str
    ) -> SingleRunResult:
        """运行单次回测."""
        start_time = time.time()
        
        # 创建策略配置
        strategy_config = LastWindowThresholdConfig(
            strategy_id="prediction_last_window_threshold",
            version=1,
            window_seconds=params["window_seconds"],
            threshold_price=Decimal(str(params["threshold_price"])),
            max_entry_price=Decimal(str(params["max_entry_price"])),
            order_size=Decimal(str(params["order_size"])),
            min_ask_size=Decimal(str(params["min_ask_size"])),
            fee_model="polymarket_crypto_taker",
            fee_rate=Decimal(str(params["fee_rate"])),
            allowed_outcomes=("UP", "DOWN"),
            one_trade_per_market=True,
        )
        
        # 创建策略实例
        strategy = LastWindowThresholdStrategy(strategy_config)
        
        # 运行回测
        backtest_result = run_backtest(events, strategy)
        
        execution_time = (time.time() - start_time) * 1000  # 转换为毫秒
        
        metrics = dict(backtest_result.metrics)
        metrics["byOutcome"] = {
            outcome: build_metrics([
                fill for fill in backtest_result.fills if fill.outcome == outcome
            ])
            for outcome in ("UP", "DOWN")
        }

        return SingleRunResult(
            run_id=run_id,
            parameters=params,
            metrics=metrics,
            fills_count=len(backtest_result.fills),
            execution_time_ms=execution_time,
        )
    
    def save_results(self) -> None:
        """保存实验结果."""
        experiment_dir = self.config.get_experiment_dir()
        self.result.save(experiment_dir)
        print(f"Results saved to: {experiment_dir}")
    
    def get_top_performers(self, n: int = 10, metric: str = "winRate") -> list[SingleRunResult]:
        """获取表现最佳的参数组合."""
        successful_runs = [run for run in self.result.runs if not run.metrics.get("error")]
        sorted_runs = sorted(
            successful_runs,
            key=lambda r: Decimal(r.metrics.get(metric, "0")) if r.metrics else Decimal("0"),
            reverse=True,
        )
        return sorted_runs[:n]
    
    def get_parameter_analysis(self) -> dict[str, Any]:
        """分析参数性能."""
        if not self.result.runs:
            return {}
        
        analysis = {}
        
        # 按窗口时间分析
        window_analysis = {}
        for run in self.result.runs:
            window = run.parameters.get("window_seconds")
            if window not in window_analysis:
                window_analysis[window] = []
            if run.metrics:
                window_analysis[window].append({
                    "win_rate": Decimal(run.metrics.get("winRate", "0")),
                    "net_pnl": Decimal(run.metrics.get("netPnlPerShare", "0")),
                })
        
        for window, results in window_analysis.items():
            if results:
                avg_win_rate = sum(r["win_rate"] for r in results) / len(results)
                avg_pnl = sum(r["net_pnl"] for r in results) / len(results)
                analysis[f"window_{window}s"] = {
                    "avg_win_rate": str(avg_win_rate),
                    "avg_net_pnl": str(avg_pnl),
                    "count": len(results),
                }
        
        # 按阈值价格分析
        threshold_analysis = {}
        for run in self.result.runs:
            threshold = run.parameters.get("threshold_price")
            if threshold not in threshold_analysis:
                threshold_analysis[threshold] = []
            if run.metrics:
                threshold_analysis[threshold].append({
                    "win_rate": Decimal(run.metrics.get("winRate", "0")),
                    "net_pnl": Decimal(run.metrics.get("netPnlPerShare", "0")),
                })
        
        for threshold, results in threshold_analysis.items():
            if results:
                avg_win_rate = sum(r["win_rate"] for r in results) / len(results)
                avg_pnl = sum(r["net_pnl"] for r in results) / len(results)
                analysis[f"threshold_{threshold}"] = {
                    "avg_win_rate": str(avg_win_rate),
                    "avg_net_pnl": str(avg_pnl),
                    "count": len(results),
                }
        
        return analysis

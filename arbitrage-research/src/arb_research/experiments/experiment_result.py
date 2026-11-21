from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass
class SingleRunResult:
    """单次参数运行的结果."""
    
    run_id: str
    parameters: dict[str, Any]
    metrics: dict[str, Any]
    fills_count: int
    execution_time_ms: float
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> dict[str, Any]:
        """转换为字典."""
        # 转换Decimal为字符串
        parameters_serializable = {}
        for key, value in self.parameters.items():
            if isinstance(value, Decimal):
                parameters_serializable[key] = str(value)
            else:
                parameters_serializable[key] = value
        
        return {
            "run_id": self.run_id,
            "parameters": parameters_serializable,
            "metrics": self.metrics,
            "fills_count": self.fills_count,
            "execution_time_ms": self.execution_time_ms,
            "timestamp": self.timestamp,
        }


@dataclass
class ExperimentResult:
    """实验结果汇总."""
    
    experiment_id: str
    experiment_name: str
    total_runs: int
    successful_runs: int
    failed_runs: int
    runs: list[SingleRunResult]
    best_run: SingleRunResult | None = None
    worst_run: SingleRunResult | None = None
    summary_statistics: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None
    
    def add_run(self, run_result: SingleRunResult) -> None:
        """添加单次运行结果."""
        self.runs.append(run_result)
        self.total_runs += 1
        if run_result.metrics.get("error"):
            self.failed_runs += 1
        else:
            self.successful_runs += 1
    
    def calculate_summary(self) -> None:
        """计算汇总统计."""
        if not self.runs:
            return
        
        # 提取关键指标
        win_rates = []
        net_pnls = []
        fill_counts = []
        
        successful = [run for run in self.runs if not run.metrics.get("error")]
        for run in successful:
            if run.metrics:
                win_rates.append(Decimal(run.metrics.get("winRate", "0")))
                net_pnls.append(Decimal(run.metrics.get("netPnlPerShare", "0")))
                fill_counts.append(run.fills_count)
        
        if win_rates:
            avg_win_rate = sum(win_rates) / len(win_rates)
            max_win_rate = max(win_rates)
            min_win_rate = min(win_rates)
        else:
            avg_win_rate = max_win_rate = min_win_rate = Decimal("0")
        
        if net_pnls:
            avg_net_pnl = sum(net_pnls) / len(net_pnls)
            max_net_pnl = max(net_pnls)
            min_net_pnl = min(net_pnls)
        else:
            avg_net_pnl = max_net_pnl = min_net_pnl = Decimal("0")
        
        # 找出最佳和最差运行
        ranked_runs = successful or self.runs
        self.best_run = max(ranked_runs, key=lambda r: Decimal(r.metrics.get("winRate", "0")) if r.metrics else Decimal("0"))
        self.worst_run = min(ranked_runs, key=lambda r: Decimal(r.metrics.get("winRate", "0")) if r.metrics else Decimal("0"))
        
        self.summary_statistics = {
            "total_runs": self.total_runs,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "success_rate": str(self.successful_runs / self.total_runs if self.total_runs > 0 else Decimal("0")),
            "avg_win_rate": str(avg_win_rate),
            "max_win_rate": str(max_win_rate),
            "min_win_rate": str(min_win_rate),
            "avg_net_pnl_per_share": str(avg_net_pnl),
            "max_net_pnl_per_share": str(max_net_pnl),
            "min_net_pnl_per_share": str(min_net_pnl),
            "avg_fills_count": sum(fill_counts) / len(fill_counts) if fill_counts else 0,
            "total_fills": sum(fill_counts),
            "no_fill_runs": sum(run.fills_count == 0 for run in successful),
        }
    
    def save(self, output_dir: Path) -> Path:
        """保存实验结果到文件."""
        output_dir.mkdir(parents=True, exist_ok=True)
        
        self.completed_at = datetime.now(timezone.utc).isoformat()
        self.calculate_summary()
        
        result_file = output_dir / "experiment_result.json"
        
        result_dict = {
            "experiment_id": self.experiment_id,
            "experiment_name": self.experiment_name,
            "total_runs": self.total_runs,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "summary_statistics": self.summary_statistics,
            "best_run": self.best_run.to_dict() if self.best_run else None,
            "worst_run": self.worst_run.to_dict() if self.worst_run else None,
            "runs": [run.to_dict() for run in self.runs],
        }
        
        with result_file.open("w", encoding="utf-8") as f:
            json.dump(result_dict, f, indent=2, ensure_ascii=False)
        
        # 同时保存简化的CSV用于分析
        self.save_csv_summary(output_dir)
        
        return result_file
    
    def save_csv_summary(self, output_dir: Path) -> Path:
        """保存CSV格式的结果摘要，便于分析."""
        import csv
        
        csv_file = output_dir / "results_summary.csv"
        
        with csv_file.open("w", newline="", encoding="utf-8") as f:
            if not self.runs:
                return csv_file
            
            # 写入表头
            fieldnames = ["run_id", "window_seconds", "threshold_price", "max_entry_price", 
                         "order_size", "min_ask_size", "win_rate", "net_pnl_per_share", 
                         "fills_count", "execution_time_ms"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
            # 写入每行数据
            for run in self.runs:
                row = {
                    "run_id": run.run_id,
                    "window_seconds": run.parameters.get("window_seconds", ""),
                    "threshold_price": str(run.parameters.get("threshold_price", "")),
                    "max_entry_price": str(run.parameters.get("max_entry_price", "")),
                    "order_size": str(run.parameters.get("order_size", "")),
                    "min_ask_size": str(run.parameters.get("min_ask_size", "")),
                    "win_rate": run.metrics.get("winRate", ""),
                    "net_pnl_per_share": run.metrics.get("netPnlPerShare", ""),
                    "fills_count": run.fills_count,
                    "execution_time_ms": f"{run.execution_time_ms:.2f}",
                }
                writer.writerow(row)
        
        return csv_file
    
    @classmethod
    def load(cls, result_file: Path) -> "ExperimentResult":
        """从文件加载实验结果."""
        with result_file.open("r", encoding="utf-8") as f:
            result_dict = json.load(f)
        
        runs = []
        for run_dict in result_dict.get("runs", []):
            runs.append(SingleRunResult(
                run_id=run_dict["run_id"],
                parameters=run_dict["parameters"],
                metrics=run_dict["metrics"],
                fills_count=run_dict["fills_count"],
                execution_time_ms=run_dict["execution_time_ms"],
                timestamp=run_dict.get("timestamp", ""),
            ))
        
        return cls(
            experiment_id=result_dict["experiment_id"],
            experiment_name=result_dict["experiment_name"],
            total_runs=result_dict["total_runs"],
            successful_runs=result_dict["successful_runs"],
            failed_runs=result_dict["failed_runs"],
            runs=runs,
            summary_statistics=result_dict.get("summary_statistics", {}),
            started_at=result_dict["started_at"],
            completed_at=result_dict.get("completed_at"),
        )

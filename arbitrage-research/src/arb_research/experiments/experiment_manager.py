from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .experiment_config import ExperimentConfig
from .experiment_result import ExperimentResult


class ExperimentManager:
    """实验管理器，负责管理所有实验的生命周期."""
    
    def __init__(self, base_experiments_dir: Path):
        self.base_dir = Path(base_experiments_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        
        # 创建子目录结构
        self.configs_dir = self.base_dir / "configs"
        self.results_dir = self.base_dir / "results"
        self.archive_dir = self.base_dir / "archive"
        
        for dir_path in [self.configs_dir, self.results_dir, self.archive_dir]:
            dir_path.mkdir(exist_ok=True)
        
        # 实验索引
        self.index_file = self.base_dir / "experiment_index.json"
        self._load_index()
    
    def _load_index(self) -> None:
        """加载实验索引."""
        if self.index_file.exists():
            with self.index_file.open("r", encoding="utf-8") as f:
                self.index = json.load(f)
        else:
            self.index = {
                "experiments": [],
                "total_count": 0,
                "last_updated": None,
            }
    
    def _save_index(self) -> None:
        """保存实验索引."""
        from datetime import datetime, timezone
        self.index["last_updated"] = datetime.now(timezone.utc).isoformat()
        with self.index_file.open("w", encoding="utf-8") as f:
            json.dump(self.index, f, indent=2, ensure_ascii=False)
    
    def create_experiment(
        self,
        experiment_name: str,
        description: str,
        strategy_type: str,
        events_file: Path,
        parameter_space: Any = None,
        metadata: dict[str, Any] | None = None,
        use_test_params: bool = False,
    ) -> ExperimentConfig:
        """创建新实验."""
        # 检查实验名称是否已存在
        if self._experiment_name_exists(experiment_name):
            raise ValueError(f"Experiment name '{experiment_name}' already exists")
        
        # 创建实验配置
        config = ExperimentConfig.create(
            experiment_name=experiment_name,
            description=description,
            strategy_type=strategy_type,
            events_file=events_file,
            base_output_dir=self.results_dir,
            parameter_space=parameter_space,
            metadata=metadata,
            use_test_params=use_test_params,
        )
        
        # 保存配置
        config.save()
        
        # 更新索引
        self._add_to_index(config)
        
        return config
    
    def _experiment_name_exists(self, experiment_name: str) -> bool:
        """检查实验名称是否已存在."""
        return any(
            exp["experiment_name"] == experiment_name 
            for exp in self.index["experiments"]
        )
    
    def _add_to_index(self, config: ExperimentConfig) -> None:
        """添加实验到索引."""
        experiment_entry = {
            "experiment_id": config.experiment_id,
            "experiment_name": config.experiment_name,
            "description": config.description,
            "strategy_type": config.strategy_type,
            "data_source": config.data_source,
            "created_at": config.created_at,
            "config_hash": config.get_config_hash(),
            "status": "created",
            "result_file": None,
        }
        
        self.index["experiments"].append(experiment_entry)
        self.index["total_count"] += 1
        self._save_index()
    
    def run_experiment(
        self, 
        experiment_id: str, 
        progress_callback: Any | None = None
    ) -> ExperimentResult:
        """运行指定实验."""
        # 查找实验配置
        config = self.get_experiment_config(experiment_id)
        if config is None:
            raise ValueError(f"Experiment not found: {experiment_id}")
        
        # 延迟导入避免循环依赖
        from .parameter_sweep import ParameterSweep
        
        # 创建参数扫描器
        sweep = ParameterSweep(config)
        
        # 运行扫描
        result = sweep.run_sweep(progress_callback)
        
        # 保存结果
        sweep.save_results()
        
        # 更新索引
        self._update_experiment_status(experiment_id, "completed", str(config.get_experiment_dir() / "experiment_result.json"))
        
        return result
    
    def get_experiment_config(self, experiment_id: str) -> ExperimentConfig | None:
        """获取实验配置."""
        # 在索引中查找
        experiment_entry = next(
            (exp for exp in self.index["experiments"] if exp["experiment_id"] == experiment_id),
            None
        )
        
        if experiment_entry is None:
            return None
        
        # 加载配置文件
        experiment_dir = self.results_dir / f"experiment_{experiment_id[:8]}"
        config_file = experiment_dir / "experiment_config.json"
        
        if not config_file.exists():
            return None
        
        return ExperimentConfig.load(config_file)
    
    def get_experiment_result(self, experiment_id: str) -> ExperimentResult | None:
        """获取实验结果."""
        experiment_entry = next(
            (exp for exp in self.index["experiments"] if exp["experiment_id"] == experiment_id),
            None
        )
        
        if experiment_entry is None or experiment_entry["result_file"] is None:
            return None
        
        result_file = Path(experiment_entry["result_file"])
        if not result_file.exists():
            return None
        
        return ExperimentResult.load(result_file)
    
    def list_experiments(self, status: str | None = None) -> list[dict[str, Any]]:
        """列出所有实验."""
        experiments = self.index["experiments"]
        
        if status:
            experiments = [exp for exp in experiments if exp.get("status") == status]
        
        return experiments
    
    def _update_experiment_status(self, experiment_id: str, status: str, result_file: str | None = None) -> None:
        """更新实验状态."""
        for exp in self.index["experiments"]:
            if exp["experiment_id"] == experiment_id:
                exp["status"] = status
                if result_file:
                    exp["result_file"] = result_file
                break
        self._save_index()
    
    def archive_experiment(self, experiment_id: str) -> None:
        """归档实验."""
        config = self.get_experiment_config(experiment_id)
        if config is None:
            raise ValueError(f"Experiment not found: {experiment_id}")
        
        experiment_dir = config.get_experiment_dir()
        archive_dir = self.archive_dir / f"experiment_{experiment_id[:8]}"
        
        # 移动实验目录到归档
        if experiment_dir.exists():
            import shutil
            shutil.move(str(experiment_dir), str(archive_dir))
        
        # 更新索引
        self._update_experiment_status(experiment_id, "archived")
    
    def delete_experiment(self, experiment_id: str) -> None:
        """删除实验."""
        config = self.get_experiment_config(experiment_id)
        if config is None:
            raise ValueError(f"Experiment not found: {experiment_id}")
        
        experiment_dir = config.get_experiment_dir()
        
        # 删除实验目录
        if experiment_dir.exists():
            import shutil
            shutil.rmtree(str(experiment_dir))
        
        # 从索引中移除
        self.index["experiments"] = [
            exp for exp in self.index["experiments"] 
            if exp["experiment_id"] != experiment_id
        ]
        self.index["total_count"] = len(self.index["experiments"])
        self._save_index()
    
    def compare_experiments(self, experiment_ids: list[str]) -> dict[str, Any]:
        """比较多个实验的结果."""
        results = {}
        for exp_id in experiment_ids:
            result = self.get_experiment_result(exp_id)
            if result:
                results[exp_id] = result.summary_statistics
        
        return results
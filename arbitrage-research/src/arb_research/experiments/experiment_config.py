from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ParameterSpace:
    """定义参数搜索空间."""
    
    window_seconds: list[int]
    threshold_price: list[Decimal]
    max_entry_price: list[Decimal]
    order_size: list[Decimal]
    min_ask_size: list[Decimal]
    fee_rate: list[Decimal]
    
    @classmethod
    def create_default(cls) -> "ParameterSpace":
        """创建默认的参数空间."""
        return cls(
            window_seconds=[120, 180],  # 2分钟, 3分钟
            threshold_price=[Decimal(str(x/100)) for x in range(70, 89)],  # 0.70-0.88
            max_entry_price=[Decimal("0.82"), Decimal("0.85"), Decimal("0.88")],
            order_size=[Decimal("1"), Decimal("5"), Decimal("10")],
            min_ask_size=[Decimal("1"), Decimal("5"), Decimal("10")],
            fee_rate=[Decimal("0.07")],  # Polymarket 7% fee
        )
    
    @classmethod
    def create_test(cls) -> "ParameterSpace":
        """创建测试用的简化参数空间."""
        return cls(
            window_seconds=[120],  # 仅2分钟
            threshold_price=[Decimal("0.70"), Decimal("0.80"), Decimal("0.85")],  # 仅3个阈值
            max_entry_price=[Decimal("0.82")],  # 仅一个最大入场价
            order_size=[Decimal("1")],  # 仅一个订单大小
            min_ask_size=[Decimal("1")],  # 仅一个最小流动性
            fee_rate=[Decimal("0.07")],  # Polymarket 7% fee
        )
    
    def total_combinations(self) -> int:
        """计算总参数组合数."""
        return (
            len(self.window_seconds) *
            len(self.threshold_price) *
            len(self.max_entry_price) *
            len(self.order_size) *
            len(self.min_ask_size) *
            len(self.fee_rate)
        )
    
    def generate_combinations(self) -> list[dict[str, Any]]:
        """生成所有参数组合."""
        from itertools import product
        
        combinations = []
        for combo in product(
            self.window_seconds,
            self.threshold_price,
            self.max_entry_price,
            self.order_size,
            self.min_ask_size,
            self.fee_rate,
        ):
            combinations.append({
                "window_seconds": combo[0],
                "threshold_price": combo[1],
                "max_entry_price": combo[2],
                "order_size": combo[3],
                "min_ask_size": combo[4],
                "fee_rate": combo[5],
            })
        
        return combinations


@dataclass
class ExperimentConfig:
    """实验配置."""
    
    experiment_id: str
    experiment_name: str
    description: str
    strategy_type: str
    parameter_space: ParameterSpace
    data_source: str
    events_file: Path
    base_output_dir: Path
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    def create(
        cls,
        experiment_name: str,
        description: str,
        strategy_type: str,
        events_file: Path,
        base_output_dir: Path,
        parameter_space: ParameterSpace | None = None,
        metadata: dict[str, Any] | None = None,
        use_test_params: bool = False,
    ) -> "ExperimentConfig":
        """创建新的实验配置."""
        if parameter_space is None:
            if use_test_params:
                parameter_space = ParameterSpace.create_test()
            else:
                parameter_space = ParameterSpace.create_default()
        
        # 生成唯一的实验ID
        experiment_id = str(uuid.uuid4())
        
        return cls(
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            description=description,
            strategy_type=strategy_type,
            parameter_space=parameter_space,
            data_source=str(events_file),
            events_file=events_file,
            base_output_dir=base_output_dir,
            metadata=metadata or {},
        )
    
    def get_experiment_dir(self) -> Path:
        """获取实验专用目录."""
        return self.base_output_dir / f"experiment_{self.experiment_id[:8]}"
    
    def get_config_hash(self) -> str:
        """生成配置哈希值，用于配置变更检测."""
        config_dict = {
            "strategy_type": self.strategy_type,
            "parameter_space": {
                "window_seconds": self.parameter_space.window_seconds,
                "threshold_price": [str(p) for p in self.parameter_space.threshold_price],
                "max_entry_price": [str(p) for p in self.parameter_space.max_entry_price],
                "order_size": [str(p) for p in self.parameter_space.order_size],
                "min_ask_size": [str(p) for p in self.parameter_space.min_ask_size],
                "fee_rate": [str(p) for p in self.parameter_space.fee_rate],
            },
            "data_source": self.data_source,
        }
        config_str = json.dumps(config_dict, sort_keys=True)
        return hashlib.md5(config_str.encode()).hexdigest()
    
    def save(self) -> Path:
        """保存实验配置到文件."""
        experiment_dir = self.get_experiment_dir()
        experiment_dir.mkdir(parents=True, exist_ok=True)
        
        config_file = experiment_dir / "experiment_config.json"
        
        config_dict = {
            "experiment_id": self.experiment_id,
            "experiment_name": self.experiment_name,
            "description": self.description,
            "strategy_type": self.strategy_type,
            "parameter_space": {
                "window_seconds": self.parameter_space.window_seconds,
                "threshold_price": [str(p) for p in self.parameter_space.threshold_price],
                "max_entry_price": [str(p) for p in self.parameter_space.max_entry_price],
                "order_size": [str(p) for p in self.parameter_space.order_size],
                "min_ask_size": [str(p) for p in self.parameter_space.min_ask_size],
                "fee_rate": [str(p) for p in self.parameter_space.fee_rate],
            },
            "data_source": self.data_source,
            "events_file": str(self.events_file),
            "base_output_dir": str(self.base_output_dir),
            "created_at": self.created_at,
            "metadata": self.metadata,
            "config_hash": self.get_config_hash(),
        }
        
        with config_file.open("w", encoding="utf-8") as f:
            json.dump(config_dict, f, indent=2, ensure_ascii=False)
        
        return config_file
    
    @classmethod
    def load(cls, config_file: Path) -> "ExperimentConfig":
        """从文件加载实验配置."""
        with config_file.open("r", encoding="utf-8") as f:
            config_dict = json.load(f)
        
        # 重建 ParameterSpace
        parameter_space = ParameterSpace(
            window_seconds=config_dict["parameter_space"]["window_seconds"],
            threshold_price=[Decimal(p) for p in config_dict["parameter_space"]["threshold_price"]],
            max_entry_price=[Decimal(p) for p in config_dict["parameter_space"]["max_entry_price"]],
            order_size=[Decimal(p) for p in config_dict["parameter_space"]["order_size"]],
            min_ask_size=[Decimal(p) for p in config_dict["parameter_space"]["min_ask_size"]],
            fee_rate=[Decimal(p) for p in config_dict["parameter_space"]["fee_rate"]],
        )
        
        return cls(
            experiment_id=config_dict["experiment_id"],
            experiment_name=config_dict["experiment_name"],
            description=config_dict["description"],
            strategy_type=config_dict["strategy_type"],
            parameter_space=parameter_space,
            data_source=config_dict["data_source"],
            events_file=Path(config_dict["events_file"]),
            base_output_dir=Path(config_dict["base_output_dir"]),
            created_at=config_dict["created_at"],
            metadata=config_dict.get("metadata", {}),
        )
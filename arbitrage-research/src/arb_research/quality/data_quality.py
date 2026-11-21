from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ..models import MarketEvent


class QualityLevel(Enum):
    """Data quality levels for market event data."""
    
    L1 = "K线+结算"  # Candlestick + Settlement - rough direction probability
    L2 = "历史成交+结算"  # Historical trades + Settlement - price reach probability
    L3 = "买一卖一快照+结算"  # Best bid/ask snapshot + Settlement - near-real entry price
    L4 = "完整订单簿快照与增量"  # Full orderbook snapshot + delta - depth and slippage simulation
    L5 = "L4+本地接收时间+订单反馈"  # L4 + local receive time + order feedback - latency and execution simulation


@dataclass
class QualityAssessment:
    """Assessment of data quality for a dataset."""
    
    level: QualityLevel
    capabilities: list[str]
    limitations: list[str]
    can_answer: list[str]
    cannot_answer: list[str]
    score: float  # 0.0 to 1.0


class DataQualityGrader:
    """Grade data quality based on available event types and data characteristics."""
    
    # Quality level requirements
    LEVEL_REQUIREMENTS = {
        QualityLevel.L1: {
            "required_events": ["MARKET_DEFINITION", "SETTLEMENT"],
            "optional_events": [],
            "min_fields": ["underlying", "eventStartUs", "eventEndUs", "winnerOutcome"],
        },
        QualityLevel.L2: {
            "required_events": ["MARKET_DEFINITION", "SETTLEMENT", "TRADE"],
            "optional_events": [],
            "min_fields": ["underlying", "eventStartUs", "eventEndUs", "winnerOutcome", "price", "size"],
        },
        QualityLevel.L3: {
            "required_events": ["MARKET_DEFINITION", "SETTLEMENT", "PRICE_QUOTE"],
            "optional_events": ["ORDERBOOK_SNAPSHOT"],
            "min_fields": ["underlying", "eventStartUs", "eventEndUs", "winnerOutcome", "bestBid", "bestAsk"],
        },
        QualityLevel.L4: {
            "required_events": ["MARKET_DEFINITION", "SETTLEMENT", "ORDERBOOK_SNAPSHOT"],
            "optional_events": ["ORDERBOOK_DELTA", "PRICE_QUOTE"],
            "min_fields": ["underlying", "eventStartUs", "eventEndUs", "winnerOutcome", "bids", "asks"],
        },
        QualityLevel.L5: {
            "required_events": ["MARKET_DEFINITION", "SETTLEMENT", "ORDERBOOK_SNAPSHOT", "ORDERBOOK_DELTA"],
            "optional_events": ["PRICE_QUOTE", "TRADE"],
            "min_fields": ["underlying", "eventStartUs", "eventEndUs", "winnerOutcome", "bids", "asks", "receivedTimeUs"],
        },
    }
    
    # Quality level descriptions
    LEVEL_DESCRIPTIONS = {
        QualityLevel.L1: {
            "capabilities": [
                "粗略方向概率分析",
                "基本市场统计",
                "结算结果验证",
            ],
            "limitations": [
                "无法模拟真实入场价格",
                "无法计算滑点",
                "无法评估深度",
            ],
            "can_answer": [
                "最后两分钟价格达到阈值后的最终胜率",
                "市场基本统计信息",
            ],
            "cannot_answer": [
                "当时是否能够用该价格买到该数量",
                "实际成交的滑点成本",
                "订单簿深度影响",
            ],
        },
        QualityLevel.L2: {
            "capabilities": [
                "历史成交价格分析",
                "交易量统计",
                "价格达到时间分析",
            ],
            "limitations": [
                "没有实时盘口信息",
                "无法评估买卖价差",
                "无法模拟订单簿影响",
            ],
            "can_answer": [
                "历史成交价格分布",
                "交易活跃度分析",
                "价格变动模式",
            ],
            "cannot_answer": [
                "实时可成交价格",
                "订单簿深度",
                "滑点成本",
            ],
        },
        QualityLevel.L3: {
            "capabilities": [
                "接近真实的入场价格模拟",
                "买卖价差分析",
                "基本流动性评估",
            ],
            "limitations": [
                "订单簿深度有限",
                "无法精确模拟大单影响",
                "无法评估多档位深度",
            ],
            "can_answer": [
                "最佳买卖价的历史分布",
                "买卖价差统计",
                "基本流动性指标",
            ],
            "cannot_answer": [
                "大单对盘口的影响",
                "多档位深度分析",
                "精确的滑点模拟",
            ],
        },
        QualityLevel.L4: {
            "capabilities": [
                "完整订单簿深度分析",
                "滑点成本模拟",
                "大单影响评估",
            ],
            "limitations": [
                "没有网络延迟信息",
                "无法模拟排队效应",
                "无法评估执行延迟",
            ],
            "can_answer": [
                "完整订单簿状态",
                "不同数量的滑点成本",
                "市场深度分析",
            ],
            "cannot_answer": [
                "网络延迟影响",
                "订单排队时间",
                "真实执行延迟",
            ],
        },
        QualityLevel.L5: {
            "capabilities": [
                "完整回测模拟",
                "延迟分析",
                "排队效应模拟",
                "真实执行成本评估",
            ],
            "limitations": [
                "需要高质量数据采集",
                "计算复杂度高",
                "需要大量存储空间",
            ],
            "can_answer": [
                "考虑延迟的真实收益",
                "排队位置对成交的影响",
                "网络条件对策略的影响",
            ],
            "cannot_answer": [
                "未来未知的市场变化",
                "极端市场条件下的表现",
            ],
        },
    }
    
    def __init__(self):
        self.event_types_seen = set()
        self.fields_seen = set()
        self.event_count = 0
        self.market_count = 0
        self.time_range = None
    
    def analyze_events(self, events: list[MarketEvent]) -> QualityAssessment:
        """Analyze a list of events and determine data quality level."""
        self._reset_state()
        
        for event in events:
            self._process_event(event)
        
        # Determine quality level
        quality_level = self._determine_quality_level()
        
        # Get level descriptions
        descriptions = self.LEVEL_DESCRIPTIONS[quality_level]
        
        # Calculate quality score
        score = self._calculate_quality_score(quality_level)
        
        return QualityAssessment(
            level=quality_level,
            capabilities=descriptions["capabilities"],
            limitations=descriptions["limitations"],
            can_answer=descriptions["can_answer"],
            cannot_answer=descriptions["cannot_answer"],
            score=score,
        )
    
    def analyze_file(self, file_path: Path) -> QualityAssessment:
        """Analyze events from a file and determine data quality level."""
        from ..io import read_events
        
        events = list(read_events(file_path))
        return self.analyze_events(events)
    
    def _reset_state(self) -> None:
        """Reset analysis state."""
        self.event_types_seen = set()
        self.fields_seen = set()
        self.event_count = 0
        self.market_count = 0
        self.market_ids_seen = set()
        self.time_range = None
    
    def _process_event(self, event: MarketEvent) -> None:
        """Process a single event and update analysis state."""
        self.event_types_seen.add(event.event_type)
        self.event_count += 1
        
        # Track unique markets
        if event.market_id:
            self.market_ids_seen.add(event.market_id)
            self.market_count = len(self.market_ids_seen)
        
        # Track fields in payload
        if event.payload:
            self.fields_seen.update(event.payload.keys())
        
        # Track time range
        if self.time_range is None:
            self.time_range = [event.source_time_us, event.source_time_us]
        else:
            self.time_range[0] = min(self.time_range[0], event.source_time_us)
            self.time_range[1] = max(self.time_range[1], event.source_time_us)
    
    def _determine_quality_level(self) -> QualityLevel:
        """Determine the highest quality level that the data meets."""
        # Check from highest to lowest
        for level in reversed(list(QualityLevel)):
            if self._meets_level_requirements(level):
                return level
        
        return QualityLevel.L1  # Default to lowest level
    
    def _meets_level_requirements(self, level: QualityLevel) -> bool:
        """Check if the data meets the requirements for a given quality level."""
        requirements = self.LEVEL_REQUIREMENTS[level]
        
        # Check required event types
        for required_event in requirements["required_events"]:
            if required_event not in self.event_types_seen:
                return False
        
        # Check required fields
        for required_field in requirements["min_fields"]:
            if required_field not in self.fields_seen:
                return False
        
        return True
    
    def _calculate_quality_score(self, level: QualityLevel) -> float:
        """Calculate a quality score (0.0 to 1.0) based on data completeness."""
        base_score = {
            QualityLevel.L1: 0.2,
            QualityLevel.L2: 0.4,
            QualityLevel.L3: 0.6,
            QualityLevel.L4: 0.8,
            QualityLevel.L5: 1.0,
        }.get(level, 0.0)
        
        # Adjust based on data completeness
        if self.event_count == 0:
            return 0.0
        
        # Bonus for having optional events
        requirements = self.LEVEL_REQUIREMENTS[level]
        optional_count = len(requirements["optional_events"])
        if optional_count > 0:
            optional_found = sum(1 for event in requirements["optional_events"] if event in self.event_types_seen)
            bonus = (optional_found / optional_count) * 0.1
            base_score = min(1.0, base_score + bonus)
        
        return base_score
    
    def get_quality_report(self) -> dict[str, Any]:
        """Get a detailed quality report."""
        return {
            "event_types_seen": sorted(self.event_types_seen),
            "fields_seen": sorted(self.fields_seen),
            "event_count": self.event_count,
            "market_count": self.market_count,
            "time_range_us": self.time_range,
            "quality_level": self._determine_quality_level().value,
        }

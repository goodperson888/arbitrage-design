from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import unittest

from arb_research.experiments.experiment_config import ExperimentConfig, ParameterSpace
from arb_research.experiments.parameter_sweep import ParameterSweep


ROOT = Path(__file__).resolve().parents[1]


class ParameterSweepTest(unittest.TestCase):
    def test_single_combination_is_recorded_once_with_direction_metrics(self) -> None:
        config = ExperimentConfig(
            experiment_id="test-experiment",
            experiment_name="test",
            description="test",
            strategy_type="prediction_last_window_threshold",
            parameter_space=ParameterSpace(
                window_seconds=[120],
                threshold_price=[Decimal("0.80")],
                max_entry_price=[Decimal("0.82")],
                order_size=[Decimal("1")],
                min_ask_size=[Decimal("1")],
                fee_rate=[Decimal("0")],
            ),
            data_source="test",
            events_file=ROOT / "examples" / "prediction_events.ndjson",
            base_output_dir=ROOT / "runs",
        )

        result = ParameterSweep(config).run_sweep()

        self.assertEqual(result.total_runs, 1)
        self.assertEqual(len(result.runs), 1)
        self.assertNotIn("error", result.runs[0].metrics)
        self.assertIn("byOutcome", result.runs[0].metrics)
        self.assertEqual(result.runs[0].metrics["byOutcome"]["UP"]["settledFillCount"], 1)
        self.assertEqual(result.runs[0].metrics["byOutcome"]["DOWN"]["settledFillCount"], 1)


if __name__ == "__main__":
    unittest.main()

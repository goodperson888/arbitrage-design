import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analysis/analyze_binance_distance_probability.py"
SPEC = importlib.util.spec_from_file_location("binance_distance_probability", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class BinanceDistanceProbabilityTest(unittest.TestCase):
    def test_distance_bins_are_lower_inclusive(self):
        self.assertEqual(MODULE.distance_bin(-50), 1)
        self.assertEqual(MODULE.distance_bin(0), 5)
        self.assertEqual(MODULE.distance_bin(5), 6)

    def test_pava_is_monotonic(self):
        fitted = MODULE.pava([0.1, 0.8, 0.4, 0.9], [10, 10, 10, 10])
        self.assertEqual(len(fitted), 4)
        self.assertTrue(all(left <= right for left, right in zip(fitted, fitted[1:])))

    def test_higher_distance_never_reduces_probability(self):
        tables = {60: [0.05, 0.10, 0.20, 0.30, 0.40, 0.60, 0.70, 0.80, 0.90, 0.95]}
        probabilities = [MODULE.predict_probability(value, 60, tables)[0] for value in (-30, -12, -7, -2, 2, 7, 15, 30)]
        self.assertTrue(all(left <= right for left, right in zip(probabilities, probabilities[1:])))

    def test_nearest_remaining_time_table_is_used(self):
        tables = {30: [0.2] * 10, 60: [0.4] * 10, 90: [0.6] * 10}
        probability, target = MODULE.predict_probability(1, 83, tables)
        self.assertEqual(target, 90)
        self.assertEqual(probability, 0.6)

    def test_entry_cost_includes_crypto_taker_fee(self):
        self.assertAlmostEqual(MODULE.entry_cost_per_share(0.80), 0.8112)
        self.assertGreater(MODULE.entry_cost_per_share(0.85), 0.85)


if __name__ == "__main__":
    unittest.main()

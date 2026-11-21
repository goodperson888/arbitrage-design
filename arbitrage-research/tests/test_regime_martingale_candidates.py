import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analysis'))
import analyze_regime_martingale_candidates as m


def ohlc(highs, lows, closes, interval=None):
    interval = interval or 5 * m.base.MINUTE
    return [{'open_us': i * interval, 'end_us': (i + 1) * interval,
             'open': close, 'high': high, 'low': low, 'close': close}
            for i, (high, low, close) in enumerate(zip(highs, lows, closes))]


def policies(adx_times, adx_values):
    times = [20 * m.base.MINUTE, 21 * m.base.MINUTE]
    rows = [{'barEndUs': times[0], 'close': 100., 'rsi': 81., 'M2': None, 'M3': None},
            {'barEndUs': times[1], 'close': 99., 'rsi': 69., 'M2': 'DOWN', 'M3': 'DOWN'}]
    adx = (adx_times, [{'barEndUs': t, 'adx': a} for t, a in zip(adx_times, adx_values)])
    return m.add_regime_signals((times, rows), adx)[1]


class ADXTests(unittest.TestCase):
    def test_wilder_seed_and_update_by_hand_period_three(self):
        bars = ohlc([11, 12, 13, 12, 11, 12, 13], [9, 10, 11, 10, 9, 10, 11],
                    [10, 11, 12, 11, 10, 11, 12])
        _, values = m.build_adx(bars, 3)
        self.assertTrue(all(row['adx'] is None for row in values[:5]))
        self.assertAlmostEqual(values[3]['dx'], 100 / 3)
        self.assertAlmostEqual(values[4]['dx'], 100 / 9)
        self.assertAlmostEqual(values[5]['adx'], 1900 / 81)
        self.assertAlmostEqual(values[6]['adx'], 7900 / 243)

    def test_one_sided_trend_has_adx_100_after_seed(self):
        bars = ohlc(list(range(11, 51)), list(range(9, 49)), list(range(10, 50)))
        _, values = m.build_adx(bars)
        self.assertIsNone(values[26]['adx'])
        self.assertAlmostEqual(values[27]['adx'], 100.)
        self.assertTrue(all(row['minusDI'] == 0 for row in values[14:]))

    def test_equal_directional_moves_are_both_zero(self):
        _, values = m.build_adx(ohlc([11, 12], [9, 8], [10, 10]), 1)
        self.assertEqual(values[-1]['plusDI'], 0.)
        self.assertEqual(values[-1]['minusDI'], 0.)
        self.assertEqual(values[-1]['adx'], 0.)

    def test_flat_series_does_not_divide_by_zero(self):
        _, values = m.build_adx(ohlc([10] * 40, [10] * 40, [10] * 40))
        self.assertEqual(values[-1]['adx'], 0.)

    def test_gap_clears_smoothing_history(self):
        bars = ohlc(list(range(11, 51)), list(range(9, 49)), list(range(10, 50)))
        bars[-1]['open_us'] += 5 * m.base.MINUTE
        bars[-1]['end_us'] += 5 * m.base.MINUTE
        _, values = m.build_adx(bars)
        self.assertIsNone(values[-1]['adx'])
        self.assertIsNone(values[-1]['plusDI'])

    def test_aggregation_requires_aligned_five_minutes(self):
        bars = ohlc([11] * 10, [9] * 10, [10] * 10, m.base.MINUTE)
        self.assertEqual(len(m.aggregate_fives(bars)), 2)
        self.assertEqual(len(m.aggregate_fives(bars[:4])), 0)
        self.assertEqual(len(m.aggregate_fives(bars[1:5])), 0)
        self.assertEqual(len(m.aggregate_fives(bars[:2] + bars[3:])), 1)

    def test_aggregation_uses_full_ohlc(self):
        bars = ohlc([11, 13, 15, 14, 12], [9, 8, 7, 8, 9], [10, 11, 12, 11, 10], m.base.MINUTE)
        result = m.aggregate_fives(bars)[0]
        self.assertEqual((result['high'], result['low'], result['close']), (15, 7, 10))
        self.assertEqual(result['end_us'], 5 * m.base.MINUTE)


class SignalTests(unittest.TestCase):
    def test_range_and_trend_boundaries(self):
        at = 20 * m.base.MINUTE
        row = policies([at], [19.99])[-1]
        self.assertEqual(row['M9'], 'DOWN')
        self.assertEqual(row['M9S'], 'DOWN')
        self.assertIsNone(row['M10'])
        row = policies([at], [20])[-1]
        self.assertIsNone(row['M9'])
        self.assertIsNone(row['M10'])
        row = policies([at], [25])[-1]
        self.assertIsNone(row['M9'])
        self.assertEqual(row['M10'], 'DOWN')

    def test_future_adx_not_available(self):
        row = policies([22 * m.base.MINUTE], [10])[-1]
        self.assertIsNone(row['adx'])
        self.assertIsNone(row['M9'])
        self.assertEqual(row['M2'], 'DOWN')

    def test_old_adx_expires_after_gap(self):
        row = policies([15 * m.base.MINUTE], [10])[-1]
        self.assertIsNone(row['adx'])
        self.assertIsNone(row['M9'])

    def test_closed_minute_only_and_signal_expires(self):
        rows = policies([20 * m.base.MINUTE], [10])
        times = [r['barEndUs'] for r in rows]
        self.assertIsNone(m.base.indicator_at((times, rows), times[1] - 1)['M9'])
        self.assertEqual(m.base.indicator_at((times, rows), times[1])['M9'], 'DOWN')
        self.assertIsNone(m.base.indicator_at((times, rows), times[1] + m.base.MINUTE))

    def test_rsi_threshold_direction_and_equality(self):
        self.assertEqual(m.return_cross(80, 79, -1), 'DOWN')
        self.assertIsNone(m.return_cross(81, 80, -1))
        self.assertEqual(m.return_cross(20, 21, 1), 'UP')
        self.assertIsNone(m.return_cross(80, 79, 1))

    def test_population_standard_deviation(self):
        mid, lo, hi = m.bollinger([1, 2, 3, 4], 4)
        self.assertEqual(mid, 2.5)
        self.assertAlmostEqual(hi - mid, 2 * 1.25 ** .5)
        self.assertAlmostEqual(mid - lo, hi - mid)
        self.assertIsNone(m.bollinger([1] * 19))

    def test_bollinger_reentry_requires_previous_outside_current_inside(self):
        band = (100, 90, 110)
        self.assertEqual(m.bb_return(89, band, 91, band), 'UP')
        self.assertEqual(m.bb_return(111, band, 109, band), 'DOWN')
        self.assertIsNone(m.bb_return(89, band, 111, band))
        self.assertIsNone(m.bb_return(90, band, 91, band))
        self.assertIsNone(m.bb_return(89, band, 88, (100, 80, 120)))

    def test_gap_does_not_create_rsi_cross(self):
        times = [20 * m.base.MINUTE, 22 * m.base.MINUTE]
        rows = [{'barEndUs': times[0], 'close': 100., 'rsi': 71., 'M2': None, 'M3': None},
                {'barEndUs': times[1], 'close': 99., 'rsi': 69., 'M2': None, 'M3': None}]
        result = m.add_regime_signals((times, rows), ([], []))[1]
        self.assertIsNone(result[-1]['R70'])

    def test_adx_prefix_is_unaffected_by_future_bars(self):
        bars = ohlc(list(range(11, 61)), list(range(9, 59)), list(range(10, 60)))
        self.assertEqual(m.build_adx(bars[:35])[1], m.build_adx(bars)[1][:35])

    def test_empty_strategies_are_reported_with_zero_not_omitted(self):
        with tempfile.TemporaryDirectory() as directory:
            results, _ = m.complete_results([], Path(directory))
        self.assertEqual(len(results), 2176)
        self.assertTrue(all(row['n'] == 0 and row['winRate'] is None for row in results))


if __name__ == '__main__':
    unittest.main()

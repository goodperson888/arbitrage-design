import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analysis'))
import analyze_atr_candidates as m


def bars(values, start=0, step=None):
    step = step or m.base.MINUTE
    return [{'open_us': start+i*step, 'end_us': start+(i+1)*step,
             'open': v, 'high': v+1, 'low': v-1, 'close': v} for i,v in enumerate(values)]


class ATRTests(unittest.TestCase):
    def test_true_range_uses_previous_close(self):
        data = bars([100, 100])
        data[1]['high'], data[1]['low'] = 110, 109
        _, values = m.build_atr(data, period=2)
        self.assertEqual(values[1]['tr'], 10)

    def test_wilder_seed_and_update(self):
        data = bars([100, 100, 100, 100])
        data[0]['high'], data[0]['low'] = 101, 99
        data[1]['high'], data[1]['low'] = 102, 100
        data[2]['high'], data[2]['low'] = 103, 101
        data[3]['high'], data[3]['low'] = 107, 105
        _, values = m.build_atr(data, period=3)
        self.assertAlmostEqual(values[2]['atr'], 7/3)
        self.assertAlmostEqual(values[3]['atr'], 35/9)

    def test_gap_resets_atr(self):
        data = bars([100]*20)
        data[-1]['open_us'] += m.base.MINUTE
        data[-1]['end_us'] += m.base.MINUTE
        _, values = m.build_atr(data, period=3)
        self.assertIsNone(values[-1]['atr'])

    def test_rank_is_current_and_bounded(self):
        _, values = m.build_atr(bars(list(range(100, 180))), period=3, rank_window=5)
        ranks = [v['rank'] for v in values if v['rank'] is not None]
        self.assertTrue(ranks)
        self.assertTrue(all(0 < x <= 1 for x in ranks))

    def test_five_minute_aggregation_requires_alignment(self):
        data = bars([100]*10)
        self.assertEqual(len(m.aggregate_fives(data)), 2)
        self.assertEqual(len(m.aggregate_fives(data[1:])), 1)

    def test_normalized_distance_thresholds(self):
        ind = {'atr': 1., 'atrShockReverse': None}
        xc = [0, 0, 102., 100.]
        out = m.dynamic_signals(0, ind, xc, 60, cross_rows=[])
        self.assertEqual(out['A4'], 'UP')
        self.assertEqual(out['A5'], 'UP')
        self.assertEqual(out['A6'], 'UP')
        xc = [0, 0, 100.25, 100.]
        out = m.dynamic_signals(0, ind, xc, 60, cross_rows=[])
        self.assertIsNone(out['A4'])

    def test_breakout_requires_past_spot(self):
        ind = {'atr': 1., 'atrShockReverse': None}
        xc = [120*m.base.SECOND, 120*m.base.SECOND, 102., 100.]
        old = [[60*m.base.SECOND, 60*m.base.SECOND, 100., 100.]]
        self.assertEqual(m.dynamic_signals(120*m.base.SECOND, ind, xc, 60, cross_rows=old)['A7'], 'UP')
        self.assertIsNone(m.dynamic_signals(10, ind, xc, 60, cross_rows=[]).get('A7'))

    def test_shock_reversal_uses_completed_bars(self):
        base_ind = ([m.base.MINUTE, 2*m.base.MINUTE], [
            {'barEndUs':m.base.MINUTE, 'M3':None, 'atrShockReverse':None},
            {'barEndUs':2*m.base.MINUTE, 'M3':None, 'atrShockReverse':'UP'}])
        augmented = m.add_signals(base_ind, ([m.base.MINUTE], [{'barEndUs':m.base.MINUTE,'atr':1,'tr':1,'rank':.5,'atrBps':1,'previousTrAtr':2}]), ([], []))
        self.assertEqual(augmented[1][1]['atrShockReverse'], None)


if __name__ == '__main__':
    unittest.main()

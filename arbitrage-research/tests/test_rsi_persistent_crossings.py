import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analysis'))
import analyze_rsi_persistent_crossings as m


def sequence(rsis, times=None):
    times = times or [(i + 1) * m.base.MINUTE for i in range(len(rsis))]
    rows = [{'rsi': r, 'barEndUs': t} for r, t in zip(rsis, times)]
    return m.add_crossing_states((times, rows))[1]


class CrossingTests(unittest.TestCase):
    def test_no_initial_guess_or_extreme_entry(self):
        rows = sequence([50, 81, 85])
        self.assertEqual([r['M7'] for r in rows], [None, None, None])

    def test_cross_then_persist_beyond_one_minute(self):
        rows = sequence([81, 79, 65, 55, 21])
        self.assertEqual([r['M7'] for r in rows], [None] + ['DOWN'] * 4)

    def test_only_return_crossings_switch(self):
        rows = sequence([81, 79, 19, 21, 81, 79])
        self.assertEqual([r['M7'] for r in rows], [None, 'DOWN', 'DOWN', 'UP', 'UP', 'DOWN'])

    def test_pause_preserves_latent_direction(self):
        rows = sequence([19, 21, 81, 85, 79])
        self.assertEqual([r['M8'] for r in rows], [None, 'UP', None, None, 'DOWN'])
        self.assertEqual(rows[3]['M7'], 'UP')

    def test_equal_threshold_is_not_a_return(self):
        rows = sequence([82, 80, 79, 20, 21])
        self.assertEqual([r['M7'] for r in rows], [None, None, 'DOWN', 'DOWN', 'UP'])
        self.assertIsNone(rows[3]['M8'])

    def test_gap_does_not_create_crossing(self):
        rows = sequence([81, 79, 18, 21], [m.base.MINUTE, 2*m.base.MINUTE, 4*m.base.MINUTE, 5*m.base.MINUTE])
        self.assertEqual([r['M7'] for r in rows], [None, 'DOWN', None, 'UP'])

    def test_missing_rsi_resets(self):
        rows = sequence([81, 79, None, 70])
        self.assertEqual([r['M7'] for r in rows], [None, 'DOWN', None, None])

    def test_cannot_see_cross_before_bar_closes(self):
        times = [m.base.MINUTE, 2*m.base.MINUTE]
        indicators = m.add_crossing_states((times, [{'rsi': 81, 'barEndUs': times[0]}, {'rsi': 79, 'barEndUs': times[1]}]))
        self.assertIsNone(m.base.indicator_at(indicators, times[1]-1)['M7'])
        self.assertEqual(m.base.indicator_at(indicators, times[1])['M7'], 'DOWN')

    def test_original_indicators_unchanged(self):
        original = ([1], [{'rsi': 81, 'barEndUs': 1}])
        m.add_crossing_states(original)
        self.assertNotIn('M7', original[1][0])


if __name__ == '__main__':
    unittest.main()

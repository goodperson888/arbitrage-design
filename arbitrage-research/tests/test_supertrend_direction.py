import sys
from pathlib import Path
import sqlite3
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analysis'))
import analyze_supertrend_direction as m


def bars(closes, start=0):
    return [{'open_us': start + i * m.base.MINUTE, 'end_us': start + (i + 1) * m.base.MINUTE,
             'open': value, 'high': value + .5, 'low': value - .5, 'close': value}
            for i, value in enumerate(closes)]


def indicator(side='UP', flip=None, atr=2):
    return {'supertrends': {'primary': {'direction': side, 'flip': flip, 'atr': atr}}}


class SupertrendTests(unittest.TestCase):
    def test_atr_and_trailing_bands_hand_calculated(self):
        _, rows = m.build_supertrend(bars([100, 100, 104, 108, 102, 98]), 2, 1)
        self.assertIsNone(rows[0]['atr'])
        self.assertEqual(rows[1]['atr'], 1)
        self.assertIsNone(rows[1]['direction'])
        self.assertEqual(rows[2]['atr'], 2.75)
        self.assertEqual(rows[2]['upper'], 101)
        self.assertEqual(rows[2]['lower'], 101.25)
        self.assertEqual(rows[2]['flip'], 'UP')
        self.assertEqual(rows[2]['line'], 101.25)
        self.assertEqual(rows[3]['atr'], 3.625)
        self.assertEqual(rows[3]['lower'], 104.375)
        self.assertEqual(rows[4]['atr'], 5.0625)
        self.assertEqual(rows[4]['lower'], 104.375)
        self.assertEqual(rows[4]['upper'], 107.0625)
        self.assertEqual(rows[4]['flip'], 'DOWN')
        self.assertEqual(rows[4]['line'], 107.0625)
        self.assertIsNone(rows[5]['flip'])

    def test_seed_is_not_a_direction_or_flip(self):
        _, rows = m.build_supertrend(bars([100] * 20), 3, 3)
        self.assertTrue(all(row['direction'] is None and row['flip'] is None for row in rows))

    def test_equality_does_not_flip(self):
        data = bars([100, 101, 102])
        data[1]['high'], data[1]['low'] = 101.5, 100.5
        _, rows = m.build_supertrend(data, 1, 1)
        self.assertEqual(rows[1]['upper'], 101)
        self.assertEqual(rows[1]['close'], rows[1]['upper'])
        self.assertIsNone(rows[1]['flip'])
        self.assertEqual(rows[2]['flip'], 'UP')

    def test_gap_clears_atr_and_uses_local_true_range(self):
        data = bars([100, 100, 104, 108]) + bars([200, 200], start=6 * m.base.MINUTE)
        _, rows = m.build_supertrend(data, 2, 1)
        self.assertIsNone(rows[-2]['atr'])
        self.assertEqual(rows[-2]['tr'], 1)
        self.assertEqual(rows[-1]['atr'], 1)
        self.assertIsNone(rows[-1]['direction'])

    def test_future_candles_do_not_change_prefix(self):
        data = bars([100, 100, 104, 108, 102, 98, 110, 95])
        self.assertEqual(m.build_supertrend(data[:5], 2, 1)[1], m.build_supertrend(data, 2, 1)[1][:5])

    def test_close_only_availability_and_expiration(self):
        times, rows = m.build_supertrend(bars([100, 100, 104]), 2, 1)
        self.assertIsNone(m.base.indicator_at((times, rows), 3 * m.base.MINUTE - 1)['direction'])
        self.assertEqual(m.base.indicator_at((times, rows), 3 * m.base.MINUTE)['direction'], 'UP')
        self.assertIsNone(m.base.indicator_at((times, rows), 4 * m.base.MINUTE))

    def test_bad_parameters_rejected(self):
        for period, factor in ((0, 3), (14, 0), (14, -1)):
            with self.assertRaises(ValueError):
                m.build_supertrend([], period, factor)


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.signals = m.make_signal_callback({0: 100})
        self.at = 60 * m.base.SECOND

    def test_persistent_and_flip_rules_are_distinct(self):
        out = self.signals(self.at, indicator(), None, 240)
        self.assertEqual(out['S0'], 'UP')
        self.assertIsNone(out['S1'])
        self.assertEqual(self.signals(self.at, indicator(flip='UP'), None, 240)['S1'], 'UP')

    def test_spot_missing_only_removes_spot_dependent_rules(self):
        out = self.signals(self.at, indicator(), None, 240)
        self.assertEqual(out['S0'], 'UP')
        self.assertTrue(all(out[key] is None for key in ('SX', 'S2', 'S3')))

    def test_uses_exact_binance_open_not_snapshot_anchor(self):
        xc = [self.at, self.at, 101, 200]
        out = self.signals(self.at, indicator(), xc, 240)
        self.assertEqual(out['S2'], 'UP')
        self.assertIsNone(out['S3'])

    def test_spot_below_open_conflicts_with_up_trend(self):
        out = self.signals(self.at, indicator(), [self.at, self.at, 99, 100], 240)
        self.assertEqual(out['SX'], 'UP')
        self.assertIsNone(out['S2'])

    def test_anchor_equality_has_no_side(self):
        out = self.signals(self.at, indicator(), [self.at, self.at, 100, 100], 240)
        self.assertIsNone(out['S2'])

    def test_down_signed_distance_and_time(self):
        out = self.signals(self.at, indicator('DOWN'), [self.at, self.at, 96, 100], 240)
        self.assertEqual(out['S2'], 'DOWN')
        self.assertEqual(out['S3'], 'DOWN')
        self.assertAlmostEqual(m.distance_time_score(96, 100, 2, 240, 'DOWN'), 1)
        self.assertLess(m.distance_time_score(96, 100, 2, 300, 'DOWN'), 1)

    def test_missing_anchor_does_not_substitute_future_open(self):
        out = self.signals(6 * m.base.MINUTE, indicator(), [6*m.base.MINUTE, 6*m.base.MINUTE, 101, 100], 240)
        self.assertEqual(out['S0'], 'UP')
        self.assertIsNone(out['SX'])

    def test_undefined_scale_is_rejected(self):
        for atr in (None, 0, -1):
            self.assertIsNone(m.distance_time_score(101, 100, atr, 240, 'UP'))
        self.assertIsNone(m.distance_time_score(101, 100, 1, 0, 'UP'))


class IntegrationTests(unittest.TestCase):
    def test_zero_groups_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            results, ledgers = m.analyze([], Path(folder))
        self.assertEqual(len(results), 6120)
        self.assertEqual({row['side'] for row in results}, {'UP', 'DOWN', 'BOTH'})
        self.assertTrue(all(row['n'] == 0 and row['winRate'] is None for row in results))
        self.assertIn('S0|UP|gross_le_050|test|fixed', ledgers)

    def test_shared_account_waits_instead_of_adding_direction_accounts(self):
        def e(at, side, won):
            return {'executionUs': at*m.base.SECOND, 'endUs': 300*m.base.SECOND,
                    'marketId': 'one', 'side': side, 'won': won, 'strategy': 'S0',
                    'odds': 'gross_le_050', 'remaining': 300-at, 'levels': [[.4, 1000]]}
        up, down = e(30, 'UP', False), e(90, 'DOWN', True)
        both, ledger = m.base.replay([up, down], rounds=3)
        self.assertEqual(both['n'], 1)
        self.assertEqual(ledger[0]['side'], 'UP')
        self.assertEqual(both['rejections']['waiting_for_settlement'], 1)

    def test_full_candidate_extraction_and_asof_features(self):
        con = sqlite3.connect(':memory:')
        con.row_factory = sqlite3.Row
        con.execute('CREATE TABLE snapshots(market_id TEXT,source_us INT,received_us INT,outcome TEXT,best_ask TEXT,asks_json TEXT)')
        at = 30 * m.base.SECOND
        con.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?)', ('one', at, at, 'UP', '.4', '[[0.4,10000]]'))
        row = {'barEndUs': 0, 'rsi': None, 'sigma': None, 'supertrends': {'primary':
               {'direction': 'UP', 'flip': 'UP', 'atr': 2, 'line': 99, 'close': 101, 'barEndUs': 0}}}
        indicators = ([0], [row])
        market = {'market_id': 'one', 'start_us': 0, 'end_us': 300*m.base.SECOND, 'winner': 'UP', 'label_available_us': 370*m.base.SECOND}
        crosses = {'one': [[at, at, 104, 200]]}
        with tempfile.TemporaryDirectory() as folder:
            entries, _, _ = m.base.extract_entries(con, [market], crosses, indicators, None, Path(folder), (),
                                                   include_builtin=False, signal_callback=m.make_signal_callback({0: 100}))
        m.add_entry_features(entries, indicators, {0: 100}, crosses)
        audit = m.audit_entries(entries)
        self.assertGreater(audit['entries'], 0)
        self.assertEqual(audit['duplicates'], 0)
        self.assertTrue(all(entry['side'] == 'UP' and entry['binanceAnchorOpen'] == 100 for entry in entries))
        self.assertTrue(all(entry['strategy'] != 'S3' for entry in entries))  # 4/(2*sqrt(4.5)) < 1
        con.close()


if __name__ == '__main__':
    unittest.main()

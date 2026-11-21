import sys
from pathlib import Path
import unittest
import sqlite3
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analysis'))
import analyze_supertrend_robustness as m

S = m.base.SECOND
M = m.base.MINUTE


def indicators(end=M, direction='UP', flip='UP', atr=2):
    return [end], [{'barEndUs': end, 'rsi':None, 'sigma':None, 'supertrends': {'primary':
        {'direction': direction, 'flip': flip, 'atr': atr}}}]


class AvailabilityTests(unittest.TestCase):
    def test_candle_cannot_be_seen_before_delivery(self):
        delayed = m.delayed_indicators(indicators(), 500)
        self.assertIsNone(m.available_indicator(delayed, M + 499999))
        row = m.available_indicator(delayed, M + 500000)
        self.assertEqual(row['barEndUs'], M)

    def test_delivery_does_not_extend_expiration(self):
        delayed = m.delayed_indicators(indicators(), 2000)
        self.assertIsNotNone(m.available_indicator(delayed, 2*M - 1))
        self.assertIsNone(m.available_indicator(delayed, 2*M))

    def test_old_row_not_used_while_waiting_for_new_minute(self):
        a, b = indicators(M), indicators(2*M, 'DOWN', 'DOWN')
        joined = (a[0] + b[0], a[1] + b[1])
        delayed = m.delayed_indicators(joined, 1000)
        self.assertIsNone(m.available_indicator(delayed, 2*M + S - 1))
        self.assertEqual(m.available_indicator(delayed, 2*M + S)['supertrends']['primary']['flip'], 'DOWN')

    def test_zero_lag_preserves_original_timestamp(self):
        source = indicators()
        self.assertEqual(m.available_indicator(m.delayed_indicators(source, 0), M), source[1][0])

    def test_half_second_and_second_quantize_to_same_grid(self):
        a = m.delayed_indicators(indicators(), 500)
        b = m.delayed_indicators(indicators(), 1000)
        self.assertEqual([m.available_indicator(a, t) for t in range(M, 2*M, S)],
                         [m.available_indicator(b, t) for t in range(M, 2*M, S)])


class SignalTests(unittest.TestCase):
    def test_distance_control_does_not_require_supertrend(self):
        signals = m.rule_signals(indicators(direction='DOWN', flip=None)[1][0], [M,M,104,100], 100, 240)
        self.assertEqual(signals['D1'], 'UP')
        self.assertIsNone(signals['S3'])
        self.assertIsNone(signals['S13'])

    def test_intersection_requires_flip_and_distance(self):
        row = indicators()[1][0]
        signals = m.rule_signals(row, [M,M,104,100], 100, 240)
        self.assertEqual(signals, {'S1':'UP', 'S3':'UP', 'S13':'UP', 'D1':'UP'})
        small = m.rule_signals(row, [M,M,101,100], 100, 240)
        self.assertEqual(small['S1'], 'UP')
        self.assertIsNone(small['S13'])

    def test_missing_spot_does_not_cancel_pure_flip(self):
        out = m.rule_signals(indicators()[1][0], None, 100, 240)
        self.assertEqual(out['S1'], 'UP')
        self.assertIsNone(out['S3'])
        self.assertIsNone(out['D1'])

    def test_callback_uses_delivery_and_exact_current_market_open(self):
        callback = m.make_callback(indicators(), {0:100})
        spot = [M,M,104,999]
        self.assertEqual(callback(M, None, spot, 240)['S13_L0'], 'UP')
        self.assertIsNone(callback(M, None, spot, 240)['S13_L500'])
        self.assertEqual(callback(M+S, None, spot, 239)['S13_L500'], 'UP')
        self.assertIsNone(callback(M+S, None, spot, 239)['S13_L2000'])

    def test_anchor_equality_has_no_distance_signal(self):
        self.assertIsNone(m.rule_signals(indicators()[1][0], [M,M,100,100], 100, 240)['D1'])


class TraceTests(unittest.TestCase):
    def test_loss_cycle_can_switch_sides_and_skip_markets(self):
        ledger = [{'round':i+1, 'side': 'UP' if i%2 else 'DOWN', 'won':False,
                   'executionUs':i*900*S} for i in range(11)]
        self.assertEqual(m.loss_cycles(ledger), [ledger])

    def test_short_cycles_do_not_count_as_eleven_losses(self):
        ledger = [{'round':1,'won':False}, {'round':2,'won':True}, {'round':1,'won':False}]
        self.assertEqual(m.loss_cycles(ledger), [])

    def test_malformed_cycle_is_rejected(self):
        with self.assertRaises(RuntimeError):
            m.loss_cycles([{'round':11,'won':False}])

    def test_continuous_official_run_resets_on_missing_market(self):
        starts = [0, 300*S, 900*S, 1200*S, 1500*S]
        rows = [{'market_id':str(t), 'start_us':t, 'end_us':t+300*S, 'winner':'DOWN'} for t in starts]
        runs = m.consecutive_outcomes(rows)
        self.assertEqual(runs['DOWN']['count'], 3)
        self.assertEqual(runs['DOWN']['marketIds'], [str(t) for t in starts[2:]])

    def test_remove_best_day_is_ledger_description(self):
        summary = {'netPnl':5, 'daily':{'a':[10,20,5],'b':[-5,10,3]}}
        out = m.concentration(summary)
        self.assertEqual(out['pnlWithoutBestDay'], -5)
        self.assertEqual(out['activeDays'], 2)


class ExtractionTests(unittest.TestCase):
    def extract(self, snapshots):
        con = sqlite3.connect(':memory:')
        con.row_factory = sqlite3.Row
        con.execute('CREATE TABLE snapshots(market_id TEXT,source_us INTEGER,received_us INTEGER,'
                    'outcome TEXT,best_ask REAL,asks_json TEXT)')
        for at, price in snapshots:
            con.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?)',
                        ('one', at, at, 'UP', price, f'[[{price},100]]'))
        markets = [{'market_id':'one','start_us':0,'end_us':300*S,'winner':'UP','label_available_us':360*S}]
        ind = indicators()
        with tempfile.TemporaryDirectory() as folder:
            entries, _, _ = m.base.extract_entries(con, markets, {}, ind, None, Path(folder), (),
                include_builtin=False, signal_callback=m.make_callback(ind, {0:100}))
        con.close()
        return entries

    def test_delayed_availability_misses_expired_price_opportunity(self):
        rows = self.extract([(60*S,.4),(61*S,.6)])
        self.assertTrue(any(r['strategy']=='S1_L0' for r in rows))
        self.assertFalse(any(r['strategy']=='S1_L500' for r in rows))

    def test_execution_miss_cannot_pick_favorable_later_book(self):
        rows = self.extract([(60*S,.4),(60*S+200000,.6),(61*S,.4)])
        self.assertTrue(any(r['strategy']=='S1_L0' and r['delayMs']==0 for r in rows))
        self.assertFalse(any(r['strategy']=='S1_L0' and r['delayMs']==300 for r in rows))


if __name__ == '__main__':
    unittest.main()

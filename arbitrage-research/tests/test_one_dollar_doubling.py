"""Accounting, sequencing, and cash/depth stress for cash-stake replay."""
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/analysis/analyze_one_dollar_doubling.py'
SPEC = importlib.util.spec_from_file_location('one_dollar_doubling', SCRIPT)
MODEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODEL)


def entry(index, won=False, size=100000):
    execution = index * 300_000_000
    return {'market_id': f'm{index}', 'execution_us': execution,
            'end_us': execution + 60_000_000, 'outcome': 'UP',
            'winner': 'UP' if won else 'DOWN', 'levels': [[0.82, size]]}


class CashDoublingTest(unittest.TestCase):
    def test_one_loss_followed_by_double_win_does_not_recover(self):
        result = MODEL.replay([entry(1), entry(2, won=True)])
        self.assertAlmostEqual(result['netPnl'], -0.5987756097560974)
        self.assertEqual(result['winningCyclesStillNetNegative'], 1)
        self.assertEqual([r['principal'] for r in result['ledger']], [1, 2])

    def test_ten_losses_allow_eleventh_stake_but_never_twelfth_double(self):
        result = MODEL.replay([entry(i) for i in range(12)])
        self.assertEqual([r['principal'] for r in result['ledger']],
                         [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 1])
        self.assertEqual(result['elevenLossCycles'], 1)
        self.assertAlmostEqual(sum(r['principal'] for r in result['ledger'][:11]), 2047)
        self.assertGreater(-sum(r['netPnl'] for r in result['ledger'][:11]), 2047)

    def test_stop_at_eleventh_loss(self):
        result = MODEL.replay([entry(i) for i in range(12)], cap_action='stop')
        self.assertEqual(result['trades'], 11)
        self.assertEqual(result['skipped']['stopped_after_11_losses'], 1)

    def test_win_resets_nominal_stake_even_if_cycle_still_loses(self):
        result = MODEL.replay([entry(1), entry(2, won=True), entry(3, won=True)])
        self.assertEqual([r['principal'] for r in result['ledger']], [1, 2, 1])

    def test_insufficient_depth_does_not_fake_full_fill_or_reset_step(self):
        result = MODEL.replay([entry(1), entry(2, size=1), entry(3, won=True)])
        self.assertEqual(result['trades'], 2)
        self.assertEqual(result['skipped']['insufficient_depth_for_full_stake'], 1)
        self.assertEqual(result['ledger'][-1]['principal'], 2)

    def test_capital_includes_fees(self):
        result = MODEL.replay([entry(1)], bankroll=1)
        self.assertEqual(result['trades'], 0)
        self.assertEqual(result['skipped']['bankroll_exhausted'], 1)

    def test_never_use_unsettled_result_to_double_next_entry(self):
        first, second = entry(1), entry(2, won=True)
        second['execution_us'] = first['end_us'] + 30_000_000
        result = MODEL.replay([first, second], delay=60)
        self.assertEqual(result['trades'], 1)
        self.assertEqual(result['skipped']['previous_order_not_settled_yet'], 1)

    def test_depth_changes_actual_vwap_and_fees(self):
        fill = MODEL.buy_notional([[0.8, 1], [0.85, 10]], 1.65)
        self.assertAlmostEqual(fill['quantity'], 2)
        self.assertAlmostEqual(fill['vwap'], 0.825)
        self.assertAlmostEqual(fill['fee'], 0.07 * (0.8 * 0.2 + 0.85 * 0.15))
        self.assertIsNone(MODEL.buy_notional([[0.86, 100]], 1))

    def test_floor_rechecked_after_stake_change(self):
        row = entry(1, won=True)
        row['levels'] = [[0.7, 2], [0.84, 100]]
        result = MODEL.replay([row])
        self.assertEqual(result['trades'], 0)
        self.assertEqual(result['skipped']['resized_actual_vwap_outside_band'], 1)


if __name__ == '__main__':
    unittest.main()

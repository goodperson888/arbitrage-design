import importlib.util
from pathlib import Path
import unittest


PATH = Path(__file__).resolve().parents[1] / 'scripts/analysis/analyze_martingale_candidates.py'
SPEC = importlib.util.spec_from_file_location('martingale_candidates', PATH)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def bars(closes, start=0):
    return [{'open_us': start + i * m.MINUTE, 'end_us': start + (i + 1) * m.MINUTE,
             'open': x, 'high': x, 'low': x, 'close': x} for i, x in enumerate(closes)]


def entry(i, won, levels=None, odds='gross_le_050'):
    return {'marketId': str(i), 'executionUs': i * 600 * m.SECOND,
            'endUs': (i * 600 + 300) * m.SECOND,
            'levels': levels or [[.45, 10000]], 'odds': odds,
            'strategy': 'M1', 'side': 'UP', 'remaining': 100, 'won': won}


class IndicatorTests(unittest.TestCase):
    def test_wilder_rsi_seed_known_example(self):
        closes = [44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28]
        _, values = m.build_indicators(bars(closes))
        self.assertAlmostEqual(values[-1]['rsi'], 70.4641350211, places=7)
        self.assertIsNone(values[-2]['rsi'])

    def test_no_final_candle_before_close(self):
        indicators = m.build_indicators(bars(list(range(100, 121))))
        before = m.indicator_at(indicators, 15 * m.MINUTE - 1)
        self.assertIsNone(before['rsi'])
        self.assertEqual(m.indicator_at(indicators, 15 * m.MINUTE)['rsi'], 100.)

    def test_rsi_state_persists_until_other_extreme(self):
        _, values = m.build_indicators(bars(list(range(100, 120)) + [118, 117, 116, 115]))
        self.assertEqual(values[-1]['M1'], 'DOWN')
        self.assertLess(values[-1]['rsi'], 80)
        self.assertTrue(any(r['M2'] == 'DOWN' for r in values))

    def test_gap_resets_rsi_and_state(self):
        data = bars(list(range(100, 120))) + bars([120, 121], start=22 * m.MINUTE)
        _, values = m.build_indicators(data)
        self.assertIsNone(values[-1]['rsi'])
        self.assertIsNone(values[-1]['M1'])

    def test_indicators_expire_when_next_minute_missing(self):
        indicators = m.build_indicators(bars(list(range(100, 120))))
        self.assertIsNone(m.indicator_at(indicators, 21 * m.MINUTE))


class ExecutionTests(unittest.TestCase):
    def test_fee_inclusive_budget_and_string_levels(self):
        fill = m.buy_budget([['0.5', '100']], 1)
        self.assertLessEqual(fill['cost'], 1.)
        self.assertGreater(fill['effectivePrice'], .5)
        self.assertAlmostEqual(fill['quantity'], 1.93)

    def test_no_fictitious_liquidity(self):
        self.assertIsNone(m.buy_budget([[.4, 1]], 1))
        self.assertIsNone(m.buy_budget([[.51, 100]], 1))

    def test_resizing_consumes_deeper_level(self):
        levels = [[.4, 3], [.5, 100]]
        small = m.buy_budget(levels, 1)
        large = m.buy_budget(levels, 4)
        self.assertGreater(large['vwap'], small['vwap'])
        self.assertAlmostEqual(large['worst'], .5)

    def test_book_requires_past_source_and_received(self):
        row = {'source_us': 11 * m.SECOND, 'received_us': 10 * m.SECOND, 'best_ask': '.4'}
        self.assertIsNone(m.book_at([row], [10 * m.SECOND], 10 * m.SECOND))
        self.assertIsNotNone(m.book_at([row], [10 * m.SECOND], 11 * m.SECOND))
        self.assertIsNone(m.book_at([row], [10 * m.SECOND], 14 * m.SECOND))

    def test_cross_cannot_use_future_or_stale(self):
        self.assertIsNone(m.cross_at([[10*m.SECOND,11*m.SECOND,100,100]], [10*m.SECOND], 10*m.SECOND))
        self.assertIsNone(m.cross_at([[10*m.SECOND,10*m.SECOND,100,100]], [10*m.SECOND], 13*m.SECOND))

    def test_probability_cap_known_before_execution(self):
        cap = m.probability_price_cap(.50)
        self.assertLess(cap, .45)
        self.assertAlmostEqual(cap + .07 * cap * (1 - cap), .45, places=10)
        self.assertIsNone(m.buy_budget([[.45,100]], 1, cap=cap))


class SizingTests(unittest.TestCase):
    def test_doubling_and_win_reset(self):
        summary, ledger = m.replay([entry(0,False),entry(1,False),entry(2,True),entry(3,True)], rounds=5)
        self.assertEqual([x['budget'] for x in ledger], [1.,2.,4.,1.])
        self.assertEqual(summary['maxConsecutiveLosses'], 2)
        self.assertEqual(summary['negativeWinningCycles'], 0)

    def test_cap_loss_resets_and_is_counted(self):
        summary, ledger = m.replay([entry(i,False) for i in range(4)], rounds=3)
        self.assertEqual([x['budget'] for x in ledger], [1.,2.,4.,1.])
        self.assertEqual(summary['capLossCycles'], 1)

    def test_finite_bankroll_does_not_claim_eleven_rounds(self):
        summary, ledger = m.replay([entry(i,False) for i in range(11)], rounds=11, bankroll=100)
        self.assertEqual(len(ledger), 6)
        self.assertEqual(summary['rejections']['bankroll_stop'], 1)
        self.assertEqual(summary['maxBudget'], 32.)

    def test_settlement_delay_blocks_next_signal(self):
        a, b = entry(0,False), entry(1,True)
        b['executionUs'] = 320 * m.SECOND
        summary, ledger = m.replay([a,b], rounds=3, settlement_delay=60)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(summary['rejections']['waiting_for_settlement'], 1)

    def test_actual_official_arrival_overrides_assumed_delay(self):
        a, b = entry(0,False), entry(1,True)
        a['labelAvailableUs'] = 700 * m.SECOND
        summary, ledger = m.replay([a,b], rounds=3, settlement_delay=60)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(summary['rejections']['waiting_for_settlement'], 1)

    def test_illiquid_double_is_skipped_without_advancing(self):
        a, b, c = entry(0,False), entry(1,True,[[.45,3]]), entry(2,True)
        summary, ledger = m.replay([a,b,c], rounds=3)
        self.assertEqual([x['budget'] for x in ledger], [1.,2.])
        self.assertEqual(summary['rejections']['insufficient_decision_depth'], 1)

    def test_cheaper_execution_is_not_removed_after_sending(self):
        e = entry(0,False,[[.29,100]])
        e['decisionLevels'] = [[.30,100]]
        _, ledger = m.replay([e])
        self.assertEqual(len(ledger),1)
        self.assertAlmostEqual(ledger[0]['vwap'],.29)

    def test_future_liquidity_cannot_pass_decision_precheck(self):
        e = entry(0,True,[[.4,100]])
        e['decisionLevels'] = [[.4,.5]]
        summary, ledger = m.replay([e])
        self.assertEqual(len(ledger),0)
        self.assertEqual(summary['rejections']['insufficient_decision_depth'],1)

    def test_net_odds_rechecked_after_stake_resize(self):
        a = entry(0,False,odds='net_cost_le_050')
        b = entry(1,True,[[.4,.5],[.5,100]],odds='net_cost_le_050')
        summary, ledger = m.replay([a,b],rounds=3)
        self.assertEqual(len(ledger),1)
        self.assertEqual(summary['rejections']['resized_price_constraint'],1)


class ProbabilityTests(unittest.TestCase):
    def test_fit_uses_only_results_known_before_cutoff(self):
        markets, crosses, times, values = [], {}, [], []
        cutoff = 121 * 600 * m.SECOND
        for i in range(120):
            start = i * 600 * m.SECOND
            end = start + 300 * m.SECOND
            at = end - 60 * m.SECOND
            won_up = i % 2 == 0
            markets.append({'market_id': str(i), 'start_us': start, 'end_us': end,
                            'winner': 'UP' if won_up else 'DOWN',
                            'label_available_us': end + 90 * m.SECOND if i < 110 else cutoff + m.SECOND})
            crosses[str(i)] = [[at, at, 101 if won_up else 99, 100]]
            times.append(at); values.append({'sigma': .001})
        model, audit = m.fit_probability(markets, crosses, (times, values), cutoff)
        self.assertEqual(audit['markets'], 110)
        self.assertGreater(model['slope'], 0)
        self.assertTrue(all(m.math.isfinite(model[k]) for k in ('intercept','slope')))
        for r in markets[110:]:
            r['winner'] = 'DOWN' if r['winner'] == 'UP' else 'UP'
        other, _ = m.fit_probability(markets, crosses, (times, values), cutoff)
        self.assertEqual(model, other)


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analysis'))
import analyze_entry_scope as m

S = m.base.SECOND


def indicator(end):
    return ([end], [{'barEndUs': end, 'supertrends': {
        'n10': {'flip': 'UP', 'atr': 1}, 'primary': {'flip': None, 'atr': 1}}}])


def book(at, price=.2, size=100):
    return {'source_us': at, 'received_us': at, 'outcome': 'UP', 'best_ask': price,
            'asks_json': json.dumps([[price, size]])}


def market(start=60*S):
    return {'market_id': 'test', 'start_us': start, 'end_us': start+300*S,
            'winner': 'UP', 'label_available_us': start+360*S}


class EntryScopeTests(unittest.TestCase):
    def test_zero_is_never_free_entry(self):
        for p in m.PROFILES:
            self.assertFalse(m.allows(p, 0, 100))
            self.assertFalse(m.allows(p, -.01, 100))
            self.assertFalse(m.allows(p, .51, 100))

    def test_price_and_time_changes_are_independent(self):
        self.assertTrue(m.allows('price_only', .01, 100))
        self.assertFalse(m.allows('price_only', .01, 300))
        self.assertTrue(m.allows('time_only', .4, 300))
        self.assertFalse(m.allows('time_only', .2, 300))
        self.assertTrue(m.allows('price_time', .01, 300))
        self.assertFalse(m.allows('price_time', .4, 0))

    def test_publication_lag_does_not_extend_flip(self):
        data = indicator(60*S)
        self.assertIsNone(m.available(data, 60*S, 1000))
        self.assertIsNotNone(m.available(data, 61*S, 1000))
        self.assertIsNone(m.available(data, 120*S, 1000))

    def test_full_window_can_enter_at_market_start(self):
        start = 60*S
        entries, _ = m.extract_market(market(), 'test', [book(start)], [], indicator(start), {})
        selected = [e for e in entries if e['publicationLagMs'] == 0 and e['delayMs'] == 300]
        self.assertTrue(selected)
        self.assertTrue(all(e['profile'] == 'price_time' and e['remaining'] == 300 for e in selected))
        m.audit(entries)

    def test_order_arriving_at_close_is_rejected(self):
        at = 299*S
        entries, counts = m.extract_market(market(0), 'test', [book(at, .4)], [], indicator(at), {})
        self.assertTrue(entries)
        self.assertTrue(all(e['delayMs'] == 300 and e['executionUs'] < 300*S for e in entries))
        self.assertGreater(counts['execution_at_or_after_close'], 0)

    def test_execution_miss_is_not_replaced_by_later_price(self):
        start = 60*S
        rows = [book(start), book(start+100000, size=.5), book(start+S)]
        entries, counts = m.extract_market(market(), 'test', rows, [], indicator(start), {})
        old_lag = [e for e in entries if e['profile'] == 'price_time' and e['publicationLagMs'] == 0 and e['delayMs'] == 300]
        self.assertFalse(old_lag)
        self.assertGreater(counts['execution_insufficient_depth'], 0)

    def test_cheap_top_quote_still_requires_full_budget_depth(self):
        start = 60*S
        entries, _ = m.extract_market(market(), 'test', [book(start, .01, 1)], [], indicator(start), {})
        self.assertFalse(entries)


if __name__ == '__main__':
    unittest.main()

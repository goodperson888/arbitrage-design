import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analysis'))
import analyze_ema_filters as m

M, S = m.base.MINUTE, m.base.SECOND


def bars(closes, minutes=1, start=0):
    return [{'open_us': start+i*minutes*M, 'end_us': start+(i+1)*minutes*M,
             'open': v, 'high': v+1, 'low': v-1, 'close': v} for i,v in enumerate(closes)]


def entry(mid, decision, side='UP', won=True, end=None):
    return {'strategy':'A6', 'marketId':mid, 'side':side, 'decisionUs':decision,
            '_emaTime':decision, 'executionUs':decision+300_000,
            'endUs':end if end is not None else decision+300*S,
            'labelAvailableUs':0, 'remaining':100, 'won':won,
            'odds':'gross_le_050','levels':[[.4,10000]], 'decisionLevels':[[.4,10000]]}


def feature(direction='UP', ready=True, es='UP'):
    return {'ready':ready, 'E':direction, 'ES':es}


class AggregationTests(unittest.TestCase):
    def test_hourly_ohlc_hand_calculation(self):
        out, q=m.aggregate_bars(bars(list(range(100,160))),60)
        self.assertEqual(out,[{'open_us':0,'end_us':60*M,'open':100,'close':159,'high':160,'low':99}])
        self.assertEqual(q['incompleteBuckets'],0)

    def test_utc_alignment_excludes_partial_first_and_last(self):
        out,q=m.aggregate_bars(bars([100]*35,start=M),15)
        self.assertEqual([b['open_us'] for b in out],[15*M])
        self.assertEqual(q['incompleteBuckets'],2)

    def test_missing_minute_invalidates_bucket(self):
        source=bars([100]*45);del source[20]
        out,q=m.aggregate_bars(source,15)
        self.assertEqual([b['open_us'] for b in out],[0,30*M])
        self.assertEqual(q['incompleteBuckets'],1)

    def test_duplicate_and_out_of_order_fail(self):
        source=bars([100,101])
        for bad in ([source[0],source[0]],list(reversed(source))):
            with self.assertRaises(ValueError):m.aggregate_bars(bad,15)

    def test_invalid_ohlc_and_infinite_fail(self):
        for key,value in (('high',90),('close',float('inf')),('low',110)):
            source=bars([100]);source[0][key]=value
            with self.assertRaises(ValueError):m.aggregate_bars(source,15)

    def test_warmup_overlap_checked(self):
        source=bars([100,101,102])
        out,n=m.merge_bars(source[:2],source[1:])
        self.assertEqual(out,source);self.assertEqual(n,1)
        other=bars([103],start=M)
        with self.assertRaises(ValueError):m.merge_bars(source,other)


class EmaTests(unittest.TestCase):
    def test_sma_seed_then_recursive_ema(self):
        _,rows=m.build_ema(bars([1,2,3,4],minutes=15),15,period=3)
        self.assertIsNone(rows[1]['ema'])
        self.assertEqual(rows[2]['ema'],2)
        self.assertFalse(rows[2]['ready'])
        self.assertEqual(rows[3]['previousEma'],2)
        self.assertEqual(rows[3]['ema'],3)
        self.assertTrue(rows[3]['ready'])

    def test_gap_resets_ema_and_slope(self):
        source=bars([100,101,102],minutes=15)+bars([200,201,202],minutes=15,start=60*M)
        _,rows=m.build_ema(source,15,period=2)
        self.assertIsNone(rows[3]['ema'])
        self.assertEqual(rows[4]['ema'],200.5)
        self.assertIsNone(rows[4]['previousEma'])
        self.assertFalse(rows[4]['ready']);self.assertTrue(rows[5]['ready'])

    def test_equal_price_and_flat_ema_are_neutral(self):
        _,rows=m.build_ema(bars([100]*4,minutes=60),60,period=2)
        self.assertTrue(rows[-1]['ready'])
        self.assertIsNone(rows[-1]['E']);self.assertIsNone(rows[-1]['ES'])

    def test_up_and_down(self):
        for prices,direction in (([100,101,102,103],'UP'),([103,102,101,100],'DOWN')):
            _,rows=m.build_ema(bars(prices,minutes=15),15,period=2)
            self.assertEqual(rows[-1]['E'],direction);self.assertEqual(rows[-1]['ES'],direction)

    def test_no_use_of_future_bars(self):
        source=bars([100,101,102,103,20],minutes=15)
        short=m.build_ema(source[:4],15,period=2)
        long=m.build_ema(source,15,period=2)
        at=source[3]['end_us']+S
        self.assertEqual(m.row_at(short,at),m.row_at(long,at))

    def test_same_bar_price_position_and_single_bar_ema_slope_are_equivalent(self):
        source=bars([100,110,90,120,105,60,95,95,115,10,100],minutes=15)
        _,rows=m.build_ema(source,15,period=3)
        self.assertTrue(all(r['E']==r['ES'] for r in rows if r['ready']))

    def test_publication_delay_inclusive(self):
        series=m.build_ema(bars([100,101,102],minutes=15),15,period=2)
        end=45*M
        self.assertEqual(m.row_at(series,end-1)['barEndUs'],30*M)
        self.assertIsNone(m.row_at(series,end+S-1))
        self.assertEqual(m.row_at(series,end+S)['barEndUs'],end)

    def test_original_expiry_not_shifted_by_publication_lag(self):
        series=m.build_ema(bars([100,101,102],minutes=60),60,period=2)
        expiry=240*M
        self.assertIsNotNone(m.row_at(series,expiry-1))
        self.assertIsNone(m.row_at(series,expiry))
        self.assertIsNone(m.row_at(series,expiry+S-1))

    def test_no_series_before_first_publication(self):
        series=m.build_ema(bars([100],minutes=15),15,period=2)
        self.assertIsNone(m.row_at(series,15*M))


class PairedFilterTests(unittest.TestCase):
    def test_none_works_with_no_indicator_coverage(self):
        self.assertTrue(m.allows('NONE','UP',{15:None,60:None}))
        self.assertFalse(m.allows('15m-X','UP',{15:None}))

    def test_x_same_readiness_for_price_and_slope(self):
        for mode in ('X','E','ES'):
            self.assertFalse(m.allows('15m-'+mode,'UP',{15:feature(ready=False)}))
        f={15:feature(direction=None,es=None)}
        self.assertTrue(m.allows('15m-X','DOWN',f))
        self.assertFalse(m.allows('15m-E','DOWN',f))

    def test_e_and_es_can_reject_different_directions(self):
        f={15:feature(direction='UP',es=None)}
        self.assertTrue(m.allows('15m-E','UP',f))
        self.assertFalse(m.allows('15m-E','DOWN',f))
        self.assertFalse(m.allows('15m-ES','UP',f))

    def test_rejected_original_candidate_not_replaced(self):
        e=entry('first',100*S)
        decisions={100*S:{15:feature('DOWN',es='DOWN')},101*S:{15:feature()}}
        self.assertEqual(m.filter_entries([e],'15m-E',decisions),[])

    def test_filter_decision_time_not_execution_time(self):
        e=entry('first',100*S)
        decisions={100*S:{15:feature()},e['executionUs']:{15:feature('DOWN',es='DOWN')}}
        self.assertEqual(m.filter_entries([e],'15m-E',decisions),[e])

    def test_filtering_changes_shared_account_sequence(self):
        first=entry('one',100*S,won=False,end=200*S)
        second=entry('two',110*S,won=True,end=210*S)
        decisions={100*S:{15:feature('DOWN',es='DOWN')},110*S:{15:feature()}}
        _,old=m.base.replay([first,second])
        _,new=m.base.replay(m.filter_entries([first,second],'15m-E',decisions))
        self.assertEqual([r['marketId'] for r in old],['one'])
        self.assertEqual([r['marketId'] for r in new],['two'])
        overlay=m.paired_overlay(old,[first,second],decisions,'15m-E')
        self.assertEqual(overlay['kept']['n'],0)
        self.assertEqual(overlay['rejected']['n'],1)

    def test_overlay_accounting_is_partition_of_same_trades(self):
        es=[entry('one',100*S),entry('two',500*S,won=False),entry('three',900*S)]
        ds={e['_emaTime']:{15:feature('UP' if i!=1 else 'DOWN')} for i,e in enumerate(es)}
        summary,ledger=m.base.replay(es)
        result=m.paired_overlay(ledger,es,ds,'15m-E')
        self.assertEqual(result['kept']['n']+result['rejected']['n'],summary['n'])
        self.assertAlmostEqual(result['kept']['netPnl']+result['rejected']['netPnl'],summary['netPnl'])
        self.assertAlmostEqual(result['kept']['fees']+result['rejected']['fees'],summary['fees'])

    def test_single_side_accounts_do_not_equal_shared_union(self):
        es=[entry('up',100*S,'UP'),entry('down',110*S,'DOWN')]
        shared,_=m.base.replay(es)
        singles=[m.base.replay([e for e in es if e['side']==side])[0]['n'] for side in ('UP','DOWN')]
        self.assertEqual(shared['n'],1);self.assertEqual(sum(singles),2)


if __name__ == '__main__':
    unittest.main()

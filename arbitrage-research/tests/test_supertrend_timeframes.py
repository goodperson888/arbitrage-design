import sys
from pathlib import Path
import sqlite3
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'scripts/analysis'))
import analyze_supertrend_timeframes as m

S, M = m.base.SECOND, m.base.MINUTE


def bars(values, tf=1, start=0):
    return [{'open_us':start+i*tf*M,'end_us':start+(i+1)*tf*M,
             'open':v,'high':v+.5,'low':v-.5,'close':v} for i,v in enumerate(values)]


def series(tf=1, end=None, direction='UP', flip='UP'):
    end = tf*M if end is None else end
    return [end], [{'barEndUs':end,'direction':direction,'flip':flip,'atr':1.,'line':99.,'close':100.}]


class AggregationTests(unittest.TestCase):
    def test_five_minute_ohlc_hand_calculation(self):
        data=bars([100,101,99,103,102])
        out, quality=m.aggregate_bars(data,5)
        self.assertEqual(out,[{'open_us':0,'end_us':5*M,'open':100,'high':103.5,'low':98.5,'close':102}])
        self.assertEqual(quality['rejectedIncompleteBuckets'],0)

    def test_fifteen_minute_boundary_is_utc_aligned(self):
        data=bars(list(range(100,130)),start=M)
        out,quality=m.aggregate_bars(data,15)
        self.assertEqual(len(out),1)
        self.assertEqual(out[0]['open_us'],15*M)
        self.assertEqual(quality['rejectedIncompleteBuckets'],2)

    def test_missing_minute_invalidates_only_its_bucket(self):
        data=bars(list(range(100,115)))
        del data[7]
        out,quality=m.aggregate_bars(data,5)
        self.assertEqual([r['open_us'] for r in out],[0,10*M])
        self.assertEqual(quality['rejectedIncompleteBuckets'],1)

    def test_partial_trailing_bar_is_never_used(self):
        out,quality=m.aggregate_bars(bars([100]*7),5)
        self.assertEqual(len(out),1)
        self.assertEqual(out[0]['end_us'],5*M)
        self.assertEqual(quality['rejectedIncompleteBuckets'],1)

    def test_duplicate_or_unordered_source_fails(self):
        data=bars([100,101])
        for bad in ([data[0],data[0]],list(reversed(data))):
            with self.assertRaises(ValueError):m.aggregate_bars(bad,5)

    def test_invalid_ohlc_is_rejected(self):
        data=bars([100]);data[0]['low']=101
        with self.assertRaises(ValueError):m.aggregate_bars(data,1)

    def test_one_minute_identity(self):
        data=bars([100,101,99])
        self.assertEqual(m.aggregate_bars(data,1)[0],data)


class IndicatorTests(unittest.TestCase):
    def test_one_minute_matches_frozen_recursion(self):
        data=bars([100]*14+[104,108,102,95,105,110])
        for factor in (3,2,1.5):
            self.assertEqual(m.build_supertrend(data,1,factor=factor),m.old.build_supertrend(data,14,factor))

    def test_five_minute_rows_accumulate_atr_without_reset(self):
        _,out=m.build_supertrend(bars([100,100,104,108],tf=5),5,period=2,factor=1)
        self.assertEqual(out[1]['atr'],1)
        self.assertEqual(out[2]['atr'],2.75)
        self.assertEqual(out[2]['flip'],'UP')

    def test_missing_high_timeframe_bar_resets_everything(self):
        data=bars([100,100,104],tf=5)+bars([200],tf=5,start=20*M)
        _,out=m.build_supertrend(data,5,period=2,factor=1)
        self.assertIsNone(out[-1]['atr'])
        self.assertIsNone(out[-1]['direction'])
        self.assertIsNone(out[-1]['flip'])

    def test_factor_changes_band_without_changing_atr(self):
        data=bars([100,102])
        _,a=m.build_supertrend(data,1,period=1,factor=3)
        _,b=m.build_supertrend(data,1,period=1,factor=1.5)
        self.assertEqual(a[-1]['atr'],b[-1]['atr'])
        self.assertIsNone(a[-1]['flip'])
        self.assertEqual(b[-1]['flip'],'UP')

    def test_seed_does_not_trade(self):
        _,out=m.build_supertrend(bars([100]*20,tf=15),15)
        self.assertTrue(all(r['direction'] is None and r['flip'] is None for r in out))

    def test_future_aggregated_candle_cannot_change_closed_prefix(self):
        data=bars([100,101,102,103,104]*10)
        short=m.aggregate_bars(data[:27],5)[0]
        full=m.aggregate_bars(data,5)[0]
        a=m.build_supertrend(short,5,period=2,factor=1)[1]
        b=m.build_supertrend(full,5,period=2,factor=1)[1]
        self.assertEqual(a,b[:len(a)])


class TimingAndCombinationTests(unittest.TestCase):
    def test_unclosed_five_minute_bar_is_invisible(self):
        self.assertIsNone(m.row_at(series(5),5,0,5*M-1))
        self.assertIsNotNone(m.row_at(series(5),5,0,5*M))

    def test_publication_lag_keeps_original_expiry(self):
        row=series(15)
        self.assertIsNone(m.row_at(row,15,1000,15*M+S-1))
        self.assertIsNotNone(m.row_at(row,15,1000,15*M+S))
        self.assertIsNone(m.row_at(row,15,1000,30*M))

    def test_high_timeframe_can_survive_several_fast_minutes(self):
        self.assertIsNotNone(m.row_at(series(15),15,1000,23*M))
        self.assertIsNone(m.row_at(series(1),1,1000,3*M))

    def test_combo_requires_actual_fast_flip(self):
        spec=m.SPECS['C_5m_k3']
        rows={(5,'3'):series(5)[1][0],(1,'3'):series(1,flip=None)[1][0]}
        self.assertIsNone(m.choose_signal(spec,rows))
        rows[(1,'3')]['flip']='UP'
        self.assertEqual(m.choose_signal(spec,rows),'UP')
        rows[(5,'3')]['direction']='DOWN'
        self.assertIsNone(m.choose_signal(spec,rows))

    def test_combo_checks_both_sources_at_decision_time(self):
        pack={(5,'3'):series(5),(1,'3'):series(1,end=5*M)}
        callback=m.make_callback(pack)
        self.assertEqual(callback(5*M)['C_5m_k3_L0'],'UP')
        self.assertIsNone(callback(5*M)['C_5m_k3_L1000'])
        self.assertEqual(callback(5*M+S)['C_5m_k3_L1000'],'UP')
        self.assertIsNone(callback(6*M)['C_5m_k3_L0'])

    def test_entry_features_retain_distinct_original_ends(self):
        pack={(15,'2'):series(15,end=15*M),(1,'2'):series(1,end=17*M)}
        e={'strategy':'C_15m_k2_L1000','decisionUs':17*M+S}
        features=m.entry_features(e,pack)
        self.assertEqual([r['barEndUs'] for r in features['indicatorSources']],[15*M,17*M])

    def test_all_24_rules_retained(self):
        self.assertEqual(len(m.SPECS),24)
        self.assertEqual(sum(s['mode']=='C' for s in m.SPECS.values()),6)

    def test_zero_groups_cannot_qualify(self):
        results,ledgers=m.analyze([])
        self.assertEqual(len(results),10368)
        self.assertTrue(all(r['n']==0 for r in results))
        self.assertFalse(m.qualifies(results,'P_1m_k3','BOTH'))


class ExtractionTests(unittest.TestCase):
    def test_only_available_signal_can_send_and_miss_is_not_retried(self):
        con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
        con.execute('CREATE TABLE snapshots(market_id TEXT,source_us INTEGER,received_us INTEGER,'
                    'outcome TEXT,best_ask REAL,asks_json TEXT)')
        for at,price in ((60*S,.4),(60*S+200000,.6),(61*S,.4)):
            con.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?)',('one',at,at,'UP',price,f'[[{price},100]]'))
        markets=[{'market_id':'one','start_us':0,'end_us':300*S,'winner':'UP','label_available_us':360*S}]
        with tempfile.TemporaryDirectory() as folder:
            entries,_,_=m.base.extract_entries(con,markets,{},([],[]),None,Path(folder),(),include_builtin=False,
                                               signal_callback=m.make_callback({(1,'3'):series()}))
        con.close()
        self.assertFalse(any(e['strategy']=='F_1m_k3_L0' and e['delayMs']==300 for e in entries))
        self.assertTrue(any(e['strategy']=='F_1m_k3_L1000' and e['delayMs']==300 for e in entries))


if __name__=='__main__':unittest.main()

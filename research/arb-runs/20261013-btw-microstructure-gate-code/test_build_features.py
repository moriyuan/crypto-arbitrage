import unittest
import numpy as np
from build_features import EVENT_DTYPE, RateStream, Persistence, policy_masks

S=10**9

def events(rows):
    x=np.zeros(len(rows),dtype=EVENT_DTYPE)
    for i,(ns,venue,kind,count) in enumerate(rows):x[i]=(ns,venue,kind,b'\0\0',count)
    return x


class FeatureTests(unittest.TestCase):
    def test_window_boundaries_and_exact_warmup(self):
        e=events([(0,0,0,0),(0,0,2,7),(55*S,0,2,2),(60*S,0,2,3)])
        r=RateStream(e,True).at(np.array([60*S-1,60*S]))
        self.assertFalse(r['warm'][0,0]);self.assertTrue(r['warm'][1,0])
        self.assertEqual(r['count5'][1,0],3);self.assertEqual(r['count60'][1,0],5)
        self.assertAlmostEqual(r['ratio'][1,0],7.2)

    def test_same_ns_complete_ordered_batch(self):
        e=events([(0,0,0,0),(60*S,0,2,9),(60*S,0,0,0),(60*S,0,2,2),(60*S,0,2,3)])
        r=RateStream(e,True).at(np.array([60*S]))
        self.assertEqual(r['count5'][0,0],5);self.assertFalse(r['warm'][0,0])

    def test_future_event_never_enters_current_count(self):
        e=events([(0,0,0,0),(59*S,0,2,2),(60*S+1,0,2,999)])
        r=RateStream(e,False).at(np.array([60*S]))
        self.assertEqual(r['count5'][0,0],2);self.assertEqual(r['count60'][0,0],2)

    def test_disconnect_and_recovery_clear_windows(self):
        e=events([(0,0,0,0),(10*S,0,2,9),(20*S,0,1,0),(30*S,0,0,0),(89*S,0,2,2)])
        r=RateStream(e,False).at(np.array([25*S,89*S,90*S]))
        self.assertFalse(r['initialized'][0,0]);self.assertEqual(r['count60'][0,0],0)
        self.assertFalse(r['warm'][1,0]);self.assertTrue(r['warm'][2,0])
        self.assertEqual(r['count60'][2,0],2)

    def test_zero_book_delta_does_not_inflate_and_zero_denominator_invalid(self):
        r=RateStream(events([(0,0,0,0),(60*S,0,2,0)]),True).at(np.array([60*S]))
        self.assertTrue(r['warm'][0,0]);self.assertFalse(r['rate_valid'][0,0]);self.assertTrue(np.isnan(r['ratio'][0,0]))
        with self.assertRaises(ValueError):RateStream(events([(0,0,0,0),(S,0,2,0)]),False)

    def test_uninitialized_delta_rejected(self):
        with self.assertRaises(ValueError):RateStream(events([(0,0,2,2)]),True)

    def test_snapshot_resets_but_trade_prints_do_not(self):
        r=RateStream(events([(0,0,0,0),(60*S,0,2,2),(61*S,0,0,0),(62*S,0,2,1)]),True).at(np.array([60*S,62*S]))
        self.assertTrue(r['warm'][0,0]);self.assertFalse(r['warm'][1,0]);self.assertEqual(r['count60'][1,0],1)

    def test_persistence_exact_50ms_and_latest_quote(self):
        p=Persistence();ns=np.array([0,25_000_000,50_000_000,75_000_000])
        prices=np.tile([10000,9990,10060,10050],(4,1));prices[-1]=[10000,9990,10010,10000]
        x=p.at(ns,prices,np.ones(4,bool),np.zeros(4,bool))
        self.assertEqual(x['persistence_ns'].tolist(),[0,25_000_000,50_000_000,0])
        self.assertEqual(x['raw_direction'].tolist(),[0,0,0,-1])

    def test_invalid_recovery_and_short_reverse_reset(self):
        p=Persistence();prices=np.array([[10000,9990,10060,10050],[10000,9990,10060,10050],[10000,9990,10060,10050],[10060,10050,10000,9990],[10000,9990,10060,10050]])
        x=p.at(np.arange(5)*25_000_000,prices,np.array([1,0,1,1,1],bool),np.zeros(5,bool))
        self.assertEqual(x['persistence_ns'].tolist(),[0,0,0,0,0])

    def test_gap_snapshot_reset_and_chunk_continuity(self):
        p=Persistence();prices=np.tile([10000,9990,10060,10050],(2,1))
        a=p.at(np.array([0,100_000_000]),prices,np.ones(2,bool),np.zeros(2,bool))
        self.assertEqual(a['persistence_ns'].tolist(),[0,100_000_000])
        b=p.at(np.array([200_000_001,250_000_001]),prices,np.ones(2,bool),np.array([False,True]))
        self.assertEqual(b['persistence_ns'].tolist(),[0,0])
        c=p.at(np.array([300_000_001]),prices[:1],np.ones(1,bool),np.zeros(1,bool))
        self.assertEqual(c['persistence_ns'].tolist(),[50_000_000])

    def test_policy_both_venue_and_baseline(self):
        rows=[{'id':'baseline','require_book_rate':False,'require_trade_rate':False,'min_book_rate_ratio':0,'min_trade_rate_ratio':0,'min_spread_persistence_ms':0},
              {'id':'trade','require_book_rate':False,'require_trade_rate':True,'min_book_rate_ratio':0,'min_trade_rate_ratio':1,'min_spread_persistence_ms':50}]
        f={'trade_initialized':np.ones((3,2),bool),'trade_warm':np.ones((3,2),bool),'trade_rate_valid':np.ones((3,2),bool),'trade_count60':np.ones((3,2),np.int64),'trade_ratio':np.array([[1,1],[1,.9999],[2,2]]),'persistence_ns':np.array([50_000_000,50_000_000,49_999_999])}
        self.assertEqual(policy_masks(f,rows)[0].tolist(),[3,1,1])


if __name__=='__main__':unittest.main()

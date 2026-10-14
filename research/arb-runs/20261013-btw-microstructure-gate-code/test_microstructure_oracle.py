import unittest
import numpy as np
from microstructure_oracle import direct_rate, persistence_step, allowed_mask, Rates, BOOK

class OracleTests(unittest.TestCase):
 def test_indexed_rates_match_direct_including_reset_ordinal(self):
  events=[(0,0,0),(1,2,2),(55_000_000_000,2,3),(60_000_000_000,2,5),(61_000_000_000,2,8),(61_000_000_000,0,0),(61_000_000_000,2,4),(122_000_000_000,1,0),(123_000_000_000,0,0),(183_000_000_000,2,2)]
  source=np.zeros(len(events),BOOK)
  for i,(t,k,c) in enumerate(events):source[i]=(t,0,k,c)
  times=np.array([-1,0,1,59_999_999_999,60_000_000_000,61_000_000_000,121_000_000_000,122_000_000_000,183_000_000_000],np.int64)
  actual=Rates(source).query(times)
  for i,t in enumerate(times):
   expected=direct_rate(events,int(t))
   for j,key in enumerate(('count5','count60','initialized','warm','rate_valid','ratio')):
    self.assertTrue(actual[key][i,0]==expected[j] or (key=='ratio' and np.isnan(actual[key][i,0]) and np.isnan(expected[j])),(t,key))
  self.assertFalse(actual['initialized'][:,1].any())
 def test_exclusive_windows_and_warm_boundary(self):
  e=[(0,0,0),(1,2,2),(55_000_000_000,2,3),(60_000_000_000,2,5)]
  self.assertEqual(direct_rate(e,60_000_000_000)[:2],(5,10))
  self.assertFalse(direct_rate(e,59_999_999_999)[3]);self.assertTrue(direct_rate(e,60_000_000_000)[3])
 def test_same_ns_order_reset_and_recovery(self):
  e=[(0,0,0),(60_000_000_000,2,8),(60_000_000_000,0,0),(60_000_000_000,2,3)]
  self.assertEqual(direct_rate(e,60_000_000_000)[:2],(3,3))
  self.assertFalse(direct_rate(e,60_000_000_000)[3])
  e.append((60_000_000_000,1,0));self.assertEqual(direct_rate(e,60_000_000_000)[:2],(0,0));self.assertFalse(direct_rate(e,60_000_000_000)[2])
 def test_direction_and_gap_boundary(self):
  state=(None,-1,None)
  state,duration=persistence_step(state,1,True,0,False);self.assertEqual(duration,0)
  state,duration=persistence_step(state,100_000_001,True,0,False);self.assertEqual(duration,100_000_000)
  state,duration=persistence_step(state,200_000_002,True,0,False);self.assertEqual(duration,0)
  state,duration=persistence_step(state,250_000_002,True,1,False);self.assertEqual(duration,0)
 def test_invalid_and_snapshot_reset(self):
  s,d=persistence_step((1,0,1),50_000_001,False,-1,False);self.assertEqual(d,0)
  s,d=persistence_step((1,0,1),50_000_001,True,0,True);self.assertEqual(d,0)
 def test_ratio_and_persistence_exact_thresholds(self):
  policies=[{'require_book_rate':False,'require_trade_rate':False,'min_book_rate_ratio':0,'min_trade_rate_ratio':0,'min_spread_persistence_ms':0},{'require_book_rate':True,'require_trade_rate':False,'min_book_rate_ratio':1,'min_trade_rate_ratio':0,'min_spread_persistence_ms':50}]
  self.assertEqual(allowed_mask([True,True],[False,False],[1,1],[float('nan')]*2,50_000_000,policies),3)
  self.assertEqual(allowed_mask([True,True],[False,False],[1,.999],[float('nan')]*2,50_000_000,policies),1)
  self.assertEqual(allowed_mask([True,True],[False,False],[1,1],[float('nan')]*2,49_999_999,policies),1)

if __name__=='__main__':unittest.main()

import unittest
import numpy as np
from audit_gate_features import expected_features, expected_mask, cost_bps

class FeatureOracleTests(unittest.TestCase):
 def grid(self,n=3700):
  g=np.ones((n,6),dtype=np.int64);g[:,0]=np.arange(n)*10**9;g[:,1]=10000+(np.arange(n)%7)*10;g[:,2]=10000+(np.arange(n)%11)*10;g[:,3]=3;return g
 def test_future_values_cannot_change_past(self):
  g=self.grid();a=expected_features(g);g[3651:,1]*=2;b=expected_features(g)
  for k in ('basis_sigma_bps','std_short_bps','std_long_bps','cooling_ratio'):np.testing.assert_allclose(a[k][:3651],b[k][:3651],equal_nan=True)
 def test_full_clock_warmup(self):
  x=expected_features(self.grid());self.assertTrue(np.isnan(x['std_short_bps'][299]).all());self.assertTrue(np.isfinite(x['std_short_bps'][300]).all());self.assertTrue(np.isnan(x['std_long_bps'][3599]).all());self.assertTrue(np.isfinite(x['std_long_bps'][3600]).all())
 def test_invalid_current_row_rejects(self):
  g=self.grid();g[3650,3]=0;x=expected_features(g);self.assertFalse(x['basis_valid'][3650]);self.assertFalse(x['cooling_valid'][3650])
 def test_segment_and_gap_excluded(self):
  g=self.grid();g[3590,3]=0;g[3640:,4]=2;x=expected_features(g)
  self.assertLess(x['basis_count'][3650],20);self.assertLess(x['count_short'][3650,0],300)
 def test_flat_long_variance_rejected(self):
  g=self.grid();g[:,1:3]=10000;x=expected_features(g);self.assertFalse(x['cooling_valid'][-1]);self.assertTrue(np.isnan(x['cooling_ratio'][-1]))
 def test_cost_exact_normalization(self):
  self.assertAlmostEqual(cost_bps([101,99,102,98]),612)
 def test_baseline_and_coverage_controls(self):
  policies=[{'id':'baseline','require_basis':False,'require_cooling':False,'min_basis_cost_ratio':0,'min_cooling_ratio':0},{'id':'coverage','require_basis':True,'require_cooling':False,'min_basis_cost_ratio':0,'min_cooling_ratio':0},{'id':'cost','require_basis':True,'require_cooling':False,'min_basis_cost_ratio':.5,'min_cooling_ratio':0}]
  self.assertEqual(expected_mask(False,False,1.,1.,None,policies),1)
  self.assertEqual(expected_mask(True,False,1.,1.,None,policies),3)
  self.assertEqual(expected_mask(True,False,1.,1.,2.,policies),7)
  self.assertEqual(expected_mask(True,False,1.,1.,2.01,policies),3)

if __name__=='__main__':unittest.main()

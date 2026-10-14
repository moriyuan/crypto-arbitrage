import unittest
import numpy as np
from build_features import compute_features, interval_returns, rolling_std, cost_proxy, gate_masks


def grid(n=3701):
    x=np.zeros((n,6),dtype=np.int64)
    x[:,0]=np.arange(n,dtype=np.int64)*10**9
    x[:,1:3]=1_000_000
    x[:,3]=3
    x[:,4:6]=1
    return x


class FeatureTests(unittest.TestCase):
    def test_numeric_return_and_population_std(self):
        g=grid(4);g[:,1]=[1_000_000,1_100_000,990_000,1_089_000]
        ix,r=interval_returns(g,1)
        np.testing.assert_allclose(r[:,0],1e4*np.log([1.1,.9,1.1]),rtol=1e-13)
        s,c=rolling_std(np.array([np.nan,1.,3.,5.]),3,3)
        self.assertEqual(c[-1],3);self.assertAlmostEqual(s[-1],np.std([1.,3.,5.]))

    def test_no_gap_or_segment_crossing(self):
        g=grid(46);g[7,3]=1;g[20,4]=2
        ix,r=interval_returns(g,15)
        self.assertTrue(np.isnan(r[0,1]))
        self.assertTrue(np.isnan(r[1,0]))
        self.assertTrue(np.isfinite(r[2]).all())

    def test_zero_long_denominator_invalid(self):
        f=compute_features(grid())
        self.assertEqual(f['count_long'][-1,0],3600)
        self.assertTrue(np.isnan(f['cooling_ratio'][-1]))
        self.assertFalse(f['cooling_valid'][-1])
        self.assertTrue(f['basis_valid'][-1])
        self.assertEqual(f['basis_sigma_bps'][-1],0)

    def test_current_grid_invalid_rejects_both_features(self):
        g=grid();g[:,1]=1_000_000+(np.arange(len(g))%2)*1000
        g[:,2]=1_000_000+(np.arange(len(g))%3)*1000
        g[-1,3]=1
        f=compute_features(g)
        self.assertFalse(f['basis_valid'][-1]);self.assertFalse(f['cooling_valid'][-1])

    def test_15_second_endpoints_and_full_window(self):
        f=compute_features(grid(316))
        self.assertFalse(f['basis_valid'][299]);self.assertTrue(f['basis_valid'][300])
        self.assertEqual(f['basis_count'][300],20)
        self.assertEqual(f['basis_end_ns'][314],300*10**9)
        self.assertEqual(f['basis_end_ns'][315],315*10**9)

    def test_future_changes_do_not_change_prior_features(self):
        a=grid();b=a.copy();b[3601:,1]=2_000_000
        x=compute_features(a);y=compute_features(b)
        for key in x:
            np.testing.assert_equal(x[key][:3601],y[key][:3601])

    def test_coverage_boundary(self):
        values=np.r_[np.nan,np.arange(300,dtype=float)]
        values[1:61]=np.nan
        s,c=rolling_std(values,300,240)
        self.assertEqual(c[-1],240);self.assertTrue(np.isfinite(s[-1]))
        values[61]=np.nan;s,c=rolling_std(values,300,240)
        self.assertEqual(c[-1],239);self.assertTrue(np.isnan(s[-1]))

    def test_basis_16_of_20_boundary(self):
        g=grid(301);g[[7,37,67,97],3]=0
        f=compute_features(g)
        self.assertEqual(f['basis_count'][-1],16);self.assertTrue(f['basis_valid'][-1])
        g[127,3]=0;f=compute_features(g)
        self.assertEqual(f['basis_count'][-1],15);self.assertFalse(f['basis_valid'][-1])

    def test_cost_numeric_and_invalid_quotes(self):
        p=np.array([[101,99,201,199],[99,101,201,199],[101,99,201,199]],dtype=np.int64)
        c=cost_proxy(p,np.array([True,True,False]))
        self.assertAlmostEqual(c[0],10000*(4+.0003*600)/150)
        self.assertTrue(np.isnan(c[1]));self.assertTrue(np.isnan(c[2]))

    def test_policy_bits_and_no_future_lookup(self):
        f=compute_features(grid(316))
        policies=[{'id':'baseline','require_basis':False,'require_cooling':False,'min_basis_cost_ratio':0,'min_cooling_ratio':0},
                  {'id':'basis','require_basis':True,'require_cooling':False,'min_basis_cost_ratio':0,'min_cooling_ratio':0}]
        p=np.tile([101,99,101,99],(3,1))
        masks,_,_=gate_masks(np.array([-1,299_999_999_999,300_000_000_000]),p,np.ones(3,dtype=bool),f,policies)
        self.assertEqual(masks.tolist(),[1,1,3])

    def test_threshold_equality_is_inclusive(self):
        f={'ns':np.array([0]),'basis_valid':np.array([True]),'cooling_valid':np.array([True]),
           'basis_sigma_bps':np.array([6.]),'cooling_ratio':np.array([.75])}
        policies=[{'id':'baseline'}, {'id':'joint','require_basis':True,'require_cooling':True,
                  'min_basis_cost_ratio':.5,'min_cooling_ratio':.75}]
        args=(np.array([0]),np.array([[100,100,100,100]]),np.array([True]))
        self.assertEqual(gate_masks(*args,f,policies)[0].tolist(),[3])
        f['basis_sigma_bps'][0]=np.nextafter(6.,0.)
        self.assertEqual(gate_masks(*args,f,policies)[0].tolist(),[1])


if __name__=='__main__':unittest.main()

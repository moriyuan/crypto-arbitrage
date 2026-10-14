import importlib.util
import unittest
from decimal import Decimal
from pathlib import Path

spec = importlib.util.spec_from_file_location('activity_features', Path(__file__).with_name('activity_features.py'))
m = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = m
spec.loader.exec_module(m)

S = 10**9
U = 10**8

class ActivityTests(unittest.TestCase):
    def test_left_open_right_closed_and_same_ns(self):
        r = m.RollingActivity(60 * S)
        for v in range(2): r.resume(v, 0)
        r.trade(0, 0, 2, 5 * U)
        r.trade(0, 60 * S, 3, 7 * U)
        f = r.feature(60 * S)
        self.assertEqual((f.count_a, f.turnover_a, f.age_a_ns), (3, 7 * U, 0))
        self.assertEqual(f.warm_mask, 3)
    def test_no_future_read(self):
        r = m.RollingActivity(60 * S)
        r.resume(0, 0)
        with self.assertRaises(ValueError): r.trade(0, -1, 1, U)
        r.trade(0, 10 * S, 1, U)
        with self.assertRaises(ValueError): r.feature(9 * S)
    def test_disconnect_warmup_and_no_stale_window(self):
        r = m.RollingActivity(60 * S)
        r.resume(0, 0)
        r.trade(0, 50 * S, 10, 20 * U)
        r.disconnect(0, 60 * S)
        f = r.feature(61 * S)
        self.assertEqual((f.count_a, f.turnover_a, f.age_a_ns, f.warm_mask), (0, 0, -1, 0))
        r.resume(0, 62 * S)
        r.trade(0, 63 * S, 1, U)
        self.assertFalse(r.feature(122 * S - 1).warm_mask & 1)
        self.assertEqual(r.feature(122 * S).warm_mask, 1)
        self.assertEqual(r.feature(123 * S).count_a, 0)
    def test_gate_inclusive_and_absent(self):
        f=m.Feature(60*S, 4, 4, 10*U, 10*U, 5*S, 5*S, 3)
        p=m.GateConfig(min_count=4,min_turnover_1e8=10*U,max_trade_age_ns=5*S)
        self.assertTrue(m.allow_entry(f,p))
        self.assertFalse(m.allow_entry(m.Feature(*f.as_tuple()[:-1], 1),p))
        self.assertFalse(m.allow_entry(m.Feature(60*S,4,4,10*U,10*U,-1,5*S,3),p))
        self.assertFalse(m.allow_entry(f,m.GateConfig(min_count=5,min_turnover_1e8=10*U,max_trade_age_ns=5*S)))
    def test_decimal_quantities_exact_and_no_rounding(self):
        self.assertEqual(m.units_1e8('1.492324'),149232400)
        self.assertEqual(m.units_1e8('0.00000001'),1)
        self.assertEqual(m.units_1e8('3'),300000000)
        self.assertEqual(m.notional_1e8(m.units_1e8('1.23456'),m.units_1e8('3')),370368000)
        with self.assertRaises(ValueError): m.units_1e8('0.000000001')
        with self.assertRaises(ValueError): m.units_1e8('NaN')
        with self.assertRaises(ValueError): m.notional_1e8(1,1)
    def test_binary_contract_sizes(self):
        self.assertEqual(m.TRADE.size,64)
        self.assertEqual(m.FEATURE.size,56)
        self.assertEqual(m.ACT_EVENT.size,24)
    def test_resume_idempotent_and_reject_out_of_order(self):
        r=m.RollingActivity(60*S)
        r.resume(0,0);r.resume(0,50*S)
        self.assertEqual(r.feature(60*S).warm_mask,1)
        with self.assertRaises(ValueError): r.disconnect(0,59*S)
    def test_activity_does_not_count_historical_snapshot(self):
        self.assertTrue(m.is_live_trade('bybit',{'type':'snapshot'}))
        self.assertFalse(m.is_live_trade('bitget',{'action':'snapshot'}))
        self.assertTrue(m.is_live_trade('bitget',{'action':'update'}))

if __name__=='__main__': unittest.main()

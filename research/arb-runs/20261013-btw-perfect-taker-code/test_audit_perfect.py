import unittest
from audit_perfect import DeepScenario, crossings, decimal_units, funding_guard


def state():
    s = DeepScenario.__new__(DeepScenario)
    s.id = 'synthetic'
    s.price_decimals, s.money_decimals = 6, 10
    s.initial = 1000 * 10**10
    s.cash = [s.initial, s.initial]
    s.entry_bps, s.tp, s.sl, s.hard = 30, 20, 10, 100
    s.hold = 15 * 10**9
    s.position = None
    return s


class AuditBoundaryTests(unittest.TestCase):
    def test_funding_announcement_and_exact_guard_boundary(self):
        seconds = 10**9
        announced = {v: ([10 * seconds], [200 * seconds]) for v in ('bybit', 'bitget')}
        self.assertTrue(funding_guard(10 * seconds - 1, 15000, announced))
        self.assertFalse(funding_guard(125 * seconds - 1, 15000, announced))
        self.assertTrue(funding_guard(125 * seconds, 15000, announced))
        self.assertTrue(funding_guard(80 * seconds, 60000, announced))

    def test_actual_funding_inclusive_intervals(self):
        self.assertEqual(crossings(10, 20, [9, 10, 20, 21]), [10, 20])
        self.assertEqual(crossings(11, 19, [10, 20]), [])

    def test_exact_002_003_units_and_no_rounding(self):
        self.assertEqual(decimal_units('0.10173', 5), 10173)
        self.assertEqual(decimal_units('0.101731', 6), 101731)
        self.assertEqual(decimal_units('0.0000305193', 10), 305193)
        with self.assertRaises(AssertionError):
            decimal_units('0.101731', 5)

    def test_entry_uses_full_opposing_depth_and_common_step(self):
        s = state()
        prices = [100000, 99900, 100600, 100500]
        self.assertEqual(s.best_offer(prices, [219, 500, 500, 317]), (0, 210, 100000, 100500))
        self.assertEqual(s.best_offer(prices, [19, 500, 500, 317]), None)
        self.assertEqual(s.best_offer(prices, [9000, 500, 500, 9000]), (0, 2480, 100000, 100500))

    def test_fee_net_risk_relative_to_entry_baseline(self):
        s = state()
        s.position = {'long': 0, 'qty': 1000, 'opening': [100000, 100500]}
        original = [100000, 99900, 100600, 100500]
        s.position['baseline'] = s.liquidation_net(original)
        self.assertEqual(s.risk(original), '')
        # The decreased closing fee leaves the 100-unit drop just above the stop.
        self.assertEqual(s.risk([100000, 99800, 100600, 100500]), '')
        self.assertEqual(s.risk([100000, 99799, 100600, 100500]), 'stop_loss')
        self.assertEqual(s.risk([100500, 100400, 100400, 100300]), 'take_profit')

    def test_invalid_book_timeout_latches_until_first_fresh_book(self):
        s = state()
        s.position = {'id': 1, 'ns': 1, 'pending': '', 'trigger': None}
        s.decisions = 0
        s.day_ended = s.loss_halted = False
        s.entry_start = 10**18
        s.cooldown_until = 0
        s.entry_rows = {}
        exit_ns = 15 * 10**9 + 10
        s.exit_rows = {exit_ns: []}
        s.mark = lambda prices, valid: None
        seen = []
        def close(ns, prices, displays):
            seen.append((ns, s.position['pending'], s.position['trigger']))
            s.position = None
        s.close = close
        s.tick(15 * 10**9 + 1, False, 0, True, [0]*4, [0]*4, False, [10**18]*2)
        self.assertEqual(seen, [])
        s.tick(exit_ns, True, 0, True, [1]*4, [1]*4, False, [10**18]*2)
        self.assertEqual(seen, [(exit_ns, 'holding_timeout', 15 * 10**9 + 1)])


if __name__ == '__main__':
    unittest.main()

from decimal import Decimal as D
import unittest
from microstructure_attribution import decompose


class MicrostructureTests(unittest.TestCase):
    def test_basis_reverts_but_spreads_and_fees_lose_money(self):
        got = decompose([101, 99, 104, 102], [102, 100, 103, 101], 10, 'bybit')
        self.assertEqual(got, dict(mid_basis_convergence=D(20), bid_ask_crossing_cost=D(40),
                                   gross_pnl=D(-20), fees=D('1.218'), net=D('-21.218')))

    def test_inverse_direction_has_same_attribution(self):
        got = decompose([104, 102, 101, 99], [103, 101, 102, 100], 10, 'bitget')
        self.assertEqual(got['mid_basis_convergence'], D(20))
        self.assertEqual(got['net'], D('-21.218'))

    def test_rejects_crossed_quotes(self):
        with self.assertRaises(ValueError):
            decompose([99, 101, 104, 102], [102, 100, 103, 101], 10, 'bybit')


if __name__ == '__main__':
    unittest.main()

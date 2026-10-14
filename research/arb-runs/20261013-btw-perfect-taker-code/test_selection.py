import unittest
from selection import choose


def row(name, a, b, cycles=10, funding='VERIFIED_NO_SETTLEMENT', residual=0):
    return {'id': name, 'days': {
        '2026-08-01': dict(net=str(a), drawdown='2', cycles=cycles,
                          funding_status=funding, unresolved_qty=residual, accounting_pass=True),
        '2026-09-01': dict(net=str(b), drawdown='1', cycles=cycles,
                          funding_status=funding, unresolved_qty=residual, accounting_pass=True)}}


class SelectionTests(unittest.TestCase):
    dates = ['2026-08-01', '2026-09-01']

    def test_prefers_worst_day_not_big_aggregate(self):
        selected, ranked = choose([row('fragile', 100, -1), row('steady', 2, 2)], self.dates)
        self.assertEqual(selected['id'], 'steady')

    def test_excludes_missing_funding_residual_and_small_samples(self):
        rows = [row('unknown', 100, 100, funding='UNKNOWN'), row('residual', 100, 100, residual=10),
                row('few', 100, 100, cycles=9), row('valid', 1, 1)]
        self.assertEqual(choose(rows, self.dates)[0]['id'], 'valid')

    def test_all_ineligible_does_not_relax_constraints(self):
        self.assertIsNone(choose([row('few', 100, 100, cycles=1)], self.dates)[0])

    def test_negative_best_not_filtered_into_a_false_profitable_claim(self):
        best, _ = choose([row('a', -1, -2), row('b', -1, -3)], self.dates)
        self.assertEqual(best['id'], 'a')
        self.assertFalse(best['profitable_both_fit_days'])

    def test_tie_is_deterministic(self):
        self.assertEqual(choose([row('z', 1, 1), row('a', 1, 1)], self.dates)[0]['id'], 'a')

    def test_rejects_a_third_evaluation_day(self):
        extra = row('x', 1, 1)
        extra['days']['2026-10-01'] = extra['days']['2026-09-01'].copy()
        with self.assertRaises(ValueError):
            choose([extra], self.dates)


if __name__ == '__main__':
    unittest.main()

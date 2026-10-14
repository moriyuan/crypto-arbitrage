"""Focused descriptive analysis tests; no historical run or production selection."""
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
import numpy as np

from summarize_activity import (ACTIVITY, activity_at_entries, admitted, baseline_admission,
                                decompose, rejection_reasons, uniform_coverage)


def activity(ns, count=10, turnover=100, age=5_000_000_000, warm=3):
    return (ns, count, count, turnover, turnover, age, age, warm)


class ActivitySummaryTests(unittest.TestCase):
    def test_all_inclusive_activity_boundaries(self):
        limits = (10, 10, 100, 100, 5_000_000_000)
        self.assertTrue(admitted(activity(1), limits))
        self.assertFalse(admitted(activity(1, age=5_000_000_001), limits))
        self.assertFalse(admitted(activity(1, age=-1), limits))
        self.assertFalse(admitted(activity(1, warm=1), limits))
        self.assertFalse(admitted(activity(1, count=9), limits))
        self.assertFalse(admitted(activity(1, turnover=99), limits))
        self.assertEqual(rejection_reasons(activity(1, warm=1), limits), ['bitget_warm'])

    def test_lookup_uses_exact_native_ns_and_not_future_or_nearest(self):
        limits = {'q25': (10, 10, 100, 100, 5_000_000_000)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'activity.bin'
            path.write_bytes(b'BTWACT01'+b''.join(ACTIVITY.pack(*r) for r in (activity(100), activity(200, count=9), activity(300))))
            rows, coverage, count = activity_at_entries(path, limits, {200}, 200)
            self.assertFalse(admitted(rows[200], limits['q25']))
            self.assertEqual(count, 3)
            self.assertEqual(coverage['q25']['activity_pass_rows'], 1)
            self.assertEqual(coverage['q25']['post_noon_native_rows'], 2)
            with self.assertRaisesRegex(ValueError, 'timestamp missing'):
                activity_at_entries(path, limits, {201}, 200)

    def test_reject_truncated_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'activity.bin'
            path.write_bytes(b'BTWACT01'+b'partial')
            with self.assertRaisesRegex(ValueError, 'length'):
                activity_at_entries(path, {'q25': (1, 1, 1, 1, 5)}, set(), 1)

    def test_blocked_winner_loser_are_original_q25_outcomes(self):
        base = [dict(cycle=i, entry_ns=i*100, long_venue='bybit', qty=10, net_usdt=net)
                for i, net in enumerate((D(-1), D(2), D(-3), D(1)), 1)]
        actual = [dict(cycle=1, entry_ns=300, long_venue='bybit', qty=20, net_usdt=D(-4)),
                  dict(cycle=2, entry_ns=500, long_venue='bitget', qty=10, net_usdt=D(5))]
        observations = {ns: activity(ns, count=5 if ns <= 200 else 10) for ns in (100, 200, 300, 400, 500)}
        result = baseline_admission(base, actual, observations, (10, 10, 100, 100, 5_000_000_000))
        self.assertEqual(result['blocked_q25_entries']['entries'], 2)
        self.assertEqual(result['blocked_q25_entries']['original_q25_net_winners'], 1)
        self.assertEqual(result['blocked_q25_entries']['original_q25_net_losers'], 1)
        self.assertEqual(result['blocked_q25_entries']['original_q25_net_usdt'], D(1))
        self.assertEqual(result['matched_entries'], 1)
        self.assertEqual(result['matched_with_different_qty'], 1)
        self.assertEqual(result['new_actual_entries'], 1)
        self.assertEqual(result['passed_but_not_actual_entries']['entries'], 1)

    def test_an_actual_blocked_entry_is_an_error(self):
        entry = dict(cycle=1, entry_ns=100, long_venue='bybit', qty=10, net_usdt=D(-1))
        with self.assertRaisesRegex(ValueError, 'violates'):
            baseline_admission([entry], [entry], {100: activity(100, count=5)}, (10, 10, 100, 100, 5_000_000_000))

    def test_uniform_time_is_equal_seconds_and_end_exclusive(self):
        start = 1_000_000_000
        data = np.array([activity(start+i*10**9, count=count) for i, count in enumerate((10, 9, 20, 1000))], dtype='int64')
        gates = {'q25': (5, 5, 100, 100, 5_000_000_000), 'q50': (10, 10, 100, 100, 5_000_000_000), 'q75': (15, 15, 100, 100, 5_000_000_000)}
        result = uniform_coverage(data, gates, start, start+3*10**9)
        self.assertEqual(result['q25']['activity_allowed_seconds'], 3)
        self.assertEqual(result['q50']['activity_allowed_seconds'], 2)
        self.assertEqual(result['q75']['activity_allowed_seconds'], 1)
        self.assertEqual(result['q50']['activity_total_seconds'], 3)
        self.assertEqual(result['q50']['activity_time_fraction'], D(2)/3)
        with self.assertRaisesRegex(ValueError, 'every second'):
            uniform_coverage(data[[0, 2, 3]], gates, start, start+3*10**9)

    def test_midpoint_decomposition_at_micro_price_precision(self):
        unit = D('.000001')
        entry = [D(p)*unit for p in (1000001, 999999, 1004001, 1003999)]
        close = [D(p)*unit for p in (1001001, 1000999, 1003001, 1002999)]
        value = decompose(entry, close, 10, 'bybit')
        self.assertEqual(value['mid_basis_convergence'], D('.02'))
        self.assertEqual(value['bid_ask_crossing_cost'], D('.00004'))
        self.assertEqual(value['gross_pnl'], D('.01996'))
        self.assertEqual(value['fees'], D('.012024000'))
        self.assertEqual(value['net'], D('.007936'))
        reverse = decompose(entry[2:]+entry[:2], close[2:]+close[:2], 10, 'bitget')
        self.assertEqual(reverse, value)


if __name__ == '__main__':
    unittest.main()

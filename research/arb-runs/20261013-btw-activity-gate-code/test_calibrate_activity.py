import unittest
import numpy as np

from calibrate_activity import higher_quantiles, training_rows, validate_daily_features


class CalibrationTests(unittest.TestCase):
    def test_hand_computed_higher_quantiles(self):
        x = np.array([[1, 100, 9, 1000], [2, 200, 7, 2000],
                      [3, 300, 5, 3000], [4, 400, 3, 4000],
                      [5, 500, 1, 5000]], dtype=np.int64)
        self.assertEqual(higher_quantiles(x, 25), [2, 200, 3, 2000])
        self.assertEqual(higher_quantiles(x, 50), [3, 300, 5, 3000])
        self.assertEqual(higher_quantiles(x, 75), [4, 400, 7, 4000])

    def test_integer_rounding_above_exact_rank(self):
        self.assertEqual(higher_quantiles(np.array([[4]*4, [1]*4, [3]*4, [2]*4], dtype=np.int64), 75), [4]*4)

    def test_future_and_pre_start_excluded(self):
        x = np.array([[9, 999, 999, 999, 999, 0, 0, 3],
                      [10, 1, 2, 3, 4, 0, 0, 3],
                      [19, 5, 6, 7, 8, 0, 0, 3],
                      [20, 999, 999, 999, 999, 0, 0, 3]], dtype=np.int64)
        self.assertEqual(training_rows(x, 10, 20)[:, 0].tolist(), [10, 19])

    def test_requires_both_warm_venues(self):
        x = np.zeros((4, 8), dtype=np.int64)
        x[:, 0] = [10, 11, 12, 13]
        x[:, 7] = [0, 1, 2, 3]
        self.assertEqual(training_rows(x, 10, 20)[:, 0].tolist(), [13])

    def test_empty_training_rejected(self):
        with self.assertRaises(ValueError):
            training_rows(np.zeros((0, 8), dtype=np.int64), 10, 20)
        with self.assertRaises(ValueError):
            higher_quantiles(np.zeros((0, 4), dtype=np.int64), 25)

    def test_bad_dtype_values_and_quantile_rejected(self):
        for x, q in [(np.ones((1, 4)), 25), (np.array([[-1]*4], dtype=np.int64), 25),
                     (np.ones((1, 4), dtype=np.int64), 101)]:
            with self.assertRaises(ValueError):
                higher_quantiles(x, q)

    def test_uniform_grid_and_boundaries_validated(self):
        x = np.zeros((86400, 8), dtype=np.int64)
        x[:, 0] = 100 + np.arange(86400, dtype=np.int64)*10**9
        validate_daily_features(x, 100)
        x[1, 0] += 1
        with self.assertRaises(ValueError):
            validate_daily_features(x, 100)


if __name__ == '__main__':
    unittest.main()

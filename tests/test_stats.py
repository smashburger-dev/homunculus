"""Tests for homunculus.stats. Synthetic, seeded inputs only."""

import itertools
import math
import random
import statistics
import unittest

from homunculus import stats

# Two-sided critical values of Student's t (upper quantiles), 7 decimals.
T_TABLE = {
    (0.975, 1): 12.7062047, (0.975, 2): 4.3026527, (0.975, 3): 3.1824463,
    (0.975, 4): 2.7764451, (0.975, 5): 2.5705818, (0.975, 10): 2.2281389,
    (0.975, 20): 2.0859634, (0.975, 30): 2.0422725, (0.975, 60): 2.0002978,
    (0.975, 120): 1.9799304,
    (0.80, 1): 1.3763819, (0.80, 2): 1.0606602, (0.80, 5): 0.9195438,
    (0.80, 10): 0.8790578, (0.80, 30): 0.8537673, (0.80, 120): 0.8446454,
    (0.995, 1): 63.6567412, (0.995, 5): 4.0321430, (0.995, 10): 3.1692727,
    (0.90, 1): 3.0776835,
}


def _t_pdf(t, df):
    log_c = (math.lgamma((df + 1) / 2) - math.lgamma(df / 2)
             - 0.5 * math.log(df * math.pi))
    return math.exp(log_c - (df + 1) / 2 * math.log1p(t * t / df))


def _t_cdf_simpson(t, df, steps=2000):
    """Independent reference: 0.5 + Simpson integral of the pdf on [0, t]."""
    h = t / steps
    acc = _t_pdf(0.0, df) + _t_pdf(t, df)
    for i in range(1, steps):
        acc += (4 if i % 2 else 2) * _t_pdf(i * h, df)
    return 0.5 + acc * h / 3


class LocationSpreadTest(unittest.TestCase):
    def test_mean_and_empty(self):
        self.assertAlmostEqual(stats.mean([1.0, 2.0, 3.0, 6.0]), 3.0)
        with self.assertRaises(ValueError):
            stats.mean([])

    def test_sd_matches_statistics(self):
        rng = random.Random(1)
        xs = [rng.gauss(10, 2) for _ in range(50)]
        self.assertAlmostEqual(stats.sd(xs), statistics.stdev(xs), places=12)
        self.assertAlmostEqual(stats.sd(xs, ddof=0), statistics.pstdev(xs), places=12)
        with self.assertRaises(ValueError):
            stats.sd([1.0])
        self.assertEqual(stats.sd([4.0], ddof=0), 0.0)

    def test_median(self):
        self.assertEqual(stats.median([3.0, 1.0, 2.0]), 2.0)
        self.assertEqual(stats.median([4.0, 1.0, 3.0, 2.0]), 2.5)
        with self.assertRaises(ValueError):
            stats.median([])

    def test_mad_is_raw(self):
        # median 3, deviations 2,1,0,1,6 -> median 1
        self.assertEqual(stats.mad([1.0, 2.0, 3.0, 4.0, 9.0]), 1.0)

    def test_percentile_linear(self):
        xs = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(stats.percentile(xs, 0), 10.0)
        self.assertEqual(stats.percentile(xs, 100), 40.0)
        self.assertAlmostEqual(stats.percentile(xs, 50), 25.0)
        self.assertAlmostEqual(stats.percentile(xs, 25), 17.5)
        self.assertEqual(stats.percentile([5.0], 37), 5.0)
        rng = random.Random(2)
        ys = [rng.random() for _ in range(101)]
        self.assertAlmostEqual(
            stats.percentile(ys, 30),
            statistics.quantiles(ys, n=10, method="inclusive")[2],
        )
        for bad in (-1, 100.5):
            with self.assertRaises(ValueError):
                stats.percentile(xs, bad)


class RegressionSeriesTest(unittest.TestCase):
    def test_ols_exact_line(self):
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [1.0 + 2.0 * x for x in xs]
        slope, intercept, rsd = stats.ols(xs, ys)
        self.assertAlmostEqual(slope, 2.0)
        self.assertAlmostEqual(intercept, 1.0)
        self.assertAlmostEqual(rsd, 0.0)

    def test_ols_matches_statistics(self):
        rng = random.Random(3)
        xs = [float(i) for i in range(60)]
        ys = [0.5 * x - 3 + rng.gauss(0, 1.5) for x in xs]
        slope, intercept, rsd = stats.ols(xs, ys)
        ref = statistics.linear_regression(xs, ys)
        self.assertAlmostEqual(slope, ref.slope, places=10)
        self.assertAlmostEqual(intercept, ref.intercept, places=10)
        resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
        expected = math.sqrt(sum(r * r for r in resid) / (len(xs) - 2))
        self.assertAlmostEqual(rsd, expected, places=12)
        self.assertAlmostEqual(rsd, 1.5, delta=0.5)

    def test_ols_errors(self):
        with self.assertRaises(ValueError):
            stats.ols([1.0, 2.0], [1.0])
        with self.assertRaises(ValueError):
            stats.ols([1.0], [1.0])
        with self.assertRaises(ValueError):
            stats.ols([2.0, 2.0, 2.0], [1.0, 2.0, 3.0])
        self.assertEqual(stats.ols([0.0, 1.0], [0.0, 5.0])[2], 0.0)

    def test_ewma_lambda_and_seed(self):
        out = stats.ewma([10.0, 20.0, 20.0], n=7)
        lam = 0.25
        self.assertEqual(out[0], 10.0)
        self.assertAlmostEqual(out[1], 10.0 + lam * 10.0)
        self.assertAlmostEqual(out[2], out[1] + lam * (20.0 - out[1]))

    def test_ewma_none_keeps_state(self):
        out = stats.ewma([4.0, None, 8.0], n=1)  # lambda = 1
        self.assertEqual(out, [4.0, None, 8.0])
        out = stats.ewma([None, 4.0, None, 8.0], n=3)  # lambda = 0.5
        self.assertEqual(out, [None, 4.0, None, 6.0])

    def test_ewma_seed_after(self):
        out = stats.ewma([1.0, None, 1.0, 1.0, 1.0], n=3, seed_after=3)
        self.assertEqual(out, [None, None, None, 1.0, 1.0])
        self.assertEqual(stats.ewma([], n=5), [])
        with self.assertRaises(ValueError):
            stats.ewma([1.0], n=0)

    def test_chronic_lambda(self):
        out = stats.ewma([0.0, 1.0], n=28)
        self.assertAlmostEqual(out[1], 2 / 29)
        self.assertAlmostEqual(out[1], 0.069, places=3)

    def test_lag1_autocorr(self):
        self.assertAlmostEqual(stats.lag1_autocorr([1.0, -1.0] * 10), -0.95)
        rng = random.Random(4)
        phi_true = 0.6
        xs = [0.0]
        for _ in range(20000):
            xs.append(phi_true * xs[-1] + rng.gauss(0, 1))
        self.assertAlmostEqual(stats.lag1_autocorr(xs), phi_true, delta=0.03)
        with self.assertRaises(ValueError):
            stats.lag1_autocorr([1.0])
        with self.assertRaises(ValueError):
            stats.lag1_autocorr([2.0, 2.0, 2.0])

    def test_zscore(self):
        self.assertAlmostEqual(stats.zscore(12.0, 10.0, 1.0), 2.0)
        self.assertEqual(stats.zscore(30.0, 10.0, 1.0), 3.0)
        self.assertEqual(stats.zscore(-30.0, 10.0, 1.0), -3.0)
        self.assertEqual(stats.zscore(30.0, 10.0, 1.0, cap=5.0), 5.0)
        for args in ((None, 1.0, 1.0), (1.0, None, 1.0), (1.0, 1.0, None),
                     (1.0, 1.0, 0.0), (1.0, 1.0, -1.0)):
            self.assertIsNone(stats.zscore(*args))


class DistributionTest(unittest.TestCase):
    def test_phi(self):
        nd = statistics.NormalDist()
        for x in (-6.0, -1.96, -0.5, 0.0, 0.3, 1.0, 2.5, 7.0):
            self.assertAlmostEqual(stats.phi(x), nd.cdf(x), places=14)

    def test_phi_inv_accuracy(self):
        nd = statistics.NormalDist()
        ps = [1e-12, 1e-8, 1e-4, 0.001, 0.02, 0.02425, 0.025, 0.1, 0.5,
              0.8, 0.975, 0.97575, 0.99, 0.9999, 1 - 1e-8]
        rng = random.Random(5)
        ps += [rng.random() for _ in range(500)]
        for p in ps:
            if 0.0 < p < 1.0:
                self.assertLess(abs(stats.phi_inv(p) - nd.inv_cdf(p)), 1e-8, p)
        self.assertAlmostEqual(stats.phi_inv(0.975), 1.959963984540054, places=9)
        self.assertEqual(stats.phi_inv(0.5), 0.0)
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                stats.phi_inv(bad)

    def test_t_ppf_table(self):
        for (p, df), ref in T_TABLE.items():
            self.assertLess(abs(stats.t_ppf(p, df) - ref), 1e-3, (p, df))
            self.assertLess(abs(stats.t_ppf(1 - p, df) + ref), 1e-3, (p, df))

    def test_t_ppf_closed_forms(self):
        for p in (0.6, 0.8, 0.9, 0.975, 0.995, 0.9995):
            self.assertAlmostEqual(stats.t_ppf(p, 1), math.tan(math.pi * (p - 0.5)),
                                   places=6)
            df2 = (2 * p - 1) / math.sqrt(2 * p * (1 - p))
            self.assertAlmostEqual(stats.t_ppf(p, 2), df2, places=8)

    def test_t_ppf_against_integrated_cdf(self):
        for df in range(1, 121):
            for p in (0.8, 0.975):
                t = stats.t_ppf(p, df)
                self.assertLess(_t_cdf_simpson(t - 1e-3, df), p, (p, df))
                self.assertGreater(_t_cdf_simpson(t + 1e-3, df), p, (p, df))

    def test_t_ppf_monotone_and_limits(self):
        prev = math.inf
        for df in range(1, 121):
            t = stats.t_ppf(0.975, df)
            self.assertLess(t, prev)
            prev = t
        self.assertGreater(prev, stats.phi_inv(0.975))
        self.assertAlmostEqual(stats.t_ppf(0.975, 100000), stats.phi_inv(0.975),
                               places=4)
        self.assertEqual(stats.t_ppf(0.5, 7), 0.0)
        with self.assertRaises(ValueError):
            stats.t_ppf(0.9, 0)
        with self.assertRaises(ValueError):
            stats.t_ppf(1.0, 5)


class TestsAndMultiplicityTest(unittest.TestCase):
    def test_sign_flip_brute_force(self):
        rng = random.Random(6)
        for m in (1, 3, 6, 9):
            d = [round(rng.gauss(0.5, 1.0), 2) for _ in range(m)]
            obs = abs(sum(d))
            hits = sum(
                1 for signs in itertools.product((1, -1), repeat=m)
                if abs(sum(s * x for s, x in zip(signs, d))) >= obs - 1e-9
            )
            self.assertAlmostEqual(stats.sign_flip_pvalue(d), hits / 2 ** m)

    def test_sign_flip_known_values(self):
        # All six positive and distinct: only the identity and the full flip tie.
        self.assertAlmostEqual(stats.sign_flip_pvalue([1, 2, 3, 4, 5, 6]), 2 / 64)
        # Sums of [1, 1] are 2, 0, 0, -2: two of four reach |2|.
        self.assertEqual(stats.sign_flip_pvalue([1.0, 1.0]), 0.5)
        # Observed sum 0: every sign vector ties, so p = 1.
        self.assertEqual(stats.sign_flip_pvalue([1.0, -1.0]), 1.0)
        # Float ties: 0.1 + 0.2 - 0.3 is not exactly 0, the tolerance must hold.
        self.assertEqual(stats.sign_flip_pvalue([0.1, 0.2, 0.3]), 0.25)
        self.assertEqual(stats.sign_flip_pvalue([0.1, 0.2, -0.3]), 1.0)

    def test_sign_flip_limits(self):
        p = stats.sign_flip_pvalue([1.0 + i for i in range(20)])
        self.assertAlmostEqual(p, 2 / 2 ** 20)
        with self.assertRaises(ValueError):
            stats.sign_flip_pvalue([1.0] * 21)
        # Four pairs can never reach p < 0.05.
        self.assertGreaterEqual(stats.sign_flip_pvalue([1, 2, 3, 4]), 0.125)

    def test_mc_pvalue(self):
        self.assertEqual(stats.mc_pvalue(2.0, [0.5, -3.0, 2.0, 1.0]), 3 / 5)
        self.assertEqual(stats.mc_pvalue(1.0, []), 1.0)
        self.assertEqual(stats.mc_pvalue(-5.0, iter([1.0, 2.0])), 1 / 3)

    def test_benjamini_hochberg(self):
        pvals = [0.01, 0.04, 0.03, 0.005, 0.5]
        # sorted: .005(1) .01(2) .03(3) .04(4) .5(5); thresholds q*k/5 with q=.05:
        # .01 .02 .03 .04 .05 -> largest k with p<=thr is 4
        self.assertEqual(stats.benjamini_hochberg(pvals, q=0.05),
                         [True, True, True, True, False])
        self.assertEqual(stats.benjamini_hochberg([0.2, 0.3], q=0.1), [False, False])
        # Step-up: a later pass rescues earlier failures.
        self.assertEqual(stats.benjamini_hochberg([0.04, 0.045], q=0.05), [True, True])
        self.assertEqual(stats.benjamini_hochberg([]), [])
        with self.assertRaises(ValueError):
            stats.benjamini_hochberg([1.2])
        with self.assertRaises(ValueError):
            stats.benjamini_hochberg([0.1], q=0.0)


class NormalUpdateTest(unittest.TestCase):
    def test_precision_weighting(self):
        mu1, sd1 = stats.normal_update(0.0, 1.0, 2.0, 1.0)
        self.assertAlmostEqual(mu1, 1.0)
        self.assertAlmostEqual(sd1, math.sqrt(0.5))
        mu1, sd1 = stats.normal_update(1.0, 2.0, 4.0, 1.0)
        self.assertAlmostEqual(mu1, (0.25 * 1 + 1 * 4) / 1.25)
        self.assertAlmostEqual(sd1, math.sqrt(1 / 1.25))

    def test_rejects_nonpositive_sd(self):
        with self.assertRaises(ValueError):
            stats.normal_update(0.0, 0.0, 1.0, 1.0)
        with self.assertRaises(ValueError):
            stats.normal_update(0.0, 1.0, 1.0, -1.0)


if __name__ == "__main__":
    unittest.main()

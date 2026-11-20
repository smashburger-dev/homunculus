"""Tests for homunculus.experiments with small synthetic values only."""

from __future__ import annotations

import hashlib
import json
import math
import random
import tempfile
import unittest
from dataclasses import replace

from homunculus import beliefs as B
from homunculus import experiments as E
from homunculus import stats
from homunculus.beliefs import Prior
from homunculus.store import Ledger, PathNotAllowed, canonical_json


def protocol(**kw) -> E.Protocol:
    base = dict(
        id="exp-0001", belief_id="b-test", intervention="x", comparator="control",
        outcome_key="outcome_y", source="watch", design="paired_blocks", block_days=7,
        washout_days=2, pairs=6, a_days_per_week=None, swc=0.5, swc_harm=0.5,
        exclusions=({"kind": "checkin_flag", "flag": "ill", "days_after": 1},),
        covariates=("cov_z",), start_date="2026-01-05", created_at="2026-01-01T10:00:00+00:00",
    )
    base.update(kw)
    return E.Protocol(**base)


def alternating(**kw) -> E.Protocol:
    return protocol(design="daily_alternating", washout_days=0, a_days_per_week=(3, 4), pairs=8, **kw)


def prior(mu0=0.0, sd0=2.0) -> Prior:
    return Prior(mu0, sd0, ("reg:T",), 3.0, "thal-1")


def simulate(allocation, effect, noise=0.3, rng_seed=1, ramp_days=0):
    """Synthetic outcome: 10 + effect on A days (+ noise). ramp_days > 0 makes
    the effect build up linearly over the first days of every A block."""
    rng = random.Random(rng_seed)
    data, run = {}, 0
    prev = None
    for day, label in allocation:
        run = run + 1 if label == prev else 1
        prev = label
        scale = min(1.0, run / ramp_days) if ramp_days else 1.0
        data[day] = 10.0 + (effect * scale if label == "A" else 0.0) + rng.gauss(0.0, noise)
    return data


def allocation_of(p, extra=0):
    return E.allocate(p, canonical_json(p.to_dict()), extra_pairs=extra)


class ProtocolTest(unittest.TestCase):
    def test_round_trip_is_json_native(self):
        p = protocol()
        d = p.to_dict()
        self.assertEqual(json.loads(json.dumps(d)), d)
        self.assertIsInstance(d["covariates"], list)
        self.assertEqual(E.Protocol.from_dict(d), p)
        a = alternating()
        self.assertEqual(E.Protocol.from_dict(a.to_dict()).a_days_per_week, (3, 4))

    def test_from_dict_rejects_missing_or_extra_keys(self):
        d = protocol().to_dict()
        with self.assertRaises(E.ProtocolError):
            E.Protocol.from_dict({**d, "extra": 1})
        d.pop("swc")
        with self.assertRaises(E.ProtocolError):
            E.Protocol.from_dict(d)

    def test_invalid_protocols(self):
        bad = [
            dict(design="abab"),
            dict(id="../x"),
            dict(washout_days=7),
            dict(block_days=0),
            dict(a_days_per_week=(3, 4)),
            dict(swc=0.0),
            dict(start_date="2026-1-5"),
            dict(exclusions=({"kind": "weather"},)),
            dict(exclusions=({"kind": "threshold", "key": "k", "op": "~", "value": 1},)),
            dict(exclusions=({"kind": "missing", "key": "k", "days_after": -1},)),
            dict(exclusions=({"kind": "missing", "key": "k", "note": "x"},)),
            dict(design="daily_alternating", washout_days=0, a_days_per_week=None),
            dict(design="daily_alternating", washout_days=0, a_days_per_week=(0, 3)),
            dict(design="daily_alternating", washout_days=1, a_days_per_week=(3, 4)),
        ]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(E.ProtocolError):
                protocol(**kw)
        self.assertTrue(issubclass(E.ProtocolError, ValueError))


class DesignTest(unittest.TestCase):
    def test_baseline_stats_matches_stats_without_gaps(self):
        rng = random.Random(3)
        xs = [5.0 + rng.gauss(0, 1) for _ in range(28)]
        sigma, rho, mu = E.baseline_stats(xs)
        self.assertAlmostEqual(sigma, stats.sd(xs))
        self.assertAlmostEqual(mu, stats.mean(xs))
        self.assertAlmostEqual(rho, stats.lag1_autocorr(xs))

    def test_baseline_stats_gaps_and_minimum(self):
        rng = random.Random(4)
        xs = [5.0 + rng.gauss(0, 1) for _ in range(28)]
        xs[5] = xs[17] = None
        sigma, rho, mu = E.baseline_stats(xs)
        self.assertTrue(-1.0 < rho < 1.0)
        self.assertAlmostEqual(mu, stats.mean([x for x in xs if x is not None]))
        with self.assertRaises(ValueError):
            E.baseline_stats([1.0] * 20 + [None] * 8)
        with self.assertRaises(ValueError):
            E.baseline_stats([1.0, None] * 22)
        self.assertEqual(E.baseline_stats([2.0] * 21), (0.0, 0.0, 2.0))

    def test_mde_formula(self):
        sigma, rho, n, w, m = 1.0, 0.3, 7, 2, 6
        n_eff = (n - w) * (1 - rho) / (1 + rho)
        se = math.sqrt((2 * sigma**2 / n_eff + 0.04) / m)
        expected = (stats.t_ppf(0.975, m - 1) + stats.t_ppf(0.8, m - 1)) * se
        self.assertAlmostEqual(E.mde(sigma, rho, n, w, m, tau_drift=0.2), expected)
        self.assertGreater(E.mde(1.0, 0.5, 7, 2, 6), E.mde(1.0, 0.1, 7, 2, 6))
        self.assertGreater(E.mde(1.0, 0.1, 7, 2, 6), E.mde(1.0, 0.1, 7, 2, 8))
        self.assertAlmostEqual(E.mde(1.0, -0.3, 7, 2, 6), E.mde(1.0, 0.0, 7, 2, 6))
        for args in [(1.0, 0.1, 7, 7, 6), (1.0, 0.1, 7, 2, 1), (1.0, 1.0, 7, 2, 6), (-1.0, 0.1, 7, 2, 6)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                E.mde(*args)

    def test_eligibility(self):
        rng = random.Random(5)
        baseline = [10.0 + rng.gauss(0, 0.3) for _ in range(28)]
        mu = stats.mean(baseline)
        self.assertEqual(E.eligibility(protocol(), baseline, mu, 0.5), [])
        self.assertEqual(E.eligibility(alternating(), baseline, mu, 0.5), [])
        self.assertIn("pairs_lt_6", E.eligibility(protocol(pairs=4), baseline, mu, 0.5))
        self.assertIn("mde_gt_2swc", E.eligibility(protocol(swc=0.05), baseline, mu, 0.5))
        self.assertIn("baseline_outside_1sd", E.eligibility(protocol(), baseline, mu + 1.0, 0.5))
        self.assertIn("washout_lt_1", E.eligibility(protocol(washout_days=0), baseline, mu, 0.5))
        self.assertIn("source_unlocked", E.eligibility(protocol(source="any"), baseline, mu, 0.5))
        self.assertEqual(E.eligibility(protocol(), baseline[:20], mu, 0.5), ["baseline_insufficient"])


class AllocationTest(unittest.TestCase):
    def test_seed(self):
        b = b'{"x":1}'
        self.assertEqual(E.seed(b), int(hashlib.sha256(b).hexdigest()[:16], 16))

    def test_paired_blocks_layout(self):
        p = protocol()
        alloc = allocation_of(p)
        self.assertEqual(alloc, allocation_of(p))
        self.assertEqual(len(alloc), 6 * 14)
        self.assertEqual(alloc[0][0], "2026-01-05")
        self.assertEqual(alloc[-1][0], "2026-03-29")
        for k in range(6):
            pair = [lab for _, lab in alloc[k * 14:(k + 1) * 14]]
            first, second = pair[2], pair[9]
            self.assertEqual({first, second}, {"A", "B"})
            self.assertEqual(pair, ["washout"] * 2 + [first] * 5 + ["washout"] * 2 + [second] * 5)

    def test_daily_alternating_layout(self):
        p = alternating()
        alloc = allocation_of(p)
        self.assertEqual(len(alloc), 8 * 7)
        self.assertNotIn("washout", {lab for _, lab in alloc})
        for k in range(8):
            n_a = [lab for _, lab in alloc[k * 7:(k + 1) * 7]].count("A")
            self.assertIn(n_a, (3, 4))

    def test_bytes_must_be_canonical(self):
        p = protocol()
        with self.assertRaises(E.ProtocolError):
            E.allocate(p, json.dumps(p.to_dict(), indent=2).encode())
        other = protocol(id="exp-0002")
        with self.assertRaises(E.ProtocolError):
            E.allocate(p, canonical_json(other.to_dict()))

    def test_extension_keeps_prefix(self):
        for p in (protocol(), alternating()):
            base = allocation_of(p)
            ext = allocation_of(p, extra=2)
            self.assertEqual(ext[: len(base)], base)
            self.assertEqual(len(ext), len(base) + 2 * E._period_days(p))
        with self.assertRaises(E.ProtocolError):
            allocation_of(protocol(), extra=-1)


class ExclusionTest(unittest.TestCase):
    def test_rules(self):
        p = protocol(exclusions=(
            {"kind": "checkin_flag", "flag": "ill", "days_after": 1},
            {"kind": "threshold", "key": "cov_z", "op": ">", "value": 2},
            {"kind": "missing", "key": "outcome_y"},
        ))
        daily = {
            "2026-01-05": {"outcome_y": 1.0, "cov_z": 3.0},
            "2026-01-06": {"outcome_y": 1.0, "cov_z": 2.0},
            "2026-01-07": {"outcome_y": None},
            "2026-01-08": {"outcome_y": 1.0},
            "2026-01-10": {"outcome_y": 1.0},
        }
        flags = {"2026-01-10": ["ill", "other"], "2026-01-08": ["other"]}
        self.assertEqual(
            E.excluded_days(p, daily, flags),
            {"2026-01-05", "2026-01-07", "2026-01-10", "2026-01-11"},
        )
        self.assertEqual(E.excluded_days(protocol(exclusions=()), daily, flags), set())


class AnalyzeTest(unittest.TestCase):
    def test_clear_benefit_is_adopted(self):
        p = protocol()
        alloc = allocation_of(p)
        r = E.analyze(p, alloc, simulate(alloc, 2.0), set(), prior())
        self.assertEqual(r.decision, "adopt")
        self.assertEqual(len(r.pair_diffs), 6)
        self.assertTrue(all(d > 1.0 for d in r.pair_diffs))
        self.assertAlmostEqual(r.p_value, stats.sign_flip_pvalue(r.pair_diffs))
        self.assertAlmostEqual(r.p_value, 2 / 64)
        mu1, sd1 = stats.normal_update(0.0, 2.0, r.d_bar, r.se)
        self.assertAlmostEqual(r.mu1, mu1)
        self.assertAlmostEqual(r.sd1, sd1)
        self.assertGreaterEqual(r.p_benefit, 0.8)
        self.assertLess(r.p_harm, 0.05)
        self.assertEqual(r.n_excluded, 0)

    def test_se_rule(self):
        p = protocol()
        alloc = allocation_of(p)
        r = E.analyze(p, alloc, simulate(alloc, 1.0, noise=1.0, rng_seed=9), set(), prior())
        m = len(r.pair_diffs)
        floor = math.sqrt(stats.sd(r.pair_diffs) ** 2 / m * (m - 1) / (m - 3))
        self.assertGreaterEqual(r.se, floor - 1e-12)

    def test_null_effect_rejected_and_direction(self):
        p = protocol()
        alloc = allocation_of(p)
        self.assertEqual(E.analyze(p, alloc, simulate(alloc, 0.0), set(), prior()).decision, "reject")
        # Outcome goes up, but lower is better: harm.
        r = E.analyze(p, alloc, simulate(alloc, 2.0), set(), prior(), direction=-1)
        self.assertEqual(r.decision, "reject")
        self.assertGreater(r.p_harm, 0.95)
        with self.assertRaises(E.ProtocolError):
            E.analyze(p, alloc, simulate(alloc, 2.0), set(), prior(), direction=0)

    def test_ambiguous_extends_once(self):
        p = protocol()
        alloc = allocation_of(p)
        r = E.analyze(p, alloc, simulate(alloc, 0.5, noise=0.6, rng_seed=2), set(), prior(0.5, 0.05))
        self.assertEqual(r.decision, "extend")
        ext = allocation_of(p, extra=2)
        r2 = E.analyze(p, ext, simulate(ext, 0.5, noise=0.6, rng_seed=2), set(), prior(0.5, 0.05), extended=True)
        self.assertEqual(len(r2.pair_diffs), 8)
        self.assertEqual(r2.decision, "inconclusive")

    def test_exclusions_and_missing_data(self):
        p = protocol()
        alloc = allocation_of(p)
        data = simulate(alloc, 2.0)
        washout_day = alloc[0][0]
        a_or_b = [d for d, lab in alloc if lab != "washout"]
        excluded = {washout_day, a_or_b[0], a_or_b[1]}
        data[a_or_b[2]] = None
        r = E.analyze(p, alloc, data, excluded, prior())
        self.assertEqual(r.n_excluded, 2)
        self.assertEqual(r.decision, "adopt")
        # Drop a whole arm of three pairs: too few usable pairs remain.
        for k in range(3):
            for day, lab in alloc[k * 14 + 2:k * 14 + 7]:
                data[day] = None
        with self.assertRaises(E.ProtocolError):
            E.analyze(p, alloc, data, set(), prior())

    def test_allocation_must_match_protocol(self):
        p = protocol()
        alloc = allocation_of(p)
        data = simulate(alloc, 1.0)
        with self.assertRaises(E.ProtocolError):
            E.analyze(p, alloc[:-1], data, set(), prior())
        with self.assertRaises(E.ProtocolError):
            E.analyze(p, alloc, data, set(), prior(), extended=True)

    def test_daily_alternating_mc_is_deterministic(self):
        p = alternating()
        alloc = allocation_of(p)
        data = simulate(alloc, 1.5, rng_seed=7)
        r1 = E.analyze(p, alloc, data, set(), prior(), mc_draws=500)
        r2 = E.analyze(p, alloc, data, set(), prior(), mc_draws=500)
        self.assertEqual(r1, r2)
        self.assertEqual(r1.decision, "adopt")
        self.assertAlmostEqual(r1.p_value, 1 / 501)
        self.assertEqual(len(r1.pair_diffs), 8)
        self.assertIsNotNone(r1.carryover_slope)
        null = E.analyze(p, alloc, simulate(alloc, 0.0, rng_seed=7), set(), prior(), mc_draws=500)
        self.assertGreater(null.p_value, 0.05)

    def test_carryover_slope_detects_ramp(self):
        p = protocol(washout_days=1)
        alloc = allocation_of(p)
        flat = E.analyze(p, alloc, simulate(alloc, 2.0, noise=0.05), set(), prior())
        ramp = E.analyze(p, alloc, simulate(alloc, 2.0, noise=0.05, ramp_days=7), set(), prior())
        self.assertLess(abs(flat.carryover_slope), 0.05)
        self.assertGreater(ramp.carryover_slope, 0.1)

    def test_many_pairs_use_monte_carlo_sign_flip(self):
        p = protocol(pairs=22, block_days=3, washout_days=1)
        alloc = allocation_of(p)
        r = E.analyze(p, alloc, simulate(alloc, 2.0), set(), prior(), mc_draws=300)
        self.assertEqual(len(r.pair_diffs), 22)
        self.assertAlmostEqual(r.p_value, 1 / 301)


class WriteProtocolTest(unittest.TestCase):
    def test_write_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, {"hippocampus": ["experiments/*/protocol.json"]})
            p = protocol()
            sha = E.write_protocol(ledger, p)
            self.assertEqual(sha, hashlib.sha256(canonical_json(p.to_dict())).hexdigest())
            stored = ledger.read_json("experiments/exp-0001/protocol.json")
            self.assertEqual(E.Protocol.from_dict(stored), p)
            with self.assertRaises(E.ProtocolError):
                E.write_protocol(ledger, replace(p, pairs=8))
            with self.assertRaises(PathNotAllowed):
                E.write_protocol(ledger, protocol(id="exp-0002"), author="sartorius")


CLAIM = B.Claim("adult", "x", "control", "outcome_y", "u", 1)


def belief_events(mu0=0.0, sd0=2.0):
    return [B.make_event("prior_set", "b-test", "2026-01-01", claim=CLAIM, prior=prior(mu0, sd0), swc=0.5,
                         q_per_day=0.0)]


def result(decision, d_bar=2.0, se=0.5):
    return E.Result((d_bar,) * 6, d_bar, se, 0.03, 0.0, 1.0, 0.9, 0.01, decision, None, 0)


class BeliefLedgerTest(unittest.TestCase):
    def test_starting_prior_uses_posterior_without_own_result(self):
        p = protocol()
        other = B.make_event("personal_result", "b-test", "2026-03-01", est=1.0, se=1.0, experiment="exp-0000")
        own = B.make_event("personal_result", "b-test", "2026-03-02", est=9.0, se=0.1, experiment=p.id)
        start = E.starting_prior(belief_events() + [other, own], p, "2026-04-01")
        mu, sd = stats.normal_update(0.0, 2.0, 1.0, 1.0)
        self.assertAlmostEqual(start.mu0, mu)
        self.assertAlmostEqual(start.sd0, sd)
        self.assertEqual(start.sources, ("reg:T",))
        with self.assertRaises(E.ProtocolError):
            E.starting_prior([], p, "2026-04-01")

    def test_record_result_reaches_views_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, {})
            for ev in belief_events():
                ledger.append_jsonl(B.EVENTS_PATH, ev, author="pineal")
            p = protocol()
            self.assertIsNone(E.record_result(ledger, p, result("extend"), "2026-03-01"))
            self.assertEqual(len(ledger.read_jsonl(B.EVENTS_PATH)), 1)
            ev = E.record_result(ledger, p, result("adopt"), "2026-03-01")
            self.assertEqual((ev["kind"], ev["est"], ev["se"], ev["experiment"]), ("personal_result", 2.0, 0.5, p.id))
            self.assertIsNone(E.record_result(ledger, p, result("adopt"), "2026-03-02"))
            self.assertEqual(len(ledger.read_jsonl(B.EVENTS_PATH)), 2)
            view = ledger.read_json("beliefs/views/b-test.json")
            mu, sd = stats.normal_update(0.0, 2.0, 2.0, 0.5)
            self.assertAlmostEqual(view["mu"], mu)
            self.assertAlmostEqual(view["sd"], sd)

    def test_retest_starts_from_first_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, {})
            for ev in belief_events():
                ledger.append_jsonl(B.EVENTS_PATH, ev, author="pineal")
            E.record_result(ledger, protocol(), result("adopt"), "2026-03-01")
            retest = protocol(id="exp-0002", start_date="2026-04-01")
            start = E.starting_prior(ledger.read_jsonl(B.EVENTS_PATH), retest, "2026-04-01")
            mu, sd = stats.normal_update(0.0, 2.0, 2.0, 0.5)
            self.assertAlmostEqual(start.mu0, mu)
            self.assertAlmostEqual(start.sd0, sd)


if __name__ == "__main__":
    unittest.main()

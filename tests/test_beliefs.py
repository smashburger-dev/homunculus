"""Tests for homunculus.beliefs with small synthetic values only."""

from __future__ import annotations

import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from homunculus import beliefs as B
from homunculus import stats
from homunculus.store import Ledger

CLAIM = B.Claim("adult", "intervention-x", "control", "outcome-y", "u", 1)


def prior(mu0=1.0, sd0=1.0, sources=("reg:A",), certainty=3.0, rubric="thal-1") -> B.Prior:
    return B.Prior(mu0, sd0, tuple(sources), certainty, rubric)


def created(bid="b1", at="2026-01-01", p=None, claim=CLAIM, swc=0.5, **extra) -> dict:
    return B.make_event("prior_set", bid, at, claim=claim, prior=p, swc=swc, **extra)


class MakeEventTest(unittest.TestCase):
    def test_shape_and_dataclass_conversion(self):
        ev = created(p=prior())
        self.assertEqual(ev["schema"], 1)
        self.assertEqual(ev["kind"], "prior_set")
        self.assertEqual(ev["belief_id"], "b1")
        self.assertEqual(ev["prior"]["sources"], ["reg:A"])
        self.assertEqual(ev["claim"]["direction"], 1)
        self.assertEqual(json.loads(json.dumps(ev)), ev)

    def test_rejects_bad_kind_id_time_and_reserved_keys(self):
        with self.assertRaises(ValueError):
            B.make_event("nope", "b1", "2026-01-01")
        with self.assertRaises(ValueError):
            B.make_event("review", "../x", "2026-01-01")
        with self.assertRaises(ValueError):
            B.make_event("review", "b1", "yesterday")
        with self.assertRaises(ValueError):
            B.make_event("review", "b1", "2026-01-01", schema=2)


class MathTest(unittest.TestCase):
    def test_p_benefit_and_harm(self):
        self.assertAlmostEqual(B.p_benefit(0.5, 1.0, 0.5), 0.5)
        self.assertAlmostEqual(B.p_benefit(1.5, 1.0, 0.5), stats.phi(1.0))
        self.assertAlmostEqual(B.p_harm(-0.5, 1.0, 0.5), 0.5)
        self.assertAlmostEqual(B.p_harm(0.5, 1.0, 0.5), stats.phi(-1.0))
        with self.assertRaises(ValueError):
            B.p_benefit(0.0, 0.0, 0.5)

    def test_decayed_sd(self):
        self.assertAlmostEqual(B.decayed_sd(3.0, 16.0 / 10.0, 10.0), 5.0)
        self.assertEqual(B.decayed_sd(2.0, 1.0, 0.0), 2.0)
        with self.assertRaises(ValueError):
            B.decayed_sd(1.0, 1.0, -1.0)

    def test_combine_precision_weighted(self):
        c = B.combine_priors([prior(0.0, 1.0, ("reg:A",), 4.0), prior(3.0, 1.0, ("doi:b",), 2.0)])
        self.assertAlmostEqual(c.mu0, 1.5)
        self.assertAlmostEqual(c.sd0, math.sqrt(0.5))
        self.assertAlmostEqual(c.certainty, 3.0)
        self.assertEqual(c.sources, ("reg:A", "doi:b"))
        self.assertEqual(c.rubric_version, "thal-1")

    def test_combine_dedupes_registration(self):
        c = B.combine_priors([prior(1.0, 1.0, ("reg:A",)), prior(9.0, 0.1, ("reg:A",))])
        self.assertEqual(c, prior(1.0, 1.0, ("reg:A",)))
        with self.assertRaises(ValueError):
            B.combine_priors([])

    def test_combine_mixed_rubrics(self):
        c = B.combine_priors([prior(rubric="thal-2"), prior(sources=("doi:z",))])
        self.assertEqual(c.rubric_version, "thal-1+thal-2")


class FoldTest(unittest.TestCase):
    def test_prior_only_hypothesis(self):
        out = B.fold([created(p=prior(1.0, 1.0))], "2026-01-01")
        b = out["b1"]
        self.assertEqual(b.status, "hypothesis")
        self.assertAlmostEqual(b.mu, 1.0)
        self.assertAlmostEqual(b.sd, 1.0)
        self.assertAlmostEqual(b.p_benefit, stats.phi(0.5))
        self.assertAlmostEqual(b.p_harm, stats.phi(-1.5))
        self.assertEqual(b.review_after, "2026-04-01")
        self.assertEqual(b.as_of, "2026-01-01")

    def test_no_prior_no_data_is_none(self):
        b = B.fold([created()], "2026-02-01")["b1"]
        self.assertIsNone(b.prior)
        self.assertIsNone(b.mu)
        self.assertIsNone(b.p_benefit)

    def test_default_variance_decay(self):
        b = B.fold([created(p=prior(1.0, 2.0))], "2027-01-01")["b1"]
        self.assertAlmostEqual(b.sd, B.decayed_sd(2.0, 4.0 / 365.0, 365.0))
        self.assertAlmostEqual(b.sd, math.sqrt(8.0))

    def test_q_override(self):
        b = B.fold([created(p=prior(1.0, 1.0), q_per_day=0.0)], "2030-01-01")["b1"]
        self.assertAlmostEqual(b.sd, 1.0)

    def test_personal_result_updates(self):
        events = [
            created(p=prior(0.0, 1.0), q_per_day=0.0),
            B.make_event("personal_result", "b1", "2026-02-01", est=2.0, se=1.0, experiment="exp-0001"),
        ]
        b = B.fold(events, "2026-03-01")["b1"]
        self.assertAlmostEqual(b.mu, 1.0)
        self.assertAlmostEqual(b.sd, math.sqrt(0.5))
        self.assertEqual(b.personal[0]["experiment"], "exp-0001")
        self.assertEqual(b.personal[0]["at"], "2026-02-01")

    def test_personal_without_prior_seeds_from_first_result(self):
        events = [
            created(q_per_day=0.0),
            B.make_event("personal_result", "b1", "2026-02-01", est=2.0, se=1.0),
            B.make_event("personal_result", "b1", "2026-03-01", est=0.0, se=1.0),
        ]
        b = B.fold(events, "2026-03-01")["b1"]
        self.assertAlmostEqual(b.mu, 1.0)
        self.assertAlmostEqual(b.sd, math.sqrt(0.5))

    def test_direction_minus_one(self):
        claim = B.Claim("adult", "x", "c", "resting hr", "bpm", -1)
        b = B.fold([created(p=prior(-2.0, 1.0), claim=claim, swc=1.0)], "2026-01-01")["b1"]
        self.assertAlmostEqual(b.mu, -2.0)
        self.assertAlmostEqual(b.p_benefit, stats.phi(1.0))

    def test_as_of_filters_events(self):
        events = [created(p=prior()), B.make_event("decision", "b1", "2026-05-01", status="adopted")]
        self.assertEqual(B.fold(events, "2026-04-30")["b1"].status, "hypothesis")
        self.assertEqual(B.fold(events, "2026-04-30T23:59:59Z")["b1"].status, "hypothesis")
        self.assertNotIn("b1", B.fold(events, "2025-12-31"))

    def test_deterministic_under_shuffle_of_distinct_times(self):
        rng = random.Random(7)
        events = [created(p=prior(0.5, 1.0), q_per_day=0.001)]
        for i in range(10):
            at = f"2026-02-{i + 1:02d}T08:00:00+00:00"
            events.append(B.make_event("personal_result", "b1", at, est=rng.gauss(1.0, 0.5), se=0.7))
        a = B.fold(events, "2026-03-01")["b1"]
        rest = events[1:]
        rng.shuffle(rest)
        b = B.fold([events[0], *rest], "2026-03-01")["b1"]
        self.assertAlmostEqual(a.mu, b.mu, places=12)
        self.assertAlmostEqual(a.sd, b.sd, places=12)

    def test_event_before_prior_set_raises(self):
        with self.assertRaises(ValueError):
            B.fold([B.make_event("review", "b1", "2026-01-01")], "2026-02-01")

    def test_evidence_added_combines(self):
        events = [
            created(p=prior(0.0, 1.0, ("reg:A",)), q_per_day=0.0),
            B.make_event("evidence_added", "b1", "2026-01-05", prior=prior(2.0, 1.0, ("doi:b",))),
            B.make_event("evidence_added", "b1", "2026-01-06", prior=prior(50.0, 0.01, ("reg:A",))),
        ]
        b = B.fold(events, "2026-02-01")["b1"]
        self.assertAlmostEqual(b.mu, 1.0)
        self.assertEqual(b.prior.sources, ("reg:A", "doi:b"))

    def test_decision_statuses_and_review_after(self):
        events = [
            created(p=prior()),
            B.make_event("decision", "b1", "2026-01-10", status="testing", ref="experiments/exp-0001"),
            B.make_event("review", "b1", "2026-02-01", review_after="2026-03-01"),
        ]
        b = B.fold(events, "2026-02-02")["b1"]
        self.assertEqual(b.status, "testing")
        self.assertEqual(b.review_after, "2026-03-01")
        self.assertEqual(b.decisions[0]["cites"], ["reg:A"])
        self.assertFalse(b.decisions[0]["flagged"])
        with self.assertRaises(ValueError):
            B.fold([created(), B.make_event("decision", "b1", "2026-01-02", status="stale")], "2026-02-01")

    def test_stale_by_decay_alone(self):
        events = [
            created(p=prior(1.0, 0.3), swc=0.5),
            B.make_event("decision", "b1", "2026-01-02", status="adopted"),
        ]
        early = B.fold(events, "2026-01-02")["b1"]
        self.assertEqual(early.status, "adopted")
        self.assertGreaterEqual(early.p_benefit, 0.8)
        late = B.fold(events, "2029-06-01")["b1"]
        self.assertEqual(late.status, "stale")
        self.assertLess(late.p_benefit, 0.8)

    def test_adopted_below_threshold_without_decay_is_not_stale(self):
        events = [
            created(p=prior(0.5, 1.0), swc=0.5),
            B.make_event("decision", "b1", "2026-01-02", status="adopted"),
        ]
        self.assertEqual(B.fold(events, "2027-01-01")["b1"].status, "adopted")


class RetractionTest(unittest.TestCase):
    def events(self):
        return [
            created("b1", p=prior(0.0, 1.0, ("reg:A",)), q_per_day=0.0),
            B.make_event("evidence_added", "b1", "2026-01-02", prior=prior(4.0, 1.0, ("doi:bad",))),
            B.make_event("decision", "b1", "2026-01-03", status="adopted"),
            created("b2", p=prior(1.0, 1.0, ("reg:C",))),
        ]

    def test_retract_emits_events_for_citing_beliefs(self):
        views = B.fold(self.events(), "2026-01-10")
        out = B.retract(views, "doi:bad", "2026-01-11")
        self.assertEqual([e["belief_id"] for e in out], ["b1"])
        self.assertEqual(out[0]["kind"], "source_retracted")
        self.assertEqual(out[0]["source_key"], "doi:bad")
        self.assertEqual(B.retract(views, "reg:none", "2026-01-11"), [])

    def test_fold_replays_without_source_and_flags_decisions(self):
        events = self.events()
        views = B.fold(events, "2026-01-10")
        self.assertAlmostEqual(views["b1"].mu, 2.0)
        events += B.retract(views, "doi:bad", "2026-01-11")
        b = B.fold(events, "2026-01-12")["b1"]
        self.assertAlmostEqual(b.mu, 0.0)
        self.assertEqual(b.prior.sources, ("reg:A",))
        self.assertTrue(b.decisions[0]["flagged"])
        self.assertEqual(b.decisions[0]["retracted_sources"], ["doi:bad"])
        self.assertEqual(B.retract(B.fold(events, "2026-01-12"), "doi:bad", "2026-01-13")[0]["belief_id"], "b1")

    def test_evidence_after_retraction_is_excluded(self):
        events = [
            created(p=prior(0.0, 1.0, ("reg:A",))),
            B.make_event("source_retracted", "b1", "2026-01-02", source_key="doi:bad"),
            B.make_event("evidence_added", "b1", "2026-01-03", prior=prior(9.0, 1.0, ("doi:bad",))),
        ]
        self.assertEqual(B.fold(events, "2026-01-04")["b1"].prior.sources, ("reg:A",))


class ReviewTest(unittest.TestCase):
    def test_due_for_review(self):
        events = [
            created("a", p=prior(1.0, 0.3)),
            B.make_event("decision", "a", "2026-01-02", status="adopted"),
            created("b", at="2026-06-01", p=prior()),
            created("c", p=prior()),
            B.make_event("review", "c", "2026-06-01"),
        ]
        views = B.fold(events, "2026-07-01")
        self.assertEqual(views["a"].review_after, "2026-04-02")
        self.assertEqual(views["c"].review_after, "2026-08-30")
        self.assertEqual(B.due_for_review(views, "2026-07-01"), ["a"])
        self.assertEqual(B.due_for_review(views, "2026-09-01"), ["a", "b", "c"])


class RebuildViewsTest(unittest.TestCase):
    def test_writes_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, {})
            for ev in [created("b1", p=prior()), created("b2")]:
                ledger.append_jsonl(B.EVENTS_PATH, ev, author="pineal")
            paths = B.rebuild_views(ledger, "2026-02-01")
            self.assertEqual(paths, ["beliefs/views/b1.json", "beliefs/views/b2.json"])
            view = json.loads((Path(tmp) / paths[0]).read_text(encoding="utf-8"))
            self.assertEqual(view["id"], "b1")
            self.assertEqual(view["claim"]["outcome"], "outcome-y")
            self.assertEqual(view["prior"]["sources"], ["reg:A"])
            self.assertIsNone(ledger.read_json(paths[1])["mu"])

    def test_empty_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(B.rebuild_views(Ledger(tmp, {}), "2026-02-01"), [])


if __name__ == "__main__":
    unittest.main()

"""Tests for homunculus.evidence with small synthetic papers only."""

from __future__ import annotations

import copy
import math
import random
import unittest

from homunculus import agents
from homunculus import evidence as E
from homunculus.beliefs import Prior

ABSTRACT = (
    "We randomized 400 trained  adults (mean age 30) to protocol X or control.\n"
    "Outcome Y improved by 2.0 units (95% CI 1.0 to 3.0). Registered as REG-0001. "
    "Attrition was 5%. Assessors were blinded. Funding: public grant. "
    "Means were 3.25 (n=20) and 3.40 (n=20)."
)

OWNER = E.OwnerProfile(sex="male", age=32, training_status="trained", conditions=(), swc={"outcome-y": 0.5})


def f(value, quote=None):
    return {"value": value, "quote": quote}


def paper(**over) -> dict:
    """A clean synthetic RCT that matches OWNER, every quote taken from ABSTRACT."""
    base = {
        "doi": f("10.0000/Synthetic.1"),
        "pmid": f(None),
        "design": f("rct_parallel", "We randomized 400 trained adults"),
        "n_total": f(400, "randomized 400"),
        "n_per_arm": f([20, 20], "(n=20) and 3.40 (n=20)"),
        "population": {
            "sex": f("mixed", "adults"),
            "age_mean": f(30, "mean age 30"),
            "training_status": f("trained", "trained adults"),
            "bmi_mean": f(None),
            "condition": f("healthy", "adults"),
        },
        "outcome": {
            "name": f("outcome-y", "Outcome Y"),
            "unit": f("u", "units"),
            "is_surrogate": f(False, "Outcome Y"),
        },
        "effect": {
            "type": f("MD", "improved by 2.0 units"),
            "est": f(2.0, "improved by 2.0 units"),
            "ci_lo": f(1.0, "95% CI 1.0 to 3.0"),
            "ci_hi": f(3.0, "95% CI 1.0 to 3.0"),
            "sd_pop": f(None),
        },
        "registration_id": f("REG-0001", "Registered as REG-0001"),
        "preregistered": f(True, "Registered as REG-0001"),
        "attrition_pct": f(5.0, "Attrition was 5%"),
        "blinded_outcome": f(True, "Assessors were blinded"),
        "outcome_switched": f(False, "Registered as REG-0001"),
        "funding_industry_product": f(False, "Funding: public grant"),
        "i2": f(None),
        "k_studies": f(None),
        "peer_reviewed": f(True),
        "dose_response": f(None),
        "reported_means": [
            f({"mean": 3.25, "n": 20, "decimals": 2, "items": 1}, "3.25 (n=20)"),
            f({"mean": 3.40, "n": 20, "decimals": 2, "items": 1}, "3.40 (n=20)"),
        ],
    }
    for path, value in over.items():
        node = base
        parts = path.split("__")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return base


def run(p, **kw):
    kw.setdefault("update_to", [])
    kw.setdefault("swc_paper_units", 0.5)
    return E.score(p, OWNER, **kw)


class SchemaTest(unittest.TestCase):
    def test_fixture_is_schema_valid(self):
        self.assertEqual(agents.validate_json(paper(), E.PAPER_SCHEMA), [])

    def test_minimal_instance_is_valid_and_scores_closed(self):
        minimal = agents.minimal_instance(E.PAPER_SCHEMA)
        self.assertEqual(agents.validate_json(minimal, E.PAPER_SCHEMA), [])
        voided = E.void_fields(minimal, E.verify_quotes(minimal, ABSTRACT))
        s = run(voided)
        self.assertEqual((s.certainty, s.gate), (0.0, "discard"))
        self.assertIn("design_missing", s.flags)

    def test_schema_rejects_unknown_design_and_missing_quote_key(self):
        bad = paper(design={"value": "anecdote", "quote": None})
        self.assertTrue(agents.validate_json(bad, E.PAPER_SCHEMA))
        bad = paper(n_total={"value": 40})
        self.assertTrue(agents.validate_json(bad, E.PAPER_SCHEMA))

    def test_thal2_fields_are_optional_and_typed(self):
        props = E.PAPER_SCHEMA["properties"]
        for name in ("duration_weeks", "funnel_asymmetry", "median_study_n", "randomized"):
            self.assertIn(name, props)
            self.assertNotIn(name, E.PAPER_SCHEMA["required"])
        self.assertNotIn("is_acute", props["outcome"]["required"])
        self.assertIn("pi_lo", props["effect"]["properties"])
        full = paper(duration_weeks=f(8), funnel_asymmetry=f(False), median_study_n=f(30), randomized=f(True),
                     outcome__is_acute=f(False), effect__pi_lo=f(0.5), effect__pi_hi=f(3.5))
        self.assertEqual(agents.validate_json(full, E.PAPER_SCHEMA), [])
        self.assertTrue(agents.validate_json(paper(median_study_n=f(0)), E.PAPER_SCHEMA))

    def test_constants(self):
        self.assertTrue(E.RUBRIC_VERSION.startswith("thal-"))
        self.assertEqual(len(E.DESIGNS), 10)
        self.assertEqual(set(E.PAPER_FIELDS), set(E.PAPER_SCHEMA["properties"]))
        self.assertEqual(E.STATUSES, (*E.GATES, "frontier"))
        self.assertEqual(sum(E.POPULATION_WEIGHTS.values()), 1.0)
        self.assertAlmostEqual(sum(E.PROMISE_WEIGHTS.values()), 1.0)


class VerifyQuotesTest(unittest.TestCase):
    def test_clean_paper_has_no_failures(self):
        self.assertEqual(E.verify_quotes(paper(), ABSTRACT), [])

    def test_whitespace_is_normalized_but_case_is_not(self):
        p = paper(attrition_pct=f(5.0, "Attrition   was\n5%"))
        self.assertEqual(E.verify_quotes(p, ABSTRACT), [])
        p = paper(attrition_pct=f(5.0, "attrition was 5%"))
        self.assertEqual(E.verify_quotes(p, ABSTRACT), ["attrition_pct"])

    def test_fabricated_missing_and_empty_quotes_fail(self):
        p = paper(effect__est=f(9.0, "improved by 9.0 units"), population__bmi_mean=f(24.0),
                  i2=f(None, ""))
        # schema order: population precedes effect, which precedes i2
        self.assertEqual(E.verify_quotes(p, ABSTRACT), ["population/bmi_mean", "effect/est", "i2"])

    def test_identifier_fields_need_no_quote_but_wrong_quote_fails(self):
        self.assertNotIn("doi", E.verify_quotes(paper(), ABSTRACT))
        p = paper(doi=f("10.0000/x", "doi 10.0000/x"))
        self.assertEqual(E.verify_quotes(p, ABSTRACT), ["doi"])

    def test_reported_mean_paths_are_indexed(self):
        p = paper()
        p["reported_means"][1]["quote"] = "3.99 (n=20)"
        self.assertEqual(E.verify_quotes(p, ABSTRACT), ["reported_means/1"])

    def test_new_fields_are_verified_too(self):
        p = paper(duration_weeks=f(8, "8 weeks"))
        self.assertEqual(E.verify_quotes(p, ABSTRACT), ["duration_weeks"])

    def test_void_fields_copies_and_nulls_values(self):
        p = paper()
        out = E.void_fields(p, ["effect/est", "reported_means/0", "registration_id"])
        self.assertIsNone(out["effect"]["est"]["value"])
        self.assertIsNone(out["reported_means"][0]["value"])
        self.assertEqual(out["effect"]["est"]["quote"], "improved by 2.0 units")
        self.assertEqual(p["effect"]["est"]["value"], 2.0)
        self.assertEqual(E.dedupe_key(out), "doi:10.0000/synthetic.1")


class PopulationMatchTest(unittest.TestCase):
    def test_full_match(self):
        self.assertEqual(E.population_match(paper()["population"], OWNER), 1.0)

    def test_weights(self):
        pop = paper()["population"]
        cases = {
            "sex": (f("female"), 0.70),
            "age_mean": (f(50), 0.85),
            "training_status": (f("elite"), 0.825),
            "condition": (f("condition-z"), 0.8),
        }
        for key, (value, expected) in cases.items():
            with self.subTest(key=key):
                self.assertAlmostEqual(E.population_match({**pop, key: value}, OWNER), expected)

    def test_training_is_ordinal(self):
        female = E.OwnerProfile("female", 30, "trained", (), {})
        base = {"sex": "mixed", "age_mean": 30, "training_status": "trained", "condition": "healthy"}
        self.assertAlmostEqual(E.population_match({**base, "sex": "male"}, female), 0.70)
        self.assertAlmostEqual(E.population_match({**base, "training_status": "recreational"}, female), 0.825)
        elite = E.OwnerProfile("female", 30, "elite", (), {})
        self.assertAlmostEqual(E.population_match({**base, "training_status": "untrained"}, elite), 0.65)

    def test_unknowns_earn_nothing(self):
        owner = E.OwnerProfile(None, None, None, (), {})
        # an owner without conditions still matches a healthy sample
        self.assertAlmostEqual(E.population_match(paper()["population"], owner), 0.2)
        self.assertEqual(E.population_match({}, OWNER), 0.0)

    def test_condition_matches_owner_condition_and_plain_values(self):
        owner = E.OwnerProfile("female", 40, "recreational", ("Condition-Z",), {})
        pop = {"sex": "female", "age_mean": 45.0, "training_status": "recreational", "condition": "condition-z"}
        self.assertEqual(E.population_match(pop, owner), 1.0)
        self.assertAlmostEqual(E.population_match({**pop, "condition": "healthy"}, owner), 0.8)


class GrimTest(unittest.TestCase):
    def test_known_values(self):
        self.assertTrue(E.grim_ok(5.18, 28, 2))
        self.assertFalse(E.grim_ok(5.19, 28, 2))
        self.assertTrue(E.grim_ok(3.25, 20, 2))
        self.assertTrue(E.grim_ok(5.19, 28, 2, items=3))
        self.assertTrue(E.grim_ok(-1.5, 2, 1))

    def test_large_n_always_passes(self):
        self.assertTrue(E.grim_ok(5.19, 200, 2))

    def test_seeded_round_trip(self):
        rng = random.Random(7)
        for _ in range(200):
            n, items = rng.randint(1, 60), rng.randint(1, 4)
            total = rng.randint(0, 7 * n * items)
            mean = round(total / (n * items), 2)
            self.assertTrue(E.grim_ok(mean, n, 2, items), (mean, n, items))

    def test_invalid_input_raises(self):
        for args in ((1.0, 0, 2), (1.0, 5, -1), (1.0, 5, 2, 0), (math.nan, 5, 2), (1.0, 5.0, 2), (1.0, True, 2)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                E.grim_ok(*args)


class ScoreTest(unittest.TestCase):
    def test_clean_rct_without_backing_seeds_n_of_1(self):
        s = run(paper())
        self.assertEqual((s.certainty, s.base, s.adjustments, s.vetoed), (4.0, 4.0, (), False))
        self.assertEqual(s.gate, "n_of_1_only")
        self.assertEqual(s.rubric_version, E.RUBRIC_VERSION)

    def test_two_independent_rcts_make_it_actionable(self):
        self.assertEqual(run(paper(), independent_rcts=2).gate, "actionable")

    def test_meta_analysis_needs_two_known_studies(self):
        s = run(paper(design=f("meta_rct"), i2=f(20.0), k_studies=f(8)))
        self.assertEqual((s.certainty, s.gate), (4.0, "actionable"))
        s = run(paper(design=f("meta_rct"), i2=f(20.0)))
        self.assertEqual((s.certainty, s.gate), (4.0, "n_of_1_only"))

    def test_population_mismatch_blocks_actionable(self):
        p = paper(design=f("meta_rct"), i2=f(20.0), k_studies=f(8), population__training_status=f("untrained"))
        s = run(p)
        self.assertEqual(s.adjustments, (("indirectness", -0.5),))
        self.assertEqual((s.certainty, s.gate), (3.5, "n_of_1_only"))
        p = paper(population__age_mean=f(70), population__training_status=f("untrained"),
                  population__condition=f("condition-z"))
        s = run(p, independent_rcts=3)
        self.assertIn(("indirectness", -1.0), s.adjustments)
        self.assertIn("population_mismatch", s.flags)

    def test_retraction_vetoes(self):
        for kind in ("retraction", "Retraction", "withdrawal"):
            s = run(paper(), update_to=[{"type": kind, "source": "retraction-watch"}])
            self.assertEqual((s.certainty, s.vetoed, s.gate), (0.0, True, "discard"))
            self.assertIn("retracted", s.flags)

    def test_expression_of_concern_caps_at_one(self):
        s = run(paper(), update_to=[{"type": "expression-of-concern"}, {"type": "correction"}])
        self.assertEqual((s.certainty, s.vetoed, s.gate), (1.0, False, "n_of_1_only"))
        self.assertIn("cap:concern", s.flags)
        self.assertEqual(run(paper(), update_to=[{"type": "correction"}]).certainty, 4.0)

    def test_base_by_design(self):
        expected = {"cohort_prospective": 2.0, "cross_sectional": 1.0, "animal": 0.5, "in_vitro": 0.25}
        for design, base in expected.items():
            with self.subTest(design=design):
                self.assertEqual(run(paper(design=f(design))).base, base)

    def test_missing_or_unknown_design_is_discarded(self):
        for value in (None, "anecdote"):
            s = run(paper(design=f(value)))
            self.assertEqual((s.certainty, s.gate), (0.0, "discard"))
            self.assertIn("design_missing", s.flags)
        s = run(paper(design=f("review_opinion")))
        self.assertEqual((s.certainty, s.gate, s.flags), (0.0, "discard", ()))

    def test_bias_downgrades_are_capped(self):
        p = paper(attrition_pct=f(35.0), blinded_outcome=f(False), outcome_switched=f(True),
                  registration_id=f(None), preregistered=f(False))
        s = run(p)
        self.assertIn(("bias", -2.0), s.adjustments)
        for flag in ("high_attrition", "unblinded_outcome", "outcome_switched", "unregistered"):
            self.assertIn(flag, s.flags)
        self.assertEqual(s.certainty, 2.0)

    def test_unknown_bias_items_cost_half(self):
        s = run(paper(attrition_pct=f(None), blinded_outcome=f(None)))
        self.assertIn(("bias", -0.5), s.adjustments)
        self.assertEqual(s.certainty, 3.5)

    def test_imprecision_against_swc(self):
        cases = [
            ((1.0, 3.0), 0.5, 0.0, None),               # whole CI beyond SWC
            ((0.2, 3.0), 0.5, -0.5, "ci_near_swc"),     # excludes null, includes trivial effects
            ((-0.2, 3.0), 0.5, -1.0, "ci_crosses_null"),
            ((-1.0, 3.0), 0.5, -2.0, "ci_crosses_both"),
            ((1.0, 3.0), None, -0.5, "swc_unknown"),    # SWC not convertible
        ]
        for (lo, hi), swc, expected, flag in cases:
            with self.subTest(lo=lo, hi=hi, swc=swc):
                s = run(paper(effect__ci_lo=f(lo), effect__ci_hi=f(hi)), swc_paper_units=swc)
                self.assertEqual(dict(s.adjustments).get("imprecision", 0.0), expected)
                if flag:
                    self.assertIn(flag, s.flags)

    def test_missing_or_inverted_ci_is_imprecise(self):
        for lo, hi in ((None, 3.0), (3.0, 1.0)):
            s = run(paper(effect__ci_lo=f(lo), effect__ci_hi=f(hi)))
            self.assertIn(("imprecision", -1.0), s.adjustments)
            self.assertIn("no_ci", s.flags)

    def test_rr_uses_log_scale(self):
        p = paper(effect__type=f("RR"), effect__est=f(0.5), effect__ci_lo=f(0.4), effect__ci_hi=f(0.6))
        self.assertNotIn("imprecision", dict(run(p, swc_paper_units=0.1).adjustments))
        p = paper(effect__type=f("RR"), effect__est=f(0.9), effect__ci_lo=f(0.7), effect__ci_hi=f(1.2))
        self.assertEqual(dict(run(p, swc_paper_units=0.2).adjustments)["imprecision"], -1.0)
        self.assertEqual(dict(run(p, swc_paper_units=0.1).adjustments)["imprecision"], -2.0)

    def test_inconsistency_for_meta_only(self):
        for i2, expected in ((None, -0.5), (60.0, -0.5), (80.0, -1.0), (10.0, 0.0)):
            with self.subTest(i2=i2):
                s = run(paper(design=f("meta_rct"), i2=f(i2)))
                self.assertEqual(dict(s.adjustments).get("inconsistency", 0.0), expected)
        self.assertNotIn("inconsistency", dict(run(paper(i2=f(90.0))).adjustments))
        s = run(paper(design=f("meta_rct"), i2=f(10.0), k_studies=f(2)))
        self.assertEqual(dict(s.adjustments)["inconsistency"], -0.5)
        self.assertIn("few_studies", s.flags)

    def test_trust_downgrades(self):
        p = paper(peer_reviewed=f(False), funding_industry_product=f(True))
        s = run(p)
        self.assertIn(("trust", -1.0), s.adjustments)
        self.assertIn("preprint", s.flags)
        self.assertIn("product_linked_funding", s.flags)
        p = paper(effect__type=f("SMD"), effect__est=f(1.4), effect__ci_lo=f(1.0), effect__ci_hi=f(1.8))
        s = run(p, swc_paper_units=0.2)
        self.assertIn(("trust", -1.0), s.adjustments)
        self.assertIn("implausible_smd", s.flags)

    def test_grim_failure_downgrades(self):
        p = paper()
        p["reported_means"][0]["value"] = {"mean": 3.27, "n": 20, "decimals": 2, "items": 1}
        s = run(p)
        self.assertIn(("trust", -1.0), s.adjustments)
        self.assertIn("grim_fail", s.flags)

    def test_indirectness_surrogate_and_short_duration(self):
        self.assertIn(("indirectness", -0.5), run(paper(outcome__is_surrogate=f(True))).adjustments)
        s = run(paper(outcome__is_acute=f(False), duration_weeks=f(2)))
        self.assertIn(("indirectness", -0.5), s.adjustments)
        self.assertIn("short_duration", s.flags)
        for over in ({"outcome__is_acute": f(True), "duration_weeks": f(2)}, {"outcome__is_acute": f(False)}):
            self.assertNotIn("short_duration", run(paper(**over)).flags)

    def test_observational_upgrades_are_capped(self):
        p = paper(design=f("cohort_prospective"), effect__type=f("SMD"), effect__est=f(0.9),
                  effect__ci_lo=f(0.6), effect__ci_hi=f(1.0), dose_response=f(True))
        s = run(p, swc_paper_units=0.2)
        self.assertIn(("large_effect", 1.0), s.adjustments)
        self.assertIn(("dose_response", 1.0), s.adjustments)
        self.assertEqual(s.certainty, 3.0)
        self.assertIn("cap:upgraded_observational", s.flags)

    def test_very_large_rr_uses_the_whole_upgrade_cap(self):
        p = paper(design=f("cohort_prospective"), effect__type=f("RR"), effect__est=f(0.15),
                  effect__ci_lo=f(0.1), effect__ci_hi=f(0.2), dose_response=f(True))
        s = run(p, swc_paper_units=0.1054)
        self.assertIn(("large_effect", 2.0), s.adjustments)
        self.assertNotIn("dose_response", dict(s.adjustments))

    def test_no_upgrade_for_rct_bias_or_imprecision(self):
        self.assertNotIn("dose_response", dict(run(paper(dose_response=f(True))).adjustments))
        p = paper(design=f("cohort_prospective"), outcome_switched=f(True), dose_response=f(True))
        self.assertNotIn("dose_response", dict(run(p).adjustments))
        p = paper(design=f("cohort_prospective"), effect__ci_lo=f(0.2), dose_response=f(True))
        self.assertNotIn("dose_response", dict(run(p).adjustments))

    def test_replications_add_up_to_one(self):
        p = paper(design=f("cross_sectional"))
        self.assertEqual(run(p, replications=1).certainty, 1.5)
        self.assertEqual(run(p, replications=5).certainty, 2.0)

    def test_gate_thresholds(self):
        self.assertEqual(run(paper(design=f("animal"))).gate, "watchlist")
        s = run(paper(design=f("cross_sectional"), effect__ci_lo=f(-0.1)))
        self.assertEqual((s.certainty, s.gate), (0.0, "discard"))
        s = run(paper(design=f("cohort_prospective")), independent_rcts=2)
        self.assertEqual((s.certainty, s.gate), (2.0, "actionable"))

    def test_certainty_stays_in_range_for_random_papers(self):
        rng = random.Random(11)
        for _ in range(150):
            p = paper(
                design=f(rng.choice(E.DESIGNS)),
                n_total=f(rng.choice([None, 8, 40, 400])),
                attrition_pct=f(rng.choice([None, 3.0, 40.0])),
                blinded_outcome=f(rng.choice([None, True, False])),
                peer_reviewed=f(rng.choice([None, True, False])),
                dose_response=f(rng.choice([None, True])),
                effect__ci_lo=f(rng.uniform(-2.0, 1.9)),
            )
            kw = {"replications": rng.randint(0, 3), "independent_rcts": rng.randint(0, 3),
                  "includes_retracted": rng.random() < 0.2}
            s = run(p, **kw)
            self.assertTrue(0.0 <= s.certainty <= 4.0)
            self.assertIn(s.gate, E.GATES)
            self.assertEqual(s, run(copy.deepcopy(p), **kw))

    def test_score_does_not_mutate_paper(self):
        p = paper()
        before = copy.deepcopy(p)
        run(p)
        self.assertEqual(p, before)


# --------------------------------------------------------------------------
# docs/EVIDENCE_WEIGHTS.md test cases

DOC_OWNER = E.OwnerProfile(sex="female", age=30, training_status="trained", conditions=(), swc={})
SWC_SMD, SWC_RR = 0.2, 0.1054


def doc_paper(design: str, effect=("SMD", 0.5, 0.3, 0.7), *, n=None, sex="mixed", age=28,
              training="trained", **fields) -> dict:
    """Every field null except the case's inputs; defaults follow the fixture line of the doc."""
    kind, est, lo, hi = effect
    p = {name: f(None) for name in E.PAPER_FIELDS if name not in ("population", "outcome", "effect")}
    p.update(
        doi=f("10.0000/doc.case"), design=f(design), n_total=f(n), peer_reviewed=f(True), reported_means=[],
        population={"sex": f(sex), "age_mean": f(age), "training_status": f(training), "bmi_mean": f(None),
                    "condition": f("healthy")},
        outcome={"name": f("y"), "unit": f("u"), "is_surrogate": f(False), "is_acute": f(None)},
        effect={"type": f(kind), "est": f(est), "ci_lo": f(lo), "ci_hi": f(hi), "sd_pop": f(None),
                "pi_lo": f(None), "pi_hi": f(None)},
    )
    for path, value in fields.items():
        node = p
        parts = path.split("__")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = f(value)
    return p


def doc_run(p, swc=SWC_SMD, **kw):
    kw.setdefault("update_to", [])
    return E.score(p, DOC_OWNER, swc_paper_units=swc, **kw)


def case1(**over) -> dict:
    fields = {"k_studies": 12, "i2": 30.0, "registration_id": "CRD0000001", "funnel_asymmetry": False,
              "effect__sd_pop": 2.0, **over}
    return doc_paper("meta_rct", ("SMD", 0.35, 0.22, 0.48), n=900, **fields)


def case7() -> dict:
    return doc_paper("rct_crossover", ("SMD", 0.45, 0.10, 0.80), n=24, age=27, registration_id="REG-7",
                     blinded_outcome=True, attrition_pct=4.0, funding_industry_product=True, effect__sd_pop=2.0)


class RubricCasesTest(unittest.TestCase):
    def check(self, s, adjustments, certainty, gate, flags=()):
        self.assertEqual(s.adjustments, tuple(adjustments))
        self.assertAlmostEqual(s.certainty, certainty)
        self.assertEqual(s.gate, gate)
        for flag in flags:
            self.assertIn(flag, s.flags)

    def shrink(self, p, s) -> float:
        prior = E.to_prior(p, s, "b-doc")
        delta = E._effect_raw(p)[0]
        return prior.mu0 / delta

    def test_case_1_clean_meta_analysis(self):
        p = case1()
        s = doc_run(p)
        self.check(s, (), 4.0, "actionable")
        self.assertEqual(s.flags, ())
        self.assertAlmostEqual(self.shrink(p, s), 1.0)

    def test_case_2_small_male_untrained_rct(self):
        p = doc_paper("rct_parallel", ("SMD", 1.4, 0.3, 2.5), n=16, sex="male", age=22, training="untrained",
                      attrition_pct=10.0)
        s = doc_run(p)
        self.check(s, (("bias", -0.75), ("imprecision", -1.0), ("indirectness", -1.0), ("trust", -1.0)), 0.25,
                   "watchlist", ("blinding_unknown", "unregistered", "below_ois", "population_mismatch",
                                 "implausible_smd"))
        self.assertIsNone(E.to_prior(p, s, "b-doc"))

    def test_case_3_animal_ignores_replication(self):
        p = doc_paper("animal", ("SMD", 2.0, 1.0, 3.0))
        s = doc_run(p, replications=2)
        self.check(s, (), 0.5, "watchlist", ("mechanistic_only",))
        self.assertIsNone(E.to_prior(p, s, "b-doc"))

    def test_case_4_retraction(self):
        s = doc_run(case1(), update_to=[{"type": "retraction", "source": "retraction-watch"}])
        self.check(s, (), 0.0, "discard", ("retracted",))
        self.assertTrue(s.vetoed)

    def test_case_5_expression_of_concern(self):
        p = case1()
        s = doc_run(p, update_to=[{"type": "expression_of_concern"}])
        self.check(s, (), 1.0, "n_of_1_only", ("expression_of_concern", "cap:concern"))
        self.assertAlmostEqual(self.shrink(p, s), 0.25)

    def test_case_6_heterogeneous_small_meta(self):
        p = doc_paper("meta_rct", ("SMD", 0.6, 0.3, 0.9), n=84, k_studies=6, i2=85.0, effect__pi_lo=-0.4,
                      effect__pi_hi=1.6, median_study_n=14, registration_id="CRD0000006", effect__sd_pop=2.0)
        s = doc_run(p)
        self.check(s, (("imprecision", -1.0), ("inconsistency", -1.5), ("publication_bias", -0.5)), 1.0,
                   "n_of_1_only", ("below_ois", "high_heterogeneity", "pi_crosses_null", "small_studies"))
        prior = E.to_prior(p, s, "b-doc")
        se = 0.6 / 3.92 * 2.0
        tau = 2.0 / 3.92 * 2.0
        self.assertAlmostEqual(prior.mu0, 0.25 * 1.2)
        self.assertAlmostEqual(prior.sd0, math.sqrt((4.0 * se) ** 2 + tau ** 2))

    def test_case_7_crossover_with_product_funding(self):
        p = case7()
        s = doc_run(p)
        self.check(s, (("imprecision", -1.5), ("trust", -0.5)), 2.0, "n_of_1_only",
                   ("ci_near_swc", "below_ois", "product_linked_funding"))
        self.assertFalse([flag for flag in s.flags if flag.startswith("cap:")])
        self.assertAlmostEqual(self.shrink(p, s), 0.5)

    def test_case_8_backed_crossover_is_actionable(self):
        p = case7()
        s = doc_run(p, independent_rcts=2)
        self.check(s, (("imprecision", -1.5), ("trust", -0.5)), 2.0, "actionable")
        self.assertAlmostEqual(self.shrink(p, s), 0.5)

    def test_case_9_preprint_is_capped(self):
        p = doc_paper("rct_parallel", ("SMD", 0.5, 0.30, 0.70), n=400, registration_id="REG-9",
                      blinded_outcome=True, attrition_pct=5.0, peer_reviewed=False, effect__sd_pop=2.0)
        s = doc_run(p, independent_rcts=2)
        self.check(s, (("trust", -0.5),), 3.0, "n_of_1_only", ("preprint", "cap:preprint"))
        self.assertAlmostEqual(self.shrink(p, s), 0.8)

    def test_case_10_upgraded_cohort(self):
        p = doc_paper("cohort_prospective", ("RR", 0.45, 0.35, 0.58), n=5000, attrition_pct=10.0,
                      dose_response=True)
        s = doc_run(p, swc=SWC_RR)
        self.check(s, (("large_effect", 1.0), ("dose_response", 1.0)), 3.0, "n_of_1_only",
                   ("cap:upgraded_observational",))
        self.assertAlmostEqual(E.to_prior(p, s, "b-doc").mu0, 0.8 * math.log(0.45))

    def test_case_11_mechanistic_human(self):
        p = doc_paper("mechanistic_human", ("SMD", 0.9, 0.3, 1.5), n=12, outcome__is_surrogate=True)
        s = doc_run(p, replications=2)
        self.check(s, (("imprecision", -1.0), ("indirectness", -0.5), ("replication", 1.0)), 0.5, "watchlist",
                   ("below_ois", "surrogate_outcome"))
        self.assertIsNone(E.to_prior(p, s, "b-doc"))

    def test_case_12_male_sample_for_female_owner(self):
        p = doc_paper("rct_parallel", ("SMD", 0.5, 0.22, 0.78), n=200, sex="male", age=30, registration_id="R12",
                      blinded_outcome=True, attrition_pct=5.0, effect__sd_pop=2.0)
        s = doc_run(p, independent_rcts=2)
        self.check(s, (("indirectness", -0.5),), 3.5, "n_of_1_only", ("population_partial",))
        self.assertAlmostEqual(self.shrink(p, s), 0.75)

    def test_ois(self):
        self.assertEqual(E.optimal_information_size(doc_paper("rct_parallel", ("SMD", 0.1, 0.0, 0.2))), 785)
        md = doc_paper("rct_parallel", ("MD", 5.0, 1.0, 9.0), effect__sd_pop=10.0)
        self.assertEqual(E.optimal_information_size(md), 126)
        self.assertEqual(E.optimal_information_size(doc_paper("rct_parallel", ("MD", 5.0, 1.0, 9.0))), 126)
        self.assertEqual(E.optimal_information_size(doc_paper("rct_crossover", ("SMD", 0.45, 0.1, 0.8)),
                                                    "rct_crossover"), 39)
        self.assertIsNone(E.optimal_information_size(doc_paper("rct_parallel", ("RR", 0.5, 0.4, 0.6))))
        s = doc_run(doc_paper("rct_parallel", registration_id="R", blinded_outcome=True, attrition_pct=1.0))
        self.assertIn(("imprecision", -1.0), s.adjustments)
        self.assertIn("n_unknown", s.flags)

    def test_publication_bias_needs_ten_studies_for_funnel(self):
        for k, expected in ((8, 0.0), (12, -1.0)):
            with self.subTest(k=k):
                s = doc_run(case1(k_studies=k, funnel_asymmetry=True))
                self.assertEqual(dict(s.adjustments).get("publication_bias", 0.0), expected)

    def test_smallest_cap_binds_and_flags_alone(self):
        p = doc_paper("rct_parallel", n=8, registration_id="R", blinded_outcome=True, attrition_pct=1.0,
                      peer_reviewed=False)
        s = doc_run(p)
        self.assertEqual(s.certainty, 1.0)
        self.assertEqual([x for x in s.flags if x.startswith("cap:")], ["cap:tiny_n"])

    def test_clamp_runs_once_after_replication(self):
        p = doc_paper("cross_sectional", ("SMD", 0.5, -0.1, 1.1), n=400, peer_reviewed=False)
        s = doc_run(p, replications=2)
        self.assertEqual(s.adjustments, (("imprecision", -1.0), ("trust", -0.5), ("replication", 1.0)))
        self.assertAlmostEqual(s.certainty, 0.5)

    def test_pooled_retracted_study_caps_at_one(self):
        s = doc_run(case1(), includes_retracted=True)
        self.check(s, (), 1.0, "n_of_1_only", ("cap:includes_retracted",))

    def test_thal1_paper_without_new_fields_scores(self):
        p = case1()
        for name in ("duration_weeks", "funnel_asymmetry", "median_study_n", "randomized"):
            p.pop(name)
        p["outcome"].pop("is_acute")
        for name in ("pi_lo", "pi_hi"):
            p["effect"].pop(name)
        self.assertEqual(agents.validate_json(p, E.PAPER_SCHEMA), [])
        self.check(doc_run(p), (), 4.0, "actionable")


class ToPriorTest(unittest.TestCase):
    SE = 2.0 / 3.92  # MD 2.0 [1, 3]; z = 3.92 -> 0.75

    def test_level_four_md(self):
        p = paper()
        s = run(p)
        prior = E.to_prior(p, s, "b-synthetic")
        self.assertIsInstance(prior, Prior)
        self.assertAlmostEqual(prior.mu0, 0.75 * 2.0)
        self.assertAlmostEqual(prior.sd0, math.sqrt(self.SE ** 2 + 1.0))
        self.assertEqual(prior.sources, ("reg:REG-0001",))
        self.assertEqual((prior.certainty, prior.rubric_version), (4.0, E.RUBRIC_VERSION))

    def test_fractional_certainty_floors_to_conservative_level(self):
        p = paper(attrition_pct=f(None), blinded_outcome=f(None))
        s = run(p)
        self.assertEqual(s.certainty, 3.5)
        prior = E.to_prior(p, s, "b1")
        self.assertAlmostEqual(prior.mu0, 0.75 * 2.0)
        self.assertAlmostEqual(prior.sd0, math.sqrt((1.5 * self.SE) ** 2 + 1.0))

    def test_levels_match_table(self):
        p = paper()
        for c, shrink, k in ((4.0, 0.75, 1.0), (3.0, 0.75, 1.5), (2.0, 0.5, 2.5), (1.0, 0.25, 4.0)):
            s = E.Score(c, 4.0, (), False, (), "n_of_1_only", "thal-2")
            prior = E.to_prior(p, s, "b1")
            self.assertAlmostEqual(prior.mu0, shrink * 2.0)
            self.assertAlmostEqual(prior.sd0, math.sqrt((k * self.SE) ** 2 + 1.0))

    def test_z_and_n_bands(self):
        s = E.Score(4.0, 4.0, (), False, (), "n_of_1_only", "thal-2")
        tight = paper(effect__ci_lo=f(1.5), effect__ci_hi=f(2.5))  # z = 7.84
        self.assertAlmostEqual(E.to_prior(tight, s, "b1").mu0, 2.0)
        for n, factor in ((None, 0.3), (19, 0.3), (20, 0.5), (60, 0.75), (100, 1.0)):
            with self.subTest(n=n):
                self.assertAlmostEqual(E.to_prior(paper(effect__ci_lo=f(1.5), effect__ci_hi=f(2.5),
                                                        n_total=f(n)), s, "b1").mu0, factor * 2.0)
        wide = paper(effect__ci_lo=f(-0.5), effect__ci_hi=f(4.5))  # z = 1.57
        self.assertAlmostEqual(E.to_prior(wide, s, "b1").mu0, 0.25 * 2.0)

    def test_smd_needs_sd_pop(self):
        p = paper(effect__type=f("SMD"), effect__est=f(0.4), effect__ci_lo=f(0.2), effect__ci_hi=f(0.6))
        s = E.Score(4.0, 4.0, (), False, (), "actionable", "thal-2")
        self.assertIsNone(E.to_prior(p, s, "b1"))
        p["effect"]["sd_pop"] = f(5.0)
        prior = E.to_prior(p, s, "b1")
        self.assertAlmostEqual(prior.mu0, 0.75 * 2.0)
        self.assertAlmostEqual(prior.sd0, math.sqrt((0.4 / 3.92 * 5.0) ** 2 + 1.0))

    def test_rr_prior_is_log_scale(self):
        p = paper(effect__type=f("RR"), effect__est=f(0.5), effect__ci_lo=f(0.4), effect__ci_hi=f(0.625))
        s = E.Score(4.0, 4.0, (), False, (), "actionable", "thal-2")
        self.assertAlmostEqual(E.to_prior(p, s, "b1").mu0, math.log(0.5))

    def test_none_when_gate_design_or_effect_forbid(self):
        p = paper()
        for s in (
            E.Score(0.0, 4.0, (), True, ("retracted",), "discard", "thal-2"),
            E.Score(0.5, 0.5, (), False, (), "watchlist", "thal-2"),
        ):
            self.assertIsNone(E.to_prior(p, s, "b1"))
        s = run(p)
        self.assertIsNone(E.to_prior(paper(effect__est=f(None)), s, "b1"))
        self.assertIsNone(E.to_prior(paper(effect__type=f(None)), s, "b1"))
        self.assertIsNone(E.to_prior(paper(design=f("animal")), s, "b1"))

    def test_belief_id_required(self):
        with self.assertRaises(ValueError):
            E.to_prior(paper(), run(paper()), "")


# --------------------------------------------------------------------------
# Promise axis and frontier lane


def animal(**over) -> dict:
    """A clean rat study: randomized, blinded, preregistered, n 24, large effect."""
    fields = {"randomized": True, "blinded_outcome": True, "registration_id": "PCT-0001", "dose_response": True}
    fields.update(over)
    return doc_paper("animal", ("SMD", 1.2, 0.6, 1.8), n=24, **fields)


FULL_INPUTS = E.PromiseInput(plausible=True, coherent=True, conserved_pathway=True, human_link_share=2 / 3,
                             dose_mg_per_kg=50.0, species="rat", human_max_mg_per_kg=10.0)


class PromiseTest(unittest.TestCase):
    def test_human_equivalent_dose_uses_km_factors(self):
        self.assertAlmostEqual(E.human_equivalent_dose(100.0, "rat"), 100.0 * 6.0 / 37.0)
        self.assertAlmostEqual(E.human_equivalent_dose(10.0, "Mouse"), 10.0 * 3.0 / 37.0)
        self.assertEqual(E.human_equivalent_dose(5.0, "human"), 5.0)
        for args in ((10.0, "unicorn"), (-1.0, "rat"), (math.nan, "rat"), (True, "rat")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                E.human_equivalent_dose(*args)

    def test_quality_checks_by_design(self):
        self.assertEqual(E.quality(paper()).score, 1.0)
        q = E.quality(animal())
        self.assertEqual((q.score, q.weak), (1.0, False))
        self.assertEqual([name for name, _ in q.checks], ["randomized", "blinded", "preregistered", "sample_size"])
        weak = E.quality(animal(randomized=None, blinded_outcome=None, registration_id=None))
        self.assertEqual((weak.score, weak.weak), (0.25, True))
        cohort = E.quality(doc_paper("cohort_prospective", n=5000))
        self.assertEqual(([n for n, _ in cohort.checks], cohort.score, cohort.weak),
                         (["preregistered", "sample_size"], 0.5, False))
        self.assertTrue(E.quality(paper(outcome_switched=f(True))).weak)
        self.assertTrue(E.quality(paper(design=f(None))).weak)

    def test_clean_animal_study_has_high_promise(self):
        p = E.promise(animal(), FULL_INPUTS)
        expected = 4 * (0.30 * 1.0 + 0.25 * (2 / 3) + 0.10 + 0.15 + 0.20 * 1.0)
        self.assertAlmostEqual(p.value, expected)
        self.assertFalse(p.capped)
        self.assertEqual([name for name, _ in p.components], list(E.PROMISE_WEIGHTS))

    def test_dose_must_translate(self):
        too_high = E.PromiseInput(**{**FULL_INPUTS.__dict__, "dose_mg_per_kg": 100.0})  # HED 16.2 > 10
        self.assertEqual(dict(E.promise(animal(), too_high).components)["dose"], 0.0)
        no_species = E.PromiseInput(dose_mg_per_kg=5.0, human_max_mg_per_kg=10.0)
        self.assertEqual(dict(E.promise(animal(), no_species).components)["dose"], 0.0)
        self.assertEqual(dict(E.promise(paper(), no_species).components)["dose"], 1.0)  # human study

    def test_benefit_against_swc(self):
        inputs = E.PromiseInput(swc_paper_units=2.0)
        self.assertAlmostEqual(dict(E.promise(paper(), inputs).components)["benefit"], 0.5)
        self.assertEqual(dict(E.promise(paper()).components)["benefit"], 0.0)  # MD without SWC or sd_pop

    def test_weak_methods_cap_both_axes(self):
        sloppy = animal(randomized=None, blinded_outcome=None, registration_id=None)
        p = E.promise(sloppy, FULL_INPUTS)
        self.assertEqual(p.value, 1.0)
        self.assertTrue(p.capped)
        rct = paper(outcome_switched=f(True))
        s = run(rct)
        self.assertEqual(s.certainty, 3.0)
        a = E.appraise(rct, s, FULL_INPUTS)
        self.assertEqual(a.certainty, 1.0)
        self.assertIn("quality_weak", a.flags)

    def test_clean_animal_study_enters_frontier(self):
        p = animal()
        a = E.appraise(p, doc_run(p), FULL_INPUTS)
        self.assertEqual((a.status, a.frontier_blockers, a.certainty), ("frontier", (), 0.5))
        self.assertGreaterEqual(a.promise, E.FRONTIER_MIN_PROMISE)

    def test_frontier_entry_rules(self):
        sloppy = animal(randomized=None, blinded_outcome=None, registration_id=None)
        a = E.appraise(sloppy, doc_run(sloppy), FULL_INPUTS)
        self.assertEqual((a.status, a.frontier_blockers), ("watchlist", ("low_promise", "weak_methods")))
        a = E.appraise(animal(), doc_run(animal()), E.PromiseInput())
        self.assertEqual(a.frontier_blockers, ("low_promise",))
        a = E.appraise(paper(), run(paper()), E.PromiseInput(**{**FULL_INPUTS.__dict__, "swc_paper_units": 0.5}))
        self.assertEqual((a.status, a.frontier_blockers), ("n_of_1_only", ("certain_enough",)))
        retracted = doc_run(animal(), update_to=[{"type": "retraction"}])
        a = E.appraise(animal(), retracted, FULL_INPUTS)
        self.assertEqual((a.status, a.promise), ("discard", 0.0))
        self.assertEqual(a.frontier_blockers[:2], ("vetoed", "discarded"))
        concern = doc_run(animal(), update_to=[{"type": "expression_of_concern"}])
        self.assertEqual(E.appraise(animal(), concern, FULL_INPUTS).frontier_blockers, ("concern",))
        weak_cross = doc_paper("cross_sectional", ("SMD", 0.5, -0.1, 1.1), n=400, peer_reviewed=False)
        s = doc_run(weak_cross)
        self.assertEqual(s.gate, "discard")
        self.assertIn("discarded", E.appraise(weak_cross, s, FULL_INPUTS).frontier_blockers)


class CompareExtractionsTest(unittest.TestCase):
    def test_identical_and_tolerant(self):
        a, b = paper(), paper()
        b["effect"]["est"]["value"] = 2.01
        b["outcome"]["name"]["value"] = "Outcome-Y "
        b["design"]["quote"] = "different quote"
        self.assertEqual(E.compare_extractions(a, b), [])

    def test_disagreements(self):
        a = paper()
        b = paper(n_total=f(41), effect__est=f(2.2), blinded_outcome=f(None), population__sex=f("male"))
        b["reported_means"].pop()
        self.assertEqual(
            E.compare_extractions(a, b),
            ["blinded_outcome", "effect/est", "n_total", "population/sex", "reported_means/1"],
        )

    def test_missing_new_field_equals_null(self):
        self.assertEqual(E.compare_extractions(paper(), paper(duration_weeks=f(None))), [])
        self.assertEqual(E.compare_extractions(paper(), paper(duration_weeks=f(6))), ["duration_weeks"])

    def test_small_values_use_absolute_tolerance_and_bool_is_not_number(self):
        a, b = paper(i2=f(0.0)), paper(i2=f(0.005))
        self.assertEqual(E.compare_extractions(a, b), [])
        a, b = paper(n_total=f(1)), paper(n_total=f(True))
        self.assertEqual(E.compare_extractions(a, b), ["n_total"])
        self.assertEqual(E.compare_extractions(a, b, tol=0.5), ["n_total"])

    def test_nested_values_compare_by_content(self):
        a, b = paper(), paper()
        b["reported_means"][0]["value"] = {"mean": 3.25, "n": 21, "decimals": 2, "items": 1}
        self.assertEqual(E.compare_extractions(a, b), ["reported_means/0"])


class IdentifierTest(unittest.TestCase):
    def test_dedupe_key_priority(self):
        self.assertEqual(E.dedupe_key(paper(registration_id=f(" reg-0001 "))), "reg:REG-0001")
        p = paper(registration_id=f(None), doi=f("https://doi.org/10.0000/ABC"))
        self.assertEqual(E.dedupe_key(p), "doi:10.0000/abc")
        p = paper(registration_id=f(None), doi=f(None), pmid=f("12345"))
        self.assertEqual(E.dedupe_key(p), "pmid:12345")
        with self.assertRaises(ValueError):
            E.dedupe_key(paper(registration_id=f(None), doi=f(None), pmid=f("  ")))

    def test_slug(self):
        self.assertEqual(E.slug("10.1000/X"), "10-1000-x")
        self.assertEqual(E.slug("doi:10.1000/x"), "10-1000-x")
        self.assertEqual(E.slug("12345"), "12345")
        long = E.slug("10.1000/" + "a" * 300)
        self.assertLessEqual(len(long), 100)
        self.assertRegex(long, r"^[a-z0-9-]+$")
        self.assertNotEqual(long, E.slug("10.1000/" + "a" * 299))
        with self.assertRaises(ValueError):
            E.slug("///")


if __name__ == "__main__":
    unittest.main()

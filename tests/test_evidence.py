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
    "We randomized 40 trained  adults (mean age 30) to protocol X or control.\n"
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
        "design": f("rct_parallel", "We randomized 40 trained adults"),
        "n_total": f(40, "randomized 40"),
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

    def test_constants(self):
        self.assertEqual(E.RUBRIC_VERSION, "thal-1")
        self.assertEqual(len(E.DESIGNS), 10)
        self.assertEqual(set(E.PAPER_FIELDS), set(E.PAPER_SCHEMA["properties"]))


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
            "sex": (f("female"), 0.75),
            "age_mean": (f(50), 0.75),
            "training_status": (f("elite"), 0.7),
            "condition": (f("condition-z"), 0.8),
        }
        for key, (value, expected) in cases.items():
            with self.subTest(key=key):
                self.assertAlmostEqual(E.population_match({**pop, key: value}, OWNER), expected)

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
        self.assertEqual(s.rubric_version, "thal-1")

    def test_two_independent_rcts_make_it_actionable(self):
        self.assertEqual(run(paper(), independent_rcts=2).gate, "actionable")

    def test_meta_analysis_is_actionable(self):
        p = paper(design=f("meta_rct"), i2=f(20.0), k_studies=f(8))
        s = run(p)
        self.assertEqual((s.certainty, s.gate), (4.0, "actionable"))

    def test_population_mismatch_blocks_actionable(self):
        p = paper(design=f("meta_rct"), i2=f(20.0), population__training_status=f("elite"))
        s = run(p)
        self.assertEqual(s.adjustments, (("population", -0.5),))
        self.assertEqual((s.certainty, s.gate), (3.5, "n_of_1_only"))
        p = paper(population__age_mean=f(70), population__training_status=f("untrained"))
        s = run(p, independent_rcts=3)
        self.assertIn(("population", -1.0), s.adjustments)
        self.assertIn("population_mismatch", s.flags)

    def test_retraction_vetoes(self):
        for kind in ("retraction", "Retraction", "withdrawal"):
            s = run(paper(), update_to=[{"type": kind, "source": "retraction-watch"}])
            self.assertEqual((s.certainty, s.vetoed, s.gate), (0.0, True, "discard"))
            self.assertIn("retracted", s.flags)

    def test_expression_of_concern_caps_at_one(self):
        s = run(paper(), update_to=[{"type": "expression-of-concern"}, {"type": "correction"}])
        self.assertEqual((s.certainty, s.vetoed, s.gate), (1.0, False, "n_of_1_only"))
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
            ((1.0, 3.0), 0.5, 0.0),     # whole CI beyond SWC
            ((0.2, 3.0), 0.5, -0.5),    # excludes null, includes trivial effects
            ((-0.2, 3.0), 0.5, -1.0),   # includes null
            ((-1.0, 3.0), 0.5, -2.0),   # includes benefit and harm beyond SWC
            ((1.0, 3.0), None, -0.5),   # SWC not convertible
        ]
        for (lo, hi), swc, expected in cases:
            with self.subTest(lo=lo, hi=hi, swc=swc):
                p = paper(effect__ci_lo=f(lo), effect__ci_hi=f(hi))
                s = run(p, swc_paper_units=swc)
                self.assertEqual(dict(s.adjustments).get("imprecision", 0.0), expected)

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

    def test_surrogate_outcome(self):
        s = run(paper(outcome__is_surrogate=f(True)))
        self.assertIn(("indirectness", -0.5), s.adjustments)

    def test_observational_upgrades(self):
        p = paper(design=f("cohort_prospective"), effect__type=f("SMD"), effect__est=f(0.9),
                  effect__ci_lo=f(0.6), effect__ci_hi=f(1.0), dose_response=f(True))
        s = run(p, swc_paper_units=0.2)
        self.assertIn(("large_effect", 1.0), s.adjustments)
        self.assertIn(("dose_response", 1.0), s.adjustments)
        self.assertEqual(s.certainty, 4.0)

    def test_no_upgrade_for_rct_or_serious_bias(self):
        self.assertNotIn("dose_response", dict(run(paper(dose_response=f(True))).adjustments))
        p = paper(design=f("cohort_prospective"), outcome_switched=f(True), dose_response=f(True))
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
                attrition_pct=f(rng.choice([None, 3.0, 40.0])),
                blinded_outcome=f(rng.choice([None, True, False])),
                peer_reviewed=f(rng.choice([None, True, False])),
                dose_response=f(rng.choice([None, True])),
                effect__ci_lo=f(rng.uniform(-2.0, 1.9)),
            )
            kw = {"replications": rng.randint(0, 3), "independent_rcts": rng.randint(0, 3)}
            s = run(p, **kw)
            self.assertTrue(0.0 <= s.certainty <= 4.0)
            self.assertIn(s.gate, E.GATES)
            self.assertEqual(s, run(copy.deepcopy(p), **kw))

    def test_score_does_not_mutate_paper(self):
        p = paper()
        before = copy.deepcopy(p)
        run(p)
        self.assertEqual(p, before)


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


class ToPriorTest(unittest.TestCase):
    def test_level_four_md(self):
        p = paper()
        s = run(p)
        prior = E.to_prior(p, s, "b-synthetic")
        self.assertIsInstance(prior, Prior)
        se = 2.0 / 3.92
        self.assertAlmostEqual(prior.mu0, 2.0)
        self.assertAlmostEqual(prior.sd0, math.sqrt(se ** 2 + 1.0))
        self.assertEqual(prior.sources, ("reg:REG-0001",))
        self.assertEqual((prior.certainty, prior.rubric_version), (4.0, "thal-1"))

    def test_fractional_certainty_floors_to_conservative_level(self):
        p = paper(attrition_pct=f(None), blinded_outcome=f(None))
        s = run(p)
        self.assertEqual(s.certainty, 3.5)
        prior = E.to_prior(p, s, "b1")
        se = 2.0 / 3.92
        self.assertAlmostEqual(prior.mu0, 0.8 * 2.0)
        self.assertAlmostEqual(prior.sd0, math.sqrt((1.5 * se) ** 2 + 1.0))

    def test_levels_match_table(self):
        p = paper()
        for c, shrink, k in ((4.0, 1.0, 1.0), (3.0, 0.8, 1.5), (2.0, 0.5, 2.5), (1.0, 0.25, 4.0)):
            s = E.Score(c, 4.0, (), False, (), "n_of_1_only", "thal-1")
            prior = E.to_prior(p, s, "b1")
            self.assertAlmostEqual(prior.mu0, shrink * 2.0)
            self.assertAlmostEqual(prior.sd0, math.sqrt((k * 2.0 / 3.92) ** 2 + 1.0))

    def test_smd_needs_sd_pop(self):
        p = paper(effect__type=f("SMD"), effect__est=f(0.4), effect__ci_lo=f(0.2), effect__ci_hi=f(0.6))
        s = E.Score(4.0, 4.0, (), False, (), "actionable", "thal-1")
        self.assertIsNone(E.to_prior(p, s, "b1"))
        p["effect"]["sd_pop"] = f(5.0)
        prior = E.to_prior(p, s, "b1")
        self.assertAlmostEqual(prior.mu0, 2.0)
        self.assertAlmostEqual(prior.sd0, math.sqrt((0.4 / 3.92 * 5.0) ** 2 + 1.0))

    def test_rr_prior_is_log_scale(self):
        p = paper(effect__type=f("RR"), effect__est=f(0.5), effect__ci_lo=f(0.25), effect__ci_hi=f(1.0))
        s = E.Score(4.0, 4.0, (), False, (), "actionable", "thal-1")
        prior = E.to_prior(p, s, "b1")
        self.assertAlmostEqual(prior.mu0, math.log(0.5))

    def test_none_when_gate_or_effect_forbid(self):
        p = paper()
        for s in (
            E.Score(0.0, 4.0, (), True, ("retracted",), "discard", "thal-1"),
            E.Score(0.5, 0.5, (), False, (), "watchlist", "thal-1"),
        ):
            self.assertIsNone(E.to_prior(p, s, "b1"))
        s = run(p)
        self.assertIsNone(E.to_prior(paper(effect__est=f(None)), s, "b1"))
        self.assertIsNone(E.to_prior(paper(effect__type=f(None)), s, "b1"))

    def test_belief_id_required(self):
        with self.assertRaises(ValueError):
            E.to_prior(paper(), run(paper()), "")


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

"""Tests for homunculus.guardrails. All values are small synthetic fixtures."""

from __future__ import annotations

import copy
import random
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from homunculus import guardrails as g
from homunculus.checkins import WORDING_VERSION
from homunculus.metrics import RhrState
from homunculus.plans import compute_diff
from homunculus.store import CheckinItem, DailyValue, HealthEvent, Ledger

RNG_SEED = 4711
DAY = "2026-03-10"

FEATURES = {
    "ex-row": {"exercise_id": "ex-row", "pattern": "horizontal pull", "contraction": "concentric",
               "energy_system": "glycolytic", "joint_load": {"shoulder": "moderate"}},
    "ex-row2": {"exercise_id": "ex-row2", "pattern": "horizontal pull", "contraction": "concentric",
                "energy_system": "glycolytic", "joint_load": {"shoulder": "low"}},
    "ex-press": {"exercise_id": "ex-press", "pattern": "horizontal push", "contraction": "concentric",
                 "energy_system": "glycolytic", "joint_load": {"shoulder": "moderate"}},
    "ex-squat": {"exercise_id": "ex-squat", "pattern": "squat", "contraction": "concentric",
                 "energy_system": "glycolytic", "joint_load": {"knee": "high"}},
    "ex-hinge": {"exercise_id": "ex-hinge", "pattern": "hinge", "contraction": "concentric",
                 "energy_system": "glycolytic", "joint_load": {"hip": "moderate"}},
    "ex-nordic": {"exercise_id": "ex-nordic", "pattern": "hinge", "contraction": "eccentric overload",
                  "energy_system": "glycolytic", "joint_load": {"knee": "moderate"}},
    "ex-squat2": {"exercise_id": "ex-squat2", "pattern": "squat", "contraction": "concentric",
                  "energy_system": "glycolytic", "joint_load": {"knee": "moderate"}},
    "ex-jump": {"exercise_id": "ex-jump", "pattern": "squat", "contraction": "plyometric",
                "energy_system": "alactic", "joint_load": {"knee": "high"}},
    "ex-row-heavy": {"exercise_id": "ex-row-heavy", "pattern": "horizontal pull", "contraction": "concentric",
                     "energy_system": "glycolytic", "joint_load": {"shoulder": "high"}},
}
HISTORY = ("ex-row2", "ex-hinge", "ex-squat2", "ex-jump", "ex-row-heavy")  # used in earlier plan versions


def _ex(eid: str, sets: int = 3, novel: bool = False, probation: bool | None = None) -> dict:
    return {"exercise_id": eid, "sets": sets, "reps": 8, "load_kg": 20.0, "rir": 2,
            "novel": novel, "probation": novel if probation is None else probation}


def _plan() -> dict:
    return {
        "version": 1, "parent": None, "effective_from": "2026-03-02T00:00:00Z",
        "valid_until": "2026-03-09T00:00:00Z", "author": "owner", "ratified_by": "owner",
        "data_snapshot_sha": None, "rationale_refs": [],
        "sessions": [
            {"id": "s1", "weekday": 0, "kind": "strength", "duration_min": 50, "target_rpe": 6,
             "exercises": [_ex("ex-row"), _ex("ex-press")]},
            {"id": "s2", "weekday": 3, "kind": "strength", "duration_min": 50, "target_rpe": 6,
             "exercises": [_ex("ex-squat")]},
        ],
        "nutrition": {"kcal_target": 2500, "protein_g_per_kg": 1.6, "notes_de": ""},
        "supplements": [],
        "experiments": [],
        "modulation": {"max_daily_load_cut_pct": 30, "allow_swap": True, "readiness_low_action": "shorten"},
    }


def _ctx(**over) -> g.GuardContext:
    base = dict(
        weekly_loads=[600.0, 600.0, 600.0, 600.0], hard_sessions_last_week=0, days_off=0,
        ea_7d=45.0, kcal_current=2500.0, weight_kg=70.0, ffm_kg=58.0, auto_changes_3wk_pct=0.0,
        red_flag_active=False, ed_watch=False,
        injury_constraints={g.EXERCISE_FEATURES_KEY: FEATURES},
    )
    base.update(over)
    return g.GuardContext(**base)


def _check(new: dict, ctx: g.GuardContext | None = None, rules: g.Rules | None = None,
           cur: dict | None = None, refs: list[str] | None = None) -> g.Verdict:
    cur = cur or _plan()
    diff = compute_diff(cur, new)
    for op in diff:
        op["refs"] = list(refs or [])
    return g.check_diff(cur, new, diff, ctx or _ctx(), rules or g.Rules())


def _with_duration(minutes: float) -> dict:
    plan = _plan()
    plan["sessions"][0]["duration_min"] = minutes
    return plan


class NamesAndForbiddenTests(unittest.TestCase):
    def test_normalize_name(self) -> None:
        self.assertEqual(g.normalize_name("  Kölner-Liste!  "), "kolner liste")
        self.assertEqual(g.normalize_name("Kreatin  Monohydrat (Pulver)"), "kreatin monohydrat pulver")
        self.assertEqual(g.normalize_name("Straße"), "strasse")

    def test_forbidden_default_categories(self) -> None:
        rules = g.Rules()
        self.assertEqual(g.forbidden_match("Clenbuterol 40 mcg", rules), "wada_prohibited")
        self.assertEqual(g.forbidden_match("Furosemid zum Entwässern", rules), "dehydration_cut")
        self.assertEqual(g.forbidden_match("Hypoxiezelt nachts", rules), "altitude_protocol")
        self.assertIsNone(g.forbidden_match("Kreatin", rules))
        self.assertIsNone(g.forbidden_match("Tempo-Lauf", rules))  # 'epo' only as a whole word
        self.assertIsNone(g.forbidden_match("Epochen-Plan", rules))

    def test_forbidden_compounds_and_brands(self) -> None:
        rules = g.Rules()
        cases = {
            "Testosteronspritze": "wada_prohibited", "Entwässerungstabletten": "dehydration_cut",
            "Ozempic": "prescription_drug", "Wegovy": "prescription_drug", "Mounjaro": "prescription_drug",
            "Clen": "wada_prohibited", "Anavar": "wada_prohibited", "ein Diuretikum": "dehydration_cut",
            "Lasix": "dehydration_cut", "Saunaanzug": "dehydration_cut",
        }
        for text, category in cases.items():
            self.assertEqual(g.forbidden_match(text, rules), category, text)

    def test_forbidden_custom(self) -> None:
        rules = g.Rules(forbidden={"custom": ["Fantasy Stack"]})
        self.assertEqual(g.forbidden_match("my fantasy-stack", rules), "custom")


class SupplementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = g.Rules(
            supplements={"Kreatin": {"max_dose": 5, "unit": "g", "aliases": ["creatine"]},
                         "Koffein": {"max_dose": 200, "unit": "mg"}},
            interactions=[{"supplement": "Koffein", "med": "TestMed"}],
            meds=["TestMed 10 mg"],
        )

    def test_pass(self) -> None:
        self.assertEqual(g.supplement_check("Creatine", 3, "g", "Kölner Liste", self.rules), [])

    def test_problems(self) -> None:
        self.assertEqual(g.supplement_check("Kreatin", 3, "g", None, self.rules), ["untested"])
        self.assertEqual(g.supplement_check("Kreatin", 9, "g", "Informed Sport", self.rules), ["dose_above_max"])
        self.assertEqual(g.supplement_check("Kreatin", 3, "mg", "Informed Sport", self.rules), ["unit_mismatch"])
        self.assertEqual(g.supplement_check("Kreatin", 0, "g", "Informed Sport", self.rules), ["dose_invalid"])
        self.assertEqual(g.supplement_check("Zaubertrank", 1, "g", "Informed Sport", self.rules),
                         ["not_allowlisted"])
        self.assertEqual(g.supplement_check("Koffein", 100, "mg", "Informed Sport", self.rules),
                         ["interaction:testmed"])
        self.assertEqual(g.supplement_check("DMAA", 1, "mg", "Informed Sport", self.rules),
                         ["forbidden:wada_prohibited", "not_allowlisted"])

    def test_empty_allowlist_fails_closed(self) -> None:
        self.assertIn("not_allowlisted", g.supplement_check("Kreatin", 3, "g", "Informed Sport", g.Rules()))


class NutritionTests(unittest.TestCase):
    def test_energy_availability(self) -> None:
        self.assertAlmostEqual(g.energy_availability(2400, 500, 50), 38.0)
        with self.assertRaises(ValueError):
            g.energy_availability(2000, 0, 0)

    def test_ed_watch(self) -> None:
        rules = g.Rules()
        ok_ea = [40.0] * 7
        self.assertFalse(g.ed_watch([2400.0] * 7, ok_ea, [1] * 7, ["Gut geschlafen"], rules))
        self.assertTrue(g.ed_watch([2400.0] * 7, [25, 40, 28, 40, 29, 40, 40], [1] * 7, [], rules))
        self.assertFalse(g.ed_watch([2400.0] * 7, [25, 40, 28, 40, None, 40, 40], [1] * 7, [], rules))
        self.assertTrue(g.ed_watch([2400.0] * 7, ok_ea, [1, 2, 1], [], rules))
        self.assertTrue(g.ed_watch([2400.0] * 7, ok_ea, [1] * 7, ["Heute kaum gegessen"], rules))
        self.assertTrue(g.ed_watch([0, 0, 0, None, None, None, None], [None] * 7, [], [], rules))
        # only the last 7 days count
        self.assertFalse(g.ed_watch([2400.0] * 10, [20, 20, 20] + [40.0] * 7, [], [], rules))


class ExerciseAdmissibleTests(unittest.TestCase):
    def test_admissible_and_violations(self) -> None:
        feats = FEATURES["ex-squat"]
        self.assertEqual(g.exercise_admissible(feats, {}), [])
        self.assertEqual(g.exercise_admissible(feats, {"excluded_patterns": ["Squat"]}),
                         ["excluded_patterns:squat"])
        self.assertEqual(g.exercise_admissible(feats, {"joint_load_max": {"knee": "moderate"}}),
                         ["joint_load_max:knee"])
        self.assertEqual(g.exercise_admissible(feats, {"joint_load_max": {"knee": 3}}), [])
        self.assertEqual(g.exercise_admissible(feats, {"excluded_joints": ["knee"]}), ["excluded_joints:knee"])
        self.assertEqual(g.exercise_admissible(feats, {"excluded_exercises": ["ex-squat"]}),
                         ["excluded_exercises:ex-squat"])

    def test_fail_closed(self) -> None:
        self.assertEqual(g.exercise_admissible({"pattern": "squat"}, {}),
                         ["missing:contraction", "missing:energy_system", "missing:joint_load"])
        self.assertEqual(g.exercise_admissible(FEATURES["ex-squat"], {"exclude_knee": True}),
                         ["unknown_constraint:exclude_knee"])
        odd = dict(FEATURES["ex-squat"], joint_load={"knee": "extreme"})
        self.assertEqual(g.exercise_admissible(odd, {}), ["joint_level_unknown:knee"])
        self.assertEqual(g.exercise_admissible(dict(odd, joint_load="knee"), {}), ["invalid:joint_load"])


class LoadRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = Ledger(self.tmp, {})

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _write(self, rel: str, obj: object) -> None:
        self.ledger.write_json(rel, obj, author="pineal")

    def test_defaults_without_files(self) -> None:
        self.assertEqual(g.load_rules(self.ledger), g.Rules())

    def test_reads_all_files(self) -> None:
        self._write("config/guardrails.json", {"load_auto_pct": 8, "novel_per_week": 1, "pull_push": [1, 1.5]})
        self._write("config/forbidden.json", {"wada_prohibited": ["Testostack"], "custom": ["Zauberpulver"]})
        self._write("config/supplements.json", {"allowlist": {"Kreatin": {"max_dose": 5, "unit": "g"}},
                                                "interactions": [{"supplement": "Kreatin", "med": "TestMed"}]})
        self._write("profile/meds.json", {"meds": ["TestMed", {"name": "OtherMed"}]})
        rules = g.load_rules(self.ledger)
        self.assertEqual((rules.load_auto_pct, rules.novel_per_week, rules.pull_push), (8.0, 1, (1.0, 1.5)))
        self.assertIsInstance(rules.novel_per_week, int)
        self.assertEqual(g.forbidden_match("Testostack", rules), "wada_prohibited")
        self.assertEqual(g.forbidden_match("Clenbuterol", rules), "wada_prohibited")  # defaults kept
        self.assertEqual(g.forbidden_match("zauberpulver", rules), "custom")
        self.assertEqual(rules.meds, ("TestMed", "OtherMed"))
        self.assertEqual(g.supplement_check("Kreatin", 3, "g", "Informed Sport", rules), ["interaction:testmed"])

    def test_rejects_bad_config(self) -> None:
        self._write("config/guardrails.json", {"load_auto_pct": 8, "load_autp_pct": 9})
        with self.assertRaises(ValueError):
            g.load_rules(self.ledger)
        self._write("config/guardrails.json", {"load_auto_pct": "8"})
        with self.assertRaises(ValueError):
            g.load_rules(self.ledger)


class CheckDiffLoadTests(unittest.TestCase):
    # base plan load: 2 sessions x 6 x 50 = 600, trailing mean 600

    def test_empty_diff_is_auto(self) -> None:
        v = g.check_diff(_plan(), _plan(), [], _ctx(), g.Rules())
        self.assertEqual((v.tier, v.rule_ids), ("auto", ("no_change",)))

    def test_load_tiers(self) -> None:
        self.assertEqual(_check(_with_duration(55)).tier, "auto")       # +5 %
        self.assertEqual(_check(_with_duration(65)).tier, "approve")    # +15 %
        self.assertEqual(_check(_with_duration(75)).tier, "reject")     # +25 %
        self.assertEqual(_check(_with_duration(40)).tier, "auto")       # decrease

    def test_flags_reject_any_increase(self) -> None:
        for ctx in (_ctx(red_flag_active=True), _ctx(ed_watch=True)):
            v = _check(_with_duration(52), ctx)
            self.assertEqual(v.tier, "reject")
            self.assertIn("load_while_flagged", v.rule_ids)
        self.assertEqual(_check(_with_duration(40), _ctx(red_flag_active=True)).tier, "auto")

    def test_cumulative_three_weeks(self) -> None:
        v = _check(_with_duration(55), _ctx(auto_changes_3wk_pct=18.0))
        self.assertEqual(v.tier, "approve")
        self.assertIn("load_cumulative_3wk", v.rule_ids)

    def test_two_week_reject(self) -> None:
        v = _check(_with_duration(55), _ctx(weekly_loads=[640, 640, 450, 640]))
        self.assertIn("load_two_week", v.rule_ids)
        self.assertEqual(v.tier, "reject")

    def test_no_baseline_needs_approval(self) -> None:
        v = _check(_with_duration(52), _ctx(weekly_loads=[600, 600]))
        self.assertIn("load_no_baseline", v.rule_ids)
        self.assertEqual(v.tier, "approve")

    def test_return_after_days_off(self) -> None:
        # prior week 650 -> cap 455; new plan 620
        v = _check(_with_duration(52), _ctx(weekly_loads=[700, 700, 700, 650], days_off=9))
        self.assertIn("return_cap", v.rule_ids)
        self.assertEqual(v.tier, "reject")
        no_ref = _check(_with_duration(52), _ctx(weekly_loads=[0, 0, 0, 0], days_off=30))
        self.assertIn("return_no_reference", no_ref.rule_ids)

    def test_return_cap_binds_unchanged_and_reduced_plans(self) -> None:
        ctx = _ctx(weekly_loads=[700, 700, 700, 650], days_off=9)  # cap 455
        reduced = _check(_with_duration(45), ctx)  # load 570, below the current 600
        self.assertIn("return_cap", reduced.rule_ids)
        self.assertEqual(reduced.tier, "reject")
        unchanged = _plan()
        unchanged["modulation"]["allow_swap"] = False  # load stays 600
        self.assertIn("return_cap", _check(unchanged, ctx).rule_ids)
        self.assertNotIn("return_cap", _check(_with_duration(10), ctx).rule_ids)  # load 360

    def test_hard_sessions(self) -> None:
        plan = _plan()
        for s in plan["sessions"]:
            s["target_rpe"], s["duration_min"] = 8, 39  # 2 hard sessions, load 624 (+4 %)
        v = _check(plan, _ctx(hard_sessions_last_week=0))
        self.assertIn("hard_sessions", v.rule_ids)
        self.assertEqual(v.tier, "reject")
        self.assertNotIn("hard_sessions", _check(plan, _ctx(hard_sessions_last_week=1)).rule_ids)


class CheckDiffExerciseTests(unittest.TestCase):
    def _add(self, ex: dict, session: int = 1) -> dict:
        plan = _plan()
        plan["sessions"][session]["exercises"].append(ex)
        return plan

    def _edit(self, **fields) -> dict:
        plan = _plan()
        plan["sessions"][1]["exercises"][0].update(fields)  # ex-squat: 3 x 8 x 20 kg, rir 2
        return plan

    def test_volume_change_tiers(self) -> None:
        self.assertEqual(_check(self._edit(load_kg=21.0)).tier, "notify")     # +5 %
        small = _check(self._edit(load_kg=23.0))                                # +15 %
        self.assertEqual(small.tier, "approve")
        self.assertIn("exercise_volume_approve", small.rule_ids)
        self.assertEqual(_check(self._edit(sets=4)).tier, "reject")           # +33 %
        self.assertEqual(_check(self._edit(sets=2)).tier, "notify")           # decrease
        self.assertEqual(_check(self._edit(load_kg=None)).tier, "notify")     # 20 kg -> bodyweight

    def test_big_exercise_load_jump_rejects(self) -> None:
        v = _check(self._edit(load_kg=140.0, sets=10, rir=0))
        self.assertEqual(v.tier, "reject")
        self.assertIn("exercise_volume_reject", v.rule_ids)
        self.assertIn("exercise_rir_down", v.rule_ids)
        self.assertEqual(_check(self._edit(rir=1)).tier, "approve")

    def test_swap_same_pattern_notify(self) -> None:
        plan = _plan()
        plan["sessions"][0]["exercises"][0] = _ex("ex-row2")
        v = _check(plan, _ctx(known_exercises=HISTORY))
        self.assertEqual(v.tier, "notify")
        self.assertIn("swap_same_pattern", v.rule_ids)

    def test_swap_to_riskier_exercise_is_not_a_swap(self) -> None:
        heavier = _plan()
        heavier["sessions"][0]["exercises"][0] = _ex("ex-row-heavy")  # shoulder moderate -> high
        v = _check(heavier, _ctx(known_exercises=HISTORY))
        self.assertNotIn("swap_same_pattern", v.rule_ids)
        self.assertEqual(v.tier, "approve")
        jump = _plan()
        jump["sessions"][1]["exercises"][0] = _ex("ex-jump", sets=6)  # concentric -> plyometric
        self.assertNotIn("swap_same_pattern", _check(jump, _ctx(known_exercises=HISTORY)).rule_ids)
        lighter = _plan()
        lighter["sessions"][1]["exercises"][0] = _ex("ex-squat2")
        self.assertIn("swap_same_pattern", _check(lighter, _ctx(known_exercises=HISTORY)).rule_ids)

    def test_novelty_comes_from_history_not_the_flag(self) -> None:
        jump = _plan()
        jump["sessions"][1]["exercises"][0] = _ex("ex-jump", sets=6)  # novel=False from the LLM
        v = _check(jump)
        self.assertEqual(v.tier, "reject")
        self.assertIn("novel_sets", v.rule_ids)
        self.assertIn("novel_probation", v.rule_ids)

    def test_probation_progress_needs_pain_free_sessions(self) -> None:
        cur = _plan()
        cur["sessions"][0]["exercises"].append(_ex("ex-hinge", sets=2, novel=True))
        for change in ({"novel": False, "probation": False}, {"sets": 3, "novel": True}):
            new = copy.deepcopy(cur)
            new["sessions"][0]["exercises"][2].update(change)
            v = _check(new, cur=cur)
            self.assertEqual(v.tier, "reject" if "sets" in change else "approve")
            self.assertIn("probation_progress", v.rule_ids)
            done = _check(new, _ctx(pain_free_sessions={"ex-hinge": 2}), cur=cur)
            self.assertNotIn("probation_progress", done.rule_ids)

    def test_added_exercise_needs_approval(self) -> None:
        v = _check(self._add(_ex("ex-hinge")), _ctx(known_exercises=HISTORY))
        self.assertEqual(v.tier, "approve")
        self.assertIn("exercise_added", v.rule_ids)

    def test_novel_exercise(self) -> None:
        v = _check(self._add(_ex("ex-hinge", sets=2, novel=True)))
        self.assertEqual(v.tier, "approve")
        self.assertIn("novel_exercise", v.rule_ids)
        too_many_sets = _check(self._add(_ex("ex-hinge", sets=3, novel=True)))
        self.assertIn("novel_sets", too_many_sets.rule_ids)
        self.assertEqual(too_many_sets.tier, "reject")
        no_probation = _check(self._add(_ex("ex-hinge", sets=2, novel=True, probation=False)))
        self.assertIn("novel_probation", no_probation.rule_ids)
        eccentric = _check(self._add(_ex("ex-nordic", sets=2, novel=True)))
        self.assertIn("novel_eccentric", eccentric.rule_ids)

    def test_novel_rpe_cap(self) -> None:
        plan = self._add(_ex("ex-hinge", sets=2, novel=True))
        plan["sessions"][1]["target_rpe"], plan["sessions"][1]["duration_min"] = 7, 42  # load 594
        self.assertIn("novel_rpe", _check(plan).rule_ids)

    def test_novel_caps(self) -> None:
        plan = self._add(_ex("ex-hinge", sets=2, novel=True))
        plan["sessions"][1]["exercises"].append(_ex("ex-row2", sets=2, novel=True))
        self.assertIn("novel_per_session", _check(plan).rule_ids)
        week = _plan()
        for i, eid in enumerate(("ex-hinge", "ex-row2")):
            week["sessions"][i]["exercises"].append(_ex(eid, sets=2, novel=True))
        self.assertNotIn("novel_per_week", _check(week).rule_ids)
        self.assertIn("novel_per_week", _check(week, rules=g.Rules(novel_per_week=1)).rule_ids)

    def test_injury_constraint_rejects(self) -> None:
        constraints = {g.EXERCISE_FEATURES_KEY: FEATURES, "excluded_patterns": ["hinge"]}
        v = _check(self._add(_ex("ex-hinge")), _ctx(injury_constraints=constraints))
        self.assertEqual(v.tier, "reject")
        self.assertIn("injury:excluded_patterns:hinge", v.rule_ids)

    def test_missing_features_reject(self) -> None:
        v = _check(self._add(_ex("ex-unknown")))
        self.assertEqual(v.tier, "reject")
        self.assertIn("features_missing", v.rule_ids)

    def test_pull_push_ratio(self) -> None:
        plan = _plan()
        plan["sessions"][0]["exercises"][1]["sets"] = 1  # pull 3 : push 1
        v = _check(plan)
        self.assertIn("pull_push", v.rule_ids)
        self.assertEqual(v.tier, "reject")


class CheckDiffNutritionTests(unittest.TestCase):
    def _kcal(self, kcal: float | None) -> dict:
        plan = _plan()
        plan["nutrition"]["kcal_target"] = kcal
        return plan

    def test_kcal_tiers(self) -> None:
        self.assertEqual(_check(self._kcal(2600)).tier, "notify")       # +4 %
        self.assertEqual(_check(self._kcal(2700)).tier, "approve")      # +8 %
        v = _check(self._kcal(2800))                                    # +300 kcal
        self.assertEqual(v.tier, "reject")
        self.assertIn("kcal_step", v.rule_ids)

    def test_step_uses_smaller_limit(self) -> None:
        cur = self._kcal(1800)
        new = self._kcal(1990)  # +190 kcal is > 10 % of 1800
        self.assertIn("kcal_step", _check(new, cur=cur).rule_ids)

    def test_deficit_checks(self) -> None:
        self.assertEqual(_check(self._kcal(2400)).tier, "notify")  # 4 % cut, EA 45 -> 43.3, loss ok
        self.assertEqual(_check(self._kcal(2350)).tier, "approve")  # 6 % cut
        flagged = _check(self._kcal(2400), _ctx(ed_watch=True))
        self.assertIn("deficit_while_flagged", flagged.rule_ids)
        floor = _check(self._kcal(2400), _ctx(ea_7d=31.0))
        self.assertIn("ea_floor", floor.rule_ids)
        low = _check(self._kcal(2400), _ctx(ea_7d=36.0))
        self.assertIn("ea_low", low.rule_ids)
        self.assertIn("ea_unknown", _check(self._kcal(2400), _ctx(ea_7d=None)).rule_ids)
        self.assertIn("loss_rate_unknown", _check(self._kcal(2400), _ctx(weight_kg=None)).rule_ids)

    def test_loss_rate(self) -> None:
        # intake 3200, target 2300: 900 kcal/day -> 0.82 kg/week -> 1.2 % of 70 kg
        cur, new = self._kcal(2500), self._kcal(2300)
        v = _check(new, _ctx(kcal_current=3200.0), cur=cur)
        self.assertIn("loss_rate", v.rule_ids)

    def test_kcal_increase_allowed_under_ed_watch(self) -> None:
        self.assertEqual(_check(self._kcal(2600), _ctx(ed_watch=True)).tier, "notify")

    def test_new_target_needs_approval(self) -> None:
        v = _check(self._kcal(2450), cur=self._kcal(None))
        self.assertIn("kcal_new_target", v.rule_ids)
        self.assertEqual(v.tier, "approve")

    def test_new_target_is_checked_as_deficit(self) -> None:
        cur = self._kcal(None)
        flagged = _check(self._kcal(1200), _ctx(ed_watch=True, kcal_current=None), cur=cur)
        self.assertEqual(flagged.tier, "reject")
        self.assertIn("deficit_while_flagged", flagged.rule_ids)
        unknown = _check(self._kcal(1200), _ctx(kcal_current=None), cur=cur)
        self.assertIn("loss_rate_unknown", unknown.rule_ids)
        self.assertEqual(_check(self._kcal(1200), cur=cur).tier, "reject")  # 1300 kcal below intake
        above = _check(self._kcal(2600), _ctx(ed_watch=True), cur=cur)
        self.assertEqual(above.tier, "approve")

    def test_fasting_and_text(self) -> None:
        plan = _plan()
        plan["nutrition"]["notes_de"] = "Einmal pro Woche 36-Stunden-Fasten"
        self.assertIn("fast_too_long", _check(plan).rule_ids)
        plan["nutrition"]["notes_de"] = "16 h Intervallfasten an Ruhetagen"
        self.assertEqual(_check(plan).tier, "approve")  # free text needs the owner's yes
        self.assertNotIn("fast_too_long", _check(plan).rule_ids)
        for text in ("Fasten für 1 Woche, nur Wasser", "Fasten über eine Woche", "fasting for 2 weeks"):
            plan["nutrition"]["notes_de"] = text
            self.assertIn("fast_too_long", _check(plan).rule_ids, text)
        plan["nutrition"]["notes_de"] = "Vor dem Wiegen Wasserentzug"
        self.assertIn("forbidden:dehydration_cut", _check(plan).rule_ids)
        plan["nutrition"]["notes_de"] = "Wegen Infekt weniger Training"
        self.assertIn("banned_term", _check(plan).rule_ids)


class CheckDiffFreeTextTests(unittest.TestCase):
    def _notes(self, text: str) -> g.Verdict:
        plan = _plan()
        plan["nutrition"]["notes_de"] = text
        return _check(plan, rules=g.Rules(supplements={"Koffein": {"max_dose": 200, "unit": "mg"}}))

    def test_notes_need_approval(self) -> None:
        v = self._notes("Iss an Ruhetagen nur 1000 kcal und lass das Frühstück weg.")
        self.assertEqual(v.tier, "approve")
        self.assertIn("free_text", v.rule_ids)
        self.assertIn("dose_in_text", v.rule_ids)
        supp = self._notes("Nimm täglich 600 mg Koffein und 10 g Kreatin, dazu Yohimbin.")
        self.assertIn("supplement_in_text", supp.rule_ids)
        self.assertEqual(supp.tier, "approve")
        drugs = self._notes("Testosteronspritze oder Entwässerungstabletten, Ozempic, Clen, Diuretikum, Lasix")
        self.assertEqual(drugs.tier, "reject")
        self.assertTrue(any(r.startswith("forbidden:") for r in drugs.rule_ids))
        self.assertEqual(self._notes("").tier, "auto")  # clearing the notes

    def test_session_kind_needs_approval(self) -> None:
        plan = _plan()
        plan["sessions"][1]["kind"] = "VO2max 8x4 min all-out + Sprints"
        v = _check(plan)
        self.assertEqual(v.tier, "approve")
        self.assertIn("free_text", v.rule_ids)
        added = _plan()
        added["sessions"].append({"id": "s3", "weekday": 5, "kind": "Sprints", "duration_min": 5,
                                  "target_rpe": 6, "exercises": []})
        self.assertIn("free_text", _check(added).rule_ids)


class CheckDiffMiscTests(unittest.TestCase):
    def test_supplements(self) -> None:
        rules = g.Rules(supplements={"Kreatin": {"max_dose": 5, "unit": "g"}})
        plan = _plan()
        plan["supplements"] = [{"name": "Kreatin", "dose": 3, "unit": "g", "tested_by": "Kölner Liste"}]
        v = _check(plan, rules=rules)
        self.assertEqual((v.tier, v.rule_ids[0]), ("approve", "supplement"))
        plan["supplements"][0]["tested_by"] = None
        self.assertIn("supplement:untested", _check(plan, rules=rules).rule_ids)
        removed = _check(_plan(), cur=copy.deepcopy(plan), rules=rules)
        self.assertEqual(removed.tier, "notify")

    def test_single_study_needs_approval(self) -> None:
        v = _check(_with_duration(52), refs=["evidence/papers/10-0000-synthetic.json"])
        self.assertEqual(v.tier, "approve")
        self.assertIn("single_study", v.rule_ids)
        two = ["evidence/papers/a.json", "evidence/papers/b.json"]
        self.assertEqual(_check(_with_duration(52), refs=two).tier, "auto")
        same_file = ["evidence/papers/a.json", "evidence/papers/a.json#/effect"]
        v = _check(_with_duration(52), refs=same_file)
        self.assertEqual(v.tier, "approve")
        self.assertIn("single_study", v.rule_ids)

    def test_frontier_refs_need_approval(self) -> None:
        v = _check(_with_duration(52), refs=["evidence/deepdives/x.json"])
        self.assertEqual(v.tier, "approve")
        self.assertIn("frontier_evidence", v.rule_ids)

    def test_paths(self) -> None:
        cur = _plan()
        diff = [{"op": "set", "path": "/version", "value": 9, "refs": []}]
        self.assertEqual(g.check_diff(cur, cur, diff, _ctx(), g.Rules()).tier, "reject")
        diff = [{"op": "move", "path": "/sessions/0", "refs": []}]
        self.assertIn("malformed_op", g.check_diff(cur, cur, diff, _ctx(), g.Rules()).rule_ids)
        new = copy.deepcopy(cur)
        new["experiments"] = ["exp-0001"]
        self.assertEqual(_check(new).tier, "approve")
        new = copy.deepcopy(cur)
        new["modulation"]["allow_swap"] = False
        self.assertEqual(_check(new).tier, "notify")

    def test_malformed_plan_rejected(self) -> None:
        new = _plan()
        del new["sessions"][0]["target_rpe"]
        diff = [{"op": "remove", "path": "/sessions/0/target_rpe", "refs": []}]
        v = g.check_diff(_plan(), new, diff, _ctx(), g.Rules())
        self.assertEqual((v.tier, v.rule_ids), ("reject", ("malformed_plan",)))

    def test_strictest_first(self) -> None:
        plan = _with_duration(75)
        plan["modulation"]["allow_swap"] = False
        v = _check(plan)
        self.assertEqual((v.tier, v.rule_ids[0]), ("reject", "load_reject"))
        self.assertIn("path:modulation", v.rule_ids)
        self.assertEqual(len(v.reasons), len(v.rule_ids))

    def test_random_diffs_never_crash(self) -> None:
        rng = random.Random(RNG_SEED)
        for _ in range(200):
            new = _with_duration(rng.choice([30, 50, 55, 70, 90]))
            new["nutrition"]["kcal_target"] = rng.choice([None, 2000, 2450, 2500, 2600, 3000])
            v = _check(new, _ctx(ea_7d=rng.choice([None, 32.0, 45.0]), ed_watch=rng.random() < 0.3))
            self.assertIn(v.tier, g.TIERS)


def _dv(day: str, key: str, value: float | None) -> DailyValue:
    return DailyValue(day, key, value, "watch", 1, "ok" if value is not None else "missing")


def _series(key: str, values_by_offset: dict[int, float]) -> list[DailyValue]:
    d0 = date.fromisoformat(DAY)
    return [_dv((d0 + timedelta(days=o)).isoformat(), key, v) for o, v in sorted(values_by_offset.items())]


def _checkin(item: str, value: float | None = 1.0, text: str | None = None, day: str = DAY) -> CheckinItem:
    return CheckinItem(day, item, value, text, None, f"{day}T07:00:00+01:00", WORDING_VERSION, "form")


def _rhr(delta: float | None) -> RhrState:
    return RhrState(55.0, 1.5, delta, None if delta is None else delta / 2.2, False)


class RedFlagTests(unittest.TestCase):
    def _flags(self, daily=None, rhr=(), checkins=(), profile=None) -> list[g.Alert]:
        return g.red_flags(daily or {}, list(rhr), list(checkins), DAY, profile or {})

    def test_quiet_day(self) -> None:
        rng = random.Random(RNG_SEED)
        daily = {
            "spo2_min": _series("spo2_min", {o: rng.uniform(95, 98) for o in range(-6, 1)}),
            "weight_kg": _series("weight_kg", {o: 70 + rng.uniform(-0.3, 0.3) for o in range(-30, 1)}),
            "bp_sys": _series("bp_sys", {0: 122.0}), "bp_dia": _series("bp_dia", {0: 78.0}),
        }
        self.assertEqual(self._flags(daily, [_rhr(1.0)] * 3, [_checkin("mood", 5)]), [])

    def test_urgent_symptoms_and_self_harm(self) -> None:
        alerts = self._flags(checkins=[_checkin("symptom:chest_pain"), _checkin("symptom:dark_urine"),
                                       _checkin("note", None, "Ich will nicht mehr leben")])
        ids = [a.rule_id for a in alerts]
        self.assertEqual(ids, ["symptom_urgent", "self_harm", "dark_urine"])
        self.assertEqual(alerts[0].data["symptoms"], ["chest_pain"])
        self.assertEqual(alerts[1].data, {})  # no text copied into alert data
        other_day = self._flags(checkins=[_checkin("symptom:fainting", day="2026-03-09")])
        self.assertEqual(other_day, [])

    def test_note_text_raises_urgent_symptoms(self) -> None:
        alerts = self._flags(checkins=[_checkin("note", None, "Brustschmerzen beim Intervall")])
        self.assertEqual([(a.rule_id, a.data) for a in alerts], [("symptom_urgent", {"symptoms": ["chest_pain"]})])
        both = self._flags(checkins=[_checkin("symptom:chest_pain"), _checkin("note", None, "kaum Luft bekommen")])
        self.assertEqual(both[0].data["symptoms"], ["chest_pain", "short_breath"])
        self.assertEqual(self._flags(checkins=[_checkin("note", None, "Lockerer Lauf, gute Beine")]), [])

    def test_urgent_symptom_phrasings(self) -> None:
        cases = {
            "Druck in der Brust": ["chest_pain"], "Schmerzen im Brustkorb": ["chest_pain"],
            "Stechen in der Brust": ["chest_pain"], "my chest hurts": ["chest_pain"],
            "bekomme kaum Luft": ["short_breath"], "bin beim Laufen umgefallen": ["fainting"],
            "Beine schwer, sonst gut": [],
        }
        for text, expected in cases.items():
            self.assertEqual(g.urgent_symptoms(text), expected, text)

    def test_self_harm_phrasings(self) -> None:
        for text in ("Ich habe Suizidgedanken", "Ich bin suizidal", "Selbstmordgedanken seit Tagen",
                     "bin lebensmüde", "Ich will nicht mehr.", "Ich will nicht mehr", "habe mich geritzt",
                     "möchte nicht mehr aufwachen", "Ich will nicht mehr, alles ist sinnlos"):
            self.assertTrue(g._self_harm(text), text)
        for text in ("Ich will nicht mehr so viel Cardio", "Neues Ritzel am Rad", "Lauf war hart"):
            self.assertFalse(g._self_harm(text), text)

    def test_bp(self) -> None:
        alerts = self._flags({"bp_sys": _series("bp_sys", {0: 182.0}), "bp_dia": _series("bp_dia", {0: 95.0})})
        self.assertEqual([(a.level, a.rule_id) for a in alerts], [("urgent", "bp_high")])

    def test_rhr_rules(self) -> None:
        self.assertEqual([a.rule_id for a in self._flags(rhr=[_rhr(11), _rhr(12), _rhr(10)])], ["rhr_up_3d"])
        self.assertEqual(self._flags(rhr=[_rhr(11), _rhr(None), _rhr(10)]), [])
        base = {o: 14.0 for o in range(-35, -7)}
        daily = {"resp_rate": _series("resp_rate", {**base, 0: 16.5})}
        alerts = self._flags(daily, rhr=[_rhr(6)])
        self.assertEqual([a.rule_id for a in alerts], ["rhr_resp_temp"])
        self.assertTrue(alerts[0].data["pause_intense"])
        self.assertEqual(self._flags(daily, rhr=[_rhr(4)]), [])

    def test_spo2_af_weight_periods(self) -> None:
        spo2 = {"spo2_min": _series("spo2_min", {-5: 0.91, -3: 90.0, -1: 95.0, 0: 91.0})}
        self.assertEqual([a.rule_id for a in self._flags(spo2)], ["spo2_low"])
        af = {"af_burden": _series("af_burden", {-10: 0.0, 0: 2.0})}
        self.assertEqual([a.rule_id for a in self._flags(af)], ["af_new"])
        af_old = {"af_burden": _series("af_burden", {-10: 1.0, 0: 2.0})}
        self.assertEqual(self._flags(af_old), [])
        weights = {"weight_kg": _series("weight_kg", {-30: 70.0, -28: 70.2, -25: 69.8, -1: 66.0, 0: 66.2})}
        self.assertEqual([a.rule_id for a in self._flags(weights)], ["weight_loss_30d"])
        self.assertEqual(self._flags(weights, profile={"weight_loss_intended": True}), [])
        self.assertEqual([a.rule_id for a in self._flags(profile={"missed_periods": 3})], ["missed_periods"])

    def test_urgent_sorted_first(self) -> None:
        alerts = self._flags({"bp_sys": _series("bp_sys", {0: 185.0})}, rhr=[_rhr(11)] * 3)
        self.assertEqual([a.level for a in alerts], ["urgent", "prompt"])

    def test_event_alerts(self) -> None:
        events = [
            HealthEvent("hr_notification", "2026-03-10 03:12:00 +0100", None, "{}", "watch"),
            HealthEvent("state_of_mind", "2026-03-10 08:00:00 +0100", None, "{}", "phone"),
        ]
        alerts = g.event_alerts(events)
        self.assertEqual([(a.level, a.rule_id, a.date) for a in alerts], [("urgent", "hr_notification", DAY)])

    def test_alert_level_validated(self) -> None:
        with self.assertRaises(ValueError):
            g.Alert("info", "x", DAY, {})


SAMPLE_DATA = {
    "symptom_urgent": {"symptoms": ["chest_pain", "short_breath"]},
    "self_harm": {},
    "bp_high": {"sys": 182.0, "dia": None},
    "hr_notification": {"start": "2026-03-10 03:12:00 +0100"},
    "rhr_up_3d": {"delta": 10.5},
    "rhr_resp_temp": {"delta": 6.0, "resp_delta": 2.5, "temp_delta": None, "pause_intense": True},
    "spo2_low": {"days": 3, "min": 90.0},
    "af_new": {"burden": 2.0},
    "weight_loss_30d": {"loss_pct": 5.4},
    "dark_urine": {},
    "focal_bone_pain": {},
    "missed_periods": {"count": 3},
}


class TemplateTests(unittest.TestCase):
    def test_every_template_renders_clean(self) -> None:
        self.assertEqual(set(SAMPLE_DATA), set(g.TEMPLATES_DE))
        for rule_id, data in SAMPLE_DATA.items():
            text = g.render_alert(g.Alert("prompt", rule_id, DAY, data), {})
            self.assertNotIn("$", text, rule_id)
            self.assertEqual(g.scan_text(text), [], rule_id)

    def test_contacts(self) -> None:
        urgent = g.render_alert(g.Alert("urgent", "symptom_urgent", DAY, {"symptoms": ["chest_pain"]}), {})
        self.assertIn("112", urgent)
        self.assertIn("Schmerzen oder Druck in der Brust", urgent)
        crisis = g.render_alert(g.Alert("urgent", "self_harm", DAY, {}), {})
        self.assertIn("Telefonseelsorge", crisis)
        self.assertIn("0800 111 0 111", crisis)
        fallback = g.render_alert(g.Alert("prompt", "dark_urine", DAY, {}), {})
        self.assertIn(g.DOCTOR_FALLBACK_DE, fallback)
        profile = {"doctor_contact": {"name": "Praxis Beispiel", "phone": "000 0000"}}
        named = g.render_alert(g.Alert("prompt", "dark_urine", DAY, {}), profile)
        self.assertIn("Praxis Beispiel, 000 0000", named)
        weight = g.render_alert(g.Alert("prompt", "weight_loss_30d", DAY, {"loss_pct": 5.44}), {})
        self.assertIn("5,4 %", weight)

    def test_unknown_rule_or_missing_field(self) -> None:
        with self.assertRaises(KeyError):
            g.render_alert(g.Alert("prompt", "nope", DAY, {}), {})
        with self.assertRaises(KeyError):
            g.render_alert(g.Alert("prompt", "spo2_low", DAY, {}), {})


class ScanTextTests(unittest.TestCase):
    def test_finds_terms(self) -> None:
        self.assertEqual(g.scan_text("Das klingt nach einer Infektion."), ["infekt"])
        self.assertEqual(g.scan_text("Possible atrial fibrillation; get a diagnosis"),
                         ["diagnos", "atrial fibrillation"])
        self.assertEqual(g.scan_text("Verdacht auf Übertrainingssyndrom"), ["ubertrainingssyndrom"])
        self.assertEqual(g.scan_text("Mögliche Stressfraktur"), ["stressfraktur"])

    def test_no_false_positives(self) -> None:
        clean = "Heute 30 Minuten Backstroke, fast nichts Neues, Ruhepuls stabil. Sehr gut!"
        self.assertEqual(g.scan_text(clean), [])
        self.assertEqual(g.scan_text(""), [])


if __name__ == "__main__":
    unittest.main()

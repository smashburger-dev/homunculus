"""Tests for homunculus.plans. All plans are small synthetic fixtures."""

from __future__ import annotations

import copy
import random
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from homunculus import plans
from homunculus.plans import PlanError
from homunculus.store import Ledger

RNG_SEED = 4711
NOW = datetime(2026, 1, 4, 21, 0, tzinfo=timezone.utc)


def _exercise(eid: str = "ex-a", sets: int = 3, reps: int = 5, novel: bool = False) -> dict:
    return {"exercise_id": eid, "sets": sets, "reps": reps, "load_kg": 40.0, "rir": 2,
            "novel": novel, "probation": novel}


def _plan(**over) -> dict:
    plan = {
        "version": 1, "parent": None, "effective_from": "2026-01-01T00:00:00Z",
        "valid_until": "2026-01-08T00:00:00Z", "author": "owner", "ratified_by": "owner",
        "data_snapshot_sha": None, "rationale_refs": [],
        "sessions": [
            {"id": "s1", "weekday": 0, "kind": "kraft", "duration_min": 60, "target_rpe": 7,
             "exercises": [_exercise("ex-a"), _exercise("ex-b", sets=2)]},
            {"id": "s2", "weekday": 2, "kind": "ausdauer", "duration_min": 40, "target_rpe": 4, "exercises": []},
            {"id": "s3", "weekday": 4, "kind": "kraft", "duration_min": 50, "target_rpe": 8,
             "exercises": [_exercise("ex-c", novel=True)]},
        ],
        "nutrition": {"kcal_target": 2500, "protein_g_per_kg": 1.6, "notes_de": "Viel Gemüse."},
        "supplements": [{"name": "Testpulver", "dose": 3, "unit": "g", "tested_by": "Kölner Liste"}],
        "experiments": ["exp-0001"],
        "modulation": {"max_daily_load_cut_pct": 25, "allow_swap": True, "readiness_low_action": "shorten"},
    }
    plan.update(over)
    return plan


def _set(path: str, value) -> dict:
    return {"op": "set", "path": path, "value": value, "refs": ["reviews/2026-W01/packet.json#/x"]}


class ValidateTests(unittest.TestCase):
    def test_valid_plan(self) -> None:
        self.assertEqual(plans.validate_plan(_plan()), [])

    def test_missing_wrong_extra(self) -> None:
        plan = _plan()
        del plan["nutrition"]
        plan["sessions"][0]["weekday"] = 7
        plan["sessions"][1]["duration_min"] = "40"
        plan["extra"] = 1
        errors = plans.validate_plan(plan)
        joined = " | ".join(errors)
        self.assertIn("/nutrition: required", joined)
        self.assertIn("/sessions/0/weekday: above maximum", joined)
        self.assertIn("/sessions/1/duration_min: expected number", joined)
        self.assertIn("/extra: unexpected key", joined)

    def test_bool_is_not_integer_and_enum(self) -> None:
        plan = _plan()
        plan["sessions"][0]["exercises"][0]["sets"] = True
        plan["modulation"]["readiness_low_action"] = "sprint"
        errors = plans.validate_plan(plan)
        self.assertEqual(len(errors), 2)

    def test_semantic_checks(self) -> None:
        plan = _plan(version=2, parent=2)
        plan["sessions"][1]["id"] = "s1"
        self.assertEqual(len(plans.validate_plan(plan)), 2)
        self.assertEqual(plans.validate_plan([]), ["/: expected object"])


class DiffTests(unittest.TestCase):
    def test_set_add_remove_and_purity(self) -> None:
        plan = _plan()
        before = copy.deepcopy(plan)
        diff = [
            _set("/sessions/0/exercises/0/sets", 4),
            {"op": "add", "path": "/sessions/1/exercises/-", "value": _exercise("ex-d"), "refs": []},
            {"op": "remove", "path": "/sessions/2", "refs": []},
            {"op": "add", "path": "/experiments/0", "value": "exp-0000", "refs": []},
        ]
        out = plans.apply_diff(plan, diff)
        self.assertEqual(plan, before)
        self.assertEqual(out["sessions"][0]["exercises"][0]["sets"], 4)
        self.assertEqual(out["sessions"][1]["exercises"][0]["exercise_id"], "ex-d")
        self.assertEqual(len(out["sessions"]), 2)
        self.assertEqual(out["experiments"], ["exp-0000", "exp-0001"])

    def test_bad_ops_raise(self) -> None:
        plan = _plan()
        bad = [
            [_set("/sessions/9/kind", "x")],
            [_set("/sessions/01/kind", "x")],
            [_set("/nope", 1)],
            [{"op": "add", "path": "/nutrition/notes_de", "value": "x", "refs": []}],
            [{"op": "set", "path": "/sessions/0/kind", "refs": []}],
            [{"op": "move", "path": "/sessions/0", "refs": []}],
            [{"op": "set", "path": "/sessions/0/kind", "value": "x"}],
            [{"op": "remove", "path": "", "refs": []}],
            [_set("sessions", 1)],
            [_set("/sessions/0/kind/x", 1)],
            [_set("/sessions/0/target_rpe", 11)],
            [{"op": "remove", "path": "/nutrition", "refs": []}],
        ]
        for diff in bad:
            with self.subTest(diff=diff), self.assertRaises(PlanError):
                plans.apply_diff(plan, diff)

    def test_pointer_escaping(self) -> None:
        a = {"a/b": {"c~d": 1}}
        b = {"a/b": {"c~d": 2}}
        diff = plans.compute_diff(a, b)
        self.assertEqual(diff, [{"op": "set", "path": "/a~1b/c~0d", "value": 2, "refs": []}])
        doc = plans._apply_op(copy.deepcopy(a), diff[0])
        self.assertEqual(doc, b)

    def test_compute_diff_roundtrip_seeded(self) -> None:
        rng = random.Random(RNG_SEED)
        for _ in range(200):
            a = _plan()
            b = _mutate(_plan(), rng)
            diff = plans.compute_diff(a, b)
            self.assertEqual(plans.apply_diff(a, diff), b)
            self.assertEqual(plans.compute_diff(b, b), [])

    def test_type_change_is_set(self) -> None:
        diff = plans.compute_diff({"x": 1}, {"x": 1.5})
        self.assertEqual(diff[0]["op"], "set")
        self.assertEqual(plans.compute_diff({"x": True}, {"x": 1})[0]["value"], 1)

    def test_changed_paths_and_load(self) -> None:
        diff = [_set("/a", 1), _set("/b", 2), _set("/a", 3)]
        self.assertEqual(plans.changed_paths(diff), {"/a", "/b"})
        self.assertEqual(plans.weekly_load(_plan()), 60 * 7 + 40 * 4 + 50 * 8)
        self.assertEqual(plans.weekly_load(_plan(sessions=[])), 0.0)


def _mutate(plan: dict, rng: random.Random) -> dict:
    sessions = plan["sessions"]
    if rng.random() < 0.5:
        sessions[rng.randrange(len(sessions))]["duration_min"] = rng.randrange(10, 90)
    if rng.random() < 0.4:
        sessions.pop(rng.randrange(len(sessions)))
    if rng.random() < 0.4:
        sessions.append({"id": f"n{rng.randrange(1000, 9999)}", "weekday": rng.randrange(7), "kind": "neu",
                         "duration_min": 30, "target_rpe": 5, "exercises": [_exercise(sets=rng.randrange(1, 5))]})
    if rng.random() < 0.3 and sessions and sessions[0]["exercises"]:
        sessions[0]["exercises"].pop()
    if rng.random() < 0.5:
        plan["nutrition"]["kcal_target"] = rng.choice([None, 2300, 2450.5])
    if rng.random() < 0.3:
        plan["experiments"] = rng.sample(["exp-1", "exp-2", "exp-3"], rng.randrange(4))
    return plan


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.ledger = Ledger(self.tmp, {"cerebellum": ["plans/pending/*.json"]})
        self.ledger.write_json("plans/v0001.json", _plan(), author="pineal")
        self.ledger.write_text("plans/CURRENT", "v0001\n", author="pineal")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _propose(self, tier: str, value: int = 4, now: datetime = NOW) -> plans.Pending:
        return plans.propose(self.ledger, [_set("/sessions/0/exercises/0/sets", value)], tier,
                             ["synthetic"], ["evidence/papers/x.json"], "reviews/2026-W01/06-cerebellum-ruling.json", now)

    def test_current_and_get(self) -> None:
        self.assertEqual(plans.current(self.ledger)["version"], 1)
        with self.assertRaises(PlanError):
            plans.get(self.ledger, 9)
        empty = Ledger(tempfile.mkdtemp(), {})
        with self.assertRaises(PlanError):
            plans.current(empty)
        shutil.rmtree(empty.root)

    def test_auto_ratifies_now(self) -> None:
        pending = self._propose("auto")
        self.assertIsNone(pending.veto_until)
        self.assertTrue(pending.id.startswith("p-"))
        self.assertEqual(plans.ratify_due(self.ledger, NOW, "a" * 64), [2])
        new = plans.current(self.ledger)
        self.assertEqual((new["version"], new["parent"], new["ratified_by"]), (2, 1, "auto"))
        self.assertEqual(new["data_snapshot_sha"], "a" * 64)
        self.assertEqual(new["sessions"][0]["exercises"][0]["sets"], 4)
        self.assertIn("reviews/2026-W01/06-cerebellum-ruling.json", new["rationale_refs"])
        self.assertEqual(new["valid_until"], "2026-01-11T21:00:00Z")
        self.assertTrue(self.ledger.exists("plans/v0002.md"))
        self.assertEqual(self.ledger.read_text("plans/CURRENT"), "v0002\n")
        self.assertEqual(plans.ratify_due(self.ledger, NOW, "a" * 64), [])
        self.assertEqual(self.ledger.read_json(f"plans/pending/{pending.id}.json")["status"], "applied")

    def test_notify_waits_for_veto_window(self) -> None:
        pending = self._propose("notify")
        self.assertEqual(pending.veto_until, "2026-01-05T21:00:00Z")
        self.assertEqual(plans.ratify_due(self.ledger, NOW + timedelta(hours=23), "s"), [])
        self.assertEqual(plans.ratify_due(self.ledger, NOW + timedelta(hours=24), "s"), [2])
        self.assertEqual(plans.current(self.ledger)["ratified_by"], "veto_window")

    def test_veto_blocks(self) -> None:
        pending = self._propose("notify")
        plans.veto(self.ledger, pending.id, NOW)
        self.assertEqual(plans.ratify_due(self.ledger, NOW + timedelta(days=2), "s"), [])
        with self.assertRaises(PlanError):
            plans.approve(self.ledger, pending.id, NOW)
        with self.assertRaises(PlanError):
            plans.veto(self.ledger, pending.id, NOW)

    def test_approve_tier_needs_owner(self) -> None:
        pending = self._propose("approve")
        self.assertEqual(plans.ratify_due(self.ledger, NOW + timedelta(days=30), "s"), [])
        self.assertEqual(plans.approve(self.ledger, pending.id, NOW), 2)
        self.assertEqual(plans.current(self.ledger)["ratified_by"], "owner")
        with self.assertRaises(PlanError):
            plans.approve(self.ledger, pending.id, NOW)

    def test_propose_is_idempotent_and_checked(self) -> None:
        first = self._propose("approve")
        again = self._propose("approve", now=NOW + timedelta(minutes=5))
        self.assertEqual(first, again)
        self.assertEqual(len(self.ledger.glob("plans/pending/*.json")), 1)
        with self.assertRaises(PlanError):
            self._propose("reject")
        with self.assertRaises(PlanError):
            plans.propose(self.ledger, [_set("/version", 5)], "auto", [], [], "r", NOW)
        with self.assertRaises(PlanError):
            plans.propose(self.ledger, [_set("/sessions/7/kind", "x")], "auto", [], [], "r", NOW)
        with self.assertRaises(PlanError):
            plans.approve(self.ledger, "../../config/HALT", NOW)

    def test_rollback_and_superseded_pending(self) -> None:
        self._propose("auto")
        plans.ratify_due(self.ledger, NOW, "s")
        stale = self._propose("approve", value=5)
        self.assertEqual(plans.rollback(self.ledger, 1, NOW), 3)
        rolled = plans.current(self.ledger)
        self.assertEqual((rolled["version"], rolled["parent"], rolled["author"]), (3, 2, "owner"))
        self.assertEqual(rolled["sessions"], _plan()["sessions"])
        self.assertEqual(rolled["rationale_refs"], ["plans/v0001.json"])
        with self.assertRaises(PlanError):
            plans.approve(self.ledger, stale.id, NOW)
        self.assertEqual(self.ledger.read_json(f"plans/pending/{stale.id}.json")["status"], "superseded")
        with self.assertRaises(PlanError):
            plans.rollback(self.ledger, 3, NOW)

    def test_diff_may_set_valid_until(self) -> None:
        plans.propose(self.ledger, [_set("/valid_until", "2026-02-01T00:00:00Z")], "auto", [], [], "r", NOW)
        plans.ratify_due(self.ledger, NOW, "s")
        self.assertEqual(plans.current(self.ledger)["valid_until"], "2026-02-01T00:00:00Z")

    def test_load_exercises(self) -> None:
        self.assertEqual(plans.load_exercises(self.ledger), {})
        self.ledger.write_json("config/exercises.json", [{"id": "ex-a", "pattern": "squat"}], author="pineal")
        self.assertEqual(plans.load_exercises(self.ledger)["ex-a"]["pattern"], "squat")
        self.ledger.write_json("config/exercises.json", {"ex-b": {"pattern": "pull"}}, author="pineal")
        self.assertEqual(plans.load_exercises(self.ledger), {"ex-b": {"pattern": "pull"}})
        self.ledger.write_json("config/exercises.json", {"exercises": [1]}, author="pineal")
        with self.assertRaises(PlanError):
            plans.load_exercises(self.ledger)


class RenderTests(unittest.TestCase):
    def test_markdown_de(self) -> None:
        md = plans.render_markdown_de(_plan())
        for needle in ("# Trainingsplan v0001", "### Montag: kraft (s1)", "Ziel-RPE 7", "1,6 g/kg",
                       "- ex-a: 3 × 5, 40 kg, RIR 2", "neu, Probephase", "Testpulver: 3 g (geprüft: Kölner Liste)",
                       "Bei niedriger Bereitschaft: kürzen", "Keine Referenzen.", "Vorgänger: keiner",
                       "Keine Übungen festgelegt.", "Wochenlast (RPE × Minuten): 980"):
            self.assertIn(needle, md)
        self.assertLess(md.index("Montag"), md.index("Mittwoch"))

    def test_markdown_empty_plan(self) -> None:
        plan = _plan(sessions=[], supplements=[], experiments=[], parent=None)
        plan["nutrition"] = {"kcal_target": None, "protein_g_per_kg": None, "notes_de": ""}
        md = plans.render_markdown_de(plan)
        self.assertIn("Keine Einheiten geplant.", md)
        self.assertIn("Kalorienziel: nicht festgelegt", md)


class ModulateTests(unittest.TestCase):
    def test_branches(self) -> None:
        plan = _plan()
        cases = [
            ((0, "low", True, False), "rest", ["red_flag"]),
            ((1, "low", False, False), "as_planned", ["no_session"]),
            ((0, "low", False, True), "as_planned", ["stale_data"]),
            ((0, "high", False, False), "as_planned", ["readiness_high"]),
            ((0, None, False, False), "as_planned", ["readiness_unknown"]),
            ((0, "low", False, False), "shorten", ["readiness_low"]),
        ]
        for args, action, reasons in cases:
            with self.subTest(args=args):
                out = plans.modulate_day(plan, *args)
                self.assertEqual((out["action"], out["reason_codes"]), (action, reasons))

    def test_shorten_cuts_duration_and_sets(self) -> None:
        out = plans.modulate_day(_plan(), 0, "low", False, False)
        self.assertEqual(out["session"]["duration_min"], 45)
        self.assertEqual([e["sets"] for e in out["session"]["exercises"]], [2, 1])

    def test_swap_rest_and_fallbacks(self) -> None:
        plan = _plan()
        plan["modulation"]["readiness_low_action"] = "swap"
        out = plans.modulate_day(plan, 0, "low", False, False)
        self.assertEqual((out["action"], out["session"]["id"]), ("swap", "s2"))
        lightest = plans.modulate_day(plan, 2, "low", False, False)
        self.assertEqual((lightest["action"], lightest["reason_codes"]), ("shorten", ["readiness_low", "no_lighter_session"]))
        plan["modulation"]["allow_swap"] = False
        self.assertIn("swap_not_allowed", plans.modulate_day(plan, 0, "low", False, False)["reason_codes"])
        plan["modulation"]["readiness_low_action"] = "rest"
        self.assertEqual(plans.modulate_day(plan, 0, "low", False, False)["session"], None)
        with self.assertRaises(PlanError):
            plans.modulate_day(plan, 7, "low", False, False)

    def test_never_increases_load_seeded(self) -> None:
        rng = random.Random(RNG_SEED)
        for _ in range(300):
            plan = _mutate(_plan(), rng)
            plan["modulation"] = {"max_daily_load_cut_pct": rng.uniform(0, 100), "allow_swap": rng.random() < 0.5,
                                  "readiness_low_action": rng.choice(["shorten", "swap", "rest"])}
            day = rng.randrange(7)
            planned = plans._session_for(plan, day)
            out = plans.modulate_day(plan, day, rng.choice([None, "low", "normal", "high", "odd"]),
                                     rng.random() < 0.1, rng.random() < 0.1)
            if out["session"] is not None:
                self.assertLessEqual(plans._session_load(out["session"]), plans._session_load(planned))
                planned_sets = sum(e["sets"] for e in planned["exercises"])
                if out["action"] != "swap":
                    self.assertLessEqual(sum(e["sets"] for e in out["session"]["exercises"]), planned_sets)

    def test_does_not_mutate_plan(self) -> None:
        plan = _plan()
        before = copy.deepcopy(plan)
        out = plans.modulate_day(plan, 0, "low", False, False)
        out["session"]["kind"] = "x"
        self.assertEqual(plan, before)


if __name__ == "__main__":
    unittest.main()

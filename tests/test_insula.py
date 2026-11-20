"""Tests for homunculus.insula. All values are small synthetic fixtures."""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from homunculus import agents, checkins
from homunculus.insula import (
    INSULA_OUTPUT_SCHEMA,
    MAX_OWNER_QUESTIONS,
    Conflict,
    DayScore,
    conflict_id,
    day_score,
    detect_day_conflicts,
    detect_session_conflicts,
    output_errors,
    weekly_reconciliation,
)
from homunculus.metrics import MetricsConfig
from homunculus.store import CheckinItem, DailyValue, Store, Workout

RNG_SEED = 4711
START = "2026-03-02"


def _d(k: int, start: str = START) -> str:
    return (date.fromisoformat(start) + timedelta(days=k)).isoformat()


def _score(day: str, s: float | None, o: float | None) -> DayScore:
    ok = s is not None and o is not None
    inputs = {"hrv_m7_z": None if o is None else o, "rhr_z": None if o is None else -o,
              "tst_z": None if o is None else o}
    return DayScore(day, o, s, s - o if ok else None, ok, inputs)


def _series(pairs: list[tuple[float | None, float | None]]) -> list[DayScore]:
    return [_score(_d(k), s, o) for k, (s, o) in enumerate(pairs)]


def _rules(found: list[Conflict]) -> list[str]:
    return [c.rule_id for c in found]


class DayScoreTest(unittest.TestCase):
    SUBJ = {"mood": 1.0, "energy": 0.5, "stress": -0.3, "motivation": None}

    def test_objective_negates_rhr_and_gap(self) -> None:
        s = day_score({"hrv_m7_z": 0.6, "rhr_z": 0.9, "tst_z": 0.3}, self.SUBJ, START)
        self.assertAlmostEqual(s.objective, 0.0)
        self.assertAlmostEqual(s.subjective, 0.4)
        self.assertAlmostEqual(s.gap, 0.4)
        self.assertTrue(s.quality_ok)
        self.assertEqual(s.inputs["rhr_z"], 0.9)
        self.assertEqual(s.inputs["subj_mood"], 1.0)
        self.assertIsNone(s.inputs["subj_motivation"])

    def test_missing_objective_input_fails_quality(self) -> None:
        s = day_score({"hrv_m7_z": 0.6, "rhr_z": None, "tst_z": 0.3}, self.SUBJ, START)
        self.assertIsNone(s.objective)
        self.assertIsNone(s.gap)
        self.assertFalse(s.quality_ok)
        s = day_score({"hrv_m7_z": 0.6, "tst_z": 0.3}, self.SUBJ, START)
        self.assertFalse(s.quality_ok)

    def test_too_few_subjective_items_fail_quality(self) -> None:
        s = day_score({"hrv_m7_z": 0.0, "rhr_z": 0.0, "tst_z": 0.0}, {"mood": 1.0, "energy": None}, START)
        self.assertEqual(s.objective, 0.0)
        self.assertIsNone(s.subjective)
        self.assertFalse(s.quality_ok)


class ConflictIdTest(unittest.TestCase):
    def test_formula_and_stability(self) -> None:
        want = hashlib.sha256(f"R1_sustained|{START}|{_d(4)}|".encode()).hexdigest()[:10]
        self.assertEqual(conflict_id("R1_sustained", (START, _d(4)), None), want)
        self.assertNotEqual(conflict_id("R2_single_day", (START, START), None),
                            conflict_id("DATA_GAP", (START, START), None))
        self.assertNotEqual(conflict_id("R3_session_residual", (START, START), "w1"),
                            conflict_id("R3_session_residual", (START, START), "w2"))


class R1Test(unittest.TestCase):
    def test_three_of_five_same_sign(self) -> None:
        found = detect_day_conflicts(_series([(-1.0, 0.8), (0.0, 0.0), (-1.2, 0.5), (0.1, 0.0), (-0.9, 1.0)]))
        self.assertEqual(_rules(found), ["R1_sustained"])
        c = found[0]
        self.assertEqual((c.window, c.sign, c.session_id), ((START, _d(4)), -1, None))
        self.assertEqual(c.numbers["hit_days"], 3.0)
        self.assertAlmostEqual(c.numbers["mean_gap"], (-1.8 - 1.7 - 1.9) / 3)
        self.assertAlmostEqual(c.numbers["max_abs_gap"], 1.9)
        self.assertEqual(c.id, conflict_id("R1_sustained", c.window, None))

    def test_mixed_signs_or_below_threshold_do_not_fire(self) -> None:
        mixed = _series([(2.0, 0.0), (-2.0, 0.0), (2.0, 0.0), (-2.0, 0.0), (0.0, 0.0)])
        self.assertEqual(detect_day_conflicts(mixed), [])
        low = _series([(1.4, 0.0)] * 5)
        self.assertEqual(detect_day_conflicts(low), [])

    def test_overlapping_windows_merge_into_one_episode(self) -> None:
        found = detect_day_conflicts(_series([(2.0, 0.0)] * 8))
        self.assertEqual(_rules(found), ["R1_sustained"])
        self.assertEqual(found[0].window, (START, _d(7)))
        self.assertEqual(found[0].numbers["hit_days"], 8.0)

    def test_separate_episodes_and_sign_change(self) -> None:
        pairs = [(2.0, 0.0)] * 5 + [(0.0, 0.0)] * 5 + [(-2.0, 0.0)] * 5
        found = detect_day_conflicts(_series(pairs))
        self.assertEqual([(c.rule_id, c.sign) for c in found], [("R1_sustained", 1), ("R1_sustained", -1)])
        self.assertEqual(found[1].window, (_d(10), _d(14)))

    def test_bad_day_in_window_yields_data_gap_instead(self) -> None:
        found = detect_day_conflicts(_series([(2.0, 0.0), (2.0, 0.0), (None, 0.0), (2.0, 0.0), (0.0, 0.0)]))
        self.assertEqual(_rules(found), ["DATA_GAP"])

    def test_missing_calendar_day_blocks_window(self) -> None:
        scores = [_score(_d(k), 2.0, 0.0) for k in (0, 1, 3, 4)]
        self.assertEqual(detect_day_conflicts(scores), [])

    def test_input_order_does_not_matter(self) -> None:
        scores = _series([(2.0, 0.0)] * 5)
        rng = random.Random(RNG_SEED)
        shuffled = scores[:]
        rng.shuffle(shuffled)
        self.assertEqual(detect_day_conflicts(shuffled), detect_day_conflicts(scores))


class R2Test(unittest.TestCase):
    def test_opposite_signs_fire(self) -> None:
        found = detect_day_conflicts([_score(START, 1.5, -1.2)])
        self.assertEqual(_rules(found), ["R2_single_day"])
        c = found[0]
        self.assertEqual((c.window, c.sign), ((START, START), 1))
        self.assertAlmostEqual(c.numbers["gap"], 2.7)
        self.assertEqual(c.numbers["subjective"], 1.5)

    def test_side_too_small_same_sign_or_small_gap(self) -> None:
        for s, o in ((2.6, 0.0), (4.0, 1.0), (1.2, -1.2), (-0.9, 1.8)):
            with self.subTest(s=s, o=o):
                self.assertEqual(detect_day_conflicts([_score(START, s, o)]), [])

    def test_r1_and_r2_can_both_fire(self) -> None:
        found = detect_day_conflicts(_series([(-1.5, 1.5)] * 5))
        self.assertEqual(_rules(found).count("R1_sustained"), 1)
        self.assertEqual(_rules(found).count("R2_single_day"), 5)


class DataGapTest(unittest.TestCase):
    def test_runs_of_bad_days_with_counts(self) -> None:
        pairs = [(None, 0.0), (0.1, None), (0.0, 0.0), (None, None), (0.0, 0.0)]
        found = detect_day_conflicts(_series(pairs))
        self.assertEqual(_rules(found), ["DATA_GAP", "DATA_GAP"])
        first, second = found
        self.assertEqual((first.window, first.sign), ((START, _d(1)), 0))
        self.assertEqual(first.numbers, {"days": 2.0, "missing_hrv_m7_z": 1.0, "missing_rhr_z": 1.0,
                                         "missing_tst_z": 1.0, "missing_subjective": 1.0})
        self.assertEqual(second.window, (_d(3), _d(3)))
        self.assertEqual(second.numbers["missing_subjective"], 1.0)


def _sessions(rng: random.Random, n: int, start: str = START) -> list[tuple[str, str, float, float]]:
    rows = []
    for k in range(n):
        trimp = rng.uniform(40.0, 200.0)
        rows.append((f"w{k:02d}", _d(2 * k, start), 3.0 * trimp + 20.0 + rng.gauss(0.0, 15.0), trimp))
    return rows


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(RNG_SEED)

    def test_flags_session_that_felt_harder(self) -> None:
        rows = _sessions(self.rng, 24)
        rows[10] = (rows[10][0], rows[10][1], rows[10][2] + 250.0, rows[10][3])
        found = detect_session_conflicts(rows)
        self.assertEqual([c.session_id for c in found], ["w10"])
        c = found[0]
        self.assertEqual((c.rule_id, c.sign, c.window), ("R3_session_residual", 1, (rows[10][1],) * 2))
        self.assertGreaterEqual(c.numbers["residual_z"], 2.0)
        self.assertEqual(c.numbers["n_sessions"], 24.0)
        self.assertAlmostEqual(c.numbers["residual"], c.numbers["srpe_load"] - c.numbers["predicted_srpe"])

    def test_easier_session_is_not_flagged(self) -> None:
        rows = _sessions(self.rng, 24)
        rows[10] = (rows[10][0], rows[10][1], rows[10][2] - 250.0, rows[10][3])
        self.assertEqual(detect_session_conflicts(rows), [])

    def test_needs_fifteen_sessions_within_sixty_days(self) -> None:
        rows = _sessions(self.rng, 14)
        rows[3] = (rows[3][0], rows[3][1], rows[3][2] + 400.0, rows[3][3])
        self.assertEqual(detect_session_conflicts(rows), [])
        old = [(f"old{k}", _d(-100 - k), 10.0, 10.0) for k in range(10)]
        self.assertEqual(detect_session_conflicts(old + rows), [])

    def test_old_sessions_leave_the_fit(self) -> None:
        rows = _sessions(self.rng, 24)
        rows[10] = (rows[10][0], rows[10][1], rows[10][2] + 250.0, rows[10][3])
        old = [(f"old{k}", _d(-61 - k), 900.0, 10.0) for k in range(5)]
        found = detect_session_conflicts(old + rows)
        self.assertEqual([c.session_id for c in found], ["w10"])
        self.assertEqual(found[0].numbers["n_sessions"], 24.0)

    def test_degenerate_inputs_return_empty(self) -> None:
        flat = [(f"w{k}", _d(k), 100.0 + k, 50.0) for k in range(20)]
        self.assertEqual(detect_session_conflicts(flat), [])
        exact = [(f"w{k}", _d(k), 2.0 * k + 1.0, float(k)) for k in range(20)]
        self.assertEqual(detect_session_conflicts(exact), [])
        self.assertEqual(detect_session_conflicts([]), [])
        nan = [(f"w{k}", _d(k), float("nan"), 1.0) for k in range(20)]
        self.assertEqual(detect_session_conflicts(nan), [])


# --------------------------------------------------------------------------
# weekly_reconciliation over a synthetic store

LIKERT = [k for k in checkins.DAILY_ITEMS if k != "wellbeing"]
LOW_DAYS = (1, 2, 3)  # Tue..Thu of the week: check-ins far below baseline
OUTLIER = "w-outlier"
CFG = MetricsConfig(tz="Europe/Berlin", source_priority={}, hrmax=190.0)


def _daily_rows(rng: random.Random) -> list[DailyValue]:
    rows = []
    for k in range(-110, 7):
        day = _d(k)
        rows.append(DailyValue(day, "hrv_sdnn_ln", rng.gauss(3.9, 0.08), "watch", 4, "ok"))
        rows.append(DailyValue(day, "rhr", rng.gauss(55.0, 1.5), "watch", 10, "ok"))
        rows.append(DailyValue(day, "tst_h", rng.gauss(7.3, 0.4), "watch", 1, "ok"))
    return rows


def _checkin(day: str, item: str, value: float, ref: str | None = None) -> CheckinItem:
    return CheckinItem(day, item, value, None, ref, f"{day}T07:30:00+01:00", checkins.WORDING_VERSION, "form")


def _morning(day: str, rng: random.Random, low: bool) -> list[CheckinItem]:
    rows = []
    for key in LIKERT:
        spec = checkins.ITEMS[key]
        value = (7.0 if spec.inverted else 1.0) if low else float(rng.randint(3, 5))
        rows.append(_checkin(day, key, value))
    rows.append(_checkin(day, "wellbeing", 0.0 if low else float(rng.randint(5, 7))))
    return rows


def _hr_series(start_iso: str, minutes: int, bpm: float) -> tuple[tuple[str, float], ...]:
    head = start_iso[:11]
    base_h, base_m = int(start_iso[11:13]), int(start_iso[14:16])
    out = []
    for t in range(0, minutes + 1, 5):
        total = base_h * 60 + base_m + t
        out.append((f"{head}{total // 60:02d}:{total % 60:02d}:00+01:00", bpm))
    return tuple(out)


def _workout(wid: str, day: str, minutes: int, bpm: float, source: str = "watch",
             hae_format: bool = False) -> Workout:
    start = f"{day}T17:00:00+01:00"
    end_h = 17 + minutes // 60
    end = f"{day}T{end_h:02d}:{minutes % 60:02d}:00+01:00"
    duration: float | None = minutes * 60.0
    if hae_format:
        start, end, duration = f"{day} 17:00:00 +0100", f"{day} {end_h:02d}:{minutes % 60:02d}:00 +0100", None
    hr = _hr_series(f"{day}T17:00:00+01:00", minutes, bpm)
    return Workout(wid, "Lauf", start, end, duration, None, bpm, bpm, hr, False, source, "{}")


def _training(rng: random.Random) -> tuple[list[Workout], list[CheckinItem]]:
    workouts, ratings = [], []
    for i, k in enumerate(range(-56, 7, 3)):
        day, wid = _d(k), f"w{i:02d}"
        zone = rng.randint(1, 4)
        minutes = rng.choice((30, 40, 50))
        workouts.append(_workout(wid, day, minutes, 190.0 * (0.55 + 0.1 * zone), hae_format=i == 0))
        ratings.append(_checkin(day, "session_rpe", min(10.0, 2.0 * zone + rng.gauss(0.0, 0.2)), wid))
    day = _d(2)
    workouts.append(_workout(OUTLIER, day, 50, 190.0 * 0.6))
    ratings.append(_checkin(day, "session_rpe", 10.0, OUTLIER))
    workouts.append(_workout("w-phone-dup", day, 50, 190.0 * 0.6, source="phone"))
    return workouts, ratings


class WeeklyReconciliationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp())
        cls.store = Store(cls.tmp / "t.sqlite")
        cls.store.migrate()
        rng = random.Random(RNG_SEED)
        cls.store.put_daily(_daily_rows(rng))
        morning = [it for k in range(-40, 7) for it in _morning(_d(k), rng, k in LOW_DAYS)]
        workouts, ratings = _training(rng)
        cls.store.put_checkins(morning + ratings)
        cls.store.upsert_workouts(workouts)
        cls.result = weekly_reconciliation(cls.store, "2026-W10", CFG)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.store.close()
        shutil.rmtree(cls.tmp)

    def test_shape_and_json_round_trip(self) -> None:
        r = self.result
        self.assertEqual(set(r), {"week", "days", "conflicts", "sessions_flagged"})
        self.assertEqual(r["week"], "2026-W10")
        self.assertEqual([d["date"] for d in r["days"]], [_d(k) for k in range(-4, 7)])
        self.assertTrue(all(d["quality_ok"] for d in r["days"]))
        self.assertEqual(json.loads(json.dumps(r)), r)
        for c in r["conflicts"]:
            self.assertEqual(set(c), {"id", "rule_id", "window", "numbers", "sign", "session_id"})
            self.assertEqual(c["id"], conflict_id(c["rule_id"], tuple(c["window"]), c["session_id"]))

    def test_low_checkins_against_normal_body_fire_r1(self) -> None:
        r1 = [c for c in self.result["conflicts"] if c["rule_id"] == "R1_sustained"]
        self.assertEqual(len(r1), 1)
        self.assertEqual(r1[0]["sign"], -1)
        self.assertLessEqual(r1[0]["window"][0], _d(LOW_DAYS[0]))
        self.assertGreaterEqual(r1[0]["window"][1], _d(LOW_DAYS[-1]))
        low = {d["date"]: d for d in self.result["days"]}[_d(2)]
        self.assertLess(low["subjective"], -2.5)

    def test_session_outlier_is_flagged_once(self) -> None:
        flagged = self.result["sessions_flagged"]
        self.assertEqual([f["workout_id"] for f in flagged], [OUTLIER])
        self.assertEqual(flagged[0]["date"], _d(2))
        self.assertGreaterEqual(flagged[0]["residual_z"], 2.0)
        ids = {c["id"] for c in self.result["conflicts"] if c["rule_id"] == "R3_session_residual"}
        self.assertEqual(ids, {flagged[0]["conflict_id"]})

    def test_deterministic(self) -> None:
        self.assertEqual(weekly_reconciliation(self.store, "2026-W10", CFG), self.result)

    def test_bad_week(self) -> None:
        with self.assertRaises(ValueError):
            weekly_reconciliation(self.store, "2026-10", CFG)


class EmptyStoreTest(unittest.TestCase):
    def test_no_data_is_one_data_gap(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "t.sqlite")
        try:
            store.migrate()
            r = weekly_reconciliation(store, "2026-W10", CFG)
        finally:
            store.close()
            shutil.rmtree(tmp)
        self.assertEqual([c["rule_id"] for c in r["conflicts"]], ["DATA_GAP"])
        self.assertEqual(r["conflicts"][0]["window"], [_d(-4), _d(6)])
        self.assertEqual(r["conflicts"][0]["numbers"]["days"], 11.0)
        self.assertEqual(r["sessions_flagged"], [])


class OutputContractTest(unittest.TestCase):
    RECON = {"conflicts": [{"id": "a" * 10}, {"id": "b" * 10}]}

    def _item(self, cid: str, question: str | None = None) -> dict:
        return {"conflict_id": cid, "interpretation_de": "Die Werte weichen ab.", "noisier_signal": "unclear",
                "question_for_owner_de": question, "refs": ["data/conflicts/2026-W10.json"]}

    def test_schema_accepts_valid_and_rejects_invalid(self) -> None:
        good = {"items": [self._item("a" * 10, "Bist du gereist?"), self._item("b" * 10)]}
        self.assertEqual(agents.validate_json(good, INSULA_OUTPUT_SCHEMA), [])
        bad = {"items": [{**self._item("a" * 10), "noisier_signal": "both", "refs": []}]}
        self.assertEqual(len(agents.validate_json(bad, INSULA_OUTPUT_SCHEMA)), 2)
        self.assertTrue(agents.validate_json({"items": [{**self._item("a" * 10), "extra": 1}]},
                                             INSULA_OUTPUT_SCHEMA))

    def test_output_errors(self) -> None:
        good = {"items": [self._item("a" * 10), self._item("b" * 10)]}
        self.assertEqual(output_errors(good, self.RECON), [])
        errors = output_errors({"items": [self._item("a" * 10), self._item("a" * 10), self._item("c" * 10)]},
                               self.RECON)
        self.assertEqual(len(errors), 3)
        recon = {"conflicts": [{"id": f"{k:010d}"} for k in range(4)]}
        asked = {"items": [self._item(f"{k:010d}", "Frage?") for k in range(4)]}
        self.assertEqual(output_errors(asked, recon), [f"4 owner questions, at most {MAX_OWNER_QUESTIONS}"])


if __name__ == "__main__":
    unittest.main()

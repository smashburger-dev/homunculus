"""Tests for homunculus.checkins. All values are small synthetic fixtures."""

from __future__ import annotations

import random
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from homunculus import checkins, stats
from homunculus.checkins import (
    COACH_CHECKIN_SCHEMA,
    ITEMS,
    SYMPTOMS,
    URGENT_SYMPTOMS,
    WORDING_VERSION,
    CheckinError,
    composite,
    from_chat,
    morning_form_html,
    record,
    restrictive_language,
    subjective_scores,
    symptom_flags,
    validate,
)
from homunculus.store import CheckinItem, Store

RNG_SEED = 4711
DAY = "2026-03-10"
TS = "2026-03-10T07:05:00+01:00"


def _item(day: str, item: str, value: float | None, origin: str = "form") -> CheckinItem:
    return CheckinItem(day, item, value, None, None, f"{day}T07:00:00+00:00", WORDING_VERSION, origin)


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp / "t.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp)


class ConstantsTest(unittest.TestCase):
    def test_items_and_ranges(self) -> None:
        self.assertEqual(WORDING_VERSION, "de-1")
        likert = ("sleep_quality", "energy", "mood", "soreness", "stress", "motivation")
        for key in likert:
            self.assertEqual((ITEMS[key].lo, ITEMS[key].hi), (1, 7), key)
        self.assertEqual((ITEMS["wellbeing"].lo, ITEMS["wellbeing"].hi), (0, 10))
        self.assertEqual((ITEMS["session_rpe"].lo, ITEMS["session_rpe"].hi), (0, 10))
        inverted = {k for k, s in ITEMS.items() if s.inverted}
        self.assertEqual(inverted, {"soreness", "stress"})
        self.assertTrue(all(s.key == k and s.prompt_de for k, s in ITEMS.items()))

    def test_symptoms(self) -> None:
        self.assertEqual(len(SYMPTOMS), 10)
        self.assertEqual(URGENT_SYMPTOMS, frozenset({"chest_pain", "fainting", "short_breath"}))
        self.assertTrue(URGENT_SYMPTOMS <= set(SYMPTOMS))

    def test_items_mapping_is_read_only(self) -> None:
        with self.assertRaises(TypeError):
            ITEMS["x"] = ITEMS["mood"]  # type: ignore[index]


class ValidateTest(unittest.TestCase):
    def test_numbers_strings_and_lists(self) -> None:
        form = {"mood": 5, "energy": "3", "stress": ["2"], "wellbeing": 7.0, "date": DAY}
        items = validate(form, date=DAY, ts=TS)
        got = {it.item: it.value for it in items}
        self.assertEqual(got, {"mood": 5.0, "energy": 3.0, "stress": 2.0, "wellbeing": 7.0})
        self.assertEqual([it.item for it in items], ["energy", "mood", "stress", "wellbeing"])
        for it in items:
            self.assertEqual((it.date, it.ts, it.origin, it.wording_version), (DAY, TS, "form", "de-1"))
            self.assertIsNone(it.ref)
            self.assertIsInstance(it.value, float)

    def test_empty_answers_are_skipped(self) -> None:
        items = validate({"mood": "", "energy": " ", "motivation": [], "sleep_quality": 6, "note": "  "},
                         date=DAY, ts=TS)
        self.assertEqual([(it.item, it.value) for it in items], [("sleep_quality", 6.0)])

    def test_symptoms_note_and_session_rpe(self) -> None:
        form = {
            "symptoms": ["fever", "chest_pain"],
            "symptom:dizziness": "on",
            "symptom:fainting": "0",
            "note": "  Beine schwer  ",
            "session_rpe": "8",
            "workout_id": "w-1",
        }
        items = validate(form, date=DAY, ts=TS)
        by = {it.item: it for it in items}
        self.assertEqual(by["session_rpe"].ref, "w-1")
        self.assertEqual(by["session_rpe"].value, 8.0)
        self.assertEqual(by["note"].text, "Beine schwer")
        self.assertIsNone(by["note"].value)
        self.assertEqual(by["symptom:fever"].value, 1.0)
        self.assertNotIn("symptom:fainting", by)
        self.assertEqual(symptom_flags(items), ["fever", "chest_pain", "dizziness"])

    def test_comma_separated_symptoms(self) -> None:
        items = validate({"symptoms": "sore_throat, fever"}, date=DAY, ts=TS)
        self.assertEqual(symptom_flags(items), ["fever", "sore_throat"])

    def test_origin_passthrough(self) -> None:
        items = validate({"mood": 4}, date=DAY, ts=TS, origin="hae")
        self.assertEqual(items[0].origin, "hae")

    def test_rejections(self) -> None:
        bad = [
            {"mood": 8},
            {"mood": 0},
            {"wellbeing": 11},
            {"session_rpe": -1},
            {"mood": 4.5},
            {"mood": True},
            {"mood": "nan"},
            {"mood": "inf"},
            {"mood": "viel"},
            {"mood": ["3", "4"]},
            {"mood": {"v": 3}},
            {"mood": 3, "weight": 70},
            {"symptom:headache": "on"},
            {"symptoms": ["fever", "headache"]},
            {"symptoms": 5},
            {"symptom:fever": "maybe"},
            {"note": "x" * (checkins.NOTE_MAX + 1)},
            {"note": 5},
            {"session_rpe": 5, "workout_id": 12},
            {},
            {"mood": ""},
        ]
        for form in bad:
            with self.subTest(form=form), self.assertRaises(CheckinError):
                validate(form, date=DAY, ts=TS)

    def test_bad_date_param(self) -> None:
        for day in ("2026-13-01", "20260310", "", None):
            with self.subTest(day=day), self.assertRaises(CheckinError):
                validate({"mood": 3}, date=day, ts=TS)  # type: ignore[arg-type]

    def test_checkin_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(CheckinError, ValueError))


class FromChatTest(unittest.TestCase):
    def test_coach_origin_and_unknown_keys(self) -> None:
        items = from_chat({"mood": 6, "symptoms": ["palpitations"], "vibe": "great", "symptom:fever": 1,
                           "date": "1999-01-01"}, date=DAY, ts=TS)
        self.assertEqual([it.item for it in items], ["mood", "symptom:palpitations"])
        self.assertTrue(all(it.origin == "coach" and it.date == DAY for it in items))

    def test_rejects_out_of_range_without_clamping(self) -> None:
        for extracted in ({"energy": 9}, {"wellbeing": -1}, {"session_rpe": 10.5}):
            with self.subTest(extracted=extracted), self.assertRaises(CheckinError):
                from_chat(extracted, date=DAY, ts=TS)

    def test_empty_and_null_values(self) -> None:
        self.assertEqual(from_chat({}, date=DAY, ts=TS), [])
        self.assertEqual(from_chat({"mood": None, "other": 1}, date=DAY, ts=TS), [])

    def test_session_rpe_with_workout(self) -> None:
        items = from_chat({"session_rpe": 7, "workout_id": "w-9"}, date=DAY, ts=TS)
        self.assertEqual([(it.item, it.value, it.ref) for it in items], [("session_rpe", 7.0, "w-9")])

    def test_schema_matches_items(self) -> None:
        props = COACH_CHECKIN_SCHEMA["properties"]
        self.assertEqual(COACH_CHECKIN_SCHEMA["type"], "object")
        self.assertFalse(COACH_CHECKIN_SCHEMA["additionalProperties"])
        for key, spec in ITEMS.items():
            self.assertEqual((props[key]["minimum"], props[key]["maximum"]), (spec.lo, spec.hi))
        self.assertEqual(props["symptoms"]["items"]["enum"], list(SYMPTOMS))
        self.assertIn("note", props)
        self.assertIn("workout_id", props)

    def test_every_schema_key_accepted(self) -> None:
        sample = {key: spec.lo for key, spec in ITEMS.items()}
        sample.update({"workout_id": "w-2", "symptoms": list(SYMPTOMS), "note": "ok"})
        self.assertEqual(set(sample), set(COACH_CHECKIN_SCHEMA["properties"]))
        items = from_chat(sample, date=DAY, ts=TS)
        self.assertEqual(len(items), len(ITEMS) + len(SYMPTOMS) + 1)


class RecordTest(_StoreCase):
    NOW = datetime(2026, 3, 10, 6, 30, tzinfo=timezone.utc)

    def test_stores_and_reports_symptoms(self) -> None:
        status, body = record(self.store, {"mood": "5", "symptoms": ["short_breath"]}, self.NOW, "Europe/Berlin")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"stored": 2, "symptoms": ["short_breath"]})
        rows = self.store.checkins("2026-03-10", "2026-03-11")
        self.assertEqual({r.item for r in rows}, {"mood", "symptom:short_breath"})
        self.assertEqual(rows[0].ts, "2026-03-10T07:30:00+01:00")
        self.assertEqual(rows[0].wording_version, "de-1")

    def test_local_date_from_tz(self) -> None:
        late = datetime(2026, 3, 10, 23, 30, tzinfo=timezone.utc)
        status, _ = record(self.store, {"mood": 4}, late, "Europe/Berlin")
        self.assertEqual(status, 200)
        self.assertEqual([r.date for r in self.store.checkins("2026-01-01")], ["2026-03-11"])

    def test_naive_now_is_utc(self) -> None:
        record(self.store, {"mood": 4}, datetime(2026, 3, 10, 23, 30), "Europe/Berlin")
        self.assertEqual([r.date for r in self.store.checkins("2026-01-01")], ["2026-03-11"])

    def test_form_date_within_window(self) -> None:
        status, _ = record(self.store, {"mood": 4, "date": ["2026-03-09"]}, self.NOW, "UTC")
        self.assertEqual(status, 200)
        self.assertEqual([r.date for r in self.store.checkins("2026-01-01")], ["2026-03-09"])

    def test_form_date_rejected(self) -> None:
        for day in ("2026-03-11", "2026-03-02", "gestern"):
            with self.subTest(day=day):
                status, body = record(self.store, {"mood": 4, "date": day}, self.NOW, "UTC")
                self.assertEqual(status, 400)
                self.assertIn("error", body)
        self.assertEqual(self.store.checkins("2000-01-01"), [])

    def test_invalid_form_stores_nothing(self) -> None:
        status, body = record(self.store, {"mood": 5, "energy": 99}, self.NOW, "UTC")
        self.assertEqual(status, 400)
        self.assertIn("energy", body["error"])
        self.assertEqual(self.store.checkins("2000-01-01"), [])

    def test_resubmission_replaces(self) -> None:
        record(self.store, {"mood": 2}, self.NOW, "UTC")
        status, body = record(self.store, {"mood": 6}, self.NOW + timedelta(minutes=5), "UTC")
        self.assertEqual((status, body["stored"]), (200, 1))
        rows = self.store.checkins("2000-01-01", item="mood")
        self.assertEqual([r.value for r in rows], [6.0])


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[dict[str, str | None]] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag in ("input", "textarea"):
            self.inputs.append(dict(attrs, tag=tag))


class MorningFormTest(unittest.TestCase):
    def test_escaping_and_no_script(self) -> None:
        page = morning_form_html('2026-03-10"><script>x</script>', action="/checkin?a=1&b=<2>")
        self.assertNotIn("<script", page)
        self.assertIn("/checkin?a=1&amp;b=&lt;2&gt;", page)
        self.assertIn("&quot;&gt;&lt;script&gt;", page)

    def test_fields_round_trip_through_validate(self) -> None:
        parser = _FormParser()
        parser.feed(morning_form_html(DAY))
        self.assertEqual(parser.tags[0], "form")
        radios: dict[str, list[str]] = {}
        for inp in parser.inputs:
            if inp.get("type") == "radio":
                radios.setdefault(str(inp["name"]), []).append(str(inp["value"]))
        daily = [k for k in ITEMS if k != "session_rpe"]
        self.assertEqual(sorted(radios), sorted(daily))
        for key, values in radios.items():
            spec = ITEMS[key]
            self.assertEqual(values, [str(n) for n in range(spec.lo, spec.hi + 1)])
        boxes = [i["value"] for i in parser.inputs if i.get("type") == "checkbox"]
        self.assertEqual(boxes, list(SYMPTOMS))
        names = {str(i["name"]) for i in parser.inputs}
        self.assertEqual(names, set(daily) | {"date", "symptoms", "note"})
        hidden = [i for i in parser.inputs if i.get("type") == "hidden"]
        self.assertEqual(hidden[0]["value"], DAY)
        form = {key: values[-1] for key, values in radios.items()}
        form.update({"date": DAY, "symptoms": boxes, "note": "ok"})
        self.assertEqual(len(validate(form, date=DAY, ts=TS)), len(daily) + len(SYMPTOMS) + 1)


class SubjectiveScoresTest(_StoreCase):
    def _seed(self, rng: random.Random, days: int, items: tuple[str, ...]) -> dict[str, list[float]]:
        base: dict[str, list[float]] = {k: [] for k in items}
        rows = []
        for offset in range(1, days + 1):
            day = (date.fromisoformat(DAY) - timedelta(days=offset)).isoformat()
            for key in items:
                value = float(rng.randint(2, 6))
                base[key].append(value)
                rows.append(_item(day, key, value))
        self.store.put_checkins(rows)
        return base

    def test_z_against_own_baseline(self) -> None:
        rng = random.Random(RNG_SEED)
        base = self._seed(rng, 28, ("mood", "stress"))
        self.store.put_checkins([_item(DAY, "mood", 6.0), _item(DAY, "stress", 6.0)])
        scores = subjective_scores(self.store, DAY)
        self.assertEqual(set(scores), {k for k in ITEMS if k != "session_rpe"})
        exp_mood = stats.zscore(6.0, stats.mean(base["mood"]), stats.sd(base["mood"]))
        exp_stress = stats.zscore(6.0, stats.mean(base["stress"]), stats.sd(base["stress"]))
        self.assertAlmostEqual(scores["mood"], exp_mood, places=12)
        self.assertAlmostEqual(scores["stress"], -exp_stress, places=12)
        self.assertIsNone(scores["energy"])

    def test_window_excludes_older_days_and_today(self) -> None:
        rows = [_item((date.fromisoformat(DAY) - timedelta(days=o)).isoformat(), "energy", float(2 + o % 3))
                for o in range(1, 8)]
        rows.append(_item("2026-02-01", "energy", 7.0))
        rows.append(_item(DAY, "energy", 5.0))
        self.store.put_checkins(rows)
        base = [float(2 + o % 3) for o in range(1, 8)]
        got = subjective_scores(self.store, DAY, window=7)["energy"]
        self.assertAlmostEqual(got, stats.zscore(5.0, stats.mean(base), stats.sd(base)), places=12)

    def test_insufficient_or_flat_baseline(self) -> None:
        rng = random.Random(RNG_SEED)
        self._seed(rng, 13, ("mood",))
        rows = [_item((date.fromisoformat(DAY) - timedelta(days=o)).isoformat(), "energy", 4.0) for o in range(1, 29)]
        rows += [_item(DAY, "mood", 5.0), _item(DAY, "energy", 6.0)]
        self.store.put_checkins(rows)
        scores = subjective_scores(self.store, DAY)
        self.assertIsNone(scores["mood"])
        self.assertIsNone(scores["energy"])

    def test_cap_and_session_rpe_ignored(self) -> None:
        rows = [_item((date.fromisoformat(DAY) - timedelta(days=o)).isoformat(), "wellbeing", float(5 + o % 2))
                for o in range(1, 15)]
        rows += [_item(DAY, "wellbeing", 0.0), _item(DAY, "session_rpe", 9.0)]
        self.store.put_checkins(rows)
        scores = subjective_scores(self.store, DAY)
        self.assertEqual(scores["wellbeing"], -3.0)
        self.assertNotIn("session_rpe", scores)

    def test_bad_arguments(self) -> None:
        with self.assertRaises(ValueError):
            subjective_scores(self.store, DAY, window=0)
        with self.assertRaises(ValueError):
            subjective_scores(self.store, "10.03.2026")


class CompositeAndFlagsTest(unittest.TestCase):
    def test_composite(self) -> None:
        self.assertIsNone(composite({}))
        self.assertIsNone(composite({"a": 1.0, "b": None, "c": 2.0}))
        self.assertAlmostEqual(composite({"a": 1.0, "b": -0.5, "c": 2.0, "d": None}), 2.5 / 3)

    def test_symptom_flags(self) -> None:
        items = [
            _item(DAY, "symptom:fainting", 1.0),
            _item(DAY, "symptom:fever", 1.0),
            _item(DAY, "symptom:dizziness", 0.0),
            _item(DAY, "symptom:fever", 1.0),
            _item(DAY, "mood", 3.0),
        ]
        self.assertEqual(symptom_flags(items), ["fever", "fainting"])
        self.assertEqual(symptom_flags([]), [])


class RestrictiveLanguageTest(unittest.TestCase):
    def test_positive(self) -> None:
        for text in (
            "Habe heute NICHTS   gegessen.",
            "Ich fühle mich fett",
            "musste das wieder abtrainieren",
            "Abführmittel genommen",
            "I skipped meals again",
            "Felt fat after dinner",
            "didn’t eat since lunch",
            "need to burn it off",
        ):
            with self.subTest(text=text):
                self.assertTrue(restrictive_language(text))

    def test_negative(self) -> None:
        for text in ("", "Hatte großen Hunger nach dem Lauf", "fettarmer Joghurt", "Beine schwer, gut gegessen",
                     "Starvation study read today"):
            with self.subTest(text=text):
                self.assertFalse(restrictive_language(text))

    def test_terms_constant(self) -> None:
        self.assertTrue(checkins.RESTRICTIVE_TERMS)
        for term in checkins.RESTRICTIVE_TERMS:
            with self.subTest(term=term):
                self.assertTrue(restrictive_language(f"x {term.upper()} y"))


if __name__ == "__main__":
    unittest.main()

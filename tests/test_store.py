"""Tests for homunculus.store. All values are small synthetic fixtures."""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from homunculus import store
from homunculus.store import (
    CheckinItem,
    DailyValue,
    HealthEvent,
    Ledger,
    PathNotAllowed,
    Sample,
    SleepNight,
    SleepSegment,
    Store,
    Workout,
    canonical_json,
)

RNG_SEED = 1234


def _samples(rng: random.Random, n: int) -> list[Sample]:
    return [
        Sample("hrv_sdnn", "ms", f"2026-01-01T0{i}:00:00+00:00", "qty", round(rng.uniform(20, 80), 2), "watch")
        for i in range(n)
    ]


def _workout(wid: str = "w-1", indoor: bool | None = True) -> Workout:
    return Workout(
        id=wid, name="Synthetic Run", start="2026-01-02T10:00:00+00:00", end="2026-01-02T10:30:00+00:00",
        duration_s=1800.0, active_kcal=200.0, hr_avg=140.0, hr_max=170.0,
        hr_series=(("2026-01-02T10:00:00+00:00", 120.0), ("2026-01-02T10:15:00+00:00", 150.0)),
        is_indoor=indoor, source="watch", raw_json='{"name": "Synthetic Run"}',
    )


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = Path(self.tmp) / "raw.sqlite"
        self.store = Store(self.db)
        self.rng = random.Random(RNG_SEED)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp)

    def test_migrate_is_idempotent(self) -> None:
        self.assertEqual(self.store.migrate(), store.SCHEMA_VERSION)
        self.assertEqual(self.store.migrate(), store.SCHEMA_VERSION)

    def test_refuses_newer_schema(self) -> None:
        self.store.close()
        conn = sqlite3.connect(self.db)
        conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
        conn.close()
        with self.assertRaises(RuntimeError):
            Store(self.db)
        self.store = Store(":memory:")

    def test_samples_upsert_and_range(self) -> None:
        rows = _samples(self.rng, 5)
        self.assertEqual(self.store.upsert_samples(rows), 5)
        changed = Sample(rows[0].metric, rows[0].unit, rows[0].ts, rows[0].field, 99.0, "watch")
        self.assertEqual(self.store.upsert_samples([changed]), 1)
        got = self.store.samples("hrv_sdnn", "2026-01-01")
        self.assertEqual(len(got), 5)
        self.assertEqual(got[0].value, 99.0)
        window = self.store.samples("hrv_sdnn", "2026-01-01T01:00", "2026-01-01T03:00")
        self.assertEqual([s.ts[11:13] for s in window], ["01", "02"])
        self.assertEqual(self.store.samples("hrv_sdnn", "2026-01-01", source="phone"), [])
        self.assertEqual(self.store.samples("other_metric", "2026-01-01"), [])

    def test_sources_are_separate_rows(self) -> None:
        a = Sample("resting_hr", "bpm", "2026-01-01T07:00:00+00:00", "qty", 55.0, "watch")
        b = Sample("resting_hr", "bpm", "2026-01-01T07:00:00+00:00", "qty", 57.0, "garmin")
        self.store.upsert_samples([a, b])
        self.assertEqual(len(self.store.samples("resting_hr", "2026")), 2)
        self.assertEqual(self.store.samples("resting_hr", "2026", source="garmin"), [b])

    def test_validation_rejects_unknown_enums(self) -> None:
        with self.assertRaises(ValueError):
            Sample("m", None, "t", "qty", 1.0, "iPhone of someone")
        with self.assertRaises(ValueError):
            SleepSegment("a", "b", "light", "watch")
        with self.assertRaises(ValueError):
            HealthEvent("unknown", "a", None, "{}", "watch")
        with self.assertRaises(ValueError):
            DailyValue("2026-01-01", "k", 1.0, "watch", 1, "good")
        with self.assertRaises(ValueError):
            CheckinItem("2026-01-01", "mood", 5.0, None, None, "t", "v1", "chat")

    def test_sleep_segments_and_nights(self) -> None:
        segs = [
            SleepSegment("2026-01-01T23:00:00+00:00", "2026-01-02T01:00:00+00:00", "core", "watch"),
            SleepSegment("2026-01-02T01:00:00+00:00", "2026-01-02T02:00:00+00:00", "deep", "watch"),
        ]
        self.assertEqual(self.store.upsert_sleep_segments(segs), 2)
        self.assertEqual(self.store.sleep_segments("2026-01-02"), [segs[1]])
        night = SleepNight("2026-01-02", segs[0].start, segs[1].end, None, None,
                           3.0, 3.0, 2.0, 1.0, None, None, "watch")
        self.store.upsert_sleep_nights([night])
        self.assertEqual(self.store.sleep_nights("2026-01-01", "2026-01-03"), [night])
        self.assertEqual(self.store.sleep_nights("2026-01-03"), [])

    def test_workout_roundtrip(self) -> None:
        for indoor in (True, False, None):
            with self.subTest(indoor=indoor):
                w = _workout(f"w-{indoor}", indoor)
                self.store.upsert_workouts([w])
                got = [x for x in self.store.workouts("2026-01-02") if x.id == w.id]
                self.assertEqual(got, [w])
                self.assertIsInstance(got[0].hr_series, tuple)

    def test_insert_events_counts_new_rows_only(self) -> None:
        ev = HealthEvent("hr_notification", "2026-01-03T12:00:00+00:00", None, '{"bpm": 130}', "watch")
        ecg = HealthEvent("ecg", "2026-01-04T12:00:00+00:00", None, "{}", "watch")
        self.assertEqual(self.store.insert_events([ev]), 1)
        self.assertEqual(self.store.insert_events([ev, ecg]), 1)
        self.assertEqual(self.store.events(None, "2026-01-01"), [ev, ecg])
        self.assertEqual(self.store.events("ecg", "2026-01-01"), [ecg])
        self.assertEqual(self.store.events(None, "2026-01-04"), [ecg])

    def test_daily_ordered_and_replaced(self) -> None:
        days = [f"2026-01-{d:02d}" for d in (3, 1, 2)]
        rows = [DailyValue(d, "hrv_ln", round(self.rng.uniform(3, 4), 3), "watch", 4, "ok") for d in days]
        rows.append(DailyValue("2026-01-02", "rhr", None, "watch", 0, "missing"))
        self.assertEqual(self.store.put_daily(rows), 4)
        self.store.put_daily([DailyValue("2026-01-02", "hrv_ln", None, "watch", 1, "sparse")])
        got = self.store.daily("hrv_ln", "2026-01-01")
        self.assertEqual([r.date for r in got], ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertIsNone(got[1].value)
        self.assertEqual(got[1].quality, "sparse")
        self.assertEqual(len(self.store.daily("hrv_ln", "2026-01-01", "2026-01-03")), 2)

    def test_checkins_ref_none_is_unique_key(self) -> None:
        a = CheckinItem("2026-01-01", "wellbeing", 6.0, None, None, "2026-01-01T21:00:00+00:00", "v1", "form")
        b = CheckinItem("2026-01-01", "wellbeing", 7.0, None, None, "2026-01-01T21:05:00+00:00", "v1", "form")
        rpe = CheckinItem("2026-01-01", "session_rpe", 6.0, None, "w-1", "2026-01-01T19:00:00+00:00", "v1", "coach")
        self.store.put_checkins([a, b, rpe])
        got = self.store.checkins("2026-01-01")
        self.assertEqual(len(got), 2)
        self.assertIn(b, got)
        self.assertEqual(self.store.checkins("2026-01-01", item="session_rpe"), [rpe])
        self.assertEqual(self.store.checkins("2026-01-02"), [])

    def test_latest_ts(self) -> None:
        self.assertEqual(self.store.latest_ts(), {})
        self.store.upsert_samples(_samples(self.rng, 3))
        self.store.upsert_workouts([_workout()])
        latest = self.store.latest_ts()
        self.assertEqual(latest["hrv_sdnn"], "2026-01-01T02:00:00+00:00")
        self.assertEqual(latest["workouts"], "2026-01-02T10:30:00+00:00")
        self.assertNotIn("events", latest)

    def test_threaded_writes(self) -> None:
        def worker(k: int) -> None:
            rows = [Sample("steps", "count", f"2026-01-01T{k:02d}:{m:02d}:00+00:00", "qty", 1.0, "phone")
                    for m in range(20)]
            self.store.upsert_samples(rows)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.store.samples("steps", "2026")), 160)

    def test_data_persists_across_connections(self) -> None:
        self.store.put_daily([DailyValue("2026-01-01", "tst_h", 7.0, "watch", 1, "ok")])
        self.store.close()
        self.store = Store(self.db)
        self.assertEqual(self.store.daily("tst_h", "2026")[0].value, 7.0)


class CanonicalJsonTests(unittest.TestCase):
    def test_sorted_compact_utf8(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": "ä"}), '{"a":"ä","b":1}'.encode("utf-8"))

    def test_rejects_nan(self) -> None:
        with self.assertRaises(ValueError):
            canonical_json({"x": float("nan")})


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "ledger"
        self.root.mkdir()
        rules = {"amygdala": ["reviews/*/02-amygdala-r1.json", "evidence/papers/*.audit.json"],
                 "Vagus": ["checkins/*"]}
        self.ledger = Ledger(self.root, rules)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def test_write_json_format_and_read(self) -> None:
        path = self.ledger.write_json("reviews/2026-W01/02-amygdala-r1.json", {"b": 1, "a": "ü"}, author="amygdala")
        self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "a": "ü",\n  "b": 1\n}\n')
        self.assertEqual(self.ledger.read_json("reviews/2026-W01/02-amygdala-r1.json"), {"a": "ü", "b": 1})
        self.assertEqual(self.ledger.read_json("missing.json", default={}), {})
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_write_rules(self) -> None:
        with self.assertRaises(PathNotAllowed):
            self.ledger.write_json("plans/v0001.json", {}, author="amygdala")
        with self.assertRaises(PathNotAllowed):
            self.ledger.write_json("checkins/x.json", {}, author="unknown")
        self.ledger.write_text("checkins/2026-01-01.json", "{}\n", author="vagus")
        self.ledger.write_json("plans/v0001.json", {"v": 1}, author="pineal")
        self.assertTrue(self.ledger.exists("plans/v0001.json"))
        self.assertTrue(issubclass(PathNotAllowed, PermissionError))

    def test_rejects_escaping_paths(self) -> None:
        for rel in ("../outside.json", "/etc/passwd", ".git/config", "a/../../b", ""):
            with self.subTest(rel=rel), self.assertRaises(PathNotAllowed):
                self.ledger.write_text(rel, "x", author="pineal")
        with self.assertRaises(PathNotAllowed):
            self.ledger.read_text("../outside.txt")
        with self.assertRaises(PathNotAllowed):
            self.ledger.glob("../*")

    def test_rejects_symlink_escape(self) -> None:
        outside = Path(self.tmp) / "outside"
        outside.mkdir()
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PathNotAllowed):
            self.ledger.write_text("link/x.txt", "x", author="pineal")
        self.assertEqual(list(outside.iterdir()), [])

    def test_jsonl_append_and_read(self) -> None:
        self.assertEqual(self.ledger.read_jsonl("ops/runs.jsonl"), [])
        self.ledger.append_jsonl("ops/runs.jsonl", {"job": "a", "n": 1}, author="pineal")
        self.ledger.append_jsonl("ops/runs.jsonl", {"job": "b", "n": 2}, author="pineal")
        self.assertEqual(self.ledger.read_jsonl("ops/runs.jsonl"), [{"job": "a", "n": 1}, {"job": "b", "n": 2}])
        self.assertEqual(self.ledger.read_text("ops/runs.jsonl").splitlines()[0], '{"job":"a","n":1}')

    def test_glob_sha_pending_halt(self) -> None:
        self.ledger.write_json("data/daily/2026/2026-01-02.json", {"x": 2}, author="pineal")
        self.ledger.write_json("data/daily/2026/2026-01-01.json", {"x": 1}, author="pineal")
        self.assertEqual(self.ledger.glob("data/daily/*/*.json"),
                         ["data/daily/2026/2026-01-01.json", "data/daily/2026/2026-01-02.json"])
        raw = (self.root / "data/daily/2026/2026-01-01.json").read_bytes()
        self.assertEqual(self.ledger.sha256("data/daily/2026/2026-01-01.json"), hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.ledger.pending(), ["data/daily/2026/2026-01-01.json", "data/daily/2026/2026-01-02.json"])
        self.assertFalse(self.ledger.halted())
        self.ledger.write_text("config/HALT", "", author="pineal")
        self.assertTrue(self.ledger.halted())

    def test_parse_identity(self) -> None:
        self.assertEqual(store.parse_identity("amygdala"), ("Amygdala", "amygdala@homunculus.local"))
        self.assertEqual(store.parse_identity("Amygdala <amygdala@homunculus.local>"),
                         ("Amygdala", "amygdala@homunculus.local"))
        with self.assertRaises(ValueError):
            store.parse_identity("bad\nname")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class LedgerCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "ledger"
        self.root.mkdir()
        empty_cfg = Path(self.tmp) / "gitconfig"
        empty_cfg.write_text("")
        self.env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(empty_cfg), "GIT_CONFIG_NOSYSTEM": "1"})
        self.env.start()
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        self.ledger = Ledger(self.root, {"amygdala": ["reviews/**"]})

    def tearDown(self) -> None:
        self.env.stop()
        shutil.rmtree(self.tmp)

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def test_nothing_to_commit_returns_none(self) -> None:
        self.assertIsNone(self.ledger.commit(author="pineal", message="noop", trailers={}))

    def test_commit_identity_trailers_and_pending(self) -> None:
        self.ledger.write_json("reviews/2026-W01/02-amygdala-r1.json", {"cid": "c1"}, author="amygdala")
        (self.root / "stray.txt").write_text("not written through the ledger")
        trailers = {"Msg-Id": "m-000000000001", "Run-Id": "r-test-1", "Model": "fake", "Kind": "challenge"}
        sha = self.ledger.commit(author="amygdala", message="Round 1 challenges", trailers=trailers)
        self.assertRegex(sha or "", r"^[0-9a-f]{40}$")
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), sha)
        self.assertEqual(self._git("log", "-1", "--format=%an <%ae>").strip(), "Amygdala <amygdala@homunculus.local>")
        self.assertEqual(self._git("log", "-1", "--format=%cn").strip(), "Pineal")
        body = self._git("log", "-1", "--format=%B")
        self.assertIn("Msg-Id: m-000000000001", body)
        self.assertTrue(body.startswith("Round 1 challenges\n\n"))
        files = self._git("show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(files, ["reviews/2026-W01/02-amygdala-r1.json"])
        self.assertEqual(self.ledger.pending(), [])

    def test_rewrite_same_content_returns_none(self) -> None:
        self.ledger.write_json("a.json", {"x": 1}, author="pineal")
        self.assertIsNotNone(self.ledger.commit(author="pineal", message="first", trailers={}))
        self.ledger.write_json("a.json", {"x": 1}, author="pineal")
        self.assertIsNone(self.ledger.commit(author="pineal", message="again", trailers={}))
        self.assertEqual(self.ledger.pending(), [])

    def test_invalid_trailer_rejected(self) -> None:
        self.ledger.write_json("a.json", {"x": 1}, author="pineal")
        with self.assertRaises(ValueError):
            self.ledger.commit(author="pineal", message="m", trailers={"Bad Key": "v"})
        with self.assertRaises(ValueError):
            self.ledger.commit(author="pineal", message="m", trailers={"Key": "a\nb"})
        self.assertEqual(self.ledger.pending(), ["a.json"])

    def test_commit_requires_git_repo(self) -> None:
        plain = Path(self.tmp) / "plain"
        plain.mkdir()
        ledger = Ledger(plain, {})
        ledger.write_json("a.json", {}, author="pineal")
        with self.assertRaises(FileNotFoundError):
            ledger.commit(author="pineal", message="m", trailers={})


if __name__ == "__main__":
    unittest.main()

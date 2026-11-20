"""Tests for homunculus.ingest. Every value is a small synthetic fixture built here."""

from __future__ import annotations

import http.client
import json
import os
import random
import shutil
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from homunculus import ingest
from homunculus.ingest import (
    ConfigError,
    IngestConfig,
    ParsedBatch,
    authorized,
    ingest_payload,
    load_config,
    make_server,
    normalize_source,
    parse_payload,
    prune_raw,
    save_raw,
    strip_workout,
)
from homunculus.store import Store

RNG_SEED = 4711
TOKEN = "t" * 40
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _ts(day: int, hour: int) -> str:
    return f"2026-01-{day:02d} {hour:02d}:00:00 +0100"


def _metric_payload(rng: random.Random) -> dict:
    hr = [{"date": _ts(9, h), "Min": 50 + h, "Avg": round(rng.uniform(60, 70), 1), "Max": 90, "source": "Synthetic Watch"}
          for h in range(3)]
    steps = [{"date": _ts(d, 0), "qty": rng.randint(1000, 9000), "source": "Synthetic iPhone"} for d in (8, 9)]
    return {"metrics": [
        {"name": "heart_rate", "units": "count/min", "data": hr},
        {"name": "step_count", "units": "count", "data": steps},
    ]}


def _sleep_payload() -> dict:
    night = {"date": "2026-01-09 00:00:00 +0100", "sleepStart": "2026-01-08 23:10:00 +0100",
             "sleepEnd": "2026-01-09 06:40:00 +0100", "inBedStart": "2026-01-08 23:00:00 +0100",
             "inBedEnd": "2026-01-09 06:50:00 +0100", "totalSleep": 7.0, "asleep": 0, "core": 4.0,
             "deep": 1.0, "rem": 1.5, "awake": 0.5, "source": "Synthetic Watch"}
    segs = [
        {"startDate": "2026-01-08 23:10:00 +0100", "endDate": "2026-01-09 01:00:00 +0100", "value": "Core", "qty": 1.8,
         "source": "Synthetic Watch"},
        {"startDate": "2026-01-09 01:00:00 +0100", "endDate": "2026-01-09 02:00:00 +0100", "value": "Deep",
         "source": "Synthetic Watch"},
        {"startDate": "2026-01-09 02:00:00 +0100", "endDate": "2026-01-09 02:10:00 +0100", "value": "Light"},
    ]
    return {"metrics": [{"name": "sleep_analysis", "units": "hr", "data": [night, *segs]}]}


def _workout_v2() -> dict:
    return {
        "id": "SYNTH-1", "name": "Outdoor Run", "start": _ts(9, 7), "end": _ts(9, 8), "duration": 3600,
        "location": "Outdoor", "activeEnergyBurned": {"qty": 1000, "units": "kJ"},
        "heartRate": {"avg": {"qty": 140, "units": "bpm"}, "max": {"qty": 170, "units": "bpm"}},
        "heartRateData": [{"date": _ts(9, 7), "Avg": 130, "Min": 120, "Max": 140, "source": "Synthetic Watch"}],
        "route": [{"latitude": 1.25, "longitude": 2.5, "altitude": 3.0, "timestamp": _ts(9, 7)}],
        "metadata": {"HKDeviceName": "Synthetic Device", "HKIndoorWorkout": 0},
        "source": "Synthetic Watch", "sourceName": "Synthetic Watch",
        "weather": {"location": {"lat": 1.0, "lon": 2.0}, "temperature": 5},
    }


def _workout_v1() -> dict:
    return {"name": "Strength", "start": _ts(8, 18), "end": _ts(8, 19), "duration": 2700,
            "activeEnergy": {"qty": 250, "units": "kcal"}, "avgHeartRate": {"qty": 110}, "maxHeartRate": {"qty": 150},
            "isIndoor": True}


def _events_payload() -> dict:
    return {
        "heartRateNotifications": [{"start": _ts(9, 3), "end": _ts(9, 4), "threshold": 120, "source": "Synthetic Watch"}],
        "ecg": [{"start": _ts(9, 5), "classification": "Sinus Rhythm", "averageHeartRate": 60}],
        "symptoms": [{"start": _ts(9, 6), "name": "Headache", "severity": "Mild"}, {"name": "no start"}],
        "stateOfMind": [{"start": _ts(9, 21), "valence": 0.4, "labels": ["calm"]}],
    }


def _cfg(tmp: Path, **kw) -> IngestConfig:
    base = dict(host="127.0.0.1", port=0, db_path=tmp / "h.sqlite", raw_dir=tmp / "raw", token=TOKEN)
    base.update(kw)
    return IngestConfig(**base)


class ConfigTests(unittest.TestCase):
    def test_requires_long_token(self) -> None:
        for env in ({}, {"HEALTH_TOKEN": "short", "HEALTH_DB": "a", "HEALTH_RAW": "b"}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                load_config(env)

    def test_defaults_from_ledger(self) -> None:
        cfg = load_config({"HEALTH_TOKEN": TOKEN, "HOMUNCULUS_LEDGER": "/x/ledger"})
        self.assertEqual((cfg.host, cfg.port), ("127.0.0.1", 8787))
        self.assertEqual(cfg.db_path, Path("/x/ledger/.private/health.sqlite"))
        self.assertEqual(cfg.raw_dir, Path("/x/ledger/.private/raw"))
        self.assertEqual(dict(cfg.source_map), {})
        self.assertNotIn(TOKEN, repr(cfg))

    def test_explicit_values(self) -> None:
        env = {"HEALTH_TOKEN": TOKEN, "HEALTH_HOST": "100.64.0.1", "HEALTH_PORT": "9000", "HEALTH_DB": "/d.db",
               "HEALTH_RAW": "/r", "HEALTH_SOURCE_MAP": '{"Fenix": "garmin"}'}
        cfg = load_config(env)
        self.assertEqual((cfg.host, cfg.port, cfg.db_path, cfg.raw_dir), ("100.64.0.1", 9000, Path("/d.db"), Path("/r")))
        self.assertEqual(dict(cfg.source_map), {"Fenix": "garmin"})

    def test_rejects_bad_values(self) -> None:
        base = {"HEALTH_TOKEN": TOKEN, "HOMUNCULUS_LEDGER": "/l"}
        bad = [{}, {"HEALTH_PORT": "x"}, {"HEALTH_PORT": "70000"}, {"HEALTH_SOURCE_MAP": "[1]"},
               {"HEALTH_SOURCE_MAP": "{"}, {"HEALTH_SOURCE_MAP": '{"a": "tablet"}'}]
        for extra in bad:
            env = {**base, **extra} if extra else {"HEALTH_TOKEN": TOKEN}
            with self.subTest(extra=extra), self.assertRaises(ConfigError):
                load_config(env)


class SourceAndAuthTests(unittest.TestCase):
    def test_normalize_source(self) -> None:
        cases = [(None, "other"), ("", "other"), ("Synthetic Apple Watch", "watch"), ("Synthetic iPhone", "phone"),
                 ("Garmin Synthetic", "garmin"), ("Synthetic Watch|Synthetic iPhone", "watch"), ("Scale", "other")]
        for name, want in cases:
            with self.subTest(name=name):
                self.assertEqual(normalize_source(name, {}), want)
        self.assertEqual(normalize_source("Fenix 9", {"fenix": "garmin"}), "garmin")
        self.assertEqual(normalize_source("My Watch Pro", {"watch": "phone", "watch pro": "garmin"}), "garmin")

    def test_authorized(self) -> None:
        self.assertTrue(authorized(AUTH, TOKEN))
        self.assertTrue(authorized({"x-token": TOKEN}, TOKEN))
        self.assertTrue(authorized({"authorization": f"bearer {TOKEN}"}, TOKEN))
        self.assertFalse(authorized({"Authorization": f"Basic {TOKEN}"}, TOKEN))
        self.assertFalse(authorized({"Authorization": "Bearer wrong"}, TOKEN))
        self.assertFalse(authorized({}, TOKEN))
        self.assertFalse(authorized({"X-Token": ""}, ""))


class StripTests(unittest.TestCase):
    def test_strip_workout_removes_gps_and_devices(self) -> None:
        w = _workout_v2()
        out = strip_workout(w)
        text = json.dumps(out)
        for needle in ("route", "latitude", "longitude", "Synthetic Device", "Synthetic Watch", "lat", "HKDeviceName"):
            self.assertNotIn(needle, text)
        self.assertEqual(out["location"], "Outdoor")
        self.assertEqual(out["weather"], {"temperature": 5})
        self.assertEqual(out["metadata"], {"HKIndoorWorkout": 0})
        self.assertIn("route", w)  # input untouched

    def test_strip_drops_non_tag_location_and_nan(self) -> None:
        out = strip_workout({"location": "Somewhere Street 1", "duration": float("nan"), "name": "x"})
        self.assertEqual(out, {"duration": None, "name": "x"})


class ParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(RNG_SEED)

    def test_metrics_wrapped_and_bare(self) -> None:
        payload = _metric_payload(self.rng)
        for shape in (payload, {"data": payload}):
            batch = parse_payload(shape, {})
            self.assertEqual(len(batch.samples), 3 * 3 + 2)
            self.assertEqual({s.source for s in batch.samples}, {"watch", "phone"})
            hr = [s for s in batch.samples if s.metric == "heart_rate" and s.field == "Min"]
            self.assertEqual([s.value for s in hr], [50.0, 51.0, 52.0])
            self.assertEqual(hr[0].ts, _ts(9, 0))
            self.assertEqual(hr[0].unit, "count/min")

    def test_sleep_night_and_segments(self) -> None:
        batch = parse_payload({"data": _sleep_payload()}, {})
        self.assertEqual(batch.samples, [])
        self.assertEqual(len(batch.sleep_nights), 1)
        night = batch.sleep_nights[0]
        self.assertEqual(night.wake_date, "2026-01-09")
        self.assertEqual(night.sleep_start, "2026-01-08 23:10:00 +0100")
        self.assertEqual((night.total_h, night.core_h, night.deep_h, night.rem_h), (7.0, 4.0, 1.0, 1.5))
        self.assertEqual(night.source, "watch")
        self.assertEqual([s.stage for s in batch.sleep_segments], ["core", "deep"])
        self.assertEqual(batch.skipped, {"sleep": 1})

    def test_workouts_v2_and_v1(self) -> None:
        batch = parse_payload({"workouts": [_workout_v2(), _workout_v1(), {"name": "no times"}]}, {})
        v2, v1 = batch.workouts
        self.assertEqual(v2.id, "SYNTH-1")
        self.assertAlmostEqual(v2.active_kcal, 1000 / 4.184)
        self.assertEqual((v2.hr_avg, v2.hr_max, v2.is_indoor, v2.duration_s), (140.0, 170.0, False, 3600.0))
        self.assertEqual(v2.hr_series, ((_ts(9, 7), 130.0),))
        self.assertEqual(v2.source, "watch")
        self.assertNotIn("latitude", v2.raw_json)
        self.assertNotIn("Synthetic", v2.raw_json)
        expected_id = ingest.hashlib.sha256(f"Strength|{_ts(8, 18)}".encode()).hexdigest()[:16]
        self.assertEqual(v1.id, expected_id)
        self.assertEqual((v1.active_kcal, v1.hr_avg, v1.hr_max, v1.is_indoor), (250.0, 110.0, 150.0, True))
        self.assertEqual(v1.source, "other")
        self.assertEqual(batch.skipped, {"workouts": 1})

    def test_events(self) -> None:
        batch = parse_payload({"data": _events_payload()}, {})
        self.assertEqual([e.kind for e in batch.events], ["hr_notification", "ecg", "symptom", "state_of_mind"])
        hrn = batch.events[0]
        self.assertEqual((hrn.start, hrn.end, hrn.source), (_ts(9, 3), _ts(9, 4), "watch"))
        self.assertNotIn("Synthetic", hrn.payload_json)
        self.assertEqual(json.loads(batch.events[2].payload_json)["name"], "Headache")
        self.assertEqual(batch.skipped, {"symptom": 1})

    def test_unknown_and_malformed_sections(self) -> None:
        payload = {"medications": [{}, {}], "metrics": [{"name": "x", "data": "bad"}, 7],
                   "workouts": "nope", "ecg": [{"start": _ts(1, 1)}]}
        batch = parse_payload(payload, {})
        self.assertEqual(batch.skipped, {"medications": 2, "metrics": 2, "workouts": 1})
        self.assertEqual(len(batch.events), 1)
        self.assertIsInstance(batch, ParsedBatch)

    def test_numeric_edge_values(self) -> None:
        point = {"date": _ts(2, 2), "qty": True, "a": float("inf"), "b": 3, "c": "4"}
        batch = parse_payload({"metrics": [{"name": "m", "data": [point, {"qty": 1}]}]}, {})
        self.assertEqual([(s.field, s.value) for s in batch.samples], [("b", 3.0)])
        self.assertEqual(batch.skipped, {"samples": 1})

    def test_rejects_non_object(self) -> None:
        for bad in ([], {"data": []}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_payload(bad, {})


class _TmpStoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)
        self.store = Store(self.cfg.db_path)
        self.rng = random.Random(RNG_SEED)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp)

    def _full_payload(self) -> dict:
        data = _metric_payload(self.rng)
        data["metrics"] += _sleep_payload()["metrics"]
        data["workouts"] = [_workout_v2(), _workout_v1()]
        data.update(_events_payload())
        return {"data": data}


class IngestTests(_TmpStoreCase):
    def test_counts_and_new_events(self) -> None:
        payload = self._full_payload()
        counts, new = ingest_payload(self.store, payload, self.cfg)
        self.assertEqual(counts, {"samples": 11, "sleep_segments": 2, "sleep_nights": 1, "workouts": 2, "events": 4})
        self.assertEqual(len(new), 4)
        counts, new = ingest_payload(self.store, payload, self.cfg)
        self.assertEqual(counts["events"], 0)
        self.assertEqual(new, [])
        self.assertEqual(len(self.store.samples("heart_rate", "")), 9)
        stored = self.store.workouts("")
        self.assertTrue(all("latitude" not in w.raw_json for w in stored))

    def test_duplicate_events_in_one_batch(self) -> None:
        ev = {"start": _ts(3, 3)}
        _, new = ingest_payload(self.store, {"ecg": [ev, dict(ev)]}, self.cfg)
        self.assertEqual(len(new), 1)


class RawTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.raw = self.tmp / "private" / "raw"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def test_save_raw_modes_and_unique_names(self) -> None:
        a = save_raw(self.raw, b'{"a": 1}', NOW)
        b = save_raw(self.raw, b'{"b": 2}', NOW)
        self.assertNotEqual(a, b)
        self.assertEqual(a.read_bytes(), b'{"a": 1}')
        self.assertEqual(stat.S_IMODE(os.stat(self.raw).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(a).st_mode), 0o600)
        self.assertEqual((self.raw / ".gitignore").read_text(), "*\n")
        self.assertTrue(a.name.startswith("20260110T120000"))

    def test_naive_now_is_utc(self) -> None:
        path = save_raw(self.raw, b"{}", NOW.replace(tzinfo=None))
        self.assertTrue(path.name.startswith("20260110T120000"))

    def test_prune_raw(self) -> None:
        old = save_raw(self.raw, b"{}", NOW - timedelta(days=31))
        older = save_raw(self.raw, b"{}", NOW - timedelta(days=31))
        fresh = save_raw(self.raw, b"{}", NOW - timedelta(days=29))
        (self.raw / "notes.txt").write_text("keep")
        self.assertEqual(prune_raw(self.raw, 30, NOW), 2)
        self.assertFalse(old.exists() or older.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue((self.raw / ".gitignore").exists() and (self.raw / "notes.txt").exists())
        self.assertEqual(prune_raw(self.tmp / "missing", 30, NOW), 0)


class AppTests(_TmpStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.site = self.tmp / "site"
        self.site.mkdir()
        (self.site / "index.html").write_text("<p>synthetic</p>", encoding="utf-8")
        (self.tmp / "secret.txt").write_text("outside", encoding="utf-8")
        self.seen: list = []
        self.forms: list = []
        self.app = ingest._App(self.cfg, self.store, self.seen.extend,
                               {"/checkin": self._route}, self.site, "owner@example.test")
        patcher = mock.patch.object(ingest, "_utcnow", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _route(self, form: dict) -> tuple[int, dict]:
        self.forms.append(form)
        return 201, {"stored": len(form)}

    def _post(self, path: str, body: bytes, headers: dict) -> tuple[int, dict]:
        def read(limit: int) -> bytes:
            if len(body) > limit:
                raise ingest._HttpError(413, {"error": "body too large"})
            return body
        status, _, out = self.app.post(path, headers, read)
        return status, json.loads(out)

    def _get(self, path: str, query: str = "", headers: dict | None = None) -> tuple[int, object]:
        status, _, out = self.app.get(path, ingest.parse_qs(query), AUTH if headers is None else headers)
        return status, json.loads(out)

    def _ingest(self) -> dict:
        status, out = self._post("/health", json.dumps(self._full_payload()).encode(), AUTH)
        self.assertEqual(status, 200)
        return out

    def test_post_health_stores_raw_and_alerts(self) -> None:
        out = self._ingest()
        self.assertEqual(out["stored"]["samples"], 11)
        self.assertEqual((out["new_events"], out["alerts"]), (4, "ok"))
        self.assertEqual(len(self.seen), 4)
        self.assertEqual(len([p for p in self.cfg.raw_dir.iterdir() if p.suffix == ".json"]), 1)
        self.assertEqual(self._ingest()["alerts"], "none")

    def test_failing_alert_hook_keeps_data(self) -> None:
        self.app.on_events = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertLogs("homunculus.ingest", "ERROR"):
            out = self._ingest()
        self.assertEqual(out["alerts"], "failed")
        self.assertEqual(len(self.store.events(None, "")), 4)

    def test_post_health_auth_and_validation(self) -> None:
        with self.assertRaises(ingest._HttpError) as ctx:
            self._post("/health", b"{}", {})
        self.assertEqual(ctx.exception.status, 401)
        for bad in (b"not json", b"[1]"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self._post("/health", bad, AUTH)
        self.assertFalse(self.cfg.raw_dir.exists())

    def test_post_route_owner_or_token(self) -> None:
        owner = {"Tailscale-User-Login": "owner@example.test", "Content-Type": "application/json"}
        self.assertEqual(self._post("/checkin", b'{"mood": 5}', owner), (201, {"stored": 1}))
        form = {"Tailscale-User-Login": "owner@example.test", "Content-Type": "application/x-www-form-urlencoded"}
        self._post("/checkin", b"mood=5&symptoms=a&symptoms=b&note=", form)
        self.assertEqual(self.forms[-1], {"mood": "5", "symptoms": ["a", "b"], "note": ""})
        self.assertEqual(self._post("/checkin", b"{}", AUTH)[0], 201)
        for headers in ({"Tailscale-User-Login": "intruder@example.test"}, {}):
            with self.subTest(headers=headers), self.assertRaises(ingest._HttpError) as ctx:
                self._post("/checkin", b"{}", headers)
            self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(ingest._HttpError) as ctx:
            self._post("/nope", b"{}", AUTH)
        self.assertEqual(ctx.exception.status, 404)

    def test_owner_header_ignored_without_owner_login(self) -> None:
        self.app.owner_login = None
        with self.assertRaises(ingest._HttpError):
            self._post("/checkin", b"{}", {"Tailscale-User-Login": ""})

    def test_get_requires_token(self) -> None:
        for headers in ({}, {"Tailscale-User-Login": "owner@example.test"}):
            with self.subTest(headers=headers), self.assertRaises(ingest._HttpError) as ctx:
                self._get("/metrics", headers=headers)
            self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(ingest._HttpError) as ctx:
            self._get("/unknown")
        self.assertEqual(ctx.exception.status, 404)

    def test_read_endpoints(self) -> None:
        self._ingest()
        _, metrics = self._get("/metrics")
        self.assertEqual([m["metric"] for m in metrics], ["heart_rate", "step_count"])
        _, latest = self._get("/latest")
        hr_min = [r for r in latest if r["metric"] == "heart_rate" and r["field"] == "Min"]
        self.assertEqual(hr_min[0]["value"], 52.0)
        _, samples = self._get("/samples", "metric=heart_rate&since=2026-01-09&limit=2")
        self.assertEqual([s["ts"] for s in samples], [_ts(9, 2), _ts(9, 2)])
        _, workouts = self._get("/workouts", "since=2026-01-09")
        self.assertEqual([w["id"] for w in workouts], ["SYNTH-1"])
        self.assertNotIn("route", workouts[0]["raw"])
        _, fresh = self._get("/freshness")
        self.assertIn("workouts", fresh["latest"])
        self.assertEqual(fresh["now"], NOW.isoformat())

    def test_samples_limit_capped_and_validated(self) -> None:
        rows = [ingest.Sample("m", None, f"2026-01-01T{i:02d}:00", "qty", float(i), "watch") for i in range(6)]
        self.store.upsert_samples(rows)
        self.app.cfg = _cfg(self.tmp, max_limit=4)
        _, out = self._get("/samples", "metric=m&limit=5000")
        self.assertEqual([s["value"] for s in out], [5.0, 4.0, 3.0, 2.0])
        for query in ("metric=m&limit=0", "metric=m&limit=x", "limit=3"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self._get("/samples", query)

    def test_summary(self) -> None:
        self._ingest()
        _, rows = self._get("/summary", "days=2")
        hr = [r for r in rows if r["metric"] == "heart_rate" and r["field"] == "Min"]
        self.assertEqual(hr, [{"metric": "heart_rate", "unit": "count/min", "field": "Min", "day": "2026-01-09",
                               "n": 3, "min": 50.0, "max": 52.0, "avg": 51.0, "sum": 153.0}])
        self.assertEqual(rows[0]["day"], "2026-01-09")
        _, short = self._get("/summary", "days=1")
        self.assertEqual({r["day"] for r in short}, {"2026-01-09"})
        for query in ("days=0", "days=400"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self._get("/summary", query)

    def test_site(self) -> None:
        owner = {"Tailscale-User-Login": "owner@example.test"}
        for path in ("/site", "/site/", "/site/index.html"):
            with self.subTest(path=path):
                status, ctype, body = self.app.get(path, {}, owner)
                self.assertEqual((status, body), (200, b"<p>synthetic</p>"))
                self.assertEqual(ctype, "text/html; charset=utf-8")
        for path in ("/site/../secret.txt", "/site/%2e%2e/secret.txt", "/site/missing.css"):
            with self.subTest(path=path), self.assertRaises(ingest._HttpError) as ctx:
                self.app.get(path, {}, AUTH)
            self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ingest._HttpError) as ctx:
            self.app.get("/site/index.html", {}, {})
        self.assertEqual(ctx.exception.status, 401)


class ServerTests(_TmpStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.events: list = []
        self.server = make_server(self.cfg, self.store, on_events=self.events.extend)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection(*self.server.server_address[:2], timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_roundtrip(self) -> None:
        body = json.dumps(self._full_payload()).encode()
        status, out = self._request("POST", "/health", body, {**AUTH, "Content-Type": "application/json"})
        self.assertEqual((status, out["new_events"]), (200, 4))
        self.assertEqual(len(self.events), 4)
        status, metrics = self._request("GET", "/metrics", headers={"X-Token": TOKEN})
        self.assertEqual((status, len(metrics)), (200, 2))
        self.assertEqual(self._request("GET", "/metrics")[0], 401)
        self.assertEqual(self._request("GET", "/samples?metric=heart_rate&limit=x", headers=AUTH)[0], 400)
        self.assertEqual(self._request("POST", "/health", b"", AUTH)[0], 400)
        self.assertEqual(self._request("POST", "/health", b"{bad", AUTH)[0], 400)

    def test_body_too_large(self) -> None:
        self.server.RequestHandlerClass.app.cfg = _cfg(self.tmp, max_body=8)
        status, out = self._request("POST", "/health", b'{"metrics": []}', AUTH)
        self.assertEqual((status, out), (413, {"error": "body too large"}))

    def test_refuses_short_token(self) -> None:
        with self.assertRaises(ConfigError):
            make_server(_cfg(self.tmp, token="short"), self.store)


if __name__ == "__main__":
    unittest.main()

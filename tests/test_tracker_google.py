"""Tests for homunculus.trackers.google. Hand-written synthetic responses; no network, no sleeping."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from homunculus.ingest import ConfigError
from homunculus.trackers import google, registry
from homunculus.trackers.base import AuthRequired, SchemaError, Token, TrackerConfig
from homunculus.trackers.google import GoogleHealthSource, camel
from tests.tracker_fakes import FakeClock, FakeTransport, MemoryTokenStore, json_response

TZ = "Europe/Berlin"
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=ZoneInfo(TZ))
ENV = {"HOMUNCULUS_GOOGLE_CLIENT_ID": "fake-client-id", "HOMUNCULUS_GOOGLE_CLIENT_SECRET": "fake-client-secret"}
BASE = "health.googleapis.com/v4/users/me/dataTypes/"
TOKEN_ROUTE = ("POST", "oauth2.googleapis.com/token")
TYPES = (("sleep", ":reconcile"), ("daily-heart-rate-variability", ""), ("daily-resting-heart-rate", ""),
         ("daily-vo2-max", ""), ("daily-oxygen-saturation", ""), ("daily-respiratory-rate", ""),
         ("daily-sleep-temperature-derivations", ""), ("steps", ":dailyRollUp"),
         ("active-energy-burned", ":dailyRollUp"), ("active-zone-minutes", ":dailyRollUp"), ("weight", ""),
         ("oxygen-saturation", ""), ("exercise", ":reconcile"), ("heart-rate", ""))


def ok(obj: object) -> object:
    return json_response(json.dumps(obj))


def page(*points: object, token: str | None = None) -> object:
    body: dict = {"dataPoints": list(points)}
    if token:
        body["nextPageToken"] = token
    return ok(body)


def route(data_type: str) -> tuple[str, str]:
    verb = dict(TYPES)[data_type]
    return ("GET", f"{BASE}{data_type}/dataPoints{verb}")


def routes(over: dict | None = None) -> dict:
    over = over or {}
    return {route(t): list(over.get(t, [page()])) for t, _ in TYPES}


def day(data_type: str, d: str, **fields: object) -> dict:
    y, m, dd = (int(p) for p in d.split("-"))
    return {camel(data_type): {"date": {"year": y, "month": m, "day": dd}, **fields}}


def at(data_type: str, utc: str, offset: str, manual: bool = False, **fields: object) -> dict:
    point = {camel(data_type): {"sampleTime": {"physicalTime": utc, "utcOffset": offset}, **fields}}
    if manual:
        point["dataSource"] = {"recordingMethod": "MANUAL"}
    return point


def interval(start: str, end: str, offset: str = "3600s") -> dict:
    return {"startTime": start, "startUtcOffset": offset, "endTime": end, "endUtcOffset": offset}


SLEEP = {"name": "users/me/dataTypes/sleep/dataPoints/s-1", "sleep": {
    "interval": interval("2026-11-19T22:30:00Z", "2026-11-20T05:30:00Z"),
    "metadata": {"processed": True, "mainSleep": True},
    "stages": [
        {"type": "LIGHT", "startTime": "2026-11-19T22:30:00Z", "endTime": "2026-11-19T23:30:00Z"},
        {"type": "DEEP", "startTime": "2026-11-19T23:30:00Z", "endTime": "2026-11-20T01:00:00Z"},
        {"type": "REM", "startTime": "2026-11-20T01:00:00Z", "endTime": "2026-11-20T02:00:00Z"},
        {"type": "AWAKE", "startTime": "2026-11-20T02:00:00Z", "endTime": "2026-11-20T02:12:00Z"},
        {"type": "LIGHT", "startTime": "2026-11-20T02:12:00Z", "endTime": "2026-11-20T05:30:00Z"},
    ]}}
EXERCISE = {"name": "users/me/dataTypes/exercise/dataPoints/ex-1", "exercise": {
    "interval": interval("2026-11-19T17:00:00Z", "2026-11-19T17:30:00Z"), "displayName": "Run",
    "caloriesKcal": 250, "route": [{"latitude": 1.0, "longitude": 2.0}]}}


class Rig:
    def __init__(self, over: dict | None = None, extra: dict | None = None, expires_in: float = 3600.0) -> None:
        self.clock = FakeClock(NOW.timestamp())
        self.store = MemoryTokenStore({"google": Token("fake-access-1", "fake-refresh-1",
                                                       self.clock.now + expires_in, "scope")})
        self.transport = FakeTransport({**routes(over), **(extra or {})})
        self._tmp = tempfile.TemporaryDirectory()
        self.source = GoogleHealthSource(env=ENV, options={}, tz=TZ, transport=self.transport,
                                         token_store=self.store, clock=self.clock, sleep=self.clock.sleep,
                                         lock_dir=Path(self._tmp.name))

    def poll(self, cursor: dict | None = None):
        return self.source.poll(cursor, now=NOW, tz=TZ)

    def close(self) -> None:
        self._tmp.cleanup()


def full_over() -> dict:
    return {
        "sleep": [page(SLEEP)],
        "daily-heart-rate-variability": [page(day("daily-heart-rate-variability", "2026-11-20",
                                                  averageHeartRateVariabilityMilliseconds=42.5, entropy=2.1))],
        "daily-resting-heart-rate": [page(day("daily-resting-heart-rate", "2026-11-19", beatsPerMinute="52"))],
        "daily-oxygen-saturation": [page(day("daily-oxygen-saturation", "2026-11-20", averagePercentage=96.0))],
        "steps": [page(day("steps", "2026-11-19", count="8000"))],
        "daily-respiratory-rate": [page(day("daily-respiratory-rate", "2026-11-20", breathsPerMinute=14.5),
                                        day("daily-respiratory-rate", "2026-11-19", breathsPerMinute=15.0))],
        "weight": [page(at("weight", "2026-11-19T06:00:00Z", "3600s", weightGrams="81500"))],
        "oxygen-saturation": [
            page(at("oxygen-saturation", "2026-11-19T05:00:00Z", "-14400s", percentage=0.95),
                 at("oxygen-saturation", "2026-11-19T05:01:00Z", "3600s", manual=True, percentage=99), token="p2"),
            page(at("oxygen-saturation", "2026-11-19T05:02:00Z", "3600s", percentage=96))],
        "exercise": [page(EXERCISE)],
        "heart-rate": [page(at("heart-rate", "2026-11-19T17:10:00Z", "3600s", beatsPerMinute="150"),
                            at("heart-rate", "2026-11-19T17:05:00Z", "3600s", beatsPerMinute="140"))],
    }


def by_metric(result, metric: str) -> list:
    return [s for s in result.batch.samples if s.metric == metric]


class ConversionTests(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(google.int64_str("52"), 52.0)
        self.assertEqual(google.int64_str(48.5), 48.5)
        with self.assertRaises(ValueError):
            google.int64_str(True)
        self.assertEqual(google.offset_from_seconds("-14400s"), "-04:00")
        self.assertEqual(google.offset_from_seconds("19800s"), "+05:30")
        self.assertEqual(google.grams_to_kg(81500), 81.5)
        self.assertEqual(google.spo2_percent(0.95), 95.0)
        with self.assertRaises(ValueError):
            google.spo2_percent(30)
        self.assertEqual([google.google_stage(s) for s in ("LIGHT", "DEEP", "REM", "AWAKE", "RESTLESS", "X")],
                         ["core", "deep", "rem", "awake", "awake", "unspecified"])
        self.assertEqual(camel("daily-heart-rate-variability"), "dailyHeartRateVariability")


class PollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig(full_over())
        self.result = self.rig.poll()

    def tearDown(self) -> None:
        self.rig.close()

    def test_window_and_cursor(self) -> None:
        self.assertEqual(self.result.window, ("2026-11-17T10:00:00+01:00", "2026-11-20T10:00:00+01:00"))
        self.assertEqual(self.result.new_cursor, {"watermark": "2026-11-20T10:00:00+01:00"})
        self.assertTrue(self.result.complete)
        self.assertEqual(self.result.requests_made, len(self.rig.transport.calls))

    def test_sleep_maps_stages_and_night(self) -> None:
        stages = [s.stage for s in self.result.batch.sleep_segments]
        self.assertEqual(stages, ["core", "deep", "rem", "awake", "core"])
        self.assertEqual(self.result.batch.sleep_segments[0].start, "2026-11-19T23:30:00+01:00")
        (night,) = self.result.batch.sleep_nights
        self.assertEqual(night.wake_date, "2026-11-20")
        self.assertAlmostEqual(night.total_h, 7.0)
        self.assertAlmostEqual(night.deep_h, 1.5)
        self.assertAlmostEqual(night.awake_h, 0.2)
        self.assertAlmostEqual(night.asleep_h, 6.8)

    def test_hrv_is_nightly_at_sleep_midpoint(self) -> None:
        (hrv,) = by_metric(self.result, "heart_rate_variability_rmssd")
        self.assertEqual((hrv.field, hrv.value, hrv.ts), ("nightly", 42.5, "2026-11-20T03:00:00+01:00"))

    def test_int64_strings_and_units(self) -> None:
        (rhr,) = by_metric(self.result, "resting_heart_rate")
        self.assertEqual((rhr.value, rhr.ts, rhr.unit), (52.0, "2026-11-19T12:00:00+01:00", "count/min"))
        (weight,) = by_metric(self.result, "weight_body_mass")
        self.assertEqual((weight.value, weight.unit, weight.ts), (81.5, "kg", "2026-11-19T07:00:00+01:00"))
        (steps,) = by_metric(self.result, "step_count")
        self.assertEqual(steps.value, 8000.0)

    def test_sleep_derived_daily_value_is_dated_at_sleep_end(self) -> None:
        resp = sorted((s.ts, s.value) for s in by_metric(self.result, "respiratory_rate"))
        self.assertEqual(resp, [("2026-11-19T12:00:00+01:00", 15.0), ("2026-11-20T06:30:00+01:00", 14.5)])

    def test_offset_applied_and_manual_spo2_skipped_over_two_pages(self) -> None:
        spo2 = by_metric(self.result, "blood_oxygen_saturation")
        self.assertEqual([(s.ts, s.value) for s in spo2],
                         [("2026-11-19T01:00:00-04:00", 95.0), ("2026-11-19T06:02:00+01:00", 96.0)])
        calls = [c for c in self.rig.transport.calls if "/oxygen-saturation/" in c.url]
        self.assertEqual(len(calls), 2)
        self.assertEqual(parse_qs(urlsplit(calls[1].url).query)["pageToken"], ["p2"])
        query = parse_qs(urlsplit(calls[0].url).query)
        self.assertEqual(query["dataSourceFamily"], ["users/me/dataSourceFamilies/google-wearables"])

    def test_workout_with_heart_rate_series(self) -> None:
        (w,) = self.result.batch.workouts
        self.assertEqual((w.id, w.name, w.duration_s, w.active_kcal), ("google:ex-1", "Run", 1800.0, 250.0))
        self.assertEqual(w.hr_series, (("2026-11-19T18:05:00+01:00", 140.0), ("2026-11-19T18:10:00+01:00", 150.0)))
        self.assertEqual((w.hr_avg, w.hr_max), (145.0, 150.0))
        self.assertNotIn("route", json.loads(w.raw_json))

    def test_vendor_scores_and_invariants(self) -> None:
        keys = {d.key: d.value for d in self.result.batch.daily_vendor_scores}
        self.assertEqual(keys, {"google.hrv_entropy": 2.1, "google.spo2_sleep_avg": 96.0})
        batch = self.result.batch
        rows = [*batch.samples, *batch.sleep_segments, *batch.sleep_nights, *batch.workouts,
                *batch.daily_vendor_scores]
        self.assertTrue(all(r.source == "google" for r in rows))
        self.assertTrue(all(d.key in GoogleHealthSource.vendor_keys for d in batch.daily_vendor_scores))
        for ts in [s.ts for s in batch.samples] + [s.start for s in batch.sleep_segments]:
            self.assertIsNotNone(datetime.fromisoformat(ts).tzinfo)

    def test_no_secret_in_requests_except_auth_header(self) -> None:
        for call in self.rig.transport.calls:
            self.assertNotIn("fake-access-1", call.url)
            self.assertEqual(call.headers["Authorization"], "Bearer fake-access-1")


class FailureTests(unittest.TestCase):
    def test_account_not_linked_raises_auth_required(self) -> None:
        body = json.dumps({"error": {"code": 400, "status": "FAILED_PRECONDITION",
                                     "details": [{"reason": "ACCOUNT_NOT_LINKED"}]}})
        rig = Rig({"sleep": [json_response(body, status=400)]})
        with self.assertRaises(AuthRequired) as ctx:
            rig.poll()
        self.assertEqual(str(ctx.exception), "account_not_linked")
        rig.close()

    def test_unprocessed_sleep_keeps_complete_false(self) -> None:
        unprocessed = json.loads(json.dumps(SLEEP))
        unprocessed["sleep"]["metadata"]["processed"] = False
        rig = Rig({"sleep": [page(unprocessed)]})
        result = rig.poll()
        self.assertFalse(result.complete)
        self.assertEqual(result.batch.sleep_nights, ())
        self.assertIn("google:sleep_unprocessed 2026-11-20", result.warnings)
        rig.close()

    def test_top_level_schema_break_raises(self) -> None:
        rig = Rig({"sleep": [ok({"dataPoints": {"not": "a list"}})]})
        with self.assertRaises(SchemaError):
            rig.poll({"watermark": "2026-11-19T10:00:00+01:00"})
        rig.close()

    def test_missing_secret_is_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            GoogleHealthSource(env={}, options={}, tz=TZ, transport=FakeTransport({}),
                               token_store=MemoryTokenStore())


class AuthTests(unittest.TestCase):
    def test_401_refreshes_once_and_keeps_refresh_token(self) -> None:
        over = {"sleep": [json_response("{}", status=401), page()]}
        rig = Rig(over, {TOKEN_ROUTE: [ok({"access_token": "fake-access-2", "expires_in": 3599})]})
        rig.poll()
        saved = rig.store.tokens["google"]
        self.assertEqual((saved.access_token, saved.refresh_token), ("fake-access-2", "fake-refresh-1"))
        calls = rig.transport.calls
        self.assertEqual(urlsplit(calls[1].url).path, "/token")
        self.assertIn(b"grant_type=refresh_token", calls[1].body)
        self.assertEqual(calls[2].headers["Authorization"], "Bearer fake-access-2")
        rig.close()

    def test_expired_token_refreshes_before_first_call(self) -> None:
        rig = Rig(None, {TOKEN_ROUTE: [ok({"access_token": "fake-access-3", "expires_in": 3599})]}, expires_in=30)
        rig.poll()
        self.assertEqual(urlsplit(rig.transport.calls[0].url).hostname, "oauth2.googleapis.com")
        self.assertEqual(rig.store.saves[0][1].access_token, "fake-access-3")
        rig.close()

    def test_second_401_requires_auth(self) -> None:
        over = {"sleep": [json_response("{}", status=401), json_response("{}", status=401)]}
        rig = Rig(over, {TOKEN_ROUTE: [ok({"access_token": "fake-access-2", "expires_in": 3599})]})
        with self.assertRaises(AuthRequired):
            rig.poll()
        rig.close()

    def test_authorize_uses_pkce_and_offline_access(self) -> None:
        rig = Rig(None, {TOKEN_ROUTE: [ok({"access_token": "fake-new", "refresh_token": "fake-new-r",
                                           "expires_in": 3599, "scope": "s"})]})
        shown: list[str] = []

        def waiter(cfg, state, on_ready):
            on_ready()
            return "fake-code"

        rig.source._waiter = waiter
        ui = type("UI", (), {"show_url": lambda self, u: shown.append(u), "ask": lambda self, p: "",
                             "info": lambda self, t: None})()
        status = rig.source.authorize(ui)
        self.assertTrue(status.ok)
        query = parse_qs(urlsplit(shown[0]).query)
        self.assertEqual(urlsplit(shown[0]).hostname, "accounts.google.com")
        self.assertEqual((query["access_type"], query["prompt"], query["code_challenge_method"]),
                         (["offline"], ["consent"], ["S256"]))
        self.assertIn("googlehealth.sleep.readonly", query["scope"][0])
        form = parse_qs(rig.transport.calls[0].body.decode())
        self.assertIn("code_verifier", form)
        self.assertEqual(rig.store.tokens["google"].refresh_token, "fake-new-r")
        rig.close()


class RegistryTests(unittest.TestCase):
    def test_registry_builds_google(self) -> None:
        cfg = TrackerConfig(("google",), {"google": {"data_source_family": "all-sources"}}, TZ)
        src = registry.build("google", env=ENV, cfg=cfg, transport=FakeTransport({}), token_store=MemoryTokenStore())
        self.assertIsInstance(src, GoogleHealthSource)
        self.assertEqual(src.options["data_source_family"], "all-sources")


if __name__ == "__main__":
    unittest.main()

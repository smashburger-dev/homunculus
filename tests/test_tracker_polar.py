"""Tests for homunculus.trackers.polar. Hand-written synthetic responses; no network, no sleeping."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from homunculus.trackers import polar
from homunculus.trackers.base import AuthRequired, SchemaError, Token
from homunculus.trackers.polar import PolarSource
from tests.tracker_fakes import FakeClock, FakeTransport, MemoryTokenStore, json_response

TZ = "Europe/Berlin"
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=ZoneInfo(TZ))
ENV = {"HOMUNCULUS_POLAR_CLIENT_ID": "fake-client-id", "HOMUNCULUS_POLAR_CLIENT_SECRET": "fake-client-secret"}
HOST = "www.polaraccesslink.com/v3"
SLEEP = ("GET", f"{HOST}/users/sleep")
RECHARGE = ("GET", f"{HOST}/users/nightly-recharge")
CARDIO = ("GET", f"{HOST}/users/cardio-load/")
SKIN = ("GET", f"{HOST}/users/biosensing/skintemperature")
SPO2 = ("GET", f"{HOST}/users/biosensing/spo2")
EXERCISES = ("GET", f"{HOST}/exercises")
TOKEN = ("POST", "polarremote.com/v2/oauth2/token")
USERS = ("POST", f"{HOST}/users")


def ok(obj: object) -> object:
    return json_response(json.dumps(obj))


NIGHT = {"date": "2026-11-20", "sleep_start_time": "2026-11-19T23:40:30+01:00",
         "sleep_end_time": "2026-11-20T06:45:00+01:00", "light_sleep": 14400, "deep_sleep": 5400,
         "rem_sleep": 3600, "total_interruption_duration": 1800,
         "hypnogram": {"23:40": 2, "23:55": 4, "00:30": 1, "01:00": 0, "01:05": 3, "06:00": 7}}
RECHARGE_ITEM = {"date": "2026-11-20", "heart_rate_avg": 55, "heart_rate_variability_avg": 42,
                 "breathing_rate_avg": 14.5, "nightly_recharge_status": 4, "ans_charge": 3.5}
EXERCISE_LIST = [{"id": "ex1", "start_time": "2026-11-19T18:00:00", "start_time_utc_offset": 60},
                 {"id": "ex-old", "start_time": "2026-11-01T18:00:00", "start_time_utc_offset": 60}]
EXERCISE_DETAIL = {"id": "ex1", "start_time": "2026-11-19T18:00:00", "start_time_utc_offset": 60,
                   "duration": "PT45M", "calories": 500, "sport": "RUNNING", "device": "Synthetic Device",
                   "heart_rate": {"average": 140, "maximum": 172},
                   "samples": [{"sample_type": "1", "recording_rate": 1, "data": "1.0,2.0"},
                               {"sample_type": "0", "recording_rate": 5, "data": "120,,125,130"}]}


def routes() -> dict:
    return {SLEEP: [ok({"nights": [NIGHT]})], RECHARGE: [ok({"recharges": [RECHARGE_ITEM]})],
            CARDIO: [ok([{"date": "2026-11-19", "cardio_load": 85.2}])],
            SKIN: [ok([{"sleep_date": "2026-11-20", "sleep_time_skin_temperature_celsius": 34.1,
                        "deviation_from_baseline_celsius": -0.3}])],
            SPO2: [json_response("{}", status=404)],
            EXERCISES: [ok(EXERCISE_LIST)], ("GET", f"{HOST}/exercises/ex1"): [ok(EXERCISE_DETAIL)]}


class Rig:
    def __init__(self, routes: dict, token: bool = True) -> None:
        self.clock = FakeClock(NOW.timestamp())
        tok = Token("fake-access-1", None, self.clock.now + 300 * 86400, "accesslink.read_all",
                    extra={"x_user_id": "1"})
        self.store = MemoryTokenStore({"polar": tok} if token else {})
        self.transport = FakeTransport(routes)
        self._tmp = tempfile.TemporaryDirectory()
        self.source = PolarSource(env=ENV, options={}, tz=TZ, transport=self.transport, token_store=self.store,
                                  clock=self.clock, sleep=self.clock.sleep, lock_dir=Path(self._tmp.name))

    def poll(self, cursor: dict | None = None, backfill_days: int = 0):
        return self.source.poll(cursor, now=NOW, tz=TZ, backfill_days=backfill_days)

    def close(self) -> None:
        self._tmp.cleanup()


class ConversionTests(unittest.TestCase):
    def test_durations_and_stages(self) -> None:
        self.assertEqual(polar.iso_duration_s("PT1H2M3.5S"), 3723.5)
        self.assertEqual(polar.iso_duration_s("PT45M"), 2700.0)
        self.assertEqual(polar.iso_duration_s(90), 90.0)
        with self.assertRaises(ValueError):
            polar.iso_duration_s("PT")
        self.assertEqual([polar.polar_stage(c) for c in (0, 1, 2, 3, 4, 5, 6)],
                         ["awake", "rem", "core", "core", "deep", "unspecified", None])

    def test_local_time_with_offset_minutes(self) -> None:
        self.assertEqual(polar.polar_time("2026-11-19T18:00:00", 60, TZ).isoformat(), "2026-11-19T18:00:00+01:00")
        self.assertEqual(polar.polar_time("2026-11-19T18:00:00", None, TZ).utcoffset().total_seconds(), 3600)

    def test_hypnogram_crossing_midnight_is_monotonic(self) -> None:
        spans = polar.polar_hypnogram("2026-11-19T23:40:30+01:00", "2026-11-20T06:45:00+01:00",
                                      {"00:30": 1, "23:40": 2, "23:55": 4, "01:00": 0})
        self.assertEqual(spans, [
            ("2026-11-19T23:40:00+01:00", "2026-11-19T23:55:00+01:00", 2),
            ("2026-11-19T23:55:00+01:00", "2026-11-20T00:30:00+01:00", 4),
            ("2026-11-20T00:30:00+01:00", "2026-11-20T01:00:00+01:00", 1),
            ("2026-11-20T01:00:00+01:00", "2026-11-20T06:45:00+01:00", 0)])


class PollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig(routes())
        self.result = self.rig.poll()

    def tearDown(self) -> None:
        self.rig.close()

    def samples(self, metric: str) -> list:
        return [(s.ts, s.field, s.value) for s in self.result.batch.samples if s.metric == metric]

    def test_sleep_night_and_segments(self) -> None:
        (night,) = self.result.batch.sleep_nights
        self.assertEqual((night.wake_date, night.asleep_h, night.core_h, night.deep_h, night.awake_h),
                         ("2026-11-20", 6.5, 4.0, 1.5, 0.5))
        segments = self.result.batch.sleep_segments
        self.assertEqual([s.stage for s in segments], ["core", "deep", "rem", "awake", "core"])
        starts = [s.start for s in segments]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(segments[2].start[:10], "2026-11-20")
        self.assertIn("polar:unknown_stage 2026-11-20", self.result.warnings)

    def test_nightly_recharge_hrv_at_sleep_midpoint(self) -> None:
        self.assertEqual(self.samples("heart_rate_variability_rmssd"), [("2026-11-20T03:12:45+01:00", "nightly", 42.0)])
        self.assertEqual(self.samples("respiratory_rate"), [("2026-11-20T06:45:00+01:00", "qty", 14.5)])
        self.assertEqual(self.samples("skin_temperature"), [("2026-11-20T06:45:00+01:00", "qty", 34.1)])

    def test_exercise_detail_with_hr_samples(self) -> None:
        (w,) = self.result.batch.workouts
        self.assertEqual((w.id, w.name, w.start, w.end, w.duration_s),
                         ("polar:ex1", "RUNNING", "2026-11-19T18:00:00+01:00", "2026-11-19T18:45:00+01:00", 2700.0))
        self.assertEqual(w.hr_series, (("2026-11-19T18:00:00+01:00", 120.0), ("2026-11-19T18:00:10+01:00", 125.0),
                                       ("2026-11-19T18:00:15+01:00", 130.0)))
        self.assertEqual((w.hr_avg, w.hr_max, w.active_kcal), (140.0, 172.0, None))
        raw = json.loads(w.raw_json)
        self.assertNotIn("samples", raw)
        self.assertNotIn("device", raw)
        detail_calls = [c for c in self.rig.transport.calls if "/exercises/" in c.url]
        self.assertEqual(len(detail_calls), 1)  # the old exercise outside the window is not fetched
        self.assertEqual(parse_qs(urlsplit(detail_calls[0].url).query)["samples"], ["true"])

    def test_vendor_scores_beta_404_and_invariants(self) -> None:
        keys = {(d.date, d.key): d.value for d in self.result.batch.daily_vendor_scores}
        self.assertEqual(keys, {("2026-11-20", "polar.sleep_hr_avg"): 55.0, ("2026-11-20", "polar.ans_charge"): 3.5,
                                ("2026-11-20", "polar.nightly_recharge"): 4.0,
                                ("2026-11-19", "polar.cardio_load"): 85.2,
                                ("2026-11-20", "polar.skin_temp_deviation"): -0.3})
        self.assertIn("polar:endpoint_missing /users/biosensing/spo2", self.result.warnings)
        batch = self.result.batch
        rows = [*batch.samples, *batch.sleep_segments, *batch.sleep_nights, *batch.workouts,
                *batch.daily_vendor_scores]
        self.assertTrue(all(r.source == "polar" for r in rows))
        self.assertTrue(all(d.key in PolarSource.vendor_keys for d in batch.daily_vendor_scores))
        self.assertTrue(self.result.complete)


class HistoryTests(unittest.TestCase):
    def test_thirty_day_gap_warns_history_lost(self) -> None:
        old = {**EXERCISE_DETAIL, "id": "ex-old", "start_time": "2026-11-01T18:00:00", "samples": []}
        rig = Rig({**routes(), ("GET", f"{HOST}/exercises/ex-old"): [ok(old)]})
        result = rig.poll({"watermark": "2026-10-21T10:00:00+02:00"})
        self.assertIn("polar:history_lost 2026-10-21..2026-10-23", result.warnings)
        rig.close()

    def test_backfill_is_capped_and_lists_are_read_once(self) -> None:
        old = {**EXERCISE_DETAIL, "id": "ex-old", "start_time": "2026-11-01T18:00:00", "samples": []}
        rig = Rig({**routes(), ("GET", f"{HOST}/exercises/ex-old"): [ok(old)]})
        result = rig.poll(None, backfill_days=60)
        self.assertEqual(result.window[0], "2026-10-23T10:00:00+02:00")
        self.assertEqual(len([c for c in rig.transport.calls if c.url.endswith("/users/sleep")]), 1)
        self.assertEqual(sorted(w.id for w in result.batch.workouts), ["polar:ex-old", "polar:ex1"])
        self.assertTrue(result.complete)
        rig.close()


class FailureTests(unittest.TestCase):
    def test_missing_consents_raise_auth_required(self) -> None:
        rig = Rig({**routes(), SLEEP: [json_response("{}", status=403)]})
        with self.assertRaises(AuthRequired) as ctx:
            rig.poll()
        self.assertEqual(str(ctx.exception), "polar_consents")
        rig.close()

    def test_401_requires_auth_without_refresh(self) -> None:
        rig = Rig({SLEEP: [json_response("{}", status=401)]})
        with self.assertRaises(AuthRequired):
            rig.poll()
        self.assertEqual(len(rig.transport.calls), 1)
        rig.close()

    def test_top_level_schema_break_raises(self) -> None:
        rig = Rig({**routes(), SLEEP: [ok({"nights": {"x": 1}})]})
        with self.assertRaises(SchemaError):
            rig.poll({"watermark": "2026-11-19T10:00:00+01:00"})
        rig.close()

    def test_empty_204_list(self) -> None:
        rig = Rig({**routes(), EXERCISES: [json_response(b"", status=204)]})
        self.assertEqual(rig.poll().batch.workouts, ())
        rig.close()


class AuthTests(unittest.TestCase):
    def test_authorize_basic_auth_then_registers_user(self) -> None:
        token = ok({"access_token": "fake-polar-access", "token_type": "bearer", "expires_in": 31535999,
                    "x_user_id": 12345})
        rig = Rig({TOKEN: [token], USERS: [json_response("{}", status=409)]}, token=False)
        rig.source._waiter = lambda cfg, state, on_ready: (on_ready(), "fake-code")[1]
        shown: list[str] = []
        ui = type("UI", (), {"show_url": lambda self, u: shown.append(u), "ask": lambda self, p: "",
                             "info": lambda self, t: None})()
        status = rig.source.authorize(ui)
        self.assertTrue(status.ok)
        self.assertEqual(status.expires_at, NOW.timestamp() + 31535999)
        self.assertIsNone(status.refresh_expires_at)
        self.assertEqual(urlsplit(shown[0]).hostname, "flow.polar.com")
        token_call, users_call = rig.transport.calls
        expected = base64.b64encode(b"fake-client-id:fake-client-secret").decode()
        self.assertEqual(token_call.headers["Authorization"], f"Basic {expected}")
        self.assertNotIn(b"client_secret", token_call.body)
        self.assertEqual(json.loads(users_call.body), {"member-id": "homunculus-owner"})
        self.assertEqual(users_call.headers["Authorization"], "Bearer fake-polar-access")
        self.assertEqual(rig.store.tokens["polar"].extra["x_user_id"], "12345")
        rig.close()

    def test_manual_mode_reads_pasted_redirect(self) -> None:
        token = ok({"access_token": "fake-polar-access", "expires_in": 31535999, "x_user_id": 1})
        rig = Rig({TOKEN: [token], USERS: [ok({})]}, token=False)
        shown: list[str] = []

        class UI:
            def show_url(self, url: str) -> None:
                shown.append(url)

            def ask(self, prompt: str) -> str:
                state = parse_qs(urlsplit(shown[0]).query)["state"][0]
                return f"http://127.0.0.1:8765/callback?code=fake-code&state={state}"

            def info(self, text: str) -> None:
                pass

        self.assertTrue(rig.source.authorize(UI(), manual=True).ok)
        self.assertIn(b"code=fake-code", rig.transport.calls[0].body)
        rig.close()

    def test_auth_status_and_revoke(self) -> None:
        rig = Rig({})
        self.assertTrue(rig.source.auth_status().ok)
        rig.source.revoke()
        self.assertFalse(rig.source.auth_status().ok)
        rig.close()


if __name__ == "__main__":
    unittest.main()

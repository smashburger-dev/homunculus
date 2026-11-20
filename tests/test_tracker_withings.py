"""Tests for homunculus.trackers.withings. Hand-written synthetic responses; no network, no sleeping."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from homunculus.trackers import withings
from homunculus.trackers.base import AuthRequired, SchemaError, Token, UpstreamError
from homunculus.trackers.withings import WithingsSource
from tests.tracker_fakes import FakeClock, FakeTransport, MemoryTokenStore, json_response

TZ = "Europe/Berlin"
ZONE = ZoneInfo(TZ)
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=ZONE)
ENV = {"HOMUNCULUS_WITHINGS_CLIENT_ID": "fake-client-id", "HOMUNCULUS_WITHINGS_CLIENT_SECRET": "fake-client-secret"}
MEAS = ("POST", "wbsapi.withings.net/measure")
SLEEP = ("POST", "wbsapi.withings.net/v2/sleep")
WORKOUTS = ("POST", "wbsapi.withings.net/v2/measure")
TOKEN = ("POST", "wbsapi.withings.net/v2/oauth2")


def e(text: str) -> int:
    """Epoch seconds of a Berlin wall-clock time."""
    return int(datetime.fromisoformat(text).replace(tzinfo=ZONE).timestamp())


def ok(body: dict, status: int = 0) -> object:
    return json_response(json.dumps({"status": status, "body": body}))


EMPTY_SERIES = ok({"series": [], "more": 0})
EMPTY_MEAS = ok({"measuregrps": [], "more": 0})

MEAS_PAGE_1 = ok({"timezone": TZ, "more": 1, "offset": 1, "measuregrps": [
    {"grpid": 1, "date": e("2026-11-19T07:00"), "category": 1, "deviceid": "dev-1", "measures": [
        {"value": 7125, "type": 1, "unit": -2}, {"value": 215, "type": 6, "unit": -1},
        {"value": 125, "type": 10, "unit": 0}, {"value": 80, "type": 9, "unit": 0},
        {"value": 5, "type": 999, "unit": 0}]},
    {"grpid": 0, "date": e("2026-11-10T07:00"), "category": 1, "measures": [{"value": 70, "type": 1, "unit": 0}]},
]})
MEAS_PAGE_2 = ok({"timezone": TZ, "more": 0, "measuregrps": [
    {"grpid": 2, "date": e("2026-11-18T07:00"), "category": 1, "measures": [{"value": 3650, "type": 73, "unit": -2}]},
]})
SUMMARY = ok({"more": 0, "series": [{
    "timezone": TZ, "startdate": e("2026-11-19T23:00"), "enddate": e("2026-11-20T06:30"),
    "data": {"total_timeinbed": 27000, "total_sleep_time": 25200, "lightsleepduration": 14400,
             "deepsleepduration": 5400, "remsleepduration": 5400, "wakeupduration": 1800, "hr_min": 48,
             "rr_average": 14, "sleep_score": 81}}]})
SLEEP_GET = ok({"series": [
    {"startdate": e("2026-11-19T23:00"), "enddate": e("2026-11-19T23:30"), "state": 1,
     "rmssd": {str(e("2026-11-19T23:10")): 45, str(e("2026-11-19T23:20")): 50}},
    {"startdate": e("2026-11-19T23:30"), "enddate": e("2026-11-20T01:00"), "state": 2},
    {"startdate": e("2026-11-20T01:00"), "enddate": e("2026-11-20T01:10"), "state": 9},
]})
WORKOUT = ok({"more": 0, "series": [{
    "id": 77, "category": 2, "timezone": TZ, "startdate": e("2026-11-19T18:00"), "enddate": e("2026-11-19T18:45"),
    "deviceid": "dev-2", "data": {"calories": 400, "hr_average": 140, "hr_max": 170}}]})


class Rig:
    def __init__(self, routes: dict, expires_in: float = 3600.0) -> None:
        self.clock = FakeClock(NOW.timestamp())
        token = Token("fake-access-1", "fake-refresh-1", self.clock.now + expires_in, "user.metrics",
                      refresh_expires_at=self.clock.now + 86400 * 300)
        self.store = MemoryTokenStore({"withings": token})
        self.transport = FakeTransport(routes)
        self._tmp = tempfile.TemporaryDirectory()
        self.source = WithingsSource(env=ENV, options={}, tz=TZ, transport=self.transport, token_store=self.store,
                                     clock=self.clock, sleep=self.clock.sleep, lock_dir=Path(self._tmp.name))

    def poll(self, cursor: dict | None = None, backfill_days: int = 0):
        return self.source.poll(cursor, now=NOW, tz=TZ, backfill_days=backfill_days)

    def form(self, i: int) -> dict:
        return {k: v[0] for k, v in parse_qs(self.transport.calls[i].body.decode()).items()}

    def close(self) -> None:
        self._tmp.cleanup()


def empty_routes(n: int = 1) -> dict:
    return {MEAS: [EMPTY_MEAS] * n, SLEEP: [EMPTY_SERIES] * (2 * n), WORKOUTS: [EMPTY_SERIES] * n}


class ConversionTests(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(withings.withings_value(7125, -2), 71.25)
        self.assertEqual(withings.withings_value(125, 0), 125.0)
        with self.assertRaises(ValueError):
            withings.withings_value(71.25, 0)
        self.assertEqual(withings.epoch_s_to_iso(e("2026-11-19T07:00"), "Europe/Berlin"), "2026-11-19T07:00:00+01:00")
        self.assertEqual(withings.epoch_s_to_iso(e("2026-11-19T07:00"), "Not/AZone", "UTC"),
                         "2026-11-19T06:00:00+00:00")
        self.assertEqual([withings.withings_stage(s) for s in (0, 1, 2, 3, 4, 5, 9, True)],
                         ["awake", "core", "deep", "rem", "unspecified", "unspecified", None, None])

    def test_token_parser_unwraps_and_sets_refresh_lifetime(self) -> None:
        token = withings.withings_token({"status": 0, "body": {"access_token": "fake-a", "refresh_token": "fake-r",
                                                               "expires_in": 10800, "userid": 123}}, 1000.0)
        self.assertEqual((token.expires_at, token.refresh_expires_at), (11800.0, 1000.0 + 365 * 86400))
        self.assertEqual(token.extra["userid"], "123")
        with self.assertRaises(AuthRequired):
            withings.withings_token({"status": 401, "body": {}}, 0.0)


class PollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig({MEAS: [MEAS_PAGE_1, MEAS_PAGE_2], SLEEP: [SUMMARY, SLEEP_GET], WORKOUTS: [WORKOUT]})
        self.result = self.rig.poll()

    def tearDown(self) -> None:
        self.rig.close()

    def samples(self, metric: str) -> list:
        return [(s.ts, s.field, s.value) for s in self.result.batch.samples if s.metric == metric]

    def test_measures_with_unit_exponent_and_pagination(self) -> None:
        self.assertEqual(self.samples("weight_body_mass"), [("2026-11-19T07:00:00+01:00", "qty", 71.25)])
        self.assertEqual(sorted(self.samples("blood_pressure")),
                         [("2026-11-19T07:00:00+01:00", "diastolic", 80.0),
                          ("2026-11-19T07:00:00+01:00", "systolic", 125.0)])
        self.assertEqual(self.samples("skin_temperature"), [("2026-11-18T07:00:00+01:00", "qty", 36.5)])
        self.assertEqual(self.rig.form(1)["offset"], "1")
        self.assertEqual(self.rig.form(0)["action"], "getmeas")

    def test_sleep_night_segments_and_rmssd_readings(self) -> None:
        (night,) = self.result.batch.sleep_nights
        self.assertEqual((night.wake_date, night.total_h, night.asleep_h, night.core_h, night.awake_h),
                         ("2026-11-20", 7.5, 7.0, 4.0, 0.5))
        self.assertEqual([s.stage for s in self.result.batch.sleep_segments], ["core", "deep"])
        self.assertEqual(self.samples("heart_rate_variability_rmssd"),
                         [("2026-11-19T23:10:00+01:00", "qty", 45.0), ("2026-11-19T23:20:00+01:00", "qty", 50.0)])
        self.assertEqual(self.samples("respiratory_rate"), [("2026-11-20T06:30:00+01:00", "qty", 14.0)])
        self.assertIn("withings:bad_record sleep_state", self.result.warnings)

    def test_workout_without_series_or_kcal(self) -> None:
        (w,) = self.result.batch.workouts
        self.assertEqual((w.id, w.duration_s, w.hr_avg, w.hr_max, w.hr_series, w.active_kcal),
                         ("withings:77", 2700.0, 140.0, 170.0, (), None))
        self.assertNotIn("deviceid", json.loads(w.raw_json))

    def test_vendor_scores_and_invariants(self) -> None:
        keys = {(d.date, d.key): d.value for d in self.result.batch.daily_vendor_scores}
        self.assertEqual(keys, {("2026-11-19", "withings.fat_pct"): 21.5, ("2026-11-20", "withings.sleep_score"): 81.0,
                                ("2026-11-20", "withings.sleep_hr_min"): 48.0})
        batch = self.result.batch
        rows = [*batch.samples, *batch.sleep_segments, *batch.sleep_nights, *batch.workouts,
                *batch.daily_vendor_scores]
        self.assertTrue(all(r.source == "withings" for r in rows))
        self.assertTrue(all(d.key in WithingsSource.vendor_keys for d in batch.daily_vendor_scores))
        self.assertTrue(all(datetime.fromisoformat(s.ts).tzinfo for s in batch.samples))

    def test_old_group_outside_window_is_dropped(self) -> None:
        self.assertNotIn(70.0, [v for _, _, v in self.samples("weight_body_mass")])


class CursorTests(unittest.TestCase):
    def test_regular_query_uses_lastupdate_and_advances_when_complete(self) -> None:
        rig = Rig(empty_routes())
        last = e("2026-11-15T00:00")
        result = rig.poll({"watermark": "2026-11-19T10:00:00+01:00", "lastupdate": last})
        self.assertEqual(rig.form(0)["lastupdate"], str(last - 3600))
        self.assertNotIn("startdate", rig.form(0))
        self.assertTrue(result.complete)
        self.assertEqual(result.new_cursor["lastupdate"], int(NOW.timestamp()))
        self.assertEqual(result.window[0], "2026-11-16T10:00:00+01:00")
        rig.close()

    def test_lastupdate_kept_when_run_is_cut_short(self) -> None:
        rig = Rig(empty_routes(3))
        rig.source.http.max_requests = 5  # chunk 1 takes 4 requests, chunk 2 runs out
        result = rig.poll({"watermark": "2026-11-19T10:00:00+01:00", "lastupdate": 1234}, backfill_days=40)
        self.assertFalse(result.complete)
        self.assertEqual(result.new_cursor["lastupdate"], 1234)
        self.assertEqual(result.new_cursor["watermark"], "2026-11-20T10:00:00+01:00")
        self.assertIn("backfill_before", result.new_cursor)
        self.assertIn("startdate", rig.form(4))  # backfill chunks read by date range
        rig.close()


class FailureTests(unittest.TestCase):
    def test_nonzero_status_raises_upstream_error(self) -> None:
        rig = Rig({MEAS: [ok({}, status=2555)]})
        with self.assertRaises(UpstreamError):
            rig.poll()
        rig.close()

    def test_auth_status_refreshes_once_then_requires_auth(self) -> None:
        refreshed = ok({"access_token": "fake-access-2", "refresh_token": "fake-refresh-2", "expires_in": 10800})
        rig = Rig({MEAS: [ok({}, status=401), ok({}, status=401)], TOKEN: [refreshed]})
        with self.assertRaises(AuthRequired):
            rig.poll()
        self.assertEqual(rig.transport.calls[2].headers["Authorization"], "Bearer fake-access-2")
        rig.close()

    def test_top_level_schema_break_raises(self) -> None:
        rig = Rig({MEAS: [json_response(json.dumps({"body": {}}))]})
        with self.assertRaises(SchemaError):
            rig.poll()
        rig.close()
        rig = Rig({MEAS: [ok({"measuregrps": {"x": 1}})]})
        with self.assertRaises(SchemaError):
            rig.poll()
        rig.close()

    def test_manual_mode_refused(self) -> None:
        rig = Rig({})
        with self.assertRaises(ValueError):
            rig.source.authorize(object(), manual=True)
        self.assertEqual(rig.transport.calls, [])
        rig.close()


class AuthTests(unittest.TestCase):
    def test_rotating_refresh_is_saved_before_the_next_api_call(self) -> None:
        refreshed = ok({"access_token": "fake-access-2", "refresh_token": "fake-refresh-2", "expires_in": 10800})
        routes = {**empty_routes(), TOKEN: [refreshed]}
        rig = Rig(routes, expires_in=30)
        seen: list[str | None] = []
        transport = rig.transport

        def spy(req):
            saved = rig.store.tokens["withings"]
            seen.append(saved.refresh_token)
            return transport(req)

        rig.source.http._transport = spy
        rig.poll()
        form = rig.form(0)
        self.assertEqual((form["action"], form["grant_type"], form["refresh_token"]),
                         ("requesttoken", "refresh_token", "fake-refresh-1"))
        self.assertEqual(form["client_secret"], "fake-client-secret")
        self.assertEqual(seen[0], "fake-refresh-1")
        self.assertEqual(seen[1], "fake-refresh-2")  # saved before the first data request
        self.assertEqual(rig.transport.calls[1].headers["Authorization"], "Bearer fake-access-2")
        saved = rig.store.tokens["withings"]
        self.assertEqual(saved.refresh_expires_at, NOW.timestamp() + 365 * 86400)
        rig.close()

    def test_authorize_exchanges_code_with_action(self) -> None:
        token = ok({"access_token": "fake-new", "refresh_token": "fake-new-r", "expires_in": 10800, "userid": 5})
        rig = Rig({TOKEN: [token]})
        shown: list[str] = []
        rig.source._waiter = lambda cfg, state, on_ready: (on_ready(), "fake-code")[1]
        ui = type("UI", (), {"show_url": lambda self, u: shown.append(u), "ask": lambda self, p: "",
                             "info": lambda self, t: None})()
        status = rig.source.authorize(ui)
        self.assertTrue(status.ok)
        self.assertIn("scope=user.metrics%2Cuser.activity", shown[0])
        self.assertNotIn("code_challenge", shown[0])
        form = rig.form(0)
        self.assertEqual((form["action"], form["grant_type"], form["code"]),
                         ("requesttoken", "authorization_code", "fake-code"))
        self.assertEqual(rig.store.tokens["withings"].extra["userid"], "5")
        rig.close()


if __name__ == "__main__":
    unittest.main()

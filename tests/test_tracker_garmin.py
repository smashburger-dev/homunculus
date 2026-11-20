"""Tests for homunculus.trackers.garmin. leak-guard: synthetic

Every response is hand-written from the spec; no network, no sleeping, no real key.
"""

from __future__ import annotations

import base64
import io
import json
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from homunculus.ingest import ConfigError
from homunculus.trackers import garmin, registry
from homunculus.trackers.base import AuthRequired, RateLimited, SchemaError, TrackerConfig, parse_iso
from tests.tracker_fakes import FakeClock, FakeTransport, MemoryTokenStore, json_response

UTC = timezone.utc
TZ = "Europe/Berlin"
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)  # 11:00 local
HOST = garmin.HOST
FAKE_KEY = "fake-garmin-key-0001"
DAYS = ("2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20")


def fake_jwt(exp: float, n: int = 1) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part({'exp': exp, 'n': n})}.sig{n}"


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def fit_ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()) - garmin.FIT_EPOCH


def fit_file(normal: list[tuple[int, int]], compressed: list[tuple[int, int]]) -> bytes:
    """Local type 0: record with timestamp + heart_rate; local type 1: heart_rate only (compressed ts)."""
    def_0 = bytes([0x40, 0, 0]) + (20).to_bytes(2, "little") + bytes([2, 253, 4, 0x86, 3, 1, 0x02])
    def_1 = bytes([0x41, 0, 0]) + (20).to_bytes(2, "little") + bytes([1, 3, 1, 0x02])
    body = def_0 + def_1
    body += b"".join(bytes([0x00]) + ts.to_bytes(4, "little") + bytes([hr]) for ts, hr in normal)
    body += b"".join(bytes([0x80 | (1 << 5) | off]) + bytes([hr]) for off, hr in compressed)
    header = bytes([12, 0x10]) + (2100).to_bytes(2, "little") + len(body).to_bytes(4, "little") + b".FIT"
    return header + body + b"\x00\x00"


T1, T2 = fit_ts("2026-11-19T06:31:00"), fit_ts("2026-11-19T06:32:00")
FIT = fit_file([(T1, 130), (T2, 142)], [((T2 + 10) & 0x1F, 151)])


def zipped(data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("1001_ACTIVITY.fit", data)
    return buf.getvalue()


SLEEP = [{
    "calendarDate": "2026-11-19",
    "sleepStartTimestampGMT": ms("2026-11-18T22:00:00"), "sleepStartTimestampLocal": ms("2026-11-18T23:00:00"),
    "sleepEndTimestampGMT": ms("2026-11-19T05:30:00"), "sleepEndTimestampLocal": ms("2026-11-19T06:30:00"),
    "sleepTimeSeconds": 25200, "deepSleepSeconds": 5400, "lightSleepSeconds": 14400, "remSleepSeconds": 5400,
    "awakeSleepSeconds": 1800, "averageRespirationValue": 14.5, "sleepScores": {"overall": {"value": 81}},
}]
HRV_19 = {
    "hrvSummary": {"calendarDate": "2026-11-19", "lastNightAvg": 48, "weeklyAvg": 50, "status": "BALANCED"},
    "hrvReadings": [
        {"hrvValue": 45, "readingTimeGMT": "2026-11-19T01:00:00.0", "readingTimeLocal": "2026-11-19T02:00:00.0"},
        {"hrvValue": 51, "readingTimeGMT": "2026-11-19T01:05:00.0", "readingTimeLocal": "2026-11-19T02:05:00.0"},
    ],
}
DAILY = [
    {"calendarDate": "2026-11-18", "heartRate": {"restingValue": 52}, "calories": {"burnedActive": 600},
     "bodyBattery": {"maxValue": 90, "minValue": 20}},
    {"calendarDate": "2026-11-19", "calories": {"burnedActive": 450}},
]
ACTIVITY = {
    "activityId": 1001, "activityName": "Morgenlauf Testort", "startTimeGMT": "2026-11-19 06:30:00",
    "startTimeLocal": "2026-11-19 07:30:00", "duration": 1800.0, "calories": 400, "bmrCalories": 50,
    "averageHR": 140, "maxHR": 165, "activityType": {"typeKey": "running"}, "startLatitude": 52.1234,
    "startLongitude": 13.4321, "ownerFullName": "Test Owner", "ownerDisplayName": "tester",
    "locationName": "Testort",
}


def ok(obj: object) -> object:
    return json_response(json.dumps(obj))


def not_found() -> object:
    return json_response("", 404)


def route(path: str) -> tuple[str, str]:
    return ("GET", HOST + path)


def full_routes(clock: FakeClock) -> dict:
    routes = {
        ("POST", HOST + garmin.TOKEN_PATH): [json_response(json.dumps({"accessToken": fake_jwt(clock.now + 86400)}))],
        route(garmin.SLEEP_PATH): [ok(SLEEP)],
        route(garmin.DAILY_PATH): [ok(DAILY)],
        route(garmin.HEART_PATH): [ok({"calendarDate": "2026-11-19", "minHeartRate": 47})],
        route(garmin.WEIGHT_PATH.format(start=DAYS[0], end=DAYS[-1])): [ok([{"calendarDate": "2026-11-19",
                                                                             "weight": 81500}])],
        route(garmin.VO2_PATH.format(start=DAYS[0], end=DAYS[-1])): [ok([{"generic": {
            "calendarDate": "2026-11-18", "vo2MaxPreciseValue": 47.3}}])],
        route(garmin.ACTIVITIES_PATH): [ok([ACTIVITY])],
        route(garmin.FIT_PATH.format(id=1001)): [json_response(zipped(FIT))],
    }
    for day in DAYS:
        routes[route(garmin.HRV_PATH.format(day=day))] = [ok(HRV_19) if day == "2026-11-19" else not_found()]
    return routes


class Dispatch:
    """Answers every request: the token path mints a JWT valid 24 h from the clock, the rest is 404."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock, self.calls = clock, []

    def __call__(self, req):
        self.calls.append(req)
        if urlsplit(req.url).path == garmin.TOKEN_PATH:
            return json_response(fake_jwt(self.clock.now + 86400, len(self.calls)))
        return not_found()

    def mints(self) -> int:
        return sum(urlsplit(r.url).path == garmin.TOKEN_PATH for r in self.calls)


def source(transport, clock: FakeClock, store: MemoryTokenStore | None = None, **options) -> garmin.GarminSource:
    return garmin.GarminSource(env={garmin.KEY_ENV: FAKE_KEY}, options=options, tz=TZ, transport=transport,
                               token_store=store or MemoryTokenStore(), clock=clock, sleep=clock.sleep)


def query(req) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(req.url).query).items()}


class UnitTests(unittest.TestCase):
    def test_weight_grams_and_kg(self) -> None:
        self.assertEqual(garmin.garmin_weight_kg(81500), 81.5)
        self.assertEqual(garmin.garmin_weight_kg(81.5), 81.5)

    def test_offset_rounds_to_quarter_hours(self) -> None:
        gmt = datetime(2026, 11, 19, 6, 30)
        self.assertEqual(garmin.garmin_offset(gmt + timedelta(hours=1, seconds=20), gmt), timedelta(hours=1))
        self.assertEqual(garmin.garmin_offset(gmt - timedelta(hours=5, minutes=30), gmt),
                         timedelta(hours=-5, minutes=-30))
        with self.assertRaises(ValueError):
            garmin.garmin_offset(gmt + timedelta(hours=20), gmt)

    def test_epoch_ms_and_seconds(self) -> None:
        self.assertEqual(garmin.epoch_ms_to_iso(ms("2026-11-18T22:00:00"), timedelta(hours=1)),
                         "2026-11-18T23:00:00+01:00")
        self.assertEqual(garmin.s_to_h(5400), 1.5)

    def test_jwt_parsing(self) -> None:
        jwt = fake_jwt(1_900_000_000)
        self.assertEqual(garmin.jwt_exp(jwt), 1_900_000_000)
        for body in (jwt.encode(), json.dumps(jwt).encode(), json.dumps({"accessToken": jwt}).encode()):
            self.assertEqual(garmin.jwt_from_body(body), jwt)
        with self.assertRaises(SchemaError):
            garmin.jwt_from_body(b'{"other": 1}')
        with self.assertRaises(SchemaError):
            garmin.jwt_exp("a.b.c")

    def test_fit_reader_normal_and_compressed_timestamps(self) -> None:
        points = garmin.fit_hr_points(garmin.fit_bytes(zipped(FIT)))
        self.assertEqual([hr for _, hr in points], [130, 142, 151])
        self.assertEqual(points[2][0] - points[1][0], 10)
        self.assertEqual(points[0][0], int(datetime(2026, 11, 19, 6, 31, tzinfo=UTC).timestamp()))

    def test_fit_reader_rejects_garbage(self) -> None:
        with self.assertRaises(ValueError):
            garmin.fit_hr_points(b"not a fit file at all")
        with self.assertRaises(ValueError):
            garmin.fit_hr_points(FIT[:20])


class PollMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(NOW.timestamp())
        self.transport = FakeTransport(full_routes(self.clock))
        self.result = source(self.transport, self.clock).poll(None, now=NOW, tz=TZ)
        self.batch = self.result.batch

    def samples(self, metric: str) -> list:
        return [s for s in self.batch.samples if s.metric == metric]

    def vendor(self) -> dict:
        return {(d.date, d.key): d.value for d in self.batch.daily_vendor_scores}

    def test_window_and_cursor(self) -> None:
        self.assertEqual(self.result.window, ("2026-11-17T11:00:00+01:00", "2026-11-20T11:00:00+01:00"))
        self.assertEqual(self.result.new_cursor, {"watermark": "2026-11-20T11:00:00+01:00"})
        self.assertTrue(self.result.complete)
        self.assertEqual(self.result.requests_made, len(self.transport.calls))

    def test_activity_becomes_workout_with_fit_series(self) -> None:
        (w,) = self.batch.workouts
        self.assertEqual((w.id, w.name, w.start, w.end), ("garmin:1001", "running", "2026-11-19T07:30:00+01:00",
                                                          "2026-11-19T08:00:00+01:00"))
        self.assertEqual((w.duration_s, w.active_kcal, w.hr_avg, w.hr_max, w.is_indoor),
                         (1800.0, 350.0, 140.0, 165.0, False))
        self.assertEqual(len(w.hr_series), 3)
        self.assertEqual(w.hr_series[0], ("2026-11-19T07:31:00+01:00", 130.0))
        self.assertEqual(w.hr_series[2], ("2026-11-19T07:32:10+01:00", 151.0))

    def test_raw_json_has_no_owner_or_location(self) -> None:
        raw = self.batch.workouts[0].raw_json
        for needle in ("Test Owner", "tester", "owner", "52.1234", "13.4321", "Latitude", "Testort"):
            self.assertNotIn(needle, raw)
        self.assertIn("averageHR", raw)

    def test_sleep_night_and_resp_rate(self) -> None:
        (night,) = self.batch.sleep_nights
        self.assertEqual((night.wake_date, night.sleep_start, night.sleep_end),
                         ("2026-11-19", "2026-11-18T23:00:00+01:00", "2026-11-19T06:30:00+01:00"))
        self.assertEqual((night.asleep_h, night.total_h, night.core_h, night.deep_h, night.rem_h, night.awake_h),
                         (7.0, 7.5, 4.0, 1.5, 1.5, 0.5))
        (resp,) = self.samples("respiratory_rate")
        self.assertEqual((resp.ts, resp.value, resp.unit), ("2026-11-19T06:30:00+01:00", 14.5, "count/min"))

    def test_hrv_readings_are_qty_with_local_offset(self) -> None:
        hrv = self.samples("heart_rate_variability_rmssd")
        self.assertEqual([(s.ts, s.value, s.field, s.unit) for s in hrv],
                         [("2026-11-19T02:00:00+01:00", 45.0, "qty", "ms"),
                          ("2026-11-19T02:05:00+01:00", 51.0, "qty", "ms")])

    def test_rhr_fallback_warns(self) -> None:
        rhr = {s.ts: s.value for s in self.samples("resting_heart_rate")}
        self.assertEqual(rhr, {"2026-11-18T12:00:00+01:00": 52.0, "2026-11-19T12:00:00+01:00": 47.0})
        self.assertIn("garmin:rhr_min_fallback 2026-11-19", self.result.warnings)
        heart_calls = [query(r) for r in self.transport.calls if urlsplit(r.url).path == garmin.HEART_PATH]
        self.assertEqual(heart_calls, [{"date": "2026-11-19"}])

    def test_weight_vo2_and_active_energy(self) -> None:
        self.assertEqual([(s.value, s.unit) for s in self.samples("weight_body_mass")], [(81.5, "kg")])
        self.assertEqual([(s.ts, s.value) for s in self.samples("vo2_max")], [("2026-11-18T12:00:00+01:00", 47.3)])
        self.assertEqual(sorted(s.value for s in self.samples("active_energy")), [450.0, 600.0])

    def test_vendor_scores(self) -> None:
        vendor = self.vendor()
        self.assertEqual(vendor[("2026-11-19", "garmin.hrv_status_code")], 4.0)
        self.assertEqual(vendor[("2026-11-19", "garmin.hrv_last_night")], 48.0)
        self.assertEqual(vendor[("2026-11-19", "garmin.sleep_score")], 81.0)
        self.assertEqual(vendor[("2026-11-18", "garmin.body_battery_max")], 90.0)

    def test_every_row_is_garmin_with_offset(self) -> None:
        b = self.batch
        rows = (*b.samples, *b.sleep_nights, *b.workouts, *b.daily_vendor_scores)
        self.assertTrue(rows)
        self.assertTrue(all(r.source == "garmin" for r in rows))
        self.assertTrue(all(d.key in garmin.VENDOR_KEYS for d in b.daily_vendor_scores))
        stamps = [s.ts for s in b.samples] + [w.start for w in b.workouts] + [w.end for w in b.workouts]
        stamps += [n.sleep_start for n in b.sleep_nights] + [ts for w in b.workouts for ts, _ in w.hr_series]
        self.assertTrue(all(parse_iso(t).utcoffset() is not None for t in stamps))

    def test_api_calls_carry_the_jwt_not_the_key(self) -> None:
        token_call, *api_calls = self.transport.calls
        self.assertEqual(token_call.headers["Authorization"], f"ApiKey {FAKE_KEY}")
        self.assertTrue(all(c.headers["Authorization"].startswith("Bearer ") for c in api_calls))
        self.assertTrue(all(FAKE_KEY not in c.url for c in self.transport.calls))


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(NOW.timestamp())

    def test_jwt_minted_once_within_23h_and_again_after(self) -> None:
        transport, store = Dispatch(self.clock), MemoryTokenStore()
        src = source(transport, self.clock, store)
        src.poll(None, now=NOW, tz=TZ)
        self.clock.advance(22 * 3600)
        src.poll(None, now=NOW + timedelta(hours=22), tz=TZ)
        self.assertEqual(transport.mints(), 1)
        self.clock.advance(1.5 * 3600)
        src.poll(None, now=NOW + timedelta(hours=23.5), tz=TZ)
        self.assertEqual(transport.mints(), 2)
        self.assertEqual(store.tokens["garmin"].expires_at, self.clock.now + 86400)
        self.assertIsNone(store.tokens["garmin"].refresh_token)

    def test_401_mints_a_new_jwt_once(self) -> None:
        routes = full_routes(self.clock)
        token_route = ("POST", HOST + garmin.TOKEN_PATH)
        second = fake_jwt(self.clock.now + 86400, 2)
        routes[token_route].append(json_response(second))
        routes[route(garmin.SLEEP_PATH)] = [json_response("", 401), ok(SLEEP)]
        transport = FakeTransport(routes)
        result = source(transport, self.clock).poll(None, now=NOW, tz=TZ)
        self.assertEqual(len(result.batch.sleep_nights), 1)
        sleep_calls = [c for c in transport.calls if urlsplit(c.url).path == garmin.SLEEP_PATH]
        self.assertEqual(sleep_calls[1].headers["Authorization"], f"Bearer {second}")

    def test_401_after_new_jwt_raises(self) -> None:
        routes = full_routes(self.clock)
        routes[("POST", HOST + garmin.TOKEN_PATH)].append(json_response(fake_jwt(self.clock.now + 86400, 2)))
        routes[route(garmin.SLEEP_PATH)] = [json_response("", 401), json_response("", 401)]
        with self.assertRaises(AuthRequired):
            source(FakeTransport(routes), self.clock).poll(None, now=NOW, tz=TZ)

    def test_rejected_key_raises_auth_required(self) -> None:
        transport = FakeTransport({("POST", HOST + garmin.TOKEN_PATH): [json_response("", 401)]})
        with self.assertRaises(AuthRequired) as ctx:
            source(transport, self.clock).poll(None, now=NOW, tz=TZ)
        self.assertIn("garmin_key_rejected", str(ctx.exception))

    def test_authorize_and_status(self) -> None:
        store = MemoryTokenStore()
        transport = FakeTransport({("POST", HOST + garmin.TOKEN_PATH): [json_response(fake_jwt(self.clock.now + 7200))]})
        ui = mock.Mock()
        status = source(transport, self.clock, store, key_expires_on="2027-11-19").authorize(ui)
        self.assertTrue(status.ok)
        self.assertEqual(status.expires_at, self.clock.now + 7200)
        self.assertEqual(status.refresh_expires_at, datetime(2027, 11, 19, tzinfo=UTC).timestamp())
        ui.info.assert_called_once()
        self.assertNotIn(FAKE_KEY, repr(ui.info.call_args))
        expired = source(transport, self.clock, store, key_expires_on="2026-11-01").auth_status()
        self.assertFalse(expired.ok)

    def test_revoke_drops_the_cached_jwt(self) -> None:
        store = MemoryTokenStore()
        transport = FakeTransport({("POST", HOST + garmin.TOKEN_PATH): [json_response(fake_jwt(self.clock.now + 7200))]})
        src = source(transport, self.clock, store)
        src.authorize(mock.Mock())
        src.revoke()
        self.assertIsNone(store.load("garmin"))

    def test_missing_key_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            garmin.GarminSource(env={}, token_store=MemoryTokenStore())


class PagingAndBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(NOW.timestamp())

    def test_activity_list_pages_by_offset(self) -> None:
        acts = [dict(ACTIVITY, activityId=2000 + i) for i in range(3)]
        routes = full_routes(self.clock)
        routes[route(garmin.ACTIVITIES_PATH)] = [ok(acts[:2]), ok(acts[2:])]
        for i in range(3):
            routes[route(garmin.FIT_PATH.format(id=2000 + i))] = [not_found()]
        transport = FakeTransport(routes)
        with mock.patch.object(garmin, "ACTIVITY_PAGE", 2):
            result = source(transport, self.clock).poll(None, now=NOW, tz=TZ)
        pages = [query(c) for c in transport.calls if urlsplit(c.url).path == garmin.ACTIVITIES_PATH]
        self.assertEqual([(p["start"], p["limit"]) for p in pages], [("0", "2"), ("2", "2")])
        self.assertEqual(pages[0]["startDate"], "2026-11-17")
        self.assertEqual(pages[0]["endDate"], "2026-11-20")
        self.assertEqual(sorted(w.id for w in result.batch.workouts), ["garmin:2000", "garmin:2001", "garmin:2002"])
        self.assertTrue(all(w.hr_series == () for w in result.batch.workouts))

    def test_budget_cut_keeps_finished_chunk_and_backfill_cursor(self) -> None:
        transport = Dispatch(self.clock)
        result = source(transport, self.clock, max_requests=12).poll(None, now=NOW, tz=TZ, backfill_days=60)
        self.assertFalse(result.complete)
        self.assertEqual(result.window, ("2026-11-17T11:00:00+01:00", "2026-11-20T11:00:00+01:00"))
        self.assertEqual(result.new_cursor["watermark"], "2026-11-20T11:00:00+01:00")
        self.assertEqual(result.new_cursor["backfill_before"], "2026-11-17T11:00:00+01:00")
        self.assertEqual(result.new_cursor["backfill_target"], "2026-09-21T11:00:00+02:00")
        self.assertEqual(result.requests_made, 12)

    def test_budget_spent_in_first_chunk_raises(self) -> None:
        with self.assertRaises(RateLimited):
            source(Dispatch(self.clock), self.clock, max_requests=3).poll(None, now=NOW, tz=TZ)

    def test_top_level_schema_break_raises(self) -> None:
        routes = full_routes(self.clock)
        routes[route(garmin.SLEEP_PATH)] = [ok({"unexpected": 1})]
        with self.assertRaises(SchemaError):
            source(FakeTransport(routes), self.clock).poll({"watermark": "2026-11-19T11:00:00+01:00"},
                                                          now=NOW, tz=TZ)

    def test_bad_record_is_skipped_with_warning(self) -> None:
        routes = full_routes(self.clock)
        routes[route(garmin.SLEEP_PATH)] = [ok([{"calendarDate": "2026-11-19"}, *SLEEP])]
        result = source(FakeTransport(routes), self.clock).poll(None, now=NOW, tz=TZ)
        self.assertEqual(len(result.batch.sleep_nights), 1)
        self.assertIn("garmin:sleep_skipped", result.warnings)


class RegistryTests(unittest.TestCase):
    def test_registry_builds_garmin(self) -> None:
        cfg = TrackerConfig(enabled=("garmin",), per_source={"garmin": {"poll_minutes": 60}}, tz=TZ)
        src = registry.build("garmin", env={garmin.KEY_ENV: FAKE_KEY}, cfg=cfg, transport=FakeTransport({}),
                             token_store=MemoryTokenStore())
        self.assertIsInstance(src, garmin.GarminSource)
        self.assertEqual((src.name, src.auth_kind, src.backfill_chunk_days), ("garmin", "api_key", 28))


if __name__ == "__main__":
    unittest.main()

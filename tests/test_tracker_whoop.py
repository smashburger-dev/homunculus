"""Tests for homunculus.trackers.whoop. leak-guard: synthetic

Every response is hand-written from the v2 docs; no network, no sleeping, no real token.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from homunculus.ingest import ConfigError
from homunculus.trackers import registry, whoop
from homunculus.trackers.base import AuthRequired, SchemaError, Token, TrackerConfig, parse_iso
from tests.tracker_fakes import FakeClock, FakeTransport, MemoryTokenStore, json_response

UTC = timezone.utc
TZ = "Europe/Berlin"
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)  # 11:00 local
HOST = whoop.HOST
API = "/developer"
ENV = {whoop.CLIENT_ID_ENV: "fake-client-id", whoop.CLIENT_SECRET_ENV: "fake-client-secret"}
SLEEP_ID = "a1b2c3d4-0000-4000-8000-000000000001"
NAP_ID = "a1b2c3d4-0000-4000-8000-000000000002"

SLEEP = {
    "id": SLEEP_ID, "cycle_id": 93845, "user_id": 10129, "start": "2026-11-18T22:00:00.000Z",
    "end": "2026-11-19T06:00:00.000Z", "timezone_offset": "+01:00", "nap": False, "score_state": "SCORED",
    "score": {
        "stage_summary": {
            "total_in_bed_time_milli": 28_800_000, "total_awake_time_milli": 1_800_000,
            "total_light_sleep_time_milli": 14_400_000, "total_slow_wave_sleep_time_milli": 5_400_000,
            "total_rem_sleep_time_milli": 7_200_000, "disturbance_count": 9},
        "respiratory_rate": 15.2, "sleep_performance_percentage": 88, "sleep_consistency_percentage": 70,
        "sleep_efficiency_percentage": 93.5},
}
NAP = dict(SLEEP, id=NAP_ID, start="2026-11-19T13:00:00.000Z", end="2026-11-19T13:30:00.000Z", nap=True)
PENDING_SLEEP = dict(SLEEP, id="a1b2c3d4-0000-4000-8000-000000000003", start="2026-11-19T22:00:00.000Z",
                     end="2026-11-20T06:00:00.000Z", score_state="PENDING_SCORE", score=None)
RECOVERY = {
    "cycle_id": 93845, "sleep_id": SLEEP_ID, "user_id": 10129, "score_state": "SCORED",
    "score": {"user_calibrating": False, "recovery_score": 66, "resting_heart_rate": 51, "hrv_rmssd_milli": 62.5,
              "spo2_percentage": 95.5, "skin_temp_celsius": 33.6},
}
PENDING_RECOVERY = {"cycle_id": 93846, "sleep_id": PENDING_SLEEP["id"], "score_state": "PENDING_SCORE"}
CYCLE = {
    "id": 93845, "start": "2026-11-18T22:00:00.000Z", "end": "2026-11-19T21:30:00.000Z",
    "timezone_offset": "+01:00", "score_state": "SCORED", "step_count": 9000,
    "score": {"strain": 12.3, "kilojoule": 8368.0, "average_heart_rate": 68, "max_heart_rate": 160},
}
OPEN_CYCLE = {"id": 93846, "start": "2026-11-19T21:30:00.000Z", "end": None, "timezone_offset": "+01:00",
              "score_state": "SCORED", "score": {"strain": 3.1, "kilojoule": 2092.0}}
WORKOUT = {
    "id": "b1b2c3d4-0000-4000-8000-000000000001", "user_id": 10129, "start": "2026-11-19T16:00:00.000Z",
    "end": "2026-11-19T16:45:00.000Z", "timezone_offset": "+01:00", "sport_name": "running",
    "score_state": "SCORED",
    "score": {"strain": 9.8, "average_heart_rate": 150, "max_heart_rate": 178, "kilojoule": 2500.0,
              "zone_durations": {"zone_two_milli": 600_000}},
}
PENDING_WORKOUT = dict(WORKOUT, id="b1b2c3d4-0000-4000-8000-000000000002", score_state="PENDING_SCORE", score=None)


def page(records: list, next_token: str | None = None):
    body = {"records": records, **({"next_token": next_token} if next_token else {})}
    return json_response(json.dumps(body))


def token(clock: FakeClock, expires_in: float = 3600, n: int = 1) -> Token:
    return Token(access_token=f"fake-access-{n:04d}", refresh_token=f"fake-refresh-{n:04d}",
                 expires_at=clock.now + expires_in, scope=" ".join(whoop.SCOPES), obtained_at=clock.now)


def token_json(n: int) -> object:
    return json_response(json.dumps({"access_token": f"fake-access-{n:04d}", "refresh_token": f"fake-refresh-{n:04d}",
                                     "expires_in": 3600, "scope": "offline", "token_type": "bearer"}))


def get(path: str) -> tuple[str, str]:
    return ("GET", HOST + API + path)


def routes(**overrides) -> dict:
    base = {
        get(whoop.SLEEP_PATH): [page([SLEEP, NAP, PENDING_SLEEP])],
        get(whoop.RECOVERY_PATH): [page([RECOVERY, PENDING_RECOVERY])],
        get(whoop.CYCLE_PATH): [page([OPEN_CYCLE, CYCLE])],
        get(whoop.WORKOUT_PATH): [page([WORKOUT, PENDING_WORKOUT])],
    }
    base.update({get(k): v for k, v in overrides.items()})
    return base


def source(transport, clock: FakeClock, store: MemoryTokenStore, **kw) -> whoop.WhoopSource:
    return whoop.WhoopSource(env=ENV, options=kw.pop("options", {}), tz=TZ, transport=transport, token_store=store,
                             clock=clock, sleep=clock.sleep, lock_dir=None, **kw)


def query(req) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(req.url).query).items()}


class UnitTests(unittest.TestCase):
    def test_conversions(self) -> None:
        self.assertAlmostEqual(whoop.kj_to_kcal(8368.0), 2000.0)
        self.assertEqual(whoop.ms_to_h(5_400_000), 1.5)
        self.assertEqual(whoop.spo2_percent(0.955), 95.5)
        self.assertEqual(whoop.spo2_percent(97), 97)
        with self.assertRaises(ValueError):
            whoop.spo2_percent(30)


class PollMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(NOW.timestamp())
        self.store = MemoryTokenStore({"whoop": token(self.clock)})
        self.transport = FakeTransport(routes())
        self.result = source(self.transport, self.clock, self.store).poll(None, now=NOW, tz=TZ)
        self.batch = self.result.batch

    def samples(self, metric: str) -> list:
        return [s for s in self.batch.samples if s.metric == metric]

    def vendor(self) -> dict:
        return {(d.date, d.key): d.value for d in self.batch.daily_vendor_scores}

    def test_window_cursor_and_request_range(self) -> None:
        self.assertEqual(self.result.window, ("2026-11-17T11:00:00+01:00", "2026-11-20T11:00:00+01:00"))
        self.assertEqual(self.result.new_cursor, {"watermark": "2026-11-20T11:00:00+01:00"})
        self.assertTrue(self.result.complete)
        first = query(self.transport.calls[0])
        self.assertEqual(first, {"start": "2026-11-16T10:00:00.000Z", "end": "2026-11-20T10:00:00.000Z",
                                 "limit": "25"})

    def test_sleep_night_from_scored_main_sleep_only(self) -> None:
        (night,) = self.batch.sleep_nights  # nap and pending sleep give none
        self.assertEqual((night.wake_date, night.sleep_start, night.sleep_end),
                         ("2026-11-19", "2026-11-18T23:00:00+01:00", "2026-11-19T07:00:00+01:00"))
        self.assertEqual((night.total_h, night.asleep_h, night.core_h, night.deep_h, night.rem_h, night.awake_h),
                         (8.0, 7.5, 4.0, 1.5, 2.0, 0.5))

    def test_recovery_joins_its_sleep(self) -> None:
        (hrv,) = self.samples("heart_rate_variability_rmssd")
        self.assertEqual((hrv.ts, hrv.value, hrv.field, hrv.unit), ("2026-11-19T03:00:00+01:00", 62.5, "nightly", "ms"))
        (rhr,) = self.samples("resting_heart_rate")
        self.assertEqual((rhr.ts, rhr.value), ("2026-11-19T07:00:00+01:00", 51.0))
        (temp,) = self.samples("skin_temperature")
        self.assertEqual((temp.value, temp.unit), (33.6, "°C"))
        (resp,) = self.samples("respiratory_rate")
        self.assertEqual((resp.ts, resp.value), ("2026-11-19T07:00:00+01:00", 15.2))

    def test_vendor_scores(self) -> None:
        v = self.vendor()
        self.assertEqual(v[("2026-11-19", "whoop.recovery")], 66.0)
        self.assertEqual(v[("2026-11-19", "whoop.calibrating")], 0.0)
        self.assertEqual(v[("2026-11-19", "whoop.spo2_sleep_avg")], 95.5)
        self.assertEqual(v[("2026-11-19", "whoop.sleep_performance")], 88.0)
        self.assertEqual(v[("2026-11-19", "whoop.disturbances")], 9.0)
        self.assertEqual(v[("2026-11-19", "whoop.day_strain")], 12.3)
        self.assertAlmostEqual(v[("2026-11-19", "whoop.cycle_kcal_total")], 2000.0)
        self.assertEqual(v[("2026-11-20", "whoop.day_strain")], 3.1)  # open cycle dated by now
        self.assertEqual(v[("2026-11-19", "whoop.workout_strain_max")], 9.8)

    def test_steps_only_from_closed_cycles(self) -> None:
        (steps,) = self.samples("step_count")
        self.assertEqual((steps.ts, steps.value, steps.unit), ("2026-11-19T22:30:00+01:00", 9000.0, "count"))

    def test_workout_without_kilojoule_as_active_kcal(self) -> None:
        (w,) = self.batch.workouts  # the pending workout gives none
        self.assertEqual((w.id, w.name, w.start, w.end, w.duration_s),
                         (f"whoop:{WORKOUT['id']}", "running", "2026-11-19T17:00:00+01:00",
                          "2026-11-19T17:45:00+01:00", 2700.0))
        self.assertIsNone(w.active_kcal)
        self.assertEqual((w.hr_avg, w.hr_max, w.hr_series), (150.0, 178.0, ()))
        self.assertNotIn("user_id", w.raw_json)
        self.assertIn("zone_durations", w.raw_json)
        self.assertEqual(self.samples("active_energy"), [])

    def test_every_row_is_whoop_with_offset(self) -> None:
        b = self.batch
        rows = (*b.samples, *b.sleep_nights, *b.workouts, *b.daily_vendor_scores)
        self.assertTrue(all(r.source == "whoop" for r in rows))
        self.assertTrue(all(d.key in whoop.VENDOR_KEYS for d in b.daily_vendor_scores))
        stamps = [s.ts for s in b.samples] + [w.start for w in b.workouts] + [n.sleep_end for n in b.sleep_nights]
        self.assertTrue(all(parse_iso(t).utcoffset() is not None for t in stamps))
        self.assertEqual(self.result.warnings, ())

    def test_api_calls_carry_the_bearer(self) -> None:
        self.assertTrue(all(c.headers["Authorization"] == "Bearer fake-access-0001" for c in self.transport.calls))


class PagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(NOW.timestamp())
        self.store = MemoryTokenStore({"whoop": token(self.clock)})

    def test_next_token_pagination(self) -> None:
        transport = FakeTransport(routes(**{whoop.SLEEP_PATH: [page([SLEEP], "tok-2"), page([NAP])]}))
        result = source(transport, self.clock, self.store).poll(None, now=NOW, tz=TZ)
        sleep_calls = [query(c) for c in transport.calls if urlsplit(c.url).path == API + whoop.SLEEP_PATH]
        self.assertEqual(len(sleep_calls), 2)
        self.assertNotIn("nextToken", sleep_calls[0])
        self.assertEqual(sleep_calls[1]["nextToken"], "tok-2")
        self.assertEqual(sleep_calls[1]["start"], sleep_calls[0]["start"])
        self.assertEqual(len(result.batch.sleep_nights), 1)
        self.assertEqual(len(result.batch.workouts), 1)

    def test_repeated_next_token_is_a_schema_error(self) -> None:
        transport = FakeTransport(routes(**{whoop.SLEEP_PATH: [page([], "same"), page([], "same")]}))
        with self.assertRaises(SchemaError):
            source(transport, self.clock, self.store).poll(None, now=NOW, tz=TZ)

    def test_top_level_schema_break_raises(self) -> None:
        transport = FakeTransport(routes(**{whoop.SLEEP_PATH: [json_response(json.dumps({"data": []}))]}))
        with self.assertRaises(SchemaError):
            source(transport, self.clock, self.store).poll({"watermark": "2026-11-19T11:00:00+01:00"}, now=NOW, tz=TZ)

    def test_unknown_score_state_and_orphan_recovery_warn(self) -> None:
        odd = dict(WORKOUT, score_state="SOMETHING_NEW")
        orphan = dict(RECOVERY, sleep_id="a1b2c3d4-0000-4000-8000-00000000ffff")
        transport = FakeTransport(routes(**{whoop.WORKOUT_PATH: [page([odd])],
                                            whoop.RECOVERY_PATH: [page([orphan])]}))
        result = source(transport, self.clock, self.store).poll(None, now=NOW, tz=TZ)
        self.assertEqual(result.batch.workouts, ())
        self.assertIn("whoop:workout_skipped", result.warnings)
        self.assertIn("whoop:recovery_skipped", result.warnings)
        self.assertEqual([s for s in result.batch.samples if s.metric == "heart_rate_variability_rmssd"], [])

    def test_nap_recovery_gives_no_rows(self) -> None:
        transport = FakeTransport(routes(**{whoop.SLEEP_PATH: [page([NAP])],
                                            whoop.RECOVERY_PATH: [page([dict(RECOVERY, sleep_id=NAP_ID)])]}))
        result = source(transport, self.clock, self.store).poll(None, now=NOW, tz=TZ)
        self.assertEqual(result.batch.sleep_nights, ())
        self.assertFalse(any(d.key == "whoop.recovery" for d in result.batch.daily_vendor_scores))


class TokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(NOW.timestamp())

    def test_rotating_refresh_is_saved_before_the_next_call(self) -> None:
        store = MemoryTokenStore({"whoop": token(self.clock, expires_in=30)})
        inner = FakeTransport({("POST", HOST + "/oauth/oauth2/token"): [token_json(2)], **routes()})
        seen: list[str | None] = []

        def transport(req):
            seen.append(store.tokens["whoop"].refresh_token)
            return inner(req)

        source(transport, self.clock, store).poll(None, now=NOW, tz=TZ)
        refresh_call, first_api = inner.calls[0], inner.calls[1]
        self.assertEqual(refresh_call.method, "POST")
        self.assertIn(b"grant_type=refresh_token", refresh_call.body)
        self.assertIn(b"refresh_token=fake-refresh-0001", refresh_call.body)
        self.assertEqual(seen[:2], ["fake-refresh-0001", "fake-refresh-0002"])
        self.assertEqual(first_api.headers["Authorization"], "Bearer fake-access-0002")
        self.assertEqual(len(store.saves), 1)
        self.assertEqual(store.tokens["whoop"].refresh_token, "fake-refresh-0002")

    def test_401_forces_one_refresh_then_retries(self) -> None:
        store = MemoryTokenStore({"whoop": token(self.clock)})
        r = routes()
        r[get(whoop.SLEEP_PATH)] = [json_response("", 401), *r[get(whoop.SLEEP_PATH)]]
        transport = FakeTransport({("POST", HOST + "/oauth/oauth2/token"): [token_json(2)], **r})
        result = source(transport, self.clock, store).poll(None, now=NOW, tz=TZ)
        self.assertEqual(len(result.batch.sleep_nights), 1)
        self.assertEqual([c.method for c in transport.calls[:3]], ["GET", "POST", "GET"])
        self.assertEqual(transport.calls[2].headers["Authorization"], "Bearer fake-access-0002")

    def test_invalid_grant_raises_auth_required(self) -> None:
        store = MemoryTokenStore({"whoop": token(self.clock, expires_in=10)})
        transport = FakeTransport({("POST", HOST + "/oauth/oauth2/token"):
                                   [json_response(json.dumps({"error": "invalid_grant"}), 400)]})
        with self.assertRaises(AuthRequired):
            source(transport, self.clock, store).poll(None, now=NOW, tz=TZ)
        self.assertEqual(store.tokens["whoop"].refresh_token, "fake-refresh-0001")

    def test_no_token_raises_auth_required(self) -> None:
        with self.assertRaises(AuthRequired):
            source(FakeTransport({}), self.clock, MemoryTokenStore()).poll(None, now=NOW, tz=TZ)


class AuthorizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(NOW.timestamp())
        self.store = MemoryTokenStore()
        self.transport = FakeTransport({("POST", HOST + "/oauth/oauth2/token"): [token_json(1)]})

    def ui(self) -> mock.Mock:
        ui = mock.Mock()
        ui.ask.side_effect = lambda _prompt: (
            "http://127.0.0.1:8765/callback?code=fake-code&state="
            + parse_qs(urlsplit(ui.show_url.call_args.args[0]).query)["state"][0])
        return ui

    def test_manual_authorize_uses_an_8_char_state_and_no_pkce(self) -> None:
        ui = self.ui()
        status = source(self.transport, self.clock, self.store).authorize(ui, manual=True)
        params = parse_qs(urlsplit(ui.show_url.call_args.args[0]).query)
        self.assertEqual(len(params["state"][0]), 8)
        self.assertNotIn("code_challenge", params)
        self.assertEqual(params["scope"][0], " ".join(whoop.SCOPES))
        body = parse_qs(self.transport.calls[0].body.decode())
        self.assertEqual(body["code"], ["fake-code"])
        self.assertEqual(body["client_secret"], ["fake-client-secret"])
        self.assertNotIn("code_verifier", body)
        self.assertTrue(status.ok)
        self.assertEqual(self.store.tokens["whoop"].refresh_token, "fake-refresh-0001")

    def test_loopback_authorize_shows_url_when_ready(self) -> None:
        ui = mock.Mock()

        def fake_wait(cfg, state, *, on_ready):
            on_ready()
            self.assertEqual(len(state), 8)
            return "fake-code"

        status = source(self.transport, self.clock, self.store, wait=fake_wait).authorize(ui)
        ui.show_url.assert_called_once()
        ui.ask.assert_not_called()
        self.assertTrue(status.ok)

    def test_status_without_token(self) -> None:
        status = source(self.transport, self.clock, self.store).auth_status()
        self.assertFalse(status.ok)
        self.assertEqual(status.detail, "no token")

    def test_revoke_calls_delete_then_drops_token(self) -> None:
        self.store.save("whoop", token(self.clock))
        transport = FakeTransport({("DELETE", HOST + API + whoop.REVOKE_PATH): [json_response("", 204)]})
        source(transport, self.clock, self.store).revoke()
        self.assertEqual(transport.calls[0].headers["Authorization"], "Bearer fake-access-0001")
        self.assertIsNone(self.store.load("whoop"))

    def test_missing_secret_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            whoop.WhoopSource(env={whoop.CLIENT_ID_ENV: "x"}, token_store=self.store)

    def test_registry_builds_whoop(self) -> None:
        cfg = TrackerConfig(enabled=("whoop",), per_source={"whoop": {"poll_minutes": 60}}, tz=TZ)
        src = registry.build("whoop", env={**ENV, "HOMUNCULUS_SECRETS": "/nonexistent-test-root"}, cfg=cfg,
                             transport=FakeTransport({}), token_store=self.store)
        self.assertIsInstance(src, whoop.WhoopSource)
        self.assertEqual((src.auth_kind, src.backfill_chunk_days, src.max_history_days), ("oauth2", 90, None))


if __name__ == "__main__":
    unittest.main()

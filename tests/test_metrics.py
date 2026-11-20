"""Tests for homunculus.metrics. All values are small synthetic fixtures."""

from __future__ import annotations

import json
import math
import random
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from homunculus import metrics
from homunculus.metrics import (
    DAILY_KEYS,
    MetricsConfig,
    baselines,
    banister_trimp,
    build_daily,
    edwards_trimp,
    finalize_day,
    freshness,
    hrv_state,
    load_summary,
    midpoint_sd,
    pick_source,
    readiness,
    rhr_state,
    sleep_debt,
    social_jetlag,
    sri,
    srpe_load,
    vo2max_trend,
    wake_date,
    weight_trend,
)
from homunculus.store import CheckinItem, DailyValue, Sample, SleepNight, SleepSegment, Store, Workout

RNG_SEED = 4711
TZ = "Europe/Berlin"
DAY = "2026-03-10"  # CET, UTC+1


def _shift(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _dv(day: str, key: str, value: float | None) -> DailyValue:
    return DailyValue(day, key, value, "watch", 1, "ok" if value is not None else "missing")


def _series(key: str, end: str, values: list[float | None]) -> list[DailyValue]:
    """values[-1] lands on end, earlier entries on earlier days."""
    n = len(values)
    return [_dv(_shift(end, i - n + 1), key, v) for i, v in enumerate(values)]


def _cfg(**kw) -> MetricsConfig:
    return MetricsConfig(tz=TZ, source_priority={}, hrmax=190.0, **kw)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp / "h.db")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp)


class TimeTests(unittest.TestCase):
    def test_wake_date_evening_moves_to_next_day(self) -> None:
        self.assertEqual(wake_date("2026-03-09 23:30:00 +0100", TZ), "2026-03-10")
        self.assertEqual(wake_date("2026-03-10 07:00:00 +0100", TZ), "2026-03-10")
        self.assertEqual(wake_date("2026-03-10T16:59:00+00:00", TZ), "2026-03-10")  # 17:59 local

    def test_wake_date_converts_offsets(self) -> None:
        # 17:30 UTC is 18:30 in Berlin, so it belongs to the next night.
        self.assertEqual(wake_date("2026-03-09T17:30:00Z", TZ), "2026-03-10")

    def test_pick_source_follows_priority(self) -> None:
        rows = [Sample("m", None, "t", "qty", 1.0, s) for s in ("phone", "garmin")]
        self.assertEqual(pick_source(rows, ("watch", "garmin", "phone")), "garmin")
        self.assertIsNone(pick_source(rows, ("watch",)))
        self.assertIsNone(pick_source([], ("watch",)))


class ConfigTests(unittest.TestCase):
    def test_from_dict_defaults_and_priority(self) -> None:
        cfg = MetricsConfig.from_dict({"tz": TZ, "source_priority": {"rhr": ["phone"], "default": ["garmin"]},
                                       "hrmax": 188, "work_days": [0, 1, 2]})
        self.assertEqual(cfg.hrmax, 188.0)
        self.assertEqual(cfg.work_days, (0, 1, 2))
        self.assertEqual(cfg.sleep_target_h, 7.5)
        self.assertEqual(cfg.priority("rhr"), ("phone",))
        self.assertEqual(cfg.priority("steps"), ("garmin",))
        self.assertEqual(MetricsConfig(TZ, {}).priority("hrv_sdnn_ln"), ("watch", "phone"))
        self.assertEqual(MetricsConfig(TZ, {}).priority("steps"), ("watch", "garmin", "phone"))

    def test_unknown_timezone_raises(self) -> None:
        with self.assertRaises(Exception):
            MetricsConfig("Not/AZone", {})


class BuildDailyTests(StoreCase):
    def _seed_night(self) -> None:
        self.store.upsert_sleep_nights([SleepNight(
            DAY, "2026-03-09 23:00:00 +0100", "2026-03-10 07:00:00 +0100", None, None,
            8.0, 7.5, 4.0, 1.5, 2.0, 0.5, "watch")])

    def test_rows_cover_every_key_in_order(self) -> None:
        rows = build_daily(self.store, DAY, _cfg())
        self.assertEqual([r.key for r in rows], list(DAILY_KEYS))
        by_key = {r.key: r for r in rows}
        self.assertIsNone(by_key["rhr"].value)
        self.assertEqual(by_key["rhr"].quality, "missing")
        self.assertEqual(by_key["load_srpe"].value, 0.0)  # rest day

    def test_watch_segments_beat_a_second_trackers_night_row(self) -> None:
        self.store.upsert_sleep_segments([
            SleepSegment("2026-03-09 23:30:00 +0100", "2026-03-10 06:30:00 +0100", "core", "watch")])
        self.store.upsert_sleep_nights([SleepNight(
            DAY, "2026-03-09T22:00:00+01:00", "2026-03-10T05:00:00+01:00", None, None,
            7.0, 6.0, 3.0, 1.5, 1.5, 1.0, "garmin")])
        by_key = {r.key: r for r in build_daily(self.store, DAY, _cfg())}
        self.assertEqual((by_key["tst_h"].source, by_key["tst_h"].value), ("watch", 7.0))
        garmin_first = MetricsConfig(tz=TZ, source_priority={"tst_h": ["garmin", "watch"]}, hrmax=190.0)
        self.assertEqual({r.key: r for r in build_daily(self.store, DAY, garmin_first)}["tst_h"].value, 6.0)

    def test_hrv_mean_ln_inside_sleep_window(self) -> None:
        self._seed_night()
        self.store.upsert_samples([
            Sample("heart_rate_variability", "ms", "2026-03-10 01:00:00 +0100", "qty", 40.0, "watch"),
            Sample("heart_rate_variability", "ms", "2026-03-10 04:00:00 +0100", "qty", 60.0, "watch"),
            Sample("heart_rate_variability", "ms", "2026-03-10 12:00:00 +0100", "qty", 99.0, "watch"),
            Sample("heart_rate_variability", "ms", "2026-03-10 02:00:00 +0100", "qty", 10.0, "garmin"),
        ])
        by_key = {r.key: r for r in build_daily(self.store, DAY, _cfg())}
        hrv = by_key["hrv_sdnn_ln"]
        self.assertAlmostEqual(hrv.value, (math.log(40) + math.log(60)) / 2)
        self.assertEqual((hrv.n, hrv.source, hrv.quality), (2, "watch", "ok"))
        self.assertAlmostEqual(by_key["tst_h"].value, 7.5)
        self.assertAlmostEqual(by_key["sleep_mid_min"].value, 180.0)
        self.assertAlmostEqual(by_key["sleep_start_min"].value, -60.0)  # 23:00, minutes before midnight

    def test_hrv_single_sample_is_sparse(self) -> None:
        self._seed_night()
        self.store.upsert_samples([
            Sample("heart_rate_variability", "ms", "2026-03-10 01:00:00 +0100", "qty", 40.0, "watch")])
        hrv = build_daily(self.store, DAY, _cfg())[0]
        self.assertIsNone(hrv.value)
        self.assertEqual(hrv.quality, "sparse")

    def test_hrv_without_sleep_window_is_missing(self) -> None:
        self.store.upsert_samples([
            Sample("heart_rate_variability", "ms", f"2026-03-10 0{h}:00:00 +0100", "qty", 50.0, "watch")
            for h in (1, 2)])
        hrv = build_daily(self.store, DAY, _cfg())[0]
        self.assertIsNone(hrv.value)

    def test_canonical_source_never_averaged(self) -> None:
        self.store.upsert_samples([
            Sample("resting_heart_rate", "bpm", "2026-03-10 08:00:00 +0100", "qty", 50.0, "watch"),
            Sample("resting_heart_rate", "bpm", "2026-03-10 08:00:00 +0100", "qty", 70.0, "phone"),
            Sample("step_count", "count", "2026-03-10 09:00:00 +0100", "qty", 1000.0, "watch"),
            Sample("step_count", "count", "2026-03-10 21:00:00 +0100", "qty", 500.0, "watch"),
            Sample("step_count", "count", "2026-03-10 09:00:00 +0100", "qty", 4000.0, "phone"),
            Sample("weight_body_mass", "lb", "2026-03-10 07:30:00 +0100", "qty", 160.0, "phone"),
            Sample("blood_pressure", "mmHg", "2026-03-10 08:00:00 +0100", "systolic", 120.0, "phone"),
            Sample("blood_pressure", "mmHg", "2026-03-10 08:00:00 +0100", "diastolic", 80.0, "phone"),
        ])
        by_key = {r.key: r for r in build_daily(self.store, DAY, _cfg())}
        self.assertEqual(by_key["rhr"].value, 50.0)
        self.assertEqual(by_key["steps"].value, 1500.0)  # local date, not wake date
        self.assertAlmostEqual(by_key["weight_kg"].value, 160 * 0.45359237)
        self.assertEqual((by_key["bp_sys"].value, by_key["bp_dia"].value), (120.0, 80.0))

    def test_sleep_from_segments_ignores_naps(self) -> None:
        self.store.upsert_sleep_segments([
            SleepSegment("2026-03-09 23:30:00 +0100", "2026-03-10 03:30:00 +0100", "core", "watch"),
            SleepSegment("2026-03-10 03:30:00 +0100", "2026-03-10 04:00:00 +0100", "awake", "watch"),
            SleepSegment("2026-03-10 04:00:00 +0100", "2026-03-10 06:30:00 +0100", "rem", "watch"),
            SleepSegment("2026-03-10 14:00:00 +0100", "2026-03-10 15:00:00 +0100", "core", "watch"),
        ])
        by_key = {r.key: r for r in build_daily(self.store, DAY, _cfg())}
        self.assertAlmostEqual(by_key["tst_h"].value, 6.5)
        self.assertAlmostEqual(by_key["sleep_mid_min"].value, 3 * 60.0)
        self.assertEqual(by_key["tst_h"].n, 2)

    def test_sleep_running_past_ten_stays_main_sleep(self) -> None:
        start = datetime(2026, 3, 10, 2, 0, tzinfo=timezone(timedelta(hours=1)))
        self.store.upsert_sleep_segments([
            SleepSegment(str(start + timedelta(minutes=30 * i)), str(start + timedelta(minutes=30 * (i + 1))),
                         "core", "watch") for i in range(19)])  # 02:00-11:30
        by_key = {r.key: r for r in build_daily(self.store, DAY, _cfg())}
        self.assertAlmostEqual(by_key["tst_h"].value, 9.5)
        self.assertAlmostEqual(by_key["sleep_start_min"].value, 120.0)
        self.assertAlmostEqual(by_key["sleep_mid_min"].value, 6 * 60 + 45.0)

    def test_evening_nap_stays_out_of_the_night(self) -> None:
        self.store.upsert_sleep_segments([
            SleepSegment("2026-03-09 18:30:00 +0100", "2026-03-09 19:15:00 +0100", "core", "watch"),
            SleepSegment("2026-03-09 23:30:00 +0100", "2026-03-10 07:00:00 +0100", "core", "watch"),
        ])
        self.store.upsert_samples([
            Sample("heart_rate_variability", "ms", "2026-03-09 21:00:00 +0100", "qty", 99.0, "watch"),
            Sample("heart_rate_variability", "ms", "2026-03-10 01:00:00 +0100", "qty", 40.0, "watch"),
            Sample("heart_rate_variability", "ms", "2026-03-10 04:00:00 +0100", "qty", 60.0, "watch"),
        ])
        by_key = {r.key: r for r in build_daily(self.store, DAY, _cfg())}
        self.assertAlmostEqual(by_key["tst_h"].value, 7.5)
        self.assertAlmostEqual(by_key["sleep_start_min"].value, -30.0)
        self.assertAlmostEqual(by_key["sleep_mid_min"].value, 195.0)
        self.assertAlmostEqual(by_key["hrv_sdnn_ln"].value, (math.log(40) + math.log(60)) / 2)

    def test_sleep_start_is_continuous_across_midnight(self) -> None:
        bedtimes = ["23:40", "00:20", "23:50", "00:10"]
        for k, hhmm in enumerate(bedtimes):
            wake = _shift(DAY, k)
            start_day = _shift(wake, -1) if hhmm.startswith("23") else wake
            self.store.upsert_sleep_nights([SleepNight(
                wake, f"{start_day} {hhmm}:00 +0100", f"{wake} 07:00:00 +0100", None, None,
                7.0, 6.5, 3.0, 1.5, 1.5, 0.5, "watch")])
        values = [{r.key: r for r in build_daily(self.store, _shift(DAY, k), _cfg())}["sleep_start_min"].value
                  for k in range(len(bedtimes))]
        self.assertEqual(values, [-20.0, 20.0, -10.0, 10.0])
        self.assertLess(metrics.stats.sd(values), 20.0)

    def _workout(self, wid: str, minutes: int, bpm: float) -> Workout:
        start = datetime(2026, 3, 10, 17, 0, tzinfo=timezone(timedelta(hours=1)))
        series = tuple(((start + timedelta(minutes=i)).isoformat(), bpm) for i in range(minutes + 1))
        return Workout(wid, "run", start.isoformat(), (start + timedelta(minutes=minutes)).isoformat(),
                       minutes * 60.0, 300.0, bpm, bpm + 10, series, False, "watch", "{}")

    def _rpe(self, ref: str, value: float) -> CheckinItem:
        return CheckinItem(DAY, "session_rpe", value, None, ref, "2026-03-10T19:00:00+01:00", "de-1", "form")

    def test_load_srpe_and_edwards(self) -> None:
        self.store.upsert_workouts([self._workout("w1", 30, 152.0)])  # 80% HRmax -> zone 4
        self.store.put_checkins([self._rpe("w1", 6)])
        by_key = {r.key: r for r in finalize_day(self.store, DAY, _cfg())}
        self.assertEqual(by_key["load_srpe"].value, 180.0)
        self.assertAlmostEqual(by_key["load_edwards"].value, 120.0)
        stored = self.store.daily("load_srpe", DAY, _shift(DAY, 1))
        self.assertEqual(stored[0].value, 180.0)

    def test_unrated_workout_fails_closed(self) -> None:
        self.store.upsert_workouts([self._workout("w1", 30, 150.0), self._workout("w2", 20, 150.0)])
        self.store.put_checkins([self._rpe("w1", 6)])
        row = {r.key: r for r in build_daily(self.store, DAY, _cfg())}["load_srpe"]
        self.assertIsNone(row.value)
        self.assertEqual((row.quality, row.n), ("sparse", 2))

    def test_late_session_rpe_refreshes_finalized_day(self) -> None:
        self.store.upsert_workouts([self._workout("w1", 30, 152.0)])
        self.assertIsNone({r.key: r for r in finalize_day(self.store, DAY, _cfg())}["load_srpe"].value)
        self.store.put_checkins([CheckinItem(_shift(DAY, 1), "session_rpe", 6, None, "w1",
                                             f"{_shift(DAY, 1)}T07:00:00+01:00", "de-1", "form")])
        finalize_day(self.store, _shift(DAY, 1), _cfg())
        stored = self.store.daily("load_srpe", _shift(DAY, -2), _shift(DAY, 2))
        self.assertEqual([(r.date, r.value) for r in stored], [(DAY, 180.0), (_shift(DAY, 1), 0.0)])

    def test_edwards_needs_hrmax(self) -> None:
        self.store.upsert_workouts([self._workout("w1", 30, 150.0)])
        row = {r.key: r for r in build_daily(self.store, DAY, MetricsConfig(TZ, {}))}["load_edwards"]
        self.assertIsNone(row.value)


class HrvRhrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(RNG_SEED)

    def _hrv(self, recent: float, n_days: int = 68) -> list[DailyValue]:
        values = [math.log(50) + self.rng.gauss(0, 0.05) for _ in range(n_days - 7)]
        values += [recent + self.rng.gauss(0, 0.01) for _ in range(7)]
        return _series("hrv_sdnn_ln", DAY, values)

    def test_states(self) -> None:
        self.assertEqual(hrv_state(self._hrv(math.log(50)), DAY).state, "normal")
        low = hrv_state(self._hrv(math.log(40)), DAY)
        self.assertEqual(low.state, "suppressed")
        self.assertAlmostEqual(low.swc, 0.5 * low.sd60)
        self.assertEqual(hrv_state(self._hrv(math.log(62)), DAY).state, "elevated")
        self.assertEqual(low.reason, "ok")

    def test_current_week_stays_out_of_baseline(self) -> None:
        series = self._hrv(math.log(40))
        state = hrv_state(series, DAY)
        base = [r.value for r in series if _shift(DAY, -67) <= r.date <= _shift(DAY, -8)]
        self.assertAlmostEqual(state.mu60, sum(base) / len(base))

    def test_insufficient_data(self) -> None:
        sparse = [r for i, r in enumerate(self._hrv(math.log(50))) if i < 61 or i % 3 == 0]
        state = hrv_state(sparse, DAY)
        self.assertIsNone(state.m7)
        self.assertIsNone(state.state)
        self.assertEqual(state.reason, "insufficient_m7")
        short = hrv_state(self._hrv(math.log(50), n_days=30), DAY)
        self.assertEqual((short.state, short.reason), (None, "insufficient_baseline"))
        self.assertIsNotNone(short.m7)

    def test_overreach_hint_needs_noisy_suppressed_week(self) -> None:
        values = [math.log(50) + self.rng.gauss(0, 0.02) for _ in range(61)]
        values += [math.log(40) + (0.3 if i % 2 else -0.3) for i in range(7)]
        state = hrv_state(_series("hrv_sdnn_ln", DAY, values), DAY)
        self.assertEqual(state.state, "suppressed")
        self.assertTrue(state.overreach_hint)

    def _ill_series(self) -> tuple[list[DailyValue], frozenset[str]]:
        base = math.log(50)
        values = [base + (0.06 if i % 2 else -0.06) for i in range(61)] + [base - 0.07] * 7
        sick = frozenset(_shift(DAY, k) for k in range(-30, -23))
        series = _series("hrv_sdnn_ln", DAY, values)
        return [_dv(r.date, r.key, r.value - 0.35) if r.date in sick else r for r in series], sick

    def test_illness_days_stay_out_of_hrv_baseline(self) -> None:
        series, sick = self._ill_series()
        self.assertEqual(hrv_state(series, DAY).state, "normal")  # the cold inflates sd60
        clean = hrv_state(series, DAY, exclude=sick)
        self.assertEqual(clean.state, "suppressed")
        self.assertAlmostEqual(clean.sd60, 0.06, delta=0.005)

    def test_rhr_watch_on_two_consecutive_days(self) -> None:
        base = [50.0 + (i % 3 - 1) for i in range(34)]
        state = rhr_state(_series("rhr", DAY, base + [58.0, 58.0]), DAY)
        self.assertEqual(state.med28, 50.0)
        self.assertEqual(state.delta, 8.0)
        self.assertAlmostEqual(state.z, 8.0 / (1.4826 * state.mad28))
        self.assertTrue(state.watch)
        self.assertFalse(rhr_state(_series("rhr", DAY, base + [50.0, 58.0]), DAY).watch)

    def test_rhr_mad_floor_of_one_bpm(self) -> None:
        state = rhr_state(_series("rhr", DAY, [50.0] * 35 + [53.0]), DAY)
        self.assertEqual(state.mad28, 0.0)
        self.assertEqual(state.z, 3.0)
        self.assertIsNone(rhr_state(_series("rhr", DAY, [50.0] * 5), DAY).z)


class SleepTests(unittest.TestCase):
    def test_sleep_debt(self) -> None:
        self.assertAlmostEqual(sleep_debt([7.0] * 14, 7.5), 7.0)
        self.assertAlmostEqual(sleep_debt([8.0] * 10 + [None] * 4, 7.5), -7.0)
        self.assertIsNone(sleep_debt([7.0] * 9 + [None] * 5, 7.5))
        self.assertAlmostEqual(sleep_debt([None] * 5 + [7.5] * 14, 7.5), 0.0)  # last 14 only

    def test_midpoint_sd_wraps_midnight(self) -> None:
        wrapped = midpoint_sd([1430.0, 10.0, 1420.0, 20.0])
        plain = midpoint_sd([230.0, 250.0, 220.0, 260.0])
        self.assertAlmostEqual(wrapped, plain)
        self.assertLess(wrapped, 30)
        self.assertIsNone(midpoint_sd([10.0, None, 20.0]))

    def test_social_jetlag(self) -> None:
        mids = {}
        start = date(2026, 3, 2)  # Monday
        for i in range(14):
            d = start + timedelta(days=i)
            mids[d.isoformat()] = 300.0 if d.weekday() >= 5 else 180.0
        self.assertAlmostEqual(social_jetlag(mids, (0, 1, 2, 3, 4)), 2.0)
        self.assertIsNone(social_jetlag({"2026-03-02": 180.0, "2026-03-07": 300.0}, (0, 1, 2, 3, 4)))

    def _nights(self, days: int, shift_min: int = 0, skip: tuple[int, ...] = ()) -> list[SleepSegment]:
        out = []
        for i in range(days):
            if i in skip:
                continue
            start = datetime(2026, 3, 1, 1, 0, tzinfo=timezone(timedelta(hours=1))) + timedelta(days=i)
            start += timedelta(minutes=shift_min * (i % 2))
            out.append(SleepSegment(start.isoformat(), (start + timedelta(hours=7)).isoformat(), "core", "watch"))
        return out

    def test_sri_regular_and_irregular(self) -> None:
        self.assertAlmostEqual(sri(self._nights(7), "2026-03-01", 7, TZ), 100.0)
        irregular = sri(self._nights(7, shift_min=120), "2026-03-01", 7, TZ)
        self.assertAlmostEqual(irregular, -100 + 200 * (1 - 240 / 1440))
        self.assertIsNone(sri(self._nights(6), "2026-03-01", 6, TZ))
        self.assertIsNone(sri(self._nights(10, skip=(1, 3, 5)), "2026-03-01", 10, TZ))


class LoadTests(unittest.TestCase):
    def test_srpe_load(self) -> None:
        self.assertEqual(srpe_load(7, 45), 315.0)
        with self.assertRaises(ValueError):
            srpe_load(11, 10)

    def test_edwards_zones(self) -> None:
        series = [(f"2026-03-10T10:{m:02d}:00+00:00", 150.0) for m in range(11)]
        self.assertAlmostEqual(edwards_trimp(series, 200.0), 30.0)  # 75% -> zone 3, 10 min
        gap = [("2026-03-10T10:00:00+00:00", 190.0), ("2026-03-10T11:00:00+00:00", 190.0)]
        self.assertAlmostEqual(edwards_trimp(gap, 200.0), 25.0)  # gap capped at 5 min, zone 5
        self.assertEqual(edwards_trimp([("2026-03-10T10:00:00+00:00", 90.0)] * 2, 200.0), 0.0)
        self.assertIsNone(edwards_trimp(series, None))

    def test_banister(self) -> None:
        frac = (150 - 50) / (190 - 50)
        self.assertAlmostEqual(banister_trimp(60, 150, 50, 190, "male"), 60 * frac * 0.64 * math.exp(1.92 * frac))
        self.assertAlmostEqual(banister_trimp(60, 150, 50, 190, "f"), 60 * frac * 0.86 * math.exp(1.67 * frac))
        self.assertIsNone(banister_trimp(60, 150, 50, 190, "x"))
        self.assertIsNone(banister_trimp(60, 150, 190, 190, "m"))

    def test_load_summary_constant(self) -> None:
        s = load_summary([300.0] * 35)
        self.assertAlmostEqual(s.acute, 300.0)
        self.assertAlmostEqual(s.chronic, 300.0)
        self.assertEqual(s.weekly_load, 2100.0)
        self.assertIsNone(s.monotony)
        self.assertAlmostEqual(s.wow_change_pct, 0.0)

    def test_load_summary_weekly(self) -> None:
        rng = random.Random(RNG_SEED)
        loads = [float(rng.choice([0, 200, 400])) for _ in range(27)] + [0, 300, 0, 400, 0, 500, 200]
        s = load_summary(loads)
        week = loads[-7:]
        mean = sum(week) / 7
        sd = math.sqrt(sum((x - mean) ** 2 for x in week) / 6)
        self.assertAlmostEqual(s.monotony, mean / sd)
        self.assertAlmostEqual(s.strain, sum(week) * mean / sd)
        prev = sum(loads[-14:-7])
        self.assertAlmostEqual(s.wow_change_pct, (sum(week) - prev) / prev * 100)
        self.assertIsNotNone(s.chronic)

    def test_load_summary_insufficient(self) -> None:
        s = load_summary([100.0] * 20)
        self.assertIsNone(s.chronic)
        self.assertIsNotNone(s.acute)
        gap = load_summary([100.0] * 30 + [None] + [100.0] * 2)
        self.assertIsNone(gap.weekly_load)
        self.assertIsNone(gap.wow_change_pct)
        self.assertEqual(load_summary([]), metrics.LoadSummary(None, None, None, None, None, None))


class TrendTests(unittest.TestCase):
    def test_weight_trend_recursion(self) -> None:
        wt = weight_trend([None, 80.0, 81.0, None])
        self.assertEqual(wt.trend[:2], [None, 80.0])
        self.assertAlmostEqual(wt.trend[2], 80.1)
        self.assertAlmostEqual(wt.trend[3], 80.1)
        self.assertIsNone(wt.rate_pct_bw_week)

    def test_weight_rate_flat_and_sparse(self) -> None:
        self.assertAlmostEqual(weight_trend([75.0] * 20).rate_pct_bw_week, 0.0)
        sparse = [75.0] + [None] * 15 + [74.0] * 4
        self.assertIsNone(weight_trend(sparse).rate_pct_bw_week)

    def test_weight_rate_sign(self) -> None:
        losing = [80.0 - 0.1 * i for i in range(40)]
        self.assertLess(weight_trend(losing).rate_pct_bw_week, 0)

    def test_vo2max_trend(self) -> None:
        start = date(2026, 1, 1)
        up = [((start + timedelta(days=10 * i)).isoformat(), 45.0 + 0.5 * i) for i in range(9)]
        change, direction = vo2max_trend(up)
        self.assertAlmostEqual(change, 4.5)
        self.assertEqual(direction, "up")
        flat = [(d, 45.0 + (0.3 if i % 2 else 0.0)) for i, (d, _) in enumerate(up)]
        self.assertEqual(vo2max_trend(flat)[1], "flat")
        self.assertEqual(vo2max_trend(up[:5]), (None, None))
        old = [("2025-01-01", 30.0)] * 3 + up[-5:]
        self.assertEqual(vo2max_trend(old), (None, None))


class ReadinessTests(unittest.TestCase):
    W = {"hrv": 2.0, "rhr": 1.0, "tst": 1.0, "midpoint_sd": 1.0, "wellness": 1.0}

    def test_score_contributions_and_missing(self) -> None:
        r = readiness({"hrv": 1.0, "rhr": 0.5, "tst": -0.5, "midpoint_sd": None}, self.W)
        self.assertAlmostEqual(r.score, (2.0 + 0.5 - 0.5) / 4)
        self.assertAlmostEqual(sum(r.contributions.values()), r.score)
        self.assertEqual(r.missing, ["midpoint_sd", "wellness"])
        self.assertEqual(r.band, "normal")

    def test_bands_and_cap(self) -> None:
        self.assertEqual(readiness({"hrv": 9, "rhr": 1, "tst": 1}, self.W).band, "high")
        self.assertAlmostEqual(readiness({"hrv": 9, "rhr": 1, "tst": 1}, self.W).score, (6 + 1 + 1) / 4)
        self.assertEqual(readiness({"hrv": -1, "rhr": -1, "tst": -1}, self.W).band, "low")

    def test_fewer_than_three_inputs(self) -> None:
        r = readiness({"hrv": 1.0, "rhr": 1.0, "other": 2.0}, self.W)
        self.assertIsNone(r.score)
        self.assertIsNone(r.band)
        self.assertEqual(len(r.missing), 3)


class BaselinesTests(StoreCase):
    def test_snapshot_is_json_and_complete(self) -> None:
        rng = random.Random(RNG_SEED)
        rows = []
        for k in range(-90, 1):
            d = _shift(DAY, k)
            rows += [
                _dv(d, "hrv_sdnn_ln", math.log(50) + rng.gauss(0, 0.05)),
                _dv(d, "rhr", 50.0 + rng.choice([-1, 0, 1])),
                _dv(d, "tst_h", 7.0 + rng.gauss(0, 0.3)),
                _dv(d, "sleep_mid_min", (180.0 + rng.gauss(0, 20)) % 1440),
                _dv(d, "load_srpe", float(rng.choice([0, 300, 450]))),
                _dv(d, "weight_kg", 75.0 + rng.gauss(0, 0.2)),
            ]
            if k % 10 == 0:
                rows.append(_dv(d, "vo2max", 45.0 + rng.gauss(0, 0.2)))
        self.store.put_daily(rows)
        self.store.put_checkins([
            CheckinItem(_shift(DAY, k), "wellbeing", float(rng.randint(5, 8)), None, None,
                        f"{_shift(DAY, k)}T07:00:00+01:00", "de-1", "form")
            for k in range(-28, 1)])
        snap = baselines(self.store, DAY, _cfg())
        json.dumps(snap, allow_nan=False)
        self.assertEqual(set(snap) - {"date"}, {"hrv", "rhr", "sleep", "load", "weight", "vo2max", "readiness"})
        self.assertIn(snap["hrv"]["sdnn"]["state"], ("suppressed", "normal", "elevated"))
        self.assertIsNone(snap["hrv"]["rmssd"]["state"])
        self.assertIsNotNone(snap["sleep"]["debt_h"])
        self.assertIsNotNone(snap["load"]["srpe"]["chronic"])
        self.assertIsNotNone(snap["readiness"]["score"])
        self.assertEqual(snap["readiness"]["missing"], [])
        self.assertIsNone(snap["sleep"]["sri"])  # no segments stored

    def test_symptom_checkins_exclude_days_from_baselines(self) -> None:
        series, sick = HrvRhrTests._ill_series(self)
        self.store.put_daily(series)
        first = min(sick)
        self.store.put_checkins([CheckinItem(d, "symptom:sore_throat", 1.0, None, None, f"{d}T07:00:00+01:00",
                                             "de-1", "form") for d in sorted(sick)])
        ill = metrics.illness_days(self.store, _shift(DAY, -67), DAY)
        self.assertEqual(ill, frozenset(_shift(first, k) for k in range(10)))
        self.assertEqual(baselines(self.store, DAY, _cfg())["hrv"]["sdnn"]["state"], "suppressed")

    def test_z_today_skips_excluded_days(self) -> None:
        tst = {_shift(DAY, k): 7.0 + (0.5 if k % 2 else -0.5) for k in range(-28, 0)}
        sick = frozenset(_shift(DAY, k) for k in range(-10, -5))
        tst.update({d: 3.0 for d in sick})
        tst[DAY] = 6.0
        clean = metrics._z_today({d: v for d, v in tst.items() if d not in sick}, DAY)
        self.assertAlmostEqual(metrics._z_today(tst, DAY, exclude=sick), clean)
        self.assertNotAlmostEqual(metrics._z_today(tst, DAY), clean)

    def test_empty_store_is_all_none(self) -> None:
        snap = baselines(self.store, DAY, _cfg())
        json.dumps(snap, allow_nan=False)
        self.assertIsNone(snap["readiness"]["score"])
        self.assertIsNone(snap["rhr"]["z"])
        self.assertIsNone(snap["weight"]["trend_kg"])


class FreshnessTests(unittest.TestCase):
    def test_ages_and_stale(self) -> None:
        now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
        latest = {"heart_rate_variability": "2026-03-10 07:00:00 +0100", "workouts": "2026-03-06T12:00:00+00:00"}
        out = freshness(latest, now, {"heart_rate_variability": 36, "workouts": 72, "sleep_nights": 36})
        self.assertAlmostEqual(out["heart_rate_variability"]["age_h"], 6.0)
        self.assertFalse(out["heart_rate_variability"]["stale"])
        self.assertTrue(out["workouts"]["stale"])
        self.assertEqual(out["sleep_nights"], {"latest": None, "age_h": None, "stale": True})


class TrackerSourceTests(StoreCase):
    """Nightly summaries, locks and vendor keys (docs/TRACKER_CONTRACT.md)."""

    WHOOP_CFG = {"hrv_rmssd_ln": ["garmin", "whoop"], "tst_h": ["whoop", "watch"]}

    def _whoop_night(self) -> None:
        self.store.upsert_sleep_nights([SleepNight(
            DAY, "2026-03-09T23:00:00+01:00", "2026-03-10T07:00:00+01:00", None, None,
            8.0, 7.5, 4.0, 1.5, 2.0, 0.5, "whoop")])

    def _rmssd(self, *rows: tuple[str, str, float, str]) -> DailyValue:
        self.store.upsert_samples([Sample("heart_rate_variability_rmssd", "ms", ts, f, v, src) for ts, f, v, src in rows])
        cfg = MetricsConfig(TZ, self.WHOOP_CFG, locked=self._locked)
        return {r.key: r for r in build_daily(self.store, DAY, cfg)}["hrv_rmssd_ln"]

    def setUp(self) -> None:
        super().setUp()
        self._locked = {}
        self._whoop_night()

    def test_one_nightly_row_gives_ln_value(self) -> None:
        row = self._rmssd(("2026-03-10T03:00:00+01:00", "nightly", 62.0, "whoop"))
        self.assertAlmostEqual(row.value, math.log(62.0))
        self.assertEqual((row.n, row.source, row.quality), (1, "whoop", "ok"))

    def test_two_nightly_rows_are_sparse(self) -> None:
        row = self._rmssd(("2026-03-10T02:00:00+01:00", "nightly", 60.0, "whoop"),
                          ("2026-03-10T03:00:00+01:00", "nightly", 64.0, "whoop"))
        self.assertIsNone(row.value)
        self.assertEqual(row.quality, "sparse")

    def test_readings_win_over_nightly_and_priority_holds(self) -> None:
        row = self._rmssd(("2026-03-10T03:00:00+01:00", "nightly", 62.0, "whoop"),
                          ("2026-03-10T02:00:00+01:00", "qty", 40.0, "garmin"),
                          ("2026-03-10T04:00:00+01:00", "qty", 60.0, "garmin"))
        self.assertAlmostEqual(row.value, (math.log(40) + math.log(60)) / 2)
        self.assertEqual((row.n, row.source), (2, "garmin"))

    def test_nightly_outside_sleep_window_is_missing(self) -> None:
        row = self._rmssd(("2026-03-10T12:00:00+01:00", "nightly", 62.0, "whoop"))
        self.assertIsNone(row.value)
        self.assertEqual(row.quality, "missing")

    def test_lock_ignores_other_sources(self) -> None:
        self._locked = {"hrv_rmssd_ln": "garmin"}
        row = self._rmssd(("2026-03-10T03:00:00+01:00", "nightly", 62.0, "whoop"))
        self.assertIsNone(row.value)
        self.assertEqual((row.source, row.quality), (metrics.NO_SOURCE, "missing"))

    def test_sdnn_default_lock_drops_phone(self) -> None:
        self.store.upsert_samples([
            Sample("heart_rate_variability", "ms", f"2026-03-10T0{h}:00:00+01:00", "qty", 50.0, "phone") for h in (2, 3)])
        cfg = MetricsConfig(TZ, {"tst_h": ["whoop"]})
        self.assertIsNone(build_daily(self.store, DAY, cfg)[0].value)
        self.assertEqual(cfg.priority("hrv_sdnn_ln"), ("watch", "phone"))
        self.assertEqual(cfg.sources("hrv_sdnn_ln"), ("watch",))

    def test_from_dict_merges_default_locks(self) -> None:
        cfg = MetricsConfig.from_dict({"tz": TZ, "locked": {"rhr": "garmin"}})
        self.assertEqual(dict(cfg.locked), {"hrv_sdnn_ln": "watch", "rhr": "garmin"})
        self.assertEqual(cfg.sources("rhr"), ("garmin",))
        self.assertEqual(cfg.sources("steps"), ("watch", "garmin", "phone"))

    def test_skin_temperature_and_spo2_readings_only(self) -> None:
        self.store.upsert_samples([
            Sample("skin_temperature", "°C", "2026-03-10T07:00:00+01:00", "qty", 34.5, "whoop"),
            Sample("blood_oxygen_saturation", "%", "2026-03-10T03:00:00+01:00", "nightly", 90.0, "watch"),
            Sample("blood_oxygen_saturation", "%", "2026-03-10T09:00:00+01:00", "qty", 96.0, "watch"),
        ])
        cfg = MetricsConfig(TZ, {"wrist_temp_c": ["whoop"]})
        by_key = {r.key: r for r in build_daily(self.store, DAY, cfg)}
        self.assertEqual((by_key["wrist_temp_c"].value, by_key["wrist_temp_c"].source), (34.5, "whoop"))
        self.assertEqual(by_key["spo2_min"].value, 96.0)

    def test_states_filter_to_locked_source(self) -> None:
        series = [DailyValue(_shift(DAY, k), "hrv_rmssd_ln", math.log(50) + 0.01 * (k % 3), "whoop", 1, "ok")
                  for k in range(-70, 1)]
        self.assertEqual(hrv_state(series, DAY).reason, "ok")
        self.assertEqual(hrv_state(series, DAY, "garmin").reason, "insufficient_m7")
        rhr = [DailyValue(_shift(DAY, k), "rhr", 50.0 + k % 2, "whoop", 1, "ok") for k in range(-40, 1)]
        self.assertIsNotNone(rhr_state(rhr, DAY).z)
        self.assertIsNone(rhr_state(rhr, DAY, "watch").z)


class VendorKeyTests(unittest.TestCase):
    def test_daily_keys_are_canonical(self) -> None:
        self.assertFalse(any(metrics.is_vendor_key(k) for k in DAILY_KEYS))
        self.assertTrue(metrics.is_vendor_key("whoop.recovery"))
        with self.assertRaises(ValueError):
            metrics._series(None, "whoop.recovery", DAY, 7)

    def test_vendor_rows_never_reach_baselines(self) -> None:
        rng = random.Random(RNG_SEED)
        tmp = Path(tempfile.mkdtemp())
        try:
            plain, mixed = Store(tmp / "a.db"), Store(tmp / "b.db")
            canonical, vendor = [], []
            for k in range(-90, 1):
                d = _shift(DAY, k)
                canonical += [_dv(d, "hrv_sdnn_ln", math.log(50) + rng.gauss(0, 0.05)),
                              _dv(d, "rhr", 50.0 + rng.choice([-1, 0, 1])),
                              _dv(d, "tst_h", 7.0 + rng.gauss(0, 0.3))]
                for src, name in (("whoop", "recovery"), ("whoop", "day_strain"), ("garmin", "body_battery_max")):
                    vendor.append(DailyValue(d, f"{src}.{name}", rng.uniform(-100, 100), src, 1, "ok"))
            plain.put_daily(canonical)
            mixed.put_daily(canonical + vendor)
            self.assertEqual(baselines(plain, DAY, _cfg()), baselines(mixed, DAY, _cfg()))
            plain.close()
            mixed.close()
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()

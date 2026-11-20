"""Deterministic physiology metrics.

Raw samples, sleep, workouts and session RPE become one ``DailyValue`` per
``DAILY_KEYS`` entry, keyed on the owner's local wake date. Each key reads one
canonical source picked by configured priority; sources are never averaged.
Baselines, states, load, trends and readiness are pure functions over those
rows. Every derived value is ``None`` when coverage is insufficient.

A key may be locked to one source (``MetricsConfig.locked``); then only that
source counts. Vendor scores live under dotted keys such as ``whoop.recovery``
and never enter this module's daily rows, baselines or readiness.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from homunculus import stats
from homunculus.checkins import SYMPTOM_PREFIX
from homunculus.store import CheckinItem, DailyValue, Sample, SleepNight, SleepSegment, Store, Workout

DAILY_KEYS = (
    "hrv_sdnn_ln", "hrv_rmssd_ln", "rhr", "tst_h", "sleep_mid_min", "sleep_start_min", "weight_kg",
    "vo2max", "load_srpe", "load_edwards", "resp_rate", "wrist_temp_c", "spo2_min", "steps",
    "active_kcal", "bp_sys", "bp_dia", "af_burden", "intake_kcal",
)
# Daily keys that feed code checks only (ed_watch) and never an LLM packet.
CODE_ONLY_KEYS = frozenset({"intake_kcal"})

DEFAULT_PRIORITY: tuple[str, ...] = ("watch", "garmin", "phone")
# Garmin reports RMSSD, never SDNN, so it is not a default SDNN source.
_KEY_PRIORITY: Mapping[str, tuple[str, ...]] = {"hrv_sdnn_ln": ("watch", "phone")}
# Only Apple reports SDNN; other vendors' HRV is RMSSD and must never feed this key.
DEFAULT_LOCKS: Mapping[str, str] = {"hrv_sdnn_ln": "watch"}
NIGHTLY_FIELD = "nightly"  # one per-night summary value, e.g. WHOOP or Polar RMSSD
DEFAULT_READINESS_WEIGHTS: Mapping[str, float] = {
    "hrv": 1.0, "rhr": 1.0, "tst": 1.0, "midpoint_sd": 1.0, "wellness": 1.0,
}

NO_SOURCE = "none"
SESSION_ITEM = "session_rpe"
WELLNESS_ITEM = "wellbeing"
ASLEEP_STAGES = frozenset({"asleep", "core", "rem", "deep", "unspecified"})

_DAY_MIN = 1440
_WAKE_SHIFT = timedelta(hours=6)  # samples from 18:00 on belong to the night ending next morning
_SLEEP_GAP = timedelta(minutes=90)  # asleep segments closer than this belong to one sleep bout
_BED_ANCHOR_MIN = 1080  # 18:00, the wake-date boundary; later bedtimes count as minutes before midnight
_RPE_LATE_DAYS = 2  # _load_rows accepts session RPE dated up to two days after the workout
_RECOVERY_DAYS = 3  # days after a symptom check-in that stay out of baselines
_MAD_K = 1.4826
_RHR_FLOOR = 1.0
_Z_CAP = 3.0
_MAX_HR_GAP_S = 300.0


# --------------------------------------------------------------------------
# Config


@dataclass(frozen=True)
class MetricsConfig:
    tz: str
    source_priority: Mapping[str, Sequence[str]]
    sleep_target_h: float = 7.5
    hrmax: float | None = None
    hrrest: float | None = None
    sex: str | None = None
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    readiness_weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_READINESS_WEIGHTS))
    locked: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_LOCKS))

    def __post_init__(self) -> None:
        ZoneInfo(self.tz)  # raises on an unknown zone

    @classmethod
    def from_dict(cls, d: Mapping) -> MetricsConfig:
        return cls(
            tz=str(d["tz"]),
            source_priority={k: tuple(v) for k, v in dict(d.get("source_priority") or {}).items()},
            sleep_target_h=float(d.get("sleep_target_h", 7.5)),
            hrmax=_opt_float(d.get("hrmax")),
            hrrest=_opt_float(d.get("hrrest")),
            sex=d.get("sex"),
            work_days=tuple(int(x) for x in d.get("work_days", (0, 1, 2, 3, 4))),
            readiness_weights=dict(d.get("readiness_weights") or DEFAULT_READINESS_WEIGHTS),
            locked={**DEFAULT_LOCKS, **{str(k): str(v) for k, v in dict(d.get("locked") or {}).items()}},
        )

    def priority(self, key: str) -> tuple[str, ...]:
        """Configured order for key, else the 'default' entry, else the built-in order."""
        chosen = self.source_priority.get(key) or self.source_priority.get("default")
        return tuple(chosen) if chosen else _KEY_PRIORITY.get(key, DEFAULT_PRIORITY)

    def sources(self, key: str) -> tuple[str, ...]:
        """Sources that may feed key: the lock alone when locked, else the priority order."""
        lock = self.locked.get(key)
        return (lock,) if lock else self.priority(key)


def is_vendor_key(key: str) -> bool:
    """Dotted keys ('whoop.recovery') are vendor scores, never canonical metrics."""
    return "." in key


def _opt_float(value: object) -> float | None:
    return None if value is None else float(value)


# --------------------------------------------------------------------------
# Time helpers


def _parse_ts(ts: str, tz: str = "UTC") -> datetime:
    """Aware datetime from ISO-8601 or Health Auto Export 'YYYY-MM-DD HH:MM:SS +0100'."""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S %z")
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz))


def _local(ts: str, tz: str) -> datetime:
    return _parse_ts(ts, tz).astimezone(ZoneInfo(tz))


def _local_date(ts: str, tz: str) -> str:
    return _local(ts, tz).date().isoformat()


def wake_date(ts: str, tz: str) -> str:
    """Local wake date: the local calendar date, with 18:00 onwards moved to the next day."""
    return (_local(ts, tz) + _WAKE_SHIFT).date().isoformat()


def _shift(day: str, n: int) -> str:
    return (Date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _clock_min(dt: datetime) -> float:
    return dt.hour * 60 + dt.minute + dt.second / 60.0


def _window(by_date: Mapping[str, float], day: str, lo: int, hi: int) -> list[float]:
    """Non-None values on days day+lo .. day+hi inclusive."""
    days = (_shift(day, k) for k in range(lo, hi + 1))
    return [by_date[d] for d in days if by_date.get(d) is not None]


def _by_date(series: Sequence[DailyValue]) -> dict[str, float]:
    return {r.date: r.value for r in series if r.value is not None}


# --------------------------------------------------------------------------
# Source selection


def pick_source(rows: Sequence[Sample], priority: Sequence[str]) -> str | None:
    """First source in priority that any row carries; None if no row matches.

    Works on any row type with a ``source`` attribute (samples, sleep, workouts).
    """
    present = {r.source for r in rows}
    return next((s for s in priority if s in present), None)


def _canonical(rows: Sequence, priority: Sequence[str]) -> tuple[str | None, list]:
    source = pick_source(rows, priority)
    return source, [r for r in rows if r.source == source]


# --------------------------------------------------------------------------
# Daily rows: samples


@dataclass(frozen=True)
class _Spec:
    metrics: tuple[str, ...]
    field: str
    agg: str  # mean | mean_ln | sum | min | max
    dating: str  # local | wake | sleep
    min_n: int = 1
    nightly: bool = False  # fall back to a single NIGHTLY_FIELD row when no readings exist


_SAMPLE_SPECS: Mapping[str, _Spec] = {
    "hrv_sdnn_ln": _Spec(("heart_rate_variability",), "qty", "mean_ln", "sleep", 2),
    "hrv_rmssd_ln": _Spec(("heart_rate_variability_rmssd", "recovery_hrv"), "qty", "mean_ln", "sleep", 2,
                          nightly=True),
    "rhr": _Spec(("resting_heart_rate",), "qty", "mean", "local"),
    "weight_kg": _Spec(("weight_body_mass", "body_mass"), "qty", "mean", "local"),
    "vo2max": _Spec(("vo2_max",), "qty", "mean", "local"),
    "resp_rate": _Spec(("respiratory_rate",), "qty", "mean", "wake"),
    "wrist_temp_c": _Spec(("apple_sleeping_wrist_temperature", "skin_temperature"), "qty", "mean", "wake"),
    "spo2_min": _Spec(("blood_oxygen_saturation",), "qty", "min", "local"),
    "steps": _Spec(("step_count",), "qty", "sum", "local"),
    "active_kcal": _Spec(("active_energy",), "qty", "sum", "local"),
    # Logged food energy; a day without any log stays None, never 0, so it is not read as a fast.
    "intake_kcal": _Spec(("dietary_energy",), "qty", "sum", "local"),
    "bp_sys": _Spec(("blood_pressure",), "systolic", "max", "local"),
    "bp_dia": _Spec(("blood_pressure",), "diastolic", "max", "local"),
    "af_burden": _Spec(("atrial_fibrillation_burden",), "qty", "max", "local"),
}

_UNIT_CONVERSIONS: Mapping[str, Callable[[float], float]] = {
    "lb": lambda v: v * 0.45359237,
    "lbs": lambda v: v * 0.45359237,
    "kj": lambda v: v / 4.184,
    "degf": lambda v: (v - 32.0) * 5.0 / 9.0,
    "°f": lambda v: (v - 32.0) * 5.0 / 9.0,
}


def _canonical_value(s: Sample) -> float:
    convert = _UNIT_CONVERSIONS.get((s.unit or "").strip().lower())
    return convert(s.value) if convert else s.value


def _mean_ln(xs: Sequence[float]) -> float | None:
    logs = [math.log(x) for x in xs if x > 0]
    return stats.mean(logs) if logs else None


_AGGREGATORS: Mapping[str, Callable[[Sequence[float]], float | None]] = {
    "mean": stats.mean, "mean_ln": _mean_ln, "sum": math.fsum, "min": min, "max": max,
}


def _dated(rows: Sequence[Sample], spec: _Spec, day: str, ctx: _DayContext) -> list[Sample]:
    if spec.dating == "sleep":
        window = ctx.sleep_window
        if window is None:
            return []
        return [r for r in rows if window[0] <= _parse_ts(r.ts, ctx.tz) <= window[1]]
    date_of = wake_date if spec.dating == "wake" else _local_date
    return [r for r in rows if date_of(r.ts, ctx.tz) == day]


def _quality(value: float | None, rows: Sequence) -> str:
    if value is not None:
        return "ok"
    return "sparse" if rows else "missing"


def _aggregate(spec: _Spec, rows: Sequence[Sample]) -> float | None:
    if len(rows) < spec.min_n:
        return None
    return _AGGREGATORS[spec.agg]([_canonical_value(r) for r in rows])


def _nightly_value(rows: Sequence[Sample]) -> float | None:
    """ln of the one nightly summary; None for zero, several or non-positive values."""
    if len(rows) != 1:
        return None
    value = _canonical_value(rows[0])
    return math.log(value) if value > 0 else None


def _sample_row(key: str, spec: _Spec, day: str, ctx: _DayContext) -> DailyValue:
    wanted = (spec.field, NIGHTLY_FIELD) if spec.nightly else (spec.field,)
    candidates = [r for r in ctx.samples.get(key, []) if r.field in wanted]
    source, rows = _canonical(_dated(candidates, spec, day, ctx), ctx.cfg.sources(key))
    readings = [r for r in rows if r.field == spec.field]
    if readings or not spec.nightly:
        value = _aggregate(spec, readings)
        return DailyValue(day, key, value, source or NO_SOURCE, len(readings), _quality(value, readings))
    value = _nightly_value(rows)
    return DailyValue(day, key, value, source or NO_SOURCE, len(rows), _quality(value, rows))


# --------------------------------------------------------------------------
# Daily rows: sleep


@dataclass(frozen=True)
class _Night:
    start: datetime | None
    end: datetime | None
    asleep_h: float | None
    source: str
    n: int


def _night_from_row(night: SleepNight, tz: str) -> _Night:
    start = _parse_ts(night.sleep_start, tz) if night.sleep_start else None
    end = _parse_ts(night.sleep_end, tz) if night.sleep_end else None
    hours = night.asleep_h if night.asleep_h is not None else night.total_h
    return _Night(start, end, hours, night.source, 1)


def _asleep_on(seg: SleepSegment, day: str, tz: str) -> bool:
    return seg.stage in ASLEEP_STAGES and wake_date(seg.start, tz) == day


def _span_seconds(spans: Sequence[tuple[datetime, datetime]]) -> float:
    return math.fsum((b - a).total_seconds() for a, b in spans)


def _main_bout(rows: Sequence[SleepSegment], tz: str) -> list[tuple[datetime, datetime]]:
    """Spans of the longest bout: asleep segments joined across gaps up to _SLEEP_GAP; the rest are naps."""
    bouts: list[list[tuple[datetime, datetime]]] = []
    end = None
    for a, b in sorted((_parse_ts(s.start, tz), _parse_ts(s.end, tz)) for s in rows):
        if not bouts or a - end > _SLEEP_GAP:
            bouts.append([])
            end = b
        bouts[-1].append((a, b))
        end = max(end, b)
    return max(bouts, key=_span_seconds, default=[])


def _night_from_segments(segments: Sequence[SleepSegment], day: str, cfg: MetricsConfig) -> _Night | None:
    asleep = [s for s in segments if _asleep_on(s, day, cfg.tz)]
    source, rows = _canonical(asleep, cfg.sources("tst_h"))
    spans = _main_bout(rows, cfg.tz)
    if not spans:
        return None
    seconds = _span_seconds(spans)
    return _Night(min(a for a, _ in spans), max(b for _, b in spans), seconds / 3600.0, source, len(spans))


def _night_for(nights: Sequence[SleepNight], segments: Sequence[SleepSegment], day: str,
               cfg: MetricsConfig) -> _Night | None:
    """The canonical source's night: nights and segments compete by source priority, not by table."""
    rows = [n for n in nights if n.wake_date == day]
    asleep = [s for s in segments if _asleep_on(s, day, cfg.tz)]
    source = pick_source([*rows, *asleep], cfg.sources("tst_h"))
    own = [n for n in rows if n.source == source]
    if own:
        return _night_from_row(own[0], cfg.tz)
    return _night_from_segments([s for s in asleep if s.source == source], day, cfg)


def _clock_of(dt: datetime | None, tz: str) -> float | None:
    return None if dt is None else _clock_min(dt.astimezone(ZoneInfo(tz)))


def _bedtime_of(dt: datetime | None, tz: str) -> float | None:
    """Signed minutes from local midnight: 23:30 -> -30, 00:30 -> 30; 18:00 onwards counts as before midnight.

    Bedtimes straddle midnight, so linear means, SDs and thresholds need this continuous scale.
    """
    clock = _clock_of(dt, tz)
    if clock is None:
        return None
    return clock - _DAY_MIN if clock >= _BED_ANCHOR_MIN else clock


def _midpoint(night: _Night) -> datetime | None:
    if night.start is None or night.end is None or night.end <= night.start:
        return None
    return night.start + (night.end - night.start) / 2


def _sleep_rows(night: _Night | None, day: str, tz: str) -> list[DailyValue]:
    if night is None:
        return [DailyValue(day, k, None, NO_SOURCE, 0, "missing") for k in ("tst_h", "sleep_mid_min", "sleep_start_min")]
    values = {
        "tst_h": night.asleep_h,
        "sleep_mid_min": _clock_of(_midpoint(night), tz),
        "sleep_start_min": _bedtime_of(night.start, tz),
    }
    return [DailyValue(day, k, v, night.source, night.n, _quality(v, [night])) for k, v in values.items()]


# --------------------------------------------------------------------------
# Daily rows: load


def _duration_min(w: Workout) -> float:
    if w.duration_s is not None:
        return w.duration_s / 60.0
    return (_parse_ts(w.end) - _parse_ts(w.start)).total_seconds() / 60.0


def _latest_rpe(items: Sequence[CheckinItem]) -> dict[str, float]:
    out: dict[str, tuple[str, float]] = {}
    for it in items:
        if it.ref and it.value is not None and (it.ref not in out or it.ts >= out[it.ref][0]):
            out[it.ref] = (it.ts, it.value)
    return {ref: value for ref, (_, value) in out.items()}


def _srpe_row(day: str, source: str | None, workouts: Sequence[Workout], rpe: Mapping[str, float]) -> DailyValue:
    if not workouts:
        return DailyValue(day, "load_srpe", 0.0, NO_SOURCE, 0, "ok")
    if any(w.id not in rpe for w in workouts):
        return DailyValue(day, "load_srpe", None, source or NO_SOURCE, len(workouts), "sparse")
    total = math.fsum(srpe_load(rpe[w.id], _duration_min(w)) for w in workouts)
    return DailyValue(day, "load_srpe", total, source or NO_SOURCE, len(workouts), "ok")


def _edwards_row(day: str, source: str | None, workouts: Sequence[Workout], hrmax: float | None) -> DailyValue:
    if not workouts:
        return DailyValue(day, "load_edwards", 0.0, NO_SOURCE, 0, "ok")
    parts = [edwards_trimp(w.hr_series, hrmax) for w in workouts]
    if any(p is None for p in parts):
        return DailyValue(day, "load_edwards", None, source or NO_SOURCE, len(workouts), "sparse")
    return DailyValue(day, "load_edwards", math.fsum(parts), source or NO_SOURCE, len(workouts), "ok")


# --------------------------------------------------------------------------
# build_daily


@dataclass(frozen=True)
class _DayContext:
    cfg: MetricsConfig
    tz: str
    samples: Mapping[str, list[Sample]]
    sleep_window: tuple[datetime, datetime] | None


def _fetch_samples(store: Store, day: str) -> dict[str, list[Sample]]:
    since, until = _shift(day, -2), _shift(day, 2)
    return {
        key: [s for m in spec.metrics for s in store.samples(m, since, until)]
        for key, spec in _SAMPLE_SPECS.items()
    }


def _sleep_window(night: _Night | None) -> tuple[datetime, datetime] | None:
    if night is None or night.start is None or night.end is None:
        return None
    return night.start, night.end


def _load_rows(store: Store, day: str, cfg: MetricsConfig) -> list[DailyValue]:
    found = [w for w in store.workouts(_shift(day, -2), _shift(day, 2)) if _local_date(w.start, cfg.tz) == day]
    source, workouts = _canonical(found, cfg.sources("load_srpe"))
    rpe = _latest_rpe(store.checkins(_shift(day, -1), _shift(day, 3), item=SESSION_ITEM))
    return [_srpe_row(day, source, workouts, rpe), _edwards_row(day, source, workouts, cfg.hrmax)]


def build_daily(store: Store, day: str, cfg: MetricsConfig) -> list[DailyValue]:
    """One DailyValue per DAILY_KEYS entry for the local wake date `day`."""
    Date.fromisoformat(day)
    nights = store.sleep_nights(day, _shift(day, 1))
    segments = store.sleep_segments(_shift(day, -2), _shift(day, 1))
    night = _night_for(nights, segments, day, cfg)
    ctx = _DayContext(cfg, cfg.tz, _fetch_samples(store, day), _sleep_window(night))
    rows = {k: _sample_row(k, spec, day, ctx) for k, spec in _SAMPLE_SPECS.items()}
    rows.update({r.key: r for r in _sleep_rows(night, day, cfg.tz)})
    rows.update({r.key: r for r in _load_rows(store, day, cfg)})
    return [rows[k] for k in DAILY_KEYS]


def _late_load_rows(store: Store, day: str, cfg: MetricsConfig) -> list[DailyValue]:
    """Load rows rebuilt for the already stored days before `day` whose session RPE may still arrive."""
    stored = {r.date for r in store.daily("load_srpe", _shift(day, -_RPE_LATE_DAYS), day)}
    return [r for d in sorted(stored) for r in _load_rows(store, d, cfg)]


def finalize_day(store: Store, day: str, cfg: MetricsConfig) -> list[DailyValue]:
    """build_daily, then store the rows; returns the rows of `day`.

    The load rows of the _RPE_LATE_DAYS stored days before are rebuilt too, so a
    session RPE logged the next morning still reaches the load history.
    """
    rows = build_daily(store, day, cfg)
    store.put_daily([*rows, *_late_load_rows(store, day, cfg)])
    return rows


def illness_days(store: Store, since: str, until: str) -> frozenset[str]:
    """Dates in [since, until) with a ticked symptom, plus the _RECOVERY_DAYS after each."""
    items = store.checkins(_shift(since, -_RECOVERY_DAYS), until)
    sick = {it.date for it in items if it.item.startswith(SYMPTOM_PREFIX) and it.value}
    days = {_shift(d, k) for d in sick for k in range(_RECOVERY_DAYS + 1)}
    return frozenset(d for d in days if since <= d < until)


def _without(by_date: Mapping[str, float], exclude: frozenset[str] | set[str]) -> Mapping[str, float]:
    return {d: v for d, v in by_date.items() if d not in exclude} if exclude else by_date


# --------------------------------------------------------------------------
# HRV and resting HR states


@dataclass(frozen=True)
class HrvState:
    m7: float | None
    mu60: float | None
    sd60: float | None
    swc: float | None
    cv7: float | None
    state: str | None
    overreach_hint: bool
    reason: str


def _hrv_class(m7: float, mu60: float, swc: float) -> str:
    if m7 < mu60 - swc:
        return "suppressed"
    if m7 > mu60 + swc:
        return "elevated"
    return "normal"


def _cv(xs: Sequence[float]) -> float | None:
    m = stats.mean(xs)
    return None if len(xs) < 2 or m == 0 else stats.sd(xs) / abs(m) * 100.0


def _hrv_gap(m7: float | None, base: Sequence[float]) -> str | None:
    """Reason the state cannot be computed, or None when it can."""
    if m7 is None:
        return "insufficient_m7"
    if len(base) < 30:
        return "insufficient_baseline"
    return "no_spread" if stats.sd(base) <= 0 else None


def _only_source(series: Sequence[DailyValue], source: str | None) -> Sequence[DailyValue]:
    return series if source is None else [r for r in series if r.source == source]


def hrv_state(series: Sequence[DailyValue], day: str, source: str | None = None,
              exclude: frozenset[str] = frozenset()) -> HrvState:
    """7-day mean of daily ln HRV against the days -67..-8 baseline.

    m7 needs 5 of 7 days, the baseline 30 values with spread. overreach_hint
    fires when the state is suppressed and day-to-day variation (cv7) exceeds
    the baseline's own coefficient of variation. With source (the key's lock)
    only that source's rows count. Dates in exclude (illness_days) stay out of
    the baseline, so a cold does not inflate sd60 for the next weeks.
    """
    by_date = _by_date(_only_source(series, source))
    week, base = _window(by_date, day, -6, 0), _window(_without(by_date, exclude), day, -67, -8)
    m7 = stats.mean(week) if len(week) >= 5 else None
    cv7 = _cv(week) if m7 is not None else None
    mu60, sd60 = (stats.mean(base), stats.sd(base)) if len(base) >= 30 else (None, None)
    swc = None if sd60 is None else 0.5 * sd60
    gap = _hrv_gap(m7, base)
    if gap is not None:
        return HrvState(m7, mu60, sd60, swc, cv7, None, False, gap)
    state = _hrv_class(m7, mu60, swc)
    hint = state == "suppressed" and cv7 is not None and cv7 > (_cv(base) or math.inf)
    return HrvState(m7, mu60, sd60, swc, cv7, state, hint, "ok")


@dataclass(frozen=True)
class RhrState:
    med28: float | None
    mad28: float | None
    delta: float | None
    z: float | None
    watch: bool


def _rhr_point(by_date: Mapping[str, float], day: str) -> tuple[float | None, float | None, float | None]:
    """(median, raw MAD, delta of day) against days -35..-8; needs 14 baseline days."""
    base = _window(by_date, day, -35, -8)
    if len(base) < 14:
        return None, None, None
    med, raw_mad = stats.median(base), stats.mad(base)
    today = by_date.get(day)
    return med, raw_mad, None if today is None else today - med


def _rhr_high(delta: float | None, raw_mad: float | None) -> bool:
    return delta is not None and raw_mad is not None and delta >= max(5.0, 2 * _MAD_K * raw_mad)


def rhr_state(series: Sequence[DailyValue], day: str, source: str | None = None) -> RhrState:
    """Resting HR delta and robust z against the days -35..-8 median and MAD (floor 1 bpm).

    With source (the key's lock) only that source's rows count.
    """
    by_date = _by_date(_only_source(series, source))
    med, raw_mad, delta = _rhr_point(by_date, day)
    z = None if delta is None else delta / max(_MAD_K * raw_mad, _RHR_FLOOR)
    _, prev_mad, prev_delta = _rhr_point(by_date, _shift(day, -1))
    watch = _rhr_high(delta, raw_mad) and _rhr_high(prev_delta, prev_mad)
    return RhrState(med, raw_mad, delta, z, watch)


# --------------------------------------------------------------------------
# Sleep


def sleep_debt(tst_h: Sequence[float | None], target_h: float) -> float | None:
    """Net hours below target over the last 14 entries, scaled to 14 days; negative = surplus.

    Needs at least 10 of the 14 days.
    """
    last = list(tst_h)[-14:]
    present = [x for x in last if x is not None]
    if len(present) < 10:
        return None
    return (target_h - stats.mean(present)) * 14.0


def _circ_mean(mins: Sequence[float]) -> float | None:
    angles = [m / _DAY_MIN * 2 * math.pi for m in mins]
    s, c = math.fsum(map(math.sin, angles)), math.fsum(map(math.cos, angles))
    if math.hypot(s, c) < 1e-9:
        return None
    return (math.atan2(s, c) / (2 * math.pi) * _DAY_MIN) % _DAY_MIN


def _circ_delta(a: float, b: float) -> float:
    """Signed shortest difference a - b on the 24 h clock, in minutes."""
    return (a - b + _DAY_MIN / 2) % _DAY_MIN - _DAY_MIN / 2


def midpoint_sd(mid_min: Sequence[float | None]) -> float | None:
    """SD in minutes of clock times, unwrapped around their circular mean. Needs 3 values."""
    present = [x for x in mid_min if x is not None]
    centre = _circ_mean(present) if len(present) >= 3 else None
    if centre is None:
        return None
    return stats.sd([_circ_delta(x, centre) for x in present])


def social_jetlag(mid_by_date: Mapping[str, float | None], work_days: Sequence[int]) -> float | None:
    """Free-day minus work-day mean sleep midpoint in hours (signed). Needs 2 nights of each.

    A night counts as a work night when its wake date's weekday is in work_days.
    """
    work, free = [], []
    for day, mid in mid_by_date.items():
        if mid is not None:
            (work if Date.fromisoformat(day).weekday() in work_days else free).append(mid)
    if len(work) < 2 or len(free) < 2:
        return None
    msw, msf = _circ_mean(work), _circ_mean(free)
    if msw is None or msf is None:
        return None
    return _circ_delta(msf, msw) / 60.0


def _epoch_days(segments: Sequence[SleepSegment], t0: float, days: int, epoch_s: int) -> list[set[int] | None]:
    """Per day the set of asleep epoch indices; None for a day without any asleep segment."""
    per_day = _DAY_MIN * 60 // epoch_s
    out: list[set[int] | None] = [None] * days
    for seg in segments:
        a = int((_parse_ts(seg.start).timestamp() - t0) // epoch_s)
        b = int(math.ceil((_parse_ts(seg.end).timestamp() - t0) / epoch_s))
        for e in range(max(a, 0), min(b, per_day * days)):
            d = e // per_day
            out[d] = out[d] if out[d] is not None else set()
            out[d].add(e % per_day)
    return out


def sri(segments: Sequence[SleepSegment], start_day: str, days: int, tz: str, epoch_s: int = 60) -> float | None:
    """Sleep Regularity Index, -100..100, over `days` local days from start_day 00:00.

    Compares each epoch with the same epoch 24 h later on consecutive day
    pairs that both carry sleep data. None with fewer than 7 days or when
    under 80% of the days have asleep segments from the canonical source.
    """
    if days < 7 or epoch_s <= 0:
        return None
    asleep = [s for s in segments if s.stage in ASLEEP_STAGES]
    _, rows = _canonical(asleep, DEFAULT_PRIORITY)
    t0 = datetime.combine(Date.fromisoformat(start_day), time(), ZoneInfo(tz)).timestamp()
    marks = _epoch_days(rows, t0, days, epoch_s)
    if sum(m is not None for m in marks) / days < 0.8:
        return None
    return _sri_score(marks, _DAY_MIN * 60 // epoch_s)


def _sri_score(marks: Sequence[set[int] | None], per_day: int) -> float | None:
    """-100 + 200 * share of epochs in the same state 24 h later, over complete day pairs."""
    pairs = [(a, b) for a, b in zip(marks, marks[1:]) if a is not None and b is not None]
    if not pairs:
        return None
    same = sum(per_day - len(a ^ b) for a, b in pairs)
    return -100.0 + 200.0 * same / (per_day * len(pairs))


# --------------------------------------------------------------------------
# Training load


def srpe_load(rpe: float, duration_min: float) -> float:
    """Session RPE load: CR-10 rating times minutes."""
    if not 0 <= rpe <= 10 or duration_min < 0:
        raise ValueError("rpe must lie in [0, 10] and duration_min must be >= 0")
    return float(rpe) * float(duration_min)


def _edwards_zone(bpm: float, hrmax: float) -> int:
    share = bpm / hrmax
    return 0 if share < 0.5 else min(5, int(share * 10) - 4)


def edwards_trimp(hr_series: Sequence[tuple[str, float]], hrmax: float | None) -> float | None:
    """Edwards TRIMP: minutes in each 10% HRmax zone from 50% up, weighted 1..5.

    Each sample covers the gap to the next one, capped at 5 minutes.
    """
    if hrmax is None or hrmax <= 0 or len(hr_series) < 2:
        return None
    points = sorted((_parse_ts(ts).timestamp(), bpm) for ts, bpm in hr_series)
    total = 0.0
    for (t, bpm), (t_next, _) in zip(points, points[1:]):
        total += _edwards_zone(bpm, hrmax) * min(t_next - t, _MAX_HR_GAP_S) / 60.0
    return total


_BANISTER = {"m": (0.64, 1.92), "male": (0.64, 1.92), "f": (0.86, 1.67), "female": (0.86, 1.67)}


def banister_trimp(duration_min: float, hr_avg: float, hrrest: float, hrmax: float, sex: str) -> float | None:
    """Banister TRIMP; heart-rate reserve fraction clamped to [0, 1]."""
    coeffs = _BANISTER.get(str(sex).strip().lower())
    if coeffs is None or hrmax <= hrrest or duration_min < 0:
        return None
    frac = min(max((hr_avg - hrrest) / (hrmax - hrrest), 0.0), 1.0)
    a, b = coeffs
    return duration_min * frac * a * math.exp(b * frac)


@dataclass(frozen=True)
class LoadSummary:
    acute: float | None
    chronic: float | None
    monotony: float | None
    strain: float | None
    wow_change_pct: float | None
    weekly_load: float | None


def _full_week(values: Sequence[float | None]) -> list[float] | None:
    return list(values) if len(values) == 7 and all(v is not None for v in values) else None


def _monotony(week: Sequence[float] | None) -> float | None:
    if week is None:
        return None
    spread = stats.sd(week)
    return stats.mean(week) / spread if spread > 0 else None


def _wow(this_week: float | None, prev: Sequence[float] | None) -> float | None:
    if this_week is None or prev is None or math.fsum(prev) <= 0:
        return None
    return (this_week - math.fsum(prev)) / math.fsum(prev) * 100.0


def load_summary(daily_loads: Sequence[float | None]) -> LoadSummary:
    """Acute (lambda 0.25) and chronic (lambda 2/29) EWMA plus Foster monotony and strain.

    Input runs oldest to newest, one entry per day. Chronic needs 28 non-None
    days; weekly figures need the last 7 (and the 7 before for wow) complete.
    """
    loads = list(daily_loads)
    acute = stats.ewma(loads, 7, seed_after=7)[-1] if loads else None
    chronic = stats.ewma(loads, 28, seed_after=28)[-1] if loads else None
    week = _full_week(loads[-7:])
    weekly = math.fsum(week) if week is not None else None
    monotony = _monotony(week)
    strain = weekly * monotony if weekly is not None and monotony is not None else None
    prev = _full_week(loads[-14:-7]) if len(loads) >= 14 else None
    return LoadSummary(acute, chronic, monotony, strain, _wow(weekly, prev), weekly)


# --------------------------------------------------------------------------
# Weight and VO2max trends


@dataclass(frozen=True)
class WeightTrend:
    trend: list[float | None]
    rate_pct_bw_week: float | None


def _weight_rate(trend: Sequence[float | None], weights: Sequence[float | None]) -> float | None:
    last_t, last_w = list(trend[-14:]), list(weights[-14:])
    if len(last_t) < 14 or any(t is None for t in last_t) or sum(w is not None for w in last_w) < 7:
        return None
    slope, _, _ = stats.ols([float(i) for i in range(14)], last_t)
    return slope * 7.0 / last_t[-1] * 100.0


def weight_trend(weights: Sequence[float | None]) -> WeightTrend:
    """Exponential trend T_t = T_{t-1} + 0.1 (w_t - T_{t-1}); missing days carry T forward.

    The rate is the OLS slope of the last 14 trend values in % body weight per
    week; it needs 14 days of trend and 7 real weigh-ins among them.
    """
    trend: list[float | None] = []
    current: float | None = None
    for w in weights:
        if w is not None:
            current = w if current is None else current + 0.1 * (w - current)
        trend.append(current)
    return WeightTrend(trend, _weight_rate(trend, weights))


def vo2max_trend(points: Sequence[tuple[str, float]]) -> tuple[float | None, str | None]:
    """Change per 90 days from an OLS slope over the 90 days up to the latest point.

    Needs 6 points in that window; the direction is 'flat' unless |change| > 2.
    """
    window = _last_90_days(points)
    if len(window) < 6 or len({x for x, _ in window}) < 2:
        return None, None
    slope, _, _ = stats.ols([x for x, _ in window], [v for _, v in window])
    change = slope * 90.0
    return change, _direction(change)


def _last_90_days(points: Sequence[tuple[str, float]]) -> list[tuple[float, float]]:
    """(day offset to the latest point, value) for points less than 90 days before it."""
    dated = sorted((Date.fromisoformat(d[:10]), v) for d, v in points)
    if not dated:
        return []
    end = dated[-1][0]
    return [(float((d - end).days), v) for d, v in dated if (end - d).days < 90]


def _direction(change: float) -> str:
    if abs(change) <= 2:
        return "flat"
    return "up" if change > 0 else "down"


# --------------------------------------------------------------------------
# Readiness


@dataclass(frozen=True)
class Readiness:
    score: float | None
    band: str | None
    contributions: dict[str, float]
    missing: list[str]


def _band(score: float) -> str:
    if score <= -1:
        return "low"
    return "high" if score >= 1 else "normal"


def readiness(components: Mapping[str, float | None], weights: Mapping[str, float]) -> Readiness:
    """Weighted mean of z components capped at +-3; None with fewer than 3 inputs.

    Inputs are the keys of weights with a positive weight. contributions sum
    to the score; missing lists inputs without a value.
    """
    inputs = sorted(k for k, w in weights.items() if w > 0)
    present = {k: max(-_Z_CAP, min(_Z_CAP, components[k])) for k in inputs if components.get(k) is not None}
    missing = [k for k in inputs if k not in present]
    if len(present) < 3:
        return Readiness(None, None, {}, missing)
    total_w = math.fsum(weights[k] for k in present)
    contributions = {k: weights[k] * z / total_w for k, z in present.items()}
    score = math.fsum(contributions.values())
    return Readiness(score, _band(score), contributions, missing)


# --------------------------------------------------------------------------
# Baselines snapshot


def _series(store: Store, key: str, day: str, back: int) -> list[DailyValue]:
    if is_vendor_key(key):
        raise ValueError(f"vendor key {key!r} is never canonical")
    return store.daily(key, _shift(day, -back), _shift(day, 1))


def _dense(series: Sequence[DailyValue], day: str, back: int) -> list[float | None]:
    """Values for days -back..0 in order, None where no row exists."""
    by_date = _by_date(series)
    return [by_date.get(_shift(day, k)) for k in range(-back, 1)]


def _z_today(by_date: Mapping[str, float], day: str, lo: int = -28, hi: int = -1,
             exclude: frozenset[str] = frozenset()) -> float | None:
    base = _window(_without(by_date, exclude), day, lo, hi)
    today = by_date.get(day)
    if today is None or len(base) < 14:
        return None
    return stats.zscore(today, stats.mean(base), stats.sd(base), cap=_Z_CAP)


def _rolling_mid_sd(mids: Mapping[str, float], day: str) -> float | None:
    return midpoint_sd([mids.get(_shift(day, k)) for k in range(-6, 1)])


def _midpoint_sd_z(mids: Mapping[str, float], day: str, exclude: frozenset[str] = frozenset()) -> float | None:
    sds = {_shift(day, k): _rolling_mid_sd(mids, _shift(day, k)) for k in range(-28, 1)}
    return _z_today({d: v for d, v in sds.items() if v is not None}, day, exclude=exclude)


def _neg(x: float | None) -> float | None:
    return None if x is None else -x


def _wellness_z(store: Store, day: str, exclude: frozenset[str] = frozenset()) -> float | None:
    items = store.checkins(_shift(day, -28), _shift(day, 1), item=WELLNESS_ITEM)
    return _z_today({it.date: it.value for it in items if it.value is not None}, day, exclude=exclude)


def _hrv_z(state: HrvState) -> float | None:
    return stats.zscore(state.m7, state.mu60, state.sd60, cap=_Z_CAP)


def _readiness_components(store: Store, day: str, hrv: HrvState, rhr: RhrState,
                          tst: Mapping[str, float], mids: Mapping[str, float],
                          ill: frozenset[str] = frozenset()) -> dict[str, float | None]:
    rhr_z = None if rhr.z is None else max(-_Z_CAP, min(_Z_CAP, rhr.z))
    return {
        "hrv": _hrv_z(hrv),
        "rhr": _neg(rhr_z),
        "tst": _z_today(tst, day, exclude=ill),
        "midpoint_sd": _neg(_midpoint_sd_z(mids, day, ill)),
        "wellness": _wellness_z(store, day, ill),
    }


def _sleep_block(store: Store, day: str, cfg: MetricsConfig, tst: Sequence[DailyValue],
                 mids: Sequence[DailyValue]) -> dict:
    mid_by_date = _by_date(mids)
    last14 = {_shift(day, k): mid_by_date.get(_shift(day, k)) for k in range(-13, 1)}
    segments = store.sleep_segments(_shift(day, -14), _shift(day, 1))
    return {
        "tst_h": _by_date(tst).get(day),
        "debt_h": sleep_debt(_dense(tst, day, 13), cfg.sleep_target_h),
        "midpoint_sd_min": midpoint_sd(_dense(mids, day, 6)),
        "social_jetlag_h": social_jetlag(last14, cfg.work_days),
        "sri": sri(segments, _shift(day, -13), 14, cfg.tz),
    }


def _weight_block(series: Sequence[DailyValue], day: str) -> dict:
    wt = weight_trend(_dense(series, day, 59))
    return {"trend_kg": wt.trend[-1] if wt.trend else None, "rate_pct_bw_week": wt.rate_pct_bw_week}


def _vo2_block(series: Sequence[DailyValue]) -> dict:
    change, direction = vo2max_trend([(r.date, r.value) for r in series if r.value is not None])
    return {"change_90d": change, "direction": direction}


def baselines(store: Store, day: str, cfg: MetricsConfig) -> dict:
    """JSON-serializable snapshot of every baseline for data/baselines/<week>.json."""
    ill = illness_days(store, _shift(day, -67), day)
    hrv = {k: hrv_state(_series(store, f"hrv_{k}_ln", day, 67), day, cfg.locked.get(f"hrv_{k}_ln"), ill)
           for k in ("sdnn", "rmssd")}
    rhr = rhr_state(_series(store, "rhr", day, 36), day, cfg.locked.get("rhr"))
    tst, mids = _series(store, "tst_h", day, 34), _series(store, "sleep_mid_min", day, 34)
    components = _readiness_components(store, day, hrv["sdnn"], rhr, _by_date(tst), _by_date(mids), ill)
    return {
        "date": day,
        "hrv": {k: asdict(v) for k, v in hrv.items()},
        "rhr": asdict(rhr),
        "sleep": _sleep_block(store, day, cfg, tst, mids),
        "load": {k: asdict(load_summary(_dense(_series(store, f"load_{k}", day, 41), day, 41)))
                 for k in ("srpe", "edwards")},
        "weight": _weight_block(_series(store, "weight_kg", day, 59), day),
        "vo2max": _vo2_block(_series(store, "vo2max", day, 179)),
        "readiness": {**asdict(readiness(components, cfg.readiness_weights)), "components": components},
    }


# --------------------------------------------------------------------------
# Freshness


def _age_h(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    now_aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return (now_aware - _parse_ts(ts)).total_seconds() / 3600.0


def freshness(latest: Mapping[str, str], now: datetime, thresholds_h: Mapping[str, float]) -> dict[str, dict]:
    """Age per threshold key; stale when the latest timestamp is missing or older than its threshold."""
    out: dict[str, dict] = {}
    for key, limit in thresholds_h.items():
        ts = latest.get(key)
        age = _age_h(ts, now)
        out[key] = {"latest": ts or None, "age_h": age, "stale": age is None or age > limit}
    return out

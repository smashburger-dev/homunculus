"""Google Health API v4 poller (Fitbit Air, Pixel Watch, other Fitbit devices).

docs/TRACKERS.md "Google / Fitbit" and docs/TRACKER_CONTRACT.md "google.py".
OAuth2 code + PKCE, refresh tokens do not rotate. Daily types per window,
sleep and exercise through ``dataPoints:reconcile`` (page size 25), heart-rate
samples per exercise session. Only ``google-wearables`` data by default, so
Apple data synced into Google does not come back.

The REST shapes below follow the field names in TRACKERS.md. Everything the
docs do not pin down is marked UNVERIFIED and kept in the small accessors
``_payload``, ``_sample_ts``, ``_interval`` and the spec tables.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from statistics import fmean
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from homunculus.store import SleepNight, SleepSegment, Workout
from homunculus.trackers.base import (
    AuthRequired,
    OAuthConfig,
    Response,
    SchemaError,
    iso,
    strip_vendor_raw,
)
from homunculus.trackers.oauth_source import (
    OAuthSource,
    Rows,
    at_noon,
    midpoint,
    number,
    spo2_percent,
    stage_hours,
)

NAME = "google"
API = "https://health.googleapis.com/v4/users/me/dataTypes"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE_PREFIX = "https://www.googleapis.com/auth/googlehealth"
SCOPES = tuple(f"{SCOPE_PREFIX}.{s}" for s in (
    "sleep.readonly", "activity_and_fitness.readonly", "health_metrics_and_measurements.readonly"))
DEFAULT_FAMILY = "google-wearables"
PAGE_SIZE = 1440  # documented default; max 10,000
RECONCILE_PAGE_SIZE = 25  # documented max for sleep and exercise

# Google sleep stage -> store.SLEEP_STAGES; anything else is 'unspecified'.
STAGES: Mapping[str, str] = {"AWAKE": "awake", "RESTLESS": "awake", "LIGHT": "core", "DEEP": "deep",
                             "REM": "rem", "ASLEEP": "asleep"}

Night = tuple[str, str]  # (sleep midpoint, sleep end) of one wake date

_OFFSET_RE = re.compile(r"^(-?\d+)s$")


# --------------------------------------------------------------------------
# Conversions (pure)


def int64_str(x: Any) -> float:
    """Google int64 fields arrive as JSON strings; plain JSON numbers pass through."""
    if isinstance(x, str):
        return float(int(x))
    value = number(x)
    if value is None:
        raise ValueError(f"not a number: {x!r}")
    return value


def offset_from_seconds(text: str) -> str:
    """'-14400s' -> '-04:00'."""
    match = _OFFSET_RE.match(text or "")
    if not match:
        raise ValueError(f"bad utc offset: {text!r}")
    seconds = int(match.group(1))
    sign = "-" if seconds < 0 else "+"
    hours, rest = divmod(abs(seconds), 3600)
    return f"{sign}{hours:02d}:{rest // 60:02d}"


def iso_with_offset(utc: datetime, offset: str) -> str:
    """UTC instant shown in a '+HH:MM' offset."""
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = (int(p) for p in offset.lstrip("+-").split(":"))
    return iso(utc.astimezone(timezone(sign * timedelta(hours=hours, minutes=minutes))))


def grams_to_kg(x: float) -> float:
    return float(x) / 1000.0


def google_stage(code: Any) -> str:
    return STAGES.get(str(code), "unspecified")


def _value(x: Any) -> float | None:
    try:
        return int64_str(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Data point accessors. UNVERIFIED: the docs name types and fields but not the
# JSON nesting. Assumed: each point holds its payload under the camelCase type
# name ('daily-resting-heart-rate' -> 'dailyRestingHeartRate'); daily payloads
# carry 'date' {year, month, day}; sample payloads 'sampleTime' {physicalTime,
# utcOffset}; sleep and exercise 'interval' {startTime, startUtcOffset,
# endTime, endUtcOffset}; recording method sits in point['dataSource'].


def camel(data_type: str) -> str:
    head, *rest = data_type.split("-")
    return head + "".join(p.capitalize() for p in rest)


def _payload(point: Any, data_type: str) -> Mapping[str, Any] | None:
    payload = point.get(camel(data_type)) if isinstance(point, Mapping) else None
    return payload if isinstance(payload, Mapping) else None


def _civil_date(obj: Any) -> str | None:
    try:
        return Date(int(obj["year"]), int(obj["month"]), int(obj["day"])).isoformat()
    except (TypeError, KeyError, ValueError):
        return None


def _instant(text: Any, offset: Any, tz: str) -> str | None:
    """RFC 3339 UTC time in its UTC offset ('3600s'); the adapter zone when the offset is missing."""
    try:
        utc = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if utc.tzinfo is None:
        return None
    if offset is None:
        return iso(utc.astimezone(ZoneInfo(tz)))
    try:
        return iso_with_offset(utc, offset_from_seconds(str(offset)))
    except ValueError:
        return None


def _sample_ts(payload: Mapping[str, Any], tz: str) -> str | None:
    st = payload.get("sampleTime")
    return _instant(st.get("physicalTime"), st.get("utcOffset"), tz) if isinstance(st, Mapping) else None


def _interval(payload: Mapping[str, Any], tz: str) -> tuple[str, str] | None:
    iv = payload.get("interval")
    if not isinstance(iv, Mapping):
        return None
    start = _instant(iv.get("startTime"), iv.get("startUtcOffset"), tz)
    end = _instant(iv.get("endTime"), iv.get("endUtcOffset"), tz)
    return (start, end) if start and end and start < end else None


def _manual(point: Mapping[str, Any]) -> bool:
    source = point.get("dataSource")
    return isinstance(source, Mapping) and source.get("recordingMethod") == "MANUAL"


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _between(path: str, lo: str, hi: str) -> str:
    # UNVERIFIED: filter field paths. The docs only say filters allow '>=', '<' and 'AND'.
    return f'{path} >= "{lo}" AND {path} < "{hi}"'


def _date_span(lo: datetime, hi: datetime) -> tuple[str, str]:
    """Civil dates covering [lo, hi], end exclusive."""
    return lo.date().isoformat(), (hi.date() + timedelta(days=1)).isoformat()


# --------------------------------------------------------------------------
# What each data type turns into


@dataclass(frozen=True)
class _Target:
    metric: str | None = None  # canonical Sample metric, or
    unit: str | None = None
    vendor_key: str | None = None  # a google.<key> daily vendor score
    at_wake: bool = False  # sleep-derived: dated at the sleep end of its wake date, else noon


@dataclass(frozen=True)
class _Daily:
    data_type: str
    fields: tuple[tuple[str, _Target], ...]
    rollup: bool = False  # UNVERIFIED: ':dailyRollUp' method name, params and response key


DAILY_SPECS: tuple[_Daily, ...] = (
    _Daily("daily-resting-heart-rate", (("beatsPerMinute", _Target("resting_heart_rate", "count/min")),)),
    _Daily("daily-vo2-max", (("vo2Max", _Target("vo2_max", "ml/kg/min")),
                             ("cardioFitnessLevel", _Target(vendor_key="cardio_fitness_level")))),
    _Daily("daily-oxygen-saturation", (("averagePercentage", _Target(vendor_key="spo2_sleep_avg")),)),
    _Daily("daily-respiratory-rate", (("breathsPerMinute", _Target("respiratory_rate", "count/min", at_wake=True)),)),
    _Daily("daily-sleep-temperature-derivations",
           (("nightlyTemperatureCelsius", _Target("skin_temperature", "°C", at_wake=True)),)),
    _Daily("steps", (("count", _Target("step_count", "count")),), rollup=True),
    _Daily("active-energy-burned", (("kcal", _Target("active_energy", "kcal")),), rollup=True),
    # UNVERIFIED: data type and field name for active zone minutes.
    _Daily("active-zone-minutes", (("activeZoneMinutes", _Target(vendor_key="azm")),), rollup=True),
)


def _emit(rows: Rows, day: str, target: _Target, value: float, ctx: tuple[str, Mapping[str, Night]]) -> None:
    tz, nights = ctx
    if not target.metric:
        rows.score(day, str(target.vendor_key), value)
        return
    ts = nights[day][1] if target.at_wake and day in nights else at_noon(day, tz)
    rows.sample(target.metric, target.unit, ts, "qty", value)


def daily_rows(rows: Rows, spec: _Daily, points: list[Any], tz: str, nights: Mapping[str, Night]) -> None:
    for point in points:
        payload = _payload(point, spec.data_type)
        day = _civil_date(payload.get("date")) if payload else None
        if day is None:
            rows.warn(f"bad_record {spec.data_type}")
            continue
        for fld, target in spec.fields:
            value = _value(payload.get(fld))
            if value is not None:
                _emit(rows, day, target, value, (tz, nights))


def _segment(stage: Any, offsets: Mapping[str, Any], tz: str) -> SleepSegment | None:
    if not isinstance(stage, Mapping):
        return None
    # UNVERIFIED: stages carry only UTC times; the sleep's own offsets are applied.
    start = _instant(stage.get("startTime"), offsets.get("startUtcOffset"), tz)
    end = _instant(stage.get("endTime"), offsets.get("startUtcOffset"), tz)
    if not start or not end or start >= end:
        return None
    return SleepSegment(start, end, google_stage(stage.get("type")), NAME)


def _night(wake: str, start: str, end: str, segments: list[SleepSegment]) -> SleepNight:
    h = stage_hours(segments)
    asleep = sum(h.get(k, 0.0) for k in ("core", "deep", "rem", "asleep"))
    total = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600.0
    return SleepNight(wake, start, end, start, end, total, asleep, h.get("core"), h.get("deep"),
                      h.get("rem"), h.get("awake"), NAME)


def _sleep_meta(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = payload.get("metadata")
    return meta if isinstance(meta, Mapping) else {}


def _sleep_record(rows: Rows, payload: Mapping[str, Any], span: tuple[str, str], tz: str) -> str | None:
    """Adds one processed main sleep; returns its wake date, None when skipped."""
    meta = _sleep_meta(payload)
    if meta.get("nap") is True or meta.get("mainSleep") is False:
        return None
    wake = span[1][:10]
    if meta.get("processed") is not True:
        rows.complete = False
        rows.warn(f"sleep_unprocessed {wake}")
        return None
    segs = [_segment(x, payload["interval"], tz) for x in payload.get("stages") or []]
    kept = [s for s in segs if s is not None]
    rows.segments.extend(kept)
    rows.nights.append(_night(wake, span[0], span[1], kept))
    return wake


def sleep_rows(rows: Rows, points: list[Any], tz: str) -> dict[str, Night]:
    """Segments and nights of processed main sleeps; returns wake date -> (sleep midpoint, sleep end)."""
    mids: dict[str, Night] = {}
    for point in points:
        payload = _payload(point, "sleep")
        span = _interval(payload, tz) if payload else None
        if span is None:
            rows.warn("bad_record sleep")
            continue
        wake = _sleep_record(rows, payload, span, tz)
        if wake is not None:
            mids[wake] = (midpoint(*span), span[1])
    return mids


def hrv_rows(rows: Rows, points: list[Any], mids: Mapping[str, Night]) -> None:
    """Nightly RMSSD at the main sleep midpoint of its date; entropy as a vendor score."""
    for point in points:
        payload = _payload(point, "daily-heart-rate-variability")
        day = _civil_date(payload.get("date")) if payload else None
        if day is None:
            rows.warn("bad_record daily-heart-rate-variability")
            continue
        entropy = _value(payload.get("entropy"))
        if entropy is not None:
            rows.score(day, "hrv_entropy", entropy)
        value = _value(payload.get("averageHeartRateVariabilityMilliseconds"))
        if value is None:
            continue
        if day not in mids:
            rows.warn(f"hrv_without_sleep {day}")
            continue
        rows.sample("heart_rate_variability_rmssd", "ms", mids[day][0], "nightly", value)


def weight_rows(rows: Rows, points: list[Any], tz: str) -> None:
    for point in points:
        payload = _payload(point, "weight")
        ts = _sample_ts(payload, tz) if payload else None
        grams = _value(payload.get("weightGrams")) if payload else None
        if ts is None or grams is None:
            rows.warn("bad_record weight")
            continue
        rows.sample("weight_body_mass", "kg", ts, "qty", grams_to_kg(grams))


def spo2_rows(rows: Rows, points: list[Any], tz: str) -> None:
    for point in points:
        payload = _payload(point, "oxygen-saturation")
        ts = _sample_ts(payload, tz) if payload else None
        value = _value(payload.get("percentage")) if payload else None
        if ts is None or value is None or _manual(point):
            continue
        try:
            rows.sample("blood_oxygen_saturation", "%", ts, "qty", spo2_percent(value))
        except ValueError:
            rows.warn(f"spo2_out_of_range {ts}")


def hr_series(points: list[Any], tz: str) -> tuple[tuple[str, float], ...]:
    out = []
    for point in points:
        payload = _payload(point, "heart-rate")
        ts = _sample_ts(payload, tz) if payload else None
        bpm = _value(payload.get("beatsPerMinute")) if payload else None
        if ts and bpm is not None and not _manual(point):
            out.append((ts, bpm))
    return tuple(sorted(out))


def workout(point: Mapping[str, Any], span: tuple[str, str], series: tuple[tuple[str, float], ...]) -> Workout:
    payload = _payload(point, "exercise") or {}
    start, end = span
    duration = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    bpm = [v for _, v in series]
    # UNVERIFIED: 'displayName' and 'exerciseType' as the session label.
    name = str(payload.get("displayName") or payload.get("exerciseType") or "exercise")
    vendor_id = str(point.get("name") or start).rsplit("/", 1)[-1]
    return Workout(f"{NAME}:{vendor_id}", name, start, end, duration, _value(payload.get("caloriesKcal")),
                   fmean(bpm) if bpm else None, max(bpm) if bpm else None, series, None, NAME,
                   strip_vendor_raw(payload))


# --------------------------------------------------------------------------
# Adapter


class GoogleHealthSource(OAuthSource):
    name: ClassVar[str] = NAME
    hosts: ClassVar[frozenset[str]] = frozenset({"health.googleapis.com", "oauth2.googleapis.com",
                                                 "accounts.google.com"})
    api_hosts: ClassVar[frozenset[str]] = frozenset({"health.googleapis.com", "oauth2.googleapis.com"})
    backfill_chunk_days: ClassVar[int] = 14  # the strictest rollup limit
    vendor_keys: ClassVar[frozenset[str]] = frozenset(
        f"{NAME}.{k}" for k in ("hrv_entropy", "cardio_fitness_level", "azm", "spo2_sleep_avg"))
    secret_env: ClassVar[tuple[str, str]] = ("HOMUNCULUS_GOOGLE_CLIENT_ID", "HOMUNCULUS_GOOGLE_CLIENT_SECRET")

    def oauth_config(self, client_id: str, client_secret: str, redirect: str) -> OAuthConfig:
        # UNVERIFIED: whether a "Personal" consent screen grants the restricted Health scopes,
        # and whether the Health API accepts a Desktop (loopback) client.
        return OAuthConfig(AUTHORIZE_URL, TOKEN_URL, client_id, client_secret, SCOPES, redirect_uri=redirect,
                           use_pkce=True, extra_authorize={"access_type": "offline", "prompt": "consent"})

    def decode(self, resp: Response) -> Any:
        if resp.status == 400 and b"ACCOUNT_NOT_LINKED" in resp.body:
            raise AuthRequired("account_not_linked")
        return super().decode(resp)

    @property
    def _family(self) -> str:
        family = str(self.options.get("data_source_family") or DEFAULT_FAMILY)
        return f"users/me/dataSourceFamilies/{family}"

    def _points(self, data_type: str, verb: str, params: Mapping[str, str]) -> list[Any]:
        """Every point over all pages; SchemaError on a wrong top-level shape."""
        out: list[Any] = []
        query = {**params, "dataSourceFamily": self._family}  # UNVERIFIED: parameter name
        while True:
            body = self.call("GET", f"{API}/{data_type}/dataPoints{verb}", params=query)
            # UNVERIFIED: list key 'dataPoints' (also assumed for reconcile and rollups).
            points = body.get("dataPoints", []) if isinstance(body, Mapping) else None
            if not isinstance(points, list):
                raise SchemaError(f"google {data_type}: dataPoints is not a list")
            out.extend(points)
            token = body.get("nextPageToken")
            if not token:
                return out
            query = {**query, "pageToken": str(token)}

    def _daily(self, spec: _Daily, lo: datetime, hi: datetime) -> list[Any]:
        first, last = _date_span(lo, hi)
        if spec.rollup:
            # UNVERIFIED: rollup range parameters.
            return self._points(spec.data_type, ":dailyRollUp",
                                {"range.startDate": first, "range.endDate": last, "pageSize": str(PAGE_SIZE)})
        path = f"{camel(spec.data_type)}.date"
        return self._points(spec.data_type, "", {"filter": _between(path, first, last), "pageSize": str(PAGE_SIZE)})

    def _samples(self, data_type: str, lo: datetime, hi: datetime) -> list[Any]:
        path = f"{camel(data_type)}.sampleTime.physicalTime"
        return self._points(data_type, "", {"filter": _between(path, _utc(lo), _utc(hi)), "pageSize": str(PAGE_SIZE)})

    def _sessions(self, data_type: str, lo: datetime, hi: datetime) -> list[Any]:
        path = f"{camel(data_type)}.interval.startTime"
        params = {"filter": _between(path, _utc(lo), _utc(hi)), "pageSize": str(RECONCILE_PAGE_SIZE)}
        return self._points(data_type, ":reconcile", params)

    def _exercises(self, rows: Rows, lo: datetime, hi: datetime, tz: str) -> None:
        for point in self._sessions("exercise", lo, hi):
            payload = _payload(point, "exercise")
            span = _interval(payload, tz) if payload else None
            if span is None:
                rows.warn("bad_record exercise")
                continue
            a, b = datetime.fromisoformat(span[0]), datetime.fromisoformat(span[1])
            rows.workouts.append(workout(point, span, hr_series(self._samples("heart-rate", a, b), tz)))

    def fetch_chunk(self, rows: Rows, lo: datetime, hi: datetime, *, tz: str, regular: bool,
                    cursor: Mapping[str, Any]) -> None:
        mids = sleep_rows(rows, self._sessions("sleep", lo, hi), tz)
        hrv = _Daily("daily-heart-rate-variability", ())
        hrv_rows(rows, self._daily(hrv, lo, hi), mids)
        for spec in DAILY_SPECS:
            daily_rows(rows, spec, self._daily(spec, lo, hi), tz, mids)
        weight_rows(rows, self._samples("weight", lo, hi), tz)
        spo2_rows(rows, self._samples("oxygen-saturation", lo, hi), tz)
        self._exercises(rows, lo, hi, tz)

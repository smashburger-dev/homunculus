"""Withings Public API poller (scales, sleep mat, watches, BP cuff).

docs/TRACKERS.md "Withings" and docs/TRACKER_CONTRACT.md "withings.py".
OAuth2 code without PKCE, confidential client, rotating refresh tokens. Every
answer is HTTP 200 with a JSON ``status``; non-zero means failure. The
authorization code is valid 30 s, so the paste-the-URL mode is refused.

Cursor: besides ``watermark`` it keeps ``lastupdate`` (epoch s), advanced only
after a complete run. The regular window is read with
``lastupdate = min(window start, cursor lastupdate - 1 h)``, a superset of
everything dated in the window; rows outside the window are dropped, and the
window is replaced as for every source. Backfill chunks read by date range.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homunculus.store import SleepNight, SleepSegment, Workout
from homunculus.trackers.base import (
    AuthRequired,
    OAuthConfig,
    Response,
    SchemaError,
    Token,
    TrackerError,
    UpstreamError,
    WindowPlan,
    decode_json,
    iso,
    parse_token_response,
    strip_vendor_raw,
)
from homunculus.trackers.oauth_source import OAuthSource, Rows, number, s_to_h, spo2_percent

NAME = "withings"
API = "https://wbsapi.withings.net"
AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = f"{API}/v2/oauth2"
SCOPES = ("user.metrics", "user.activity")
REFRESH_LIFETIME_S = 365 * 86400.0  # refresh token lasts one year
LASTUPDATE_SLACK_S = 3600
SLEEP_GET_MAX_DAYS = 7

# UNVERIFIED: the full status code list. These are the documented "authentication
# failed" codes as far as the research got; any other non-zero status is UpstreamError.
AUTH_STATUSES = frozenset({100, 101, 102, 200, 401})

# Withings sleep state -> store.SLEEP_STAGES (4 manual, 5 unspecified).
STAGES: Mapping[int, str] = {0: "awake", 1: "core", 2: "deep", 3: "rem", 4: "unspecified", 5: "unspecified"}


@dataclass(frozen=True)
class _Meas:
    metric: str | None = None
    unit: str | None = None
    fld: str = "qty"
    vendor_key: str | None = None


MEASTYPES: Mapping[int, _Meas] = {
    1: _Meas("weight_body_mass", "kg"),
    # UNVERIFIED: 9 diastolic and 10 systolic were not in the research report.
    9: _Meas("blood_pressure", "mmHg", "diastolic"),
    10: _Meas("blood_pressure", "mmHg", "systolic"),
    54: _Meas("blood_oxygen_saturation", "%"),  # plan-dependent (Advanced Biomarkers)
    73: _Meas("skin_temperature", "°C"),
    123: _Meas("vo2_max", "ml/kg/min"),
    6: _Meas(vendor_key="fat_pct"),
    8: _Meas(vendor_key="fat_mass_kg"),
    76: _Meas(vendor_key="muscle_mass_kg"),
    71: _Meas(vendor_key="body_temp_c"),
}
MEASTYPE_PARAM = ",".join(str(k) for k in sorted(MEASTYPES))

# UNVERIFIED: data_fields names for getsummary, get and getworkouts.
SUMMARY_FIELDS = ("wakeupduration,lightsleepduration,deepsleepduration,remsleepduration,total_sleep_time,"
                  "total_timeinbed,hr_min,rr_average,sleep_score,sleep_efficiency")
SLEEP_GET_FIELDS = "rmssd"
WORKOUT_FIELDS = "calories,hr_average,hr_max"


# --------------------------------------------------------------------------
# Conversions (pure)


def withings_value(value: Any, unit: Any) -> float:
    """value * 10**unit; unit is an integer exponent and may be negative."""
    if isinstance(value, bool) or isinstance(unit, bool) or not isinstance(value, int) \
            or not isinstance(unit, int):
        raise ValueError(f"bad withings measure: {value!r} e{unit!r}")
    return value * 10.0 ** unit


def withings_stage(code: Any) -> str | None:
    return STAGES.get(code) if isinstance(code, int) and not isinstance(code, bool) else None


def _zone(name: Any, fallback: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name)) if name else ZoneInfo(fallback)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(fallback)


def epoch_s_to_iso(seconds: Any, tz_name: Any, fallback: str = "UTC") -> str:
    """Withings 'date' (epoch s) in its IANA 'timezone'; fallback zone when that is missing."""
    value = number(seconds)
    if value is None:
        raise ValueError(f"bad epoch: {seconds!r}")
    return iso(datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone(_zone(tz_name, fallback)))


def status_error(status: Any) -> TrackerError:
    if status in AUTH_STATUSES:
        return AuthRequired(f"withings_status_{status}")
    return UpstreamError(f"withings_status_{status}")


def unwrap(obj: Any) -> Mapping[str, Any]:
    """The 'body' of a {'status', 'body'} answer; raises on a non-zero status or a bad shape."""
    if not isinstance(obj, Mapping) or not isinstance(obj.get("status"), int):
        raise SchemaError("withings answer without status")
    if obj["status"] != 0:
        raise status_error(obj["status"])
    body = obj.get("body")
    if not isinstance(body, Mapping):
        raise SchemaError("withings answer without body")
    return body


def withings_token(obj: Any, now: float) -> Token:
    """Token parser: Withings wraps the token in {'status', 'body'}; refresh lasts one year."""
    token = parse_token_response(unwrap(obj), now)
    if token.refresh_expires_at is None and token.refresh_token:
        token = replace(token, refresh_expires_at=now + REFRESH_LIFETIME_S)
    return token


def _series(body: Mapping[str, Any], key: str) -> list[Any]:
    items = body.get(key, [])
    if not isinstance(items, list):
        raise SchemaError(f"withings {key} is not a list")
    return items


def spans(lo: datetime, hi: datetime, days: int) -> list[tuple[datetime, datetime]]:
    out, step = [], timedelta(days=days)
    while lo < hi:
        out.append((lo, min(hi, lo + step)))
        lo += step
    return out


# --------------------------------------------------------------------------
# Mapping


def _measure(rows: Rows, measure: Any, ts: str) -> None:
    if not isinstance(measure, Mapping) or measure.get("type") not in MEASTYPES:
        return
    spec = MEASTYPES[measure["type"]]
    value = withings_value(measure.get("value"), measure.get("unit"))
    if spec.vendor_key:
        rows.score(ts[:10], spec.vendor_key, value)
    elif spec.metric == "blood_oxygen_saturation":
        rows.sample(spec.metric, spec.unit, ts, spec.fld, spo2_percent(value))
    else:
        rows.sample(str(spec.metric), spec.unit, ts, spec.fld, value)


def meas_rows(rows: Rows, body: Mapping[str, Any], tz: str) -> None:
    for grp in _series(body, "measuregrps"):
        try:
            ts = epoch_s_to_iso(grp.get("date"), grp.get("timezone") or body.get("timezone"), tz)
            for measure in grp.get("measures") or []:
                _measure(rows, measure, ts)
        except (AttributeError, ValueError):
            rows.warn("bad_record measuregrp")


def _night(item: Mapping[str, Any], tz: str) -> tuple[SleepNight, Mapping[str, Any]]:
    zone = item.get("timezone")
    start, end = epoch_s_to_iso(item.get("startdate"), zone, tz), epoch_s_to_iso(item.get("enddate"), zone, tz)
    data = item.get("data") if isinstance(item.get("data"), Mapping) else {}

    def h(key: str) -> float | None:
        v = number(data.get(key))
        return None if v is None else s_to_h(v)

    night = SleepNight(end[:10], start, end, start, end, h("total_timeinbed"), h("total_sleep_time"),
                       h("lightsleepduration"), h("deepsleepduration"), h("remsleepduration"),
                       h("wakeupduration"), NAME)
    return night, data


def summary_rows(rows: Rows, body: Mapping[str, Any], tz: str) -> None:
    for item in _series(body, "series"):
        try:
            night, data = _night(item, tz)
        except (AttributeError, ValueError):
            rows.warn("bad_record sleep_summary")
            continue
        rows.nights.append(night)
        for key, vendor in (("sleep_score", "sleep_score"), ("sleep_efficiency", "sleep_efficiency"),
                            ("hr_min", "sleep_hr_min")):
            if number(data.get(key)) is not None:
                rows.score(night.wake_date, vendor, data[key])
        if number(data.get("rr_average")) is not None:
            rows.sample("respiratory_rate", "count/min", str(night.sleep_end), "qty", data["rr_average"])


def _rmssd(rows: Rows, item: Mapping[str, Any], tz: str) -> None:
    series = item.get("rmssd")
    for epoch, value in (series.items() if isinstance(series, Mapping) else ()):
        if number(value) is not None and str(epoch).isdigit():
            rows.sample("heart_rate_variability_rmssd", "ms", epoch_s_to_iso(int(epoch), None, tz), "qty", value)


def sleep_get_rows(rows: Rows, body: Mapping[str, Any], tz: str) -> None:
    # UNVERIFIED: sleep 'get' series carry no timezone; the adapter zone is used.
    for item in _series(body, "series"):
        stage = withings_stage(item.get("state")) if isinstance(item, Mapping) else None
        if stage is None:
            rows.warn("bad_record sleep_state")
            continue
        start, end = epoch_s_to_iso(item.get("startdate"), None, tz), epoch_s_to_iso(item.get("enddate"), None, tz)
        if start < end:
            rows.segments.append(SleepSegment(start, end, stage, NAME))
        _rmssd(rows, item, tz)


def workout(item: Mapping[str, Any], tz: str) -> Workout:
    zone = item.get("timezone")
    start, end = epoch_s_to_iso(item.get("startdate"), zone, tz), epoch_s_to_iso(item.get("enddate"), zone, tz)
    data = item.get("data") if isinstance(item.get("data"), Mapping) else {}
    duration = float(int(item["enddate"]) - int(item["startdate"]))
    # active_kcal stays None: whether 'calories' is active or total energy is UNVERIFIED.
    return Workout(f"{NAME}:{item['id']}", f"withings category {item.get('category')}", start, end, duration,
                   None, number(data.get("hr_average")), number(data.get("hr_max")), (), None, NAME,
                   strip_vendor_raw(item))


def workout_rows(rows: Rows, body: Mapping[str, Any], tz: str) -> None:
    for item in _series(body, "series"):
        try:
            rows.workouts.append(workout(item, tz))
        except (AttributeError, KeyError, TypeError, ValueError):
            rows.warn("bad_record workout")


# --------------------------------------------------------------------------
# Adapter


class WithingsSource(OAuthSource):
    name: ClassVar[str] = NAME
    hosts: ClassVar[frozenset[str]] = frozenset({"wbsapi.withings.net", "account.withings.com"})
    api_hosts: ClassVar[frozenset[str]] = frozenset({"wbsapi.withings.net"})
    backfill_chunk_days: ClassVar[int] = 28
    vendor_keys: ClassVar[frozenset[str]] = frozenset(f"{NAME}.{k}" for k in (
        "sleep_score", "sleep_efficiency", "sleep_hr_min", "fat_pct", "fat_mass_kg", "muscle_mass_kg",
        "body_temp_c"))
    secret_env: ClassVar[tuple[str, str]] = ("HOMUNCULUS_WITHINGS_CLIENT_ID", "HOMUNCULUS_WITHINGS_CLIENT_SECRET")
    allow_manual: ClassVar[bool] = False  # the code is valid 30 s

    def oauth_config(self, client_id: str, client_secret: str, redirect: str) -> OAuthConfig:
        # UNVERIFIED: whether Withings accepts http://127.0.0.1:<port>/callback as redirect URI.
        return OAuthConfig(AUTHORIZE_URL, TOKEN_URL, client_id, client_secret, SCOPES, scope_sep=",",
                           redirect_uri=redirect, use_pkce=False, extra_token={"action": "requesttoken"},
                           token_parser=withings_token)

    def needs_refresh(self, resp: Response) -> bool:
        if resp.status == 401:
            return True
        try:
            return decode_json(resp.body).get("status") in AUTH_STATUSES
        except (SchemaError, AttributeError):
            return False

    def decode(self, resp: Response) -> Mapping[str, Any]:
        return unwrap(super().decode(resp))

    def _pages(self, url: str, form: Mapping[str, str], key: str) -> dict[str, Any]:
        """All pages ('more'/'offset') merged into one body with every item under key."""
        merged: dict[str, Any] = {key: []}
        offset = None
        while True:
            body = self.call("POST", url, form={**form, **({"offset": str(offset)} if offset else {})})
            merged[key].extend(_series(body, key))
            merged.setdefault("timezone", body.get("timezone"))
            if not body.get("more"):
                return merged
            offset = body.get("offset")

    def _range(self, lo: datetime, hi: datetime, regular: bool, cursor: Mapping[str, Any],
               ymd: bool) -> dict[str, str]:
        if regular:
            last = cursor.get("lastupdate")
            start = int(lo.timestamp())
            if isinstance(last, int) and not isinstance(last, bool):
                start = min(start, last - LASTUPDATE_SLACK_S)
            return {"lastupdate": str(start)}
        if ymd:
            return {"startdateymd": lo.date().isoformat(), "enddateymd": hi.date().isoformat()}
        return {"startdate": str(int(lo.timestamp())), "enddate": str(int(hi.timestamp()))}

    def fetch_chunk(self, rows: Rows, lo: datetime, hi: datetime, *, tz: str, regular: bool,
                    cursor: Mapping[str, Any]) -> None:
        meas = {"action": "getmeas", "meastypes": MEASTYPE_PARAM, "category": "1",
                **self._range(lo, hi, regular, cursor, False)}
        meas_rows(rows, self._pages(f"{API}/measure", meas, "measuregrps"), tz)
        summary = {"action": "getsummary", "data_fields": SUMMARY_FIELDS, **self._range(lo, hi, regular, cursor, True)}
        summary_rows(rows, self._pages(f"{API}/v2/sleep", summary, "series"), tz)
        for a, b in spans(lo, hi, SLEEP_GET_MAX_DAYS):
            form = {"action": "get", "data_fields": SLEEP_GET_FIELDS,
                    "startdate": str(int(a.timestamp())), "enddate": str(int(b.timestamp()))}
            sleep_get_rows(rows, self.call("POST", f"{API}/v2/sleep", form=form), tz)
        work = {"action": "getworkouts", "data_fields": WORKOUT_FIELDS, **self._range(lo, hi, regular, cursor, True)}
        workout_rows(rows, self._pages(f"{API}/v2/measure", work, "series"), tz)

    def cursor_extra(self, plan: WindowPlan, complete: bool) -> dict[str, Any]:
        return {"lastupdate": int(plan.until.timestamp())} if complete and plan.regular else {}

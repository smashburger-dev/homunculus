"""Polar AccessLink v3 poller.

docs/TRACKERS.md "Polar" and docs/TRACKER_CONTRACT.md "polar.py". OAuth2 code
without PKCE, Basic client auth, one access token valid about a year, no
refresh token. After the exchange the user is registered once with
``POST /v3/users``.

The non-transactional list endpoints return the last 28 (sleep, Nightly
Recharge) or 30 days (exercises) without a date filter, so one poll lists
each endpoint once and keeps what falls inside the window. Data older than
that is gone: a gap above ``max_history_days`` since the last complete run
adds the warning ``polar:history_lost <from>..<to>``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from statistics import fmean
from typing import Any, ClassVar
from urllib.parse import quote
from zoneinfo import ZoneInfo

from homunculus.store import SleepNight, SleepSegment, Workout
from homunculus.trackers.base import (
    AuthRequired,
    OAuthConfig,
    Response,
    SchemaError,
    Token,
    UpstreamError,
    iso,
    parse_iso,
    strip_vendor_raw,
)
from homunculus.trackers.oauth_source import (
    OAuthSource,
    Rows,
    at_noon,
    hours_between,
    midpoint,
    number,
    s_to_h,
    spo2_percent,
)

NAME = "polar"
API = "https://www.polaraccesslink.com/v3"
AUTHORIZE_URL = "https://flow.polar.com/oauth2/authorization"
TOKEN_URL = "https://polarremote.com/v2/oauth2/token"
SCOPES = ("accesslink.read_all",)
MEMBER_ID = "homunculus-owner"  # partner-side label for the one registered user, not personal data
HISTORY_DAYS = 28

# Polar hypnogram code -> store.SLEEP_STAGES.
STAGES: Mapping[int, str] = {0: "awake", 1: "rem", 2: "core", 3: "core", 4: "deep", 5: "unspecified"}

_DURATION_RE = re.compile(r"^PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$")
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


# --------------------------------------------------------------------------
# Conversions (pure)


def iso_duration_s(text: Any) -> float:
    """'PT1H2M3.5S' -> 3723.5; plain seconds pass through (format UNVERIFIED)."""
    if number(text) is not None:
        return float(text)
    match = _DURATION_RE.match(str(text or "").strip())
    if not match or not any(match.groups()):
        raise ValueError(f"bad duration: {text!r}")
    h, m, s = (float(g) if g else 0.0 for g in match.groups())
    return h * 3600 + m * 60 + s


def polar_stage(code: Any) -> str | None:
    return STAGES.get(code) if isinstance(code, int) and not isinstance(code, bool) else None


def polar_time(text: Any, offset_min: Any, tz: str) -> datetime:
    """Polar local time; its own offset, else 'utc_offset' minutes, else the adapter zone."""
    dt = datetime.fromisoformat(str(text))
    if dt.tzinfo is not None:
        return dt
    minutes = number(offset_min)
    if minutes is not None:
        return dt.replace(tzinfo=timezone(timedelta(minutes=minutes)))
    return dt.replace(tzinfo=ZoneInfo(tz))


def _clock_at(start: datetime, clock: str) -> datetime:
    """The first occurrence of 'HH:MM' from one hour before start on."""
    match = _CLOCK_RE.match(clock)
    if not match:
        raise ValueError(f"bad hypnogram time: {clock!r}")
    t = start.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
    return t + timedelta(days=1) if t < start - timedelta(hours=1) else t


def polar_hypnogram(start_iso: str, end_iso: str, mapping: Mapping[str, Any]) -> list[tuple[str, str, Any]]:
    """{'HH:MM': code} -> sorted (start, end, code); a clock time before the sleep start is the next day.

    Each entry runs until the next one, the last until the sleep end.
    """
    start, end = datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso)
    points = sorted((_clock_at(start, k), v) for k, v in mapping.items())
    bounds = [t for t, _ in points[1:]] + [end]
    return [(iso(t), iso(b), code) for (t, code), b in zip(points, bounds) if t < b]


# --------------------------------------------------------------------------
# Mapping


def _h(item: Mapping[str, Any], key: str) -> float | None:
    v = number(item.get(key))
    return None if v is None else s_to_h(v)


def _asleep(parts: list[float | None]) -> float | None:
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


def _segments(start: str, end: str, wake: str, hypnogram: Any) -> tuple[list[SleepSegment], list[str]]:
    mapping = hypnogram if isinstance(hypnogram, Mapping) else {}
    spans = [(a, b, polar_stage(code)) for a, b, code in polar_hypnogram(start, end, mapping)]
    segments = [SleepSegment(a, b, stage, NAME) for a, b, stage in spans if stage]
    bad = [f"unknown_stage {wake}" for _, _, stage in spans if not stage]
    return segments, bad


def night(item: Mapping[str, Any], tz: str) -> tuple[SleepNight, list[SleepSegment], list[str]]:
    start = iso(polar_time(item["sleep_start_time"], None, tz))
    end = iso(polar_time(item["sleep_end_time"], None, tz))
    parts = [_h(item, k) for k in ("light_sleep", "deep_sleep", "rem_sleep")]
    wake = str(item.get("date") or end[:10])
    rec = SleepNight(wake, start, end, start, end, hours_between(start, end), _asleep(parts), parts[0], parts[1],
                     parts[2], _h(item, "total_interruption_duration"), NAME)
    return (rec, *_segments(start, end, wake, item.get("hypnogram")))


def night_rows(rows: Rows, items: list[Any], tz: str) -> dict[str, tuple[str, str]]:
    """Nights and hypnogram segments; returns wake date -> (sleep midpoint, sleep end)."""
    out: dict[str, tuple[str, str]] = {}
    for item in items:
        try:
            rec, segments, bad = night(item, tz)
        except (KeyError, TypeError, ValueError, AttributeError):
            rows.warn("bad_record sleep")
            continue
        rows.nights.append(rec)
        rows.segments.extend(segments)
        for text in bad:
            rows.warn(text)
        out[rec.wake_date] = (midpoint(str(rec.sleep_start), str(rec.sleep_end)), str(rec.sleep_end))
    return out


def scores(rows: Rows, items: list[Any], date_key: str, fields: tuple[tuple[str, str], ...]) -> None:
    for item in items:
        day = item.get(date_key) if isinstance(item, Mapping) else None
        if not isinstance(day, str) or len(day) != 10:
            rows.warn(f"bad_record {date_key}")
            continue
        for fld, key in fields:
            if number(item.get(fld)) is not None:
                rows.score(day, key, item[fld])


RECHARGE_SCORES = (("heart_rate_avg", "sleep_hr_avg"), ("ans_charge", "ans_charge"),
                   ("nightly_recharge_status", "nightly_recharge"))


def recharge_rows(rows: Rows, items: list[Any], nights: Mapping[str, tuple[str, str]]) -> None:
    """Vendor scores by date; nightly RMSSD at the sleep midpoint, breathing rate at the sleep end."""
    scores(rows, items, "date", RECHARGE_SCORES)
    for item in (i for i in items if isinstance(i, Mapping)):
        day = item.get("date")
        hrv, breath = number(item.get("heart_rate_variability_avg")), number(item.get("breathing_rate_avg"))
        if day not in nights:
            rows.warn(f"recharge_without_sleep {day}")
            continue
        mid, end = nights[day]
        if hrv is not None:
            rows.sample("heart_rate_variability_rmssd", "ms", mid, "nightly", hrv)
        if breath is not None:
            rows.sample("respiratory_rate", "count/min", end, "qty", breath)


def skin_rows(rows: Rows, items: list[Any], nights: Mapping[str, tuple[str, str]], tz: str) -> None:
    """Absolute skin temperature at the sleep end of its night (noon without one); deviation as a score."""
    # UNVERIFIED: biosensing field names (beta endpoint).
    scores(rows, items, "sleep_date", (("deviation_from_baseline_celsius", "skin_temp_deviation"),))
    for item in (i for i in items if isinstance(i, Mapping)):
        day, value = item.get("sleep_date"), number(item.get("sleep_time_skin_temperature_celsius"))
        if isinstance(day, str) and len(day) == 10 and value is not None:
            ts = nights[day][1] if day in nights else at_noon(day, tz)
            rows.sample("skin_temperature", "°C", ts, "qty", value)


def spo2_rows(rows: Rows, items: list[Any], tz: str) -> None:
    # UNVERIFIED: biosensing field names and time format (beta endpoint). A spot test, not a night value.
    for item in items:
        try:
            ts = iso(polar_time(item["test_time"], item.get("time_zone_offset"), tz))
            rows.sample("blood_oxygen_saturation", "%", ts, "qty", spo2_percent(item["blood_oxygen_percent"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            rows.warn("bad_record spo2")


def hr_series(detail: Mapping[str, Any], start: datetime) -> tuple[tuple[str, float], ...]:
    """Heart-rate samples: sample_type 0, 'recording_rate' seconds apart, comma-separated values."""
    for sample in detail.get("samples") or []:
        if str(sample.get("sample_type")) != "0":
            continue
        rate = number(sample.get("recording_rate")) or 1.0
        values = str(sample.get("data") or "").split(",")
        return tuple((iso(start + timedelta(seconds=i * rate)), float(v))
                     for i, v in enumerate(values) if v.strip() and v.strip().lower() != "nan")
    return ()


def workout(detail: Mapping[str, Any], tz: str) -> Workout:
    start = polar_time(detail["start_time"], detail.get("start_time_utc_offset"), tz)
    duration = iso_duration_s(detail.get("duration"))
    hr = detail.get("heart_rate") if isinstance(detail.get("heart_rate"), Mapping) else {}
    series = hr_series(detail, start)
    name = str(detail.get("detailed_sport_info") or detail.get("sport") or "exercise")
    raw = strip_vendor_raw({k: v for k, v in detail.items() if k != "samples"})
    # active_kcal stays None: the mapping table lists no Polar energy field for workouts.
    return Workout(f"{NAME}:{detail['id']}", name, iso(start), iso(start + timedelta(seconds=duration)),
                   duration, None, number(hr.get("average")), number(hr.get("maximum")), series, None, NAME, raw)


# --------------------------------------------------------------------------
# Adapter


class PolarSource(OAuthSource):
    name: ClassVar[str] = NAME
    hosts: ClassVar[frozenset[str]] = frozenset({"www.polaraccesslink.com", "polarremote.com", "flow.polar.com"})
    api_hosts: ClassVar[frozenset[str]] = frozenset({"www.polaraccesslink.com", "polarremote.com"})
    backfill_chunk_days: ClassVar[int] = HISTORY_DAYS
    max_history_days: ClassVar[int | None] = HISTORY_DAYS
    vendor_keys: ClassVar[frozenset[str]] = frozenset(f"{NAME}.{k}" for k in (
        "ans_charge", "nightly_recharge", "cardio_load", "skin_temp_deviation", "sleep_hr_avg"))
    secret_env: ClassVar[tuple[str, str]] = ("HOMUNCULUS_POLAR_CLIENT_ID", "HOMUNCULUS_POLAR_CLIENT_SECRET")

    _listed: bool = False
    _exercise_list: list[Any] | None = None

    def oauth_config(self, client_id: str, client_secret: str, redirect: str) -> OAuthConfig:
        return OAuthConfig(AUTHORIZE_URL, TOKEN_URL, client_id, client_secret, SCOPES, redirect_uri=redirect,
                           use_pkce=False, client_auth="basic")

    def needs_refresh(self, resp: Response) -> bool:
        return False  # no refresh token; a 401 means re-authorize

    def decode(self, resp: Response) -> Any:
        if resp.status == 403:
            raise AuthRequired("polar_consents")
        if resp.status == 204:
            return None
        return super().decode(resp)

    def after_authorize(self, token: Token) -> None:
        """Registers the user once; 409 means already registered."""
        resp = self.http.request("POST", f"{API}/users", json_body={"member-id": MEMBER_ID},
                                 headers={"Authorization": f"Bearer {token.access_token}",
                                          "Accept": "application/json"})
        if resp.status != 409:
            self.decode(resp)

    def begin_poll(self, cursor: Mapping[str, Any], now: datetime, rows: Rows) -> None:
        self._listed, self._exercise_list = False, None
        mark = cursor.get("watermark")
        oldest = now - timedelta(days=HISTORY_DAYS)
        if mark and parse_iso(mark) < oldest:
            rows.warn(f"history_lost {parse_iso(mark).date()}..{oldest.date()}")

    def _list(self, path: str, key: str | None, beta: bool, rows: Rows) -> list[Any]:
        """One list endpoint; SchemaError on a wrong top-level shape; a 404 of a beta endpoint is empty."""
        try:
            body = self.call("GET", f"{API}{path}")
        except UpstreamError as err:
            if not (beta and str(err) == "http_404"):
                raise
            rows.warn(f"endpoint_missing {path}")
            return []
        if body is None:  # 204 No Content
            return []
        items = body.get(key) if key and isinstance(body, Mapping) else body
        if not isinstance(items, list):
            raise SchemaError(f"polar {path}: expected a list")
        return items

    def _lists(self, rows: Rows, tz: str) -> None:
        # UNVERIFIED: list paths and keys of the non-transactional endpoints; cardio load and
        # biosensing are beta.
        nights = night_rows(rows, self._list("/users/sleep", "nights", False, rows), tz)
        recharge_rows(rows, self._list("/users/nightly-recharge", "recharges", False, rows), nights)
        scores(rows, self._list("/users/cardio-load/", None, True, rows), "date", (("cardio_load", "cardio_load"),))
        skin_rows(rows, self._list("/users/biosensing/skintemperature", None, True, rows), nights, tz)
        spo2_rows(rows, self._list("/users/biosensing/spo2", None, True, rows), tz)

    def _exercises(self, rows: Rows, lo: datetime, hi: datetime, tz: str) -> None:
        if self._exercise_list is None:
            self._exercise_list = self._list("/exercises", None, False, rows)
        for item in self._exercise_list:
            try:
                start = polar_time(item["start_time"], item.get("start_time_utc_offset"), tz)
            except (KeyError, TypeError, ValueError, AttributeError):
                rows.warn("bad_record exercise")
                continue
            if lo <= start < hi:
                self._exercise(rows, str(item.get("id")), tz)

    def _exercise(self, rows: Rows, vendor_id: str, tz: str) -> None:
        detail = self.call("GET", f"{API}/exercises/{quote(vendor_id, safe='')}", params={"samples": "true"})
        try:
            rows.workouts.append(workout(detail, tz))
        except (KeyError, TypeError, ValueError, AttributeError):
            rows.warn("bad_record exercise")

    def fetch_chunk(self, rows: Rows, lo: datetime, hi: datetime, *, tz: str, regular: bool,
                    cursor: Mapping[str, Any]) -> None:
        if not self._listed:
            self._lists(rows, tz)
            self._listed = True
        self._exercises(rows, lo, hi, tz)

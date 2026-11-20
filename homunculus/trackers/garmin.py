"""Garmin Connect Personal API poller (docs/TRACKERS.md, docs/TRACKER_CONTRACT.md).

Auth is a personal API key from Connect Web, read from HOMUNCULUS_GARMIN_API_KEY.
The key mints a JWT (24 h at most); the JWT is cached in the token store and
renewed when less than an hour remains. The key itself is never stored.

Per chunk of the poll window: activities list plus one FIT download per
activity (heart-rate series), sleep summaries, HRV per day, daily summaries
(with dailyHeartRate only for days without restingValue), weight and VO2max.
All Garmin endpoint shapes come from a weeks-old API; the details marked
UNVERIFIED below still need a check against a real response.
"""

from __future__ import annotations

import base64
import io
import json
import random
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, ClassVar

from homunculus.ingest import ConfigError
from homunculus.store import SleepNight, Workout
from homunculus.trackers.base import (
    AuthRequired,
    AuthStatus,
    AuthUI,
    HttpClient,
    PollResult,
    SchemaError,
    Token,
    TokenStore,
    Transport,
    UpstreamError,
    decode_json,
    default_token_store,
    iso,
    plan_window,
    strip_vendor_raw,
    urllib_transport,
)
from homunculus.trackers.pollkit import (
    UTC,
    Rows,
    as_list,
    authed_get,
    day_range,
    dig,
    each,
    finish,
    iso_at,
    local_now,
    need,
    noon_iso,
    num,
    ok_json,
    run_chunks,
    spans,
)

NAME = "garmin"
HOST = "personal.connectapi.garmin.com"
BASE = f"https://{HOST}"
KEY_ENV = "HOMUNCULUS_GARMIN_API_KEY"
USER_AGENT = "homunculus-trackers/1"
JWT_RENEW_S = 3600.0

TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
FIT_PATH = "/download-service/files/activity/{id}"
SLEEP_PATH = "/sleep-service/sleep/dailySleepSummariesByDate"
HRV_PATH = "/hrv-service/hrv/{day}"
DAILY_PATH = "/usersummary-service/v2/dailysummaries"
HEART_PATH = "/wellness-service/wellness/dailyHeartRate"
WEIGHT_PATH = "/weight-service/weight/all/{start}/{end}"
VO2_PATH = "/metrics-service/metrics/maxmet/daily/{start}/{end}"

ACTIVITY_PAGE = 99
SLEEP_MAX_DAYS = 31
DAILY_MAX_DAYS = 365
WEIGHT_MAX_DAYS = 364
VO2_MAX_DAYS = 31  # UNVERIFIED: the spec states no range limit for maxmet; 31 d is a guess
# UNVERIFIED: how the API answers a day or activity without data (204, 404 or an empty object)
NO_DATA = frozenset({204, 404})

# Garmin status names; the numbers are ours (TRACKER_CONTRACT.md "Vendor scores").
HRV_STATUS = {"NONE": 0, "POOR": 1, "LOW": 2, "UNBALANCED": 3, "BALANCED": 4}

# UNVERIFIED: field paths inside a daily summary record. The spec names
# heartRate.restingValue and calories.burnedActive; the vendor paths are guesses.
_DAILY_VENDOR = {
    "body_battery_max": ("bodyBattery", "maxValue"),
    "body_battery_min": ("bodyBattery", "minValue"),
    "stress_avg": ("stress", "avgLevel"),
}
# UNVERIFIED: wrapper keys if an endpoint answers {"<key>": [...]} instead of a bare list.
_SLEEP_KEYS = ("dailySleepSummaries",)
_DAILY_KEYS = ("dailySummaries",)
_WEIGHT_KEYS = ("dateWeightList",)
# UNVERIFIED: the JSON field that carries the JWT, if the token endpoint answers JSON at all.
_JWT_KEYS = ("access_token", "accessToken", "token", "jwt")

# Free text the owner types; it can hold names and places.
_RAW_DROP = frozenset({"activityName", "description"})

# training_load and fitness_age stay declared for the contract, but no endpoint
# in the poll list delivers them yet, so nothing writes them.
VENDOR_KEYS = frozenset(f"{NAME}.{k}" for k in (
    "hrv_last_night", "hrv_weekly_avg", "hrv_status_code", "body_battery_max", "body_battery_min",
    "stress_avg", "sleep_score", "sleep_need_min", "training_load", "fitness_age", "spo2_sleep_avg",
))

FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z in unix seconds
FIT_MAX_BYTES = 32 * 1024 * 1024
_FIT_RECORD = 20
_FIT_HEART_RATE = 3
_FIT_TIMESTAMP = 253
_FIT_INVALID_TS = 0xFFFFFFFF


# --------------------------------------------------------------------------
# Units and times


def garmin_weight_kg(x: float) -> float:
    """Documented kg; the old internal API returned grams, so values above 1000 are grams."""
    return x / 1000 if x > 1000 else x


def s_to_h(x: float) -> float:
    return x / 3600


def _opt_h(seconds: Any) -> float | None:
    value = num(seconds)
    return None if value is None else s_to_h(value)


def naive(text: str) -> datetime:
    """Garmin local or GMT clock text without offset ('2026-11-19 06:40:00', '2026-11-19T02:15:00.0')."""
    dt = datetime.fromisoformat(str(text))
    if dt.tzinfo is not None:
        raise ValueError("expected a time without offset")
    return dt


def garmin_offset(local: datetime, gmt: datetime) -> timedelta:
    """Local minus GMT, rounded to 15 minutes."""
    quarters = round((local - gmt).total_seconds() / 900)
    if abs(quarters) > 14 * 4:
        raise ValueError("offset out of range")
    return timedelta(minutes=15 * quarters)


def epoch_ms_to_iso(ms: float, offset: timedelta) -> str:
    return iso_at(datetime.fromtimestamp(ms / 1000, UTC), offset)


def _pair_iso(gmt_text: str, local_text: str) -> tuple[datetime, timedelta, str]:
    """(UTC start, offset, ISO with offset) from a GMT and local clock pair."""
    gmt = naive(gmt_text).replace(tzinfo=UTC)
    offset = garmin_offset(naive(local_text), gmt.replace(tzinfo=None))
    return gmt, offset, iso_at(gmt, offset)


def _day(text: Any) -> str:
    return date.fromisoformat(str(text)).isoformat()


# --------------------------------------------------------------------------
# JWT


def jwt_from_body(body: bytes) -> str:
    """The JWT of a token response: plain text, a JSON string, or a JSON object (UNVERIFIED which)."""
    text = body.decode("utf-8", "replace").strip()
    if text.startswith(("{", '"')):
        obj = decode_json(body)
        text = obj if isinstance(obj, str) else next(
            (obj[k] for k in _JWT_KEYS if isinstance(obj, Mapping) and isinstance(obj.get(k), str)), "")
    if text.count(".") != 2:
        raise SchemaError("token response holds no JWT")
    return text


def jwt_exp(jwt: str) -> float:
    """exp of the JWT payload; the signature is not checked (only used for scheduling)."""
    try:
        payload = jwt.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return need(claims["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        raise SchemaError("JWT without a numeric exp") from None


class _Jwt:
    """Cached Garmin JWT; minted from the API key, never refreshed."""

    def __init__(self, store: TokenStore, key: str, clock: Callable[[], float]) -> None:
        self._store, self._key, self._clock = store, key, clock

    def get(self, http: HttpClient, force: bool = False) -> str:
        token = None if force else self._store.load(NAME)
        if token is not None and token.expires_at - self._clock() >= JWT_RENEW_S:
            return token.access_token
        return self.mint(http).access_token

    def mint(self, http: HttpClient) -> Token:
        # UNVERIFIED: whether the endpoint wants an empty body or a JSON {} with the POST
        resp = http.request("POST", BASE + TOKEN_PATH,
                            headers={"Authorization": f"ApiKey {self._key}", "Accept": "application/json"})
        if resp.status in (401, 403):
            raise AuthRequired("garmin_key_rejected")
        if not 200 <= resp.status < 300:
            raise UpstreamError(f"http_{resp.status}")
        jwt = jwt_from_body(resp.body)
        token = Token(access_token=jwt, refresh_token=None, expires_at=jwt_exp(jwt), scope="",
                      obtained_at=self._clock())
        self._store.save(NAME, token)
        return token


@dataclass
class _Api:
    http: HttpClient
    jwt: _Jwt

    def _headers(self, force: bool) -> dict[str, str]:
        # UNVERIFIED: the JWT goes in a standard Bearer header
        return {"Authorization": f"Bearer {self.jwt.get(self.http, force)}", "Accept": "application/json"}

    def json(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        return ok_json(authed_get(self.http, BASE + path, params, self._headers), empty=NO_DATA)

    def raw(self, path: str) -> bytes | None:
        resp = authed_get(self.http, BASE + path, None, self._headers)
        if resp.status in NO_DATA:
            return None
        if not 200 <= resp.status < 300:
            raise UpstreamError(f"http_{resp.status}")
        return resp.body


# --------------------------------------------------------------------------
# FIT: just enough to read heart rate from record messages


@dataclass(frozen=True)
class _FitDef:
    global_num: int
    order: str  # 'little' | 'big'
    fields: tuple[tuple[int, int], ...]  # (field number, size)
    dev_size: int


class _FitReader:
    """Walks FIT records; keeps (unix seconds, bpm) of every record message with both values."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 12 or data[8:12] != b".FIT":
            raise ValueError("not a FIT file")
        self.data = data
        self.pos = data[0]
        self.end = data[0] + int.from_bytes(data[4:8], "little")
        self.defs: dict[int, _FitDef] = {}
        self.last_ts: int | None = None
        self.points: list[tuple[int, int]] = []

    def run(self) -> list[tuple[int, int]]:
        while self.pos < self.end:
            self._record(self._take(1)[0])
        return self.points

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("truncated FIT file")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def _record(self, header: int) -> None:
        if header & 0x80:  # compressed timestamp header
            self._data(self._def((header >> 5) & 0x03), header & 0x1F)
        elif header & 0x40:
            self._definition(header)
        else:
            self._data(self._def(header & 0x0F), None)

    def _def(self, local: int) -> _FitDef:
        if local not in self.defs:
            raise ValueError("FIT data message before its definition")
        return self.defs[local]

    def _definition(self, header: int) -> None:
        self._take(1)  # reserved
        order = "big" if self._take(1)[0] == 1 else "little"
        global_num = int.from_bytes(self._take(2), order)
        fields = tuple((f[0], f[1]) for f in (self._take(3) for _ in range(self._take(1)[0])))
        dev_size = sum(self._take(3)[1] for _ in range(self._take(1)[0])) if header & 0x20 else 0
        self.defs[header & 0x0F] = _FitDef(global_num, order, fields, dev_size)

    def _data(self, d: _FitDef, offset: int | None) -> None:
        values = {num_: int.from_bytes(self._take(size), d.order) for num_, size in d.fields}
        self._take(d.dev_size)
        ts = self._timestamp(values.get(_FIT_TIMESTAMP), offset)
        hr = values.get(_FIT_HEART_RATE)
        if d.global_num == _FIT_RECORD and ts is not None and hr is not None and 0 < hr < 255:
            self.points.append((ts + FIT_EPOCH, hr))

    def _timestamp(self, raw: int | None, offset: int | None) -> int | None:
        if raw is not None and raw != _FIT_INVALID_TS:
            self.last_ts = raw
            return raw
        if offset is None or self.last_ts is None:
            return None
        ts = (self.last_ts & ~0x1F) + offset
        if offset < (self.last_ts & 0x1F):
            ts += 0x20
        self.last_ts = ts
        return ts


def fit_hr_points(data: bytes) -> list[tuple[int, int]]:
    """(unix seconds, bpm) of every FIT record message with heart_rate (3) and timestamp (253)."""
    return _FitReader(data).run()


def fit_bytes(body: bytes) -> bytes:
    """The first .fit member of the download ZIP, or the body itself if it is a bare FIT file."""
    if not zipfile.is_zipfile(io.BytesIO(body)):
        return body
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        member = next((i for i in zf.infolist() if i.filename.lower().endswith(".fit")), None)
        if member is None or member.file_size > FIT_MAX_BYTES:
            raise ValueError("ZIP without a usable FIT file")
        return zf.read(member)


# --------------------------------------------------------------------------
# Mapping


def _sleep_record(rec: Mapping[str, Any], out: Rows) -> None:
    start_ms, end_ms = need(rec["sleepStartTimestampGMT"]), need(rec["sleepEndTimestampGMT"])
    offset = garmin_offset(datetime.fromtimestamp(need(rec["sleepStartTimestampLocal"]) / 1000, UTC),
                           datetime.fromtimestamp(start_ms / 1000, UTC))
    start, end = epoch_ms_to_iso(start_ms, offset), epoch_ms_to_iso(end_ms, offset)
    wake = _day(rec["calendarDate"])  # UNVERIFIED: calendarDate is the wake day (open point 4)
    asleep, awake = num(rec.get("sleepTimeSeconds")), num(rec.get("awakeSleepSeconds"))
    total = None if asleep is None else asleep + (awake or 0.0)
    # light sleep stands in for Apple's core (UNVERIFIED equivalence)
    out.sleep_nights.append(SleepNight(
        wake_date=wake, sleep_start=start, sleep_end=end, in_bed_start=None, in_bed_end=None,
        total_h=_opt_h(total), asleep_h=_opt_h(asleep), core_h=_opt_h(rec.get("lightSleepSeconds")),
        deep_h=_opt_h(rec.get("deepSleepSeconds")), rem_h=_opt_h(rec.get("remSleepSeconds")),
        awake_h=_opt_h(awake), source=NAME))
    out.sample("respiratory_rate", "count/min", end, rec.get("averageRespirationValue"))
    out.vendor("sleep_score", wake, dig(rec, "sleepScores", "overall", "value"))
    out.vendor("sleep_need_min", wake, _sleep_need(rec.get("sleepNeed")))
    # UNVERIFIED: averageSpO2Value inside the sleep summary (the spec lists it under the SpO2 endpoint)
    out.vendor("spo2_sleep_avg", wake, rec.get("averageSpO2Value"))


def _sleep_need(value: Any) -> float | None:
    """UNVERIFIED: sleepNeed as minutes, either a number or an object with 'actual'."""
    return num(value.get("actual")) if isinstance(value, Mapping) else num(value)


def _hrv_summary(summary: Mapping[str, Any], out: Rows, *, day: str) -> None:
    out.vendor("hrv_last_night", day, summary.get("lastNightAvg"))
    out.vendor("hrv_weekly_avg", day, summary.get("weeklyAvg"))
    status = summary.get("status")
    if status is not None:
        out.vendor("hrv_status_code", day, HRV_STATUS[str(status).upper()])


def _hrv_reading(reading: Mapping[str, Any], out: Rows) -> None:
    _, _, ts = _pair_iso(reading["readingTimeGMT"], reading["readingTimeLocal"])
    out.sample("heart_rate_variability_rmssd", "ms", ts, need(reading["hrvValue"]))


def _daily_record(rec: Mapping[str, Any], out: Rows, *, tz: str, missing: list[str]) -> None:
    day = _day(rec["calendarDate"])
    noon = noon_iso(day, tz)
    out.sample("active_energy", "kcal", noon, dig(rec, "calories", "burnedActive"))
    for key, path in _DAILY_VENDOR.items():
        out.vendor(key, day, dig(rec, *path))
    resting = num(dig(rec, "heartRate", "restingValue"))
    if resting is None:
        missing.append(day)
    else:
        out.sample("resting_heart_rate", "count/min", noon, resting)


def _weight_record(rec: Mapping[str, Any], out: Rows, *, tz: str) -> None:
    out.sample("weight_body_mass", "kg", noon_iso(_day(rec["calendarDate"]), tz),
               garmin_weight_kg(need(rec["weight"])))


def _vo2_record(rec: Mapping[str, Any], out: Rows, *, tz: str) -> None:
    generic = rec["generic"]
    out.sample("vo2_max", "ml/kg/min", noon_iso(_day(generic["calendarDate"]), tz),
               need(generic["vo2MaxPreciseValue"]))


def _active_kcal(rec: Mapping[str, Any]) -> float | None:
    """UNVERIFIED: active energy of an activity as calories minus bmrCalories."""
    total, bmr = num(rec.get("calories")), num(rec.get("bmrCalories"))
    return None if total is None or bmr is None else max(0.0, total - bmr)


def _indoor(type_key: str) -> bool:
    return type_key.startswith("indoor_") or "treadmill" in type_key


def workout_from(rec: Mapping[str, Any]) -> tuple[Workout, timedelta]:
    """The activity summary as a Workout without hr_series, plus its UTC offset."""
    start_utc, offset, start = _pair_iso(rec["startTimeGMT"], rec["startTimeLocal"])
    duration = need(rec["duration"])
    type_key = str(dig(rec, "activityType", "typeKey") or "other")
    workout = Workout(
        id=f"{NAME}:{int(rec['activityId'])}", name=type_key, start=start,
        end=iso_at(start_utc + timedelta(seconds=duration), offset), duration_s=duration,
        active_kcal=_active_kcal(rec), hr_avg=num(rec.get("averageHR")), hr_max=num(rec.get("maxHR")),
        hr_series=(), is_indoor=_indoor(type_key), source=NAME,
        raw_json=strip_vendor_raw({k: v for k, v in rec.items() if k not in _RAW_DROP}))
    return workout, offset


def _fit_series(api: _Api, activity_id: int, offset: timedelta, out: Rows) -> tuple[tuple[str, float], ...]:
    body = api.raw(FIT_PATH.format(id=activity_id))
    if body is None:
        return ()
    try:
        points = fit_hr_points(fit_bytes(body))
    except (ValueError, zipfile.BadZipFile):
        out.warnings.append(f"{NAME}:fit_unreadable")
        return ()
    return tuple((iso_at(datetime.fromtimestamp(ts, UTC), offset), float(hr)) for ts, hr in points)


def _activity_record(rec: Mapping[str, Any], out: Rows, *, api: _Api, lo: str, hi: str) -> None:
    workout, offset = workout_from(rec)
    if not lo <= workout.start < hi:
        return
    series = _fit_series(api, int(rec["activityId"]), offset, out)
    out.workouts.append(replace(workout, hr_series=series))


# --------------------------------------------------------------------------
# Fetching one chunk


def _fetch_sleep(api: _Api, days: list[date], rows: Rows) -> None:
    for first, last in spans(days, SLEEP_MAX_DAYS):
        obj = api.json(SLEEP_PATH, {"startDate": first.isoformat(), "endDate": last.isoformat()})
        each(as_list(obj, *_SLEEP_KEYS), _sleep_record, rows, f"{NAME}:sleep")


def _fetch_hrv(api: _Api, days: list[date], rows: Rows) -> None:
    for d in days:
        obj = api.json(HRV_PATH.format(day=d.isoformat()))
        if obj is None:
            continue
        if not isinstance(obj, Mapping):
            raise SchemaError("hrv response is not an object")
        summary = obj.get("hrvSummary")
        if summary is not None:
            each([summary], partial(_hrv_summary, day=d.isoformat()), rows, f"{NAME}:hrv_summary")
        each(obj.get("hrvReadings") or [], _hrv_reading, rows, f"{NAME}:hrv_reading")


def _fetch_heart(api: _Api, day: str, tz: str, rows: Rows) -> None:
    """RHR from dailyHeartRate; its minimum is the last resort and adds a warning."""
    obj = api.json(HEART_PATH, {"date": day})
    if obj is None:
        return
    if not isinstance(obj, Mapping):
        raise SchemaError("dailyHeartRate response is not an object")
    # UNVERIFIED: field names restingHeartRate and minHeartRate of DailyHeartRateDTO
    resting, minimum = num(obj.get("restingHeartRate")), num(obj.get("minHeartRate"))
    if resting is None and minimum is not None:
        rows.warnings.append(f"{NAME}:rhr_min_fallback {day}")
    rows.sample("resting_heart_rate", "count/min", noon_iso(day, tz), resting if resting is not None else minimum)


def _fetch_daily(api: _Api, days: list[date], tz: str, rows: Rows) -> None:
    missing: list[str] = []
    for first, last in spans(days, DAILY_MAX_DAYS):
        obj = api.json(DAILY_PATH, {"startDate": first.isoformat(), "endDate": last.isoformat()})
        each(as_list(obj, *_DAILY_KEYS), partial(_daily_record, tz=tz, missing=missing), rows, f"{NAME}:daily")
    for day in missing:
        _fetch_heart(api, day, tz, rows)


def _fetch_weight(api: _Api, days: list[date], tz: str, rows: Rows) -> None:
    for first, last in spans(days, WEIGHT_MAX_DAYS):
        obj = api.json(WEIGHT_PATH.format(start=first.isoformat(), end=last.isoformat()))
        each(as_list(obj, *_WEIGHT_KEYS), partial(_weight_record, tz=tz), rows, f"{NAME}:weight")


def _fetch_vo2(api: _Api, days: list[date], tz: str, rows: Rows) -> None:
    for first, last in spans(days, VO2_MAX_DAYS):
        obj = api.json(VO2_PATH.format(start=first.isoformat(), end=last.isoformat()))
        each(as_list(obj), partial(_vo2_record, tz=tz), rows, f"{NAME}:vo2max")


def _activity_list(api: _Api, first: date, last: date) -> list:
    """Every activity between the dates; offset paging until a short page."""
    out: list = []
    offset = 0
    while True:
        page = as_list(api.json(ACTIVITIES_PATH, {"startDate": first.isoformat(), "endDate": last.isoformat(),
                                                  "start": str(offset), "limit": str(ACTIVITY_PAGE)}))
        out.extend(page)
        if len(page) < ACTIVITY_PAGE:
            return out
        offset += ACTIVITY_PAGE


def _fetch_activities(api: _Api, days: list[date], start: datetime, end: datetime, rows: Rows) -> None:
    # Every activity of the window gets its FIT again: the window replacement
    # deletes the stored workouts, so an incremental "stop at the last id" would lose them.
    acts = _activity_list(api, days[0], days[-1])
    each(acts, partial(_activity_record, api=api, lo=iso(start), hi=iso(end)), rows, f"{NAME}:activity")


def fetch_chunk(api: _Api, tz: str, start: datetime, end: datetime) -> Rows:
    rows = Rows(NAME, VENDOR_KEYS)
    days = day_range(start, end)
    _fetch_sleep(api, days, rows)
    _fetch_hrv(api, days, rows)
    _fetch_daily(api, days, tz, rows)
    _fetch_weight(api, days, tz, rows)
    _fetch_vo2(api, days, tz, rows)
    _fetch_activities(api, days, start, end, rows)
    return rows


# --------------------------------------------------------------------------
# Adapter


def _key_expiry(options: Mapping[str, Any]) -> float | None:
    """Epoch of 00:00 UTC on config 'key_expires_on' (YYYY-MM-DD), if set."""
    text = options.get("key_expires_on")
    if not text:
        return None
    return datetime.fromisoformat(f"{_day(text)}T00:00:00+00:00").timestamp()


class GarminSource:
    """TrackerSource for the Garmin Connect Personal API."""

    name: ClassVar[str] = NAME
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    auth_kind: ClassVar[str] = "api_key"
    overlap_days: ClassVar[int] = 3
    backfill_chunk_days: ClassVar[int] = 28
    max_history_days: ClassVar[int | None] = None
    vendor_keys: ClassVar[frozenset[str]] = VENDOR_KEYS

    def __init__(self, *, env: Mapping[str, str], options: Mapping[str, Any] | None = None, tz: str = "UTC",
                 transport: Transport = urllib_transport, token_store: TokenStore | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None) -> None:
        key = (env.get(KEY_ENV) or "").strip()
        if not key:
            raise ConfigError(f"{KEY_ENV} is not set")
        self._options = dict(options or {})
        self._tz = tz
        self._transport, self._clock, self._sleep, self._rng = transport, clock, sleep, rng
        self._store = token_store if token_store is not None else default_token_store()
        self._jwt = _Jwt(self._store, key, clock)

    def _http(self) -> HttpClient:
        return HttpClient(self._transport, allow_hosts=self.hosts, user_agent=USER_AGENT, clock=self._clock,
                          sleep=self._sleep, rng=self._rng, max_requests=int(self._options.get("max_requests", 200)))

    def authorize(self, ui: AuthUI, *, manual: bool = False) -> AuthStatus:
        """Mints a JWT from the key in the environment; manual changes nothing for Garmin."""
        token = self._jwt.mint(self._http())
        ui.info(f"Garmin-Token gueltig bis {datetime.fromtimestamp(token.expires_at, UTC).isoformat()}.")
        return self.auth_status()

    def auth_status(self) -> AuthStatus:
        token = self._store.load(NAME)
        key_expires = _key_expiry(self._options)
        key_ok = key_expires is None or key_expires > self._clock()
        detail = ("jwt cached" if token is not None else "no jwt cached") + ("" if key_ok else ", key expired")
        return AuthStatus(ok=key_ok, expires_at=token.expires_at if token else None,
                          refresh_expires_at=key_expires, detail=detail)

    def revoke(self) -> None:
        """Garmin has no revoke call; deleting the key in Connect Web revokes it. Drops the cached JWT."""
        self._store.delete(NAME)

    def poll(self, since_cursor: Mapping[str, Any] | None, *, now: datetime, tz: str,
             backfill_days: int = 0) -> PollResult:
        plan = plan_window(since_cursor, now=local_now(now, tz), overlap_days=self.overlap_days,
                           backfill_days=backfill_days, chunk_days=self.backfill_chunk_days,
                           max_history_days=self.max_history_days)
        api = _Api(self._http(), self._jwt)
        done, complete = run_chunks(plan, partial(fetch_chunk, api, tz))
        return finish(plan, since_cursor, done, complete, api.http.requests_made)

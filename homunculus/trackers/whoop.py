"""WHOOP Developer API v2 poller (docs/TRACKERS.md, docs/TRACKER_CONTRACT.md).

OAuth2 authorization code with a confidential client (HOMUNCULUS_WHOOP_CLIENT_ID,
HOMUNCULUS_WHOOP_CLIENT_SECRET). Refresh tokens rotate and a refresh voids the
old access token, so every refresh runs under base.TokenManager's lock and the
new token is saved before the next API call.

Per chunk: sleep, recovery, cycle and workout lists (limit 25, nextToken
paging). Only SCORED records produce rows. Recovery joins its sleep by
sleep_id; naps are skipped. HRV is one nightly value (field 'nightly'), and
WHOOP kilojoules never become active_kcal.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from homunculus.ingest import ConfigError
from homunculus.metrics import NIGHTLY_FIELD
from homunculus.store import SleepNight, Workout
from homunculus.trackers.base import (
    AuthRequired,
    AuthStatus,
    AuthUI,
    HttpClient,
    OAuthConfig,
    PollResult,
    SchemaError,
    TokenManager,
    TokenStore,
    Transport,
    UpstreamError,
    build_authorize_url,
    default_secrets_root,
    default_token_store,
    exchange_code,
    new_state,
    parse_pasted_redirect,
    plan_window,
    strip_vendor_raw,
    urllib_transport,
    wait_for_code,
)
from homunculus.trackers.pollkit import (
    Rows,
    authed_get,
    each,
    finish,
    iso_at,
    local_now,
    need,
    num,
    ok_json,
    parse_offset,
    run_chunks,
    utc_from_iso,
    utc_z,
)

NAME = "whoop"
HOST = "api.prod.whoop.com"
API = f"https://{HOST}/developer"
AUTHORIZE_URL = f"https://{HOST}/oauth/oauth2/auth"
TOKEN_URL = f"https://{HOST}/oauth/oauth2/token"
CLIENT_ID_ENV = "HOMUNCULUS_WHOOP_CLIENT_ID"
CLIENT_SECRET_ENV = "HOMUNCULUS_WHOOP_CLIENT_SECRET"
USER_AGENT = "homunculus-trackers/1"
# Only what the mapping reads. TRACKERS.md also lists read:profile and
# read:body_measurement; body weight is a profile value homunculus skips.
SCOPES = ("read:recovery", "read:cycles", "read:workout", "read:sleep", "offline")
STATE_LEN = 8

SLEEP_PATH = "/v2/activity/sleep"
RECOVERY_PATH = "/v2/recovery"
CYCLE_PATH = "/v2/cycle"
WORKOUT_PATH = "/v2/activity/workout"
REVOKE_PATH = "/v2/user/access"
PAGE_LIMIT = "25"
# Lists filter by record start, so a sleep that began before the window can
# still own rows inside it; ask one day earlier and let the clip cut.
REQUEST_PAD = timedelta(days=1)

SCORED = "SCORED"
# UNVERIFIED: PENDING_SCORE and UNSCORABLE are from memory; anything unknown is skipped with a warning
KNOWN_STATES = frozenset({SCORED, "PENDING_SCORE", "UNSCORABLE"})

VENDOR_KEYS = frozenset(f"{NAME}.{k}" for k in (
    "recovery", "day_strain", "workout_strain_max", "cycle_kcal_total", "sleep_performance",
    "sleep_consistency", "sleep_efficiency", "calibrating", "spo2_sleep_avg", "disturbances",
))
_SLEEP_VENDOR = (
    ("sleep_performance", "sleep_performance_percentage"),
    ("sleep_consistency", "sleep_consistency_percentage"),
    ("sleep_efficiency", "sleep_efficiency_percentage"),
)
_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


# --------------------------------------------------------------------------
# Units


def kj_to_kcal(x: float) -> float:
    return x / 4.184


def ms_to_h(x: float) -> float:
    """WHOOP *_milli durations to hours."""
    return x / 3_600_000


def spo2_percent(x: float) -> float:
    """Fractions become percent; ValueError outside 50-100 after conversion."""
    value = x * 100 if 0 < x <= 1.0 else x
    if not 50 <= value <= 100:
        raise ValueError(f"SpO2 out of range: {x!r}")
    return value


def _spo2_or_none(value: Any) -> float | None:
    v = num(value)
    if v is None:
        return None
    try:
        return spo2_percent(v)
    except ValueError:
        return None


def _opt_h(milli: Any) -> float | None:
    value = num(milli)
    return None if value is None else ms_to_h(value)


# --------------------------------------------------------------------------
# Records


@dataclass(frozen=True)
class _Span:
    start: str  # local ISO with the record's offset
    end: str
    mid: str
    wake: str  # local date of end
    seconds: float


def _span(rec: Mapping[str, Any]) -> _Span:
    offset = parse_offset(rec["timezone_offset"])
    start, end = utc_from_iso(rec["start"]), utc_from_iso(rec["end"])
    if end < start:
        raise ValueError("record ends before it starts")
    wake = end.astimezone(timezone(offset)).date().isoformat()
    return _Span(iso_at(start, offset), iso_at(end, offset), iso_at(start + (end - start) / 2, offset), wake,
                 (end - start).total_seconds())


def _score(rec: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The score of a SCORED record, None while not scored; ValueError for an unknown state."""
    state = rec["score_state"]
    if state not in KNOWN_STATES:
        raise ValueError(f"unknown score_state {state!r}")
    if state != SCORED:
        return None
    score = rec["score"]
    if not isinstance(score, Mapping):
        raise TypeError("score is not an object")
    return score


def _record_id(value: Any) -> str:
    text = str(value)
    if not _ID_RE.match(text):
        raise ValueError("unexpected record id")
    return text


def sleep_index(sleeps: list) -> dict[str, _Span]:
    """sleep id -> times of every non-nap sleep with valid times, for the recovery join."""
    out: dict[str, _Span] = {}
    for rec in sleeps:
        try:
            if not rec.get("nap"):
                out[_record_id(rec["id"])] = _span(rec)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue  # _sleep_record warns about the same record
    return out


def _sleep_record(rec: Mapping[str, Any], out: Rows) -> None:
    if rec.get("nap"):
        return
    score = _score(rec)
    if score is None:
        return
    span, stages = _span(rec), score["stage_summary"]
    light, sws, rem = (ms_to_h(need(stages[k])) for k in (
        "total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli", "total_rem_sleep_time_milli"))
    # light sleep stands in for Apple's core (UNVERIFIED equivalence)
    out.sleep_nights.append(SleepNight(
        wake_date=span.wake, sleep_start=span.start, sleep_end=span.end, in_bed_start=None, in_bed_end=None,
        total_h=_opt_h(stages.get("total_in_bed_time_milli")), asleep_h=light + sws + rem, core_h=light,
        deep_h=sws, rem_h=rem, awake_h=_opt_h(stages.get("total_awake_time_milli")), source=NAME))
    # UNVERIFIED: respiratory_rate is in breaths per minute (the docs state no unit)
    out.sample("respiratory_rate", "count/min", span.end, score.get("respiratory_rate"))
    for key, src in _SLEEP_VENDOR:
        out.vendor(key, span.wake, score.get(src))
    out.vendor("disturbances", span.wake, stages.get("disturbance_count"))


def _recovery_record(rec: Mapping[str, Any], out: Rows, *, nights: Mapping[str, _Span]) -> None:
    score = _score(rec)
    if score is None:
        return
    span = nights[_record_id(rec["sleep_id"])]  # KeyError: nap or sleep outside the request
    out.sample("heart_rate_variability_rmssd", "ms", span.mid, score.get("hrv_rmssd_milli"), NIGHTLY_FIELD)
    out.sample("resting_heart_rate", "count/min", span.end, score.get("resting_heart_rate"))
    out.sample("skin_temperature", "°C", span.end, score.get("skin_temp_celsius"))
    out.vendor("recovery", span.wake, score.get("recovery_score"))
    calibrating = score.get("user_calibrating")
    out.vendor("calibrating", span.wake, None if calibrating is None else float(bool(calibrating)))
    out.vendor("spo2_sleep_avg", span.wake, _spo2_or_none(score.get("spo2_percentage")))


def _steps(rec: Mapping[str, Any], score: Mapping[str, Any]) -> float | None:
    """UNVERIFIED: step_count (added 2026-09-23) on the cycle or inside its score."""
    top = num(rec.get("step_count"))
    return top if top is not None else num(score.get("step_count"))


def _cycle_record(rec: Mapping[str, Any], out: Rows, *, until: datetime) -> None:
    """Day scores dated by the local date of the cycle end (the open cycle: of until)."""
    score = _score(rec)
    if score is None:
        return
    offset = parse_offset(rec["timezone_offset"])
    end = utc_from_iso(rec["end"]) if rec.get("end") else None
    day = (end or until).astimezone(timezone(offset)).date().isoformat()
    out.vendor("day_strain", day, score.get("strain"))
    kj = num(score.get("kilojoule"))  # total energy, never active_kcal
    out.vendor("cycle_kcal_total", day, None if kj is None else kj_to_kcal(kj))
    if end is not None:  # an open cycle has partial steps
        out.sample("step_count", "count", iso_at(end, offset), _steps(rec, score))


def _workout_record(rec: Mapping[str, Any], out: Rows, *, strain: dict[str, float]) -> None:
    score = _score(rec)
    if score is None:
        return
    span = _span(rec)
    name = str(rec.get("sport_name") or "activity")
    out.workouts.append(Workout(
        id=f"{NAME}:{_record_id(rec['id'])}", name=name, start=span.start, end=span.end,
        duration_s=span.seconds, active_kcal=None, hr_avg=num(score.get("average_heart_rate")),
        hr_max=num(score.get("max_heart_rate")), hr_series=(), is_indoor=None, source=NAME,
        raw_json=strip_vendor_raw({"sport_name": name, "score": score})))
    value, day = num(score.get("strain")), span.start[:10]
    if value is not None:
        strain[day] = max(value, strain.get(day, value))


# --------------------------------------------------------------------------
# HTTP


def _page(obj: Any) -> tuple[list, str | None]:
    if not isinstance(obj, Mapping) or not isinstance(obj.get("records"), list):
        raise SchemaError("list response without records")
    # UNVERIFIED: the response names the cursor next_token (query parameter: nextToken)
    token = obj.get("next_token") or obj.get("nextToken")
    return obj["records"], (str(token) if token else None)


@dataclass
class _Api:
    http: HttpClient
    tokens: TokenManager

    def _headers(self, force: bool) -> dict[str, str]:
        token = self.tokens.force_refresh() if force else self.tokens.access_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def collect(self, path: str, params: Mapping[str, str]) -> list:
        """Every record of a list endpoint, following nextToken."""
        records: list = []
        token: str | None = None
        seen: set[str] = set()
        while True:
            query = {**params, "limit": PAGE_LIMIT, **({"nextToken": token} if token else {})}
            page, token = _page(ok_json(authed_get(self.http, API + path, query, self._headers)))
            records.extend(page)
            if token is None:
                return records
            if token in seen:
                raise SchemaError("repeated nextToken")
            seen.add(token)


def fetch_chunk(api: _Api, start: datetime, end: datetime) -> Rows:
    # UNVERIFIED: start/end accept ISO-8601 UTC with milliseconds and 'Z'
    params = {"start": utc_z(start - REQUEST_PAD), "end": utc_z(end)}
    sleeps = api.collect(SLEEP_PATH, params)
    recoveries = api.collect(RECOVERY_PATH, params)
    cycles = api.collect(CYCLE_PATH, params)
    workouts = api.collect(WORKOUT_PATH, params)
    rows = Rows(NAME, VENDOR_KEYS)
    each(sleeps, _sleep_record, rows, f"{NAME}:sleep")
    each(recoveries, partial(_recovery_record, nights=sleep_index(sleeps)), rows, f"{NAME}:recovery")
    each(cycles, partial(_cycle_record, until=end), rows, f"{NAME}:cycle")
    strain: dict[str, float] = {}
    each(workouts, partial(_workout_record, strain=strain), rows, f"{NAME}:workout")
    for day, value in sorted(strain.items()):
        rows.vendor("workout_strain_max", day, value)
    return rows


# --------------------------------------------------------------------------
# Adapter

_DEFAULT_LOCKS = object()


class WhoopSource:
    """TrackerSource for the WHOOP Developer API v2."""

    name: ClassVar[str] = NAME
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    auth_kind: ClassVar[str] = "oauth2"
    overlap_days: ClassVar[int] = 3
    backfill_chunk_days: ClassVar[int] = 90
    # UNVERIFIED: WHOOP documents no history cap
    max_history_days: ClassVar[int | None] = None
    vendor_keys: ClassVar[frozenset[str]] = VENDOR_KEYS

    def __init__(self, *, env: Mapping[str, str], options: Mapping[str, Any] | None = None, tz: str = "UTC",
                 transport: Transport = urllib_transport, token_store: TokenStore | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None, lock_dir: Any = _DEFAULT_LOCKS,
                 wait: Callable[..., str] = wait_for_code) -> None:
        client_id, secret = (env.get(k) or "" for k in (CLIENT_ID_ENV, CLIENT_SECRET_ENV))
        if not client_id or not secret:
            raise ConfigError(f"{CLIENT_ID_ENV} and {CLIENT_SECRET_ENV} must be set")
        self._options = dict(options or {})
        self._tz = tz
        self._transport, self._clock, self._sleep, self._rng, self._wait = transport, clock, sleep, rng, wait
        self._store = token_store if token_store is not None else default_token_store()
        self._lock_dir: Path | None = default_secrets_root(env) / "locks" if lock_dir is _DEFAULT_LOCKS else lock_dir
        # UNVERIFIED: whether WHOOP accepts the loopback redirect (open point 2); override with redirect_uri
        self._cfg = OAuthConfig(
            authorize_url=AUTHORIZE_URL, token_url=TOKEN_URL, client_id=client_id, client_secret=secret,
            scopes=SCOPES, use_pkce=False, client_auth="body", state_len=STATE_LEN,
            **({"redirect_uri": self._options["redirect_uri"]} if self._options.get("redirect_uri") else {}))

    def _http(self) -> HttpClient:
        return HttpClient(self._transport, allow_hosts=self.hosts, user_agent=USER_AGENT, clock=self._clock,
                          sleep=self._sleep, rng=self._rng, max_requests=int(self._options.get("max_requests", 200)))

    def _tokens(self, http: HttpClient) -> TokenManager:
        return TokenManager(NAME, self._store, http, self._cfg, clock=self._clock, lock_dir=self._lock_dir)

    def _code(self, ui: AuthUI, state: str, manual: bool) -> str:
        url = build_authorize_url(self._cfg, state, None)
        if not manual:
            return self._wait(self._cfg, state, on_ready=lambda: ui.show_url(url))
        ui.show_url(url)
        return parse_pasted_redirect(ui.ask("Weiterleitungs-URL aus dem Browser einfuegen:"), state)

    def authorize(self, ui: AuthUI, *, manual: bool = False) -> AuthStatus:
        code = self._code(ui, new_state(self._cfg.state_len), manual)
        token = exchange_code(self._http(), self._cfg, code, None, self._clock())
        self._store.save(NAME, token)
        ui.info("WHOOP ist verbunden.")
        return self.auth_status()

    def auth_status(self) -> AuthStatus:
        token = self._store.load(NAME)
        if token is None:
            return AuthStatus(ok=False, expires_at=None, refresh_expires_at=None, detail="no token")
        ok = bool(token.refresh_token) or token.expires_at > self._clock()
        return AuthStatus(ok=ok, expires_at=token.expires_at, refresh_expires_at=token.refresh_expires_at,
                          detail="token present" if ok else "token expired")

    def revoke(self) -> None:
        """DELETE /v2/user/access, then drop the local token even if the call fails."""
        http = self._http()
        try:
            bearer = self._tokens(http).access_token()
            resp = http.request("DELETE", API + REVOKE_PATH, headers={"Authorization": f"Bearer {bearer}"})
            if resp.status not in (200, 204, 401):
                raise UpstreamError(f"http_{resp.status}")
        except AuthRequired:
            pass  # nothing valid left to revoke
        finally:
            self._store.delete(NAME)

    def poll(self, since_cursor: Mapping[str, Any] | None, *, now: datetime, tz: str,
             backfill_days: int = 0) -> PollResult:
        plan = plan_window(since_cursor, now=local_now(now, tz), overlap_days=self.overlap_days,
                           backfill_days=backfill_days, chunk_days=self.backfill_chunk_days,
                           max_history_days=self.max_history_days)
        http = self._http()
        done, complete = run_chunks(plan, partial(fetch_chunk, _Api(http, self._tokens(http))))
        return finish(plan, since_cursor, done, complete, http.requests_made)

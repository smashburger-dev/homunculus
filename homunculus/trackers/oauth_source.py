"""Shared base for the OAuth2 polling adapters (google, withings, polar).

Holds what the three adapters do identically: secrets from the environment,
the authorization-code flow (loopback or pasted redirect), auth status, one
refresh after a 401, the chunked poll loop over ``plan_window`` and the row
collector that keeps every row inside the replaced window.

Conversions that more than one adapter needs live here as small pure
functions; vendor-specific ones stay in the vendor module.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from homunculus.ingest import ConfigError
from homunculus.store import DailyValue, Sample, SleepNight, SleepSegment, Workout
from homunculus.trackers.base import (
    AuthRequired,
    AuthStatus,
    AuthUI,
    HttpClient,
    OAuthConfig,
    PollResult,
    RateLimited,
    Response,
    Token,
    TokenManager,
    TokenStore,
    TrackerBatch,
    Transport,
    UpstreamError,
    WindowPlan,
    build_authorize_url,
    cursor_after,
    decode_json,
    default_secrets_root,
    exchange_code,
    iso,
    new_state,
    parse_pasted_redirect,
    pkce_pair,
    plan_window,
    wait_for_code,
)

USER_AGENT = "homunculus-tracker/1"
DEFAULT_REDIRECT_PORT = 8765
PASTE_PROMPT = "Vollstaendige Weiterleitungs-URL aus dem Browser einfuegen: "

CodeWaiter = Callable[..., str]


# --------------------------------------------------------------------------
# Pure helpers


def require_env(env: Mapping[str, str], *keys: str) -> tuple[str, ...]:
    """The values of keys; ConfigError naming the first missing one (never its value)."""
    for key in keys:
        if not env.get(key):
            raise ConfigError(f"missing secret {key}")
    return tuple(env[k] for k in keys)


def redirect_uri(options: Mapping[str, Any]) -> str:
    port = int(options.get("redirect_port") or DEFAULT_REDIRECT_PORT)
    return f"http://127.0.0.1:{port}/callback"


def s_to_h(x: float) -> float:
    """Seconds to hours (Polar, Withings durations)."""
    return float(x) / 3600.0


def spo2_percent(x: float) -> float:
    """Fractions become percent; ValueError outside 50-100."""
    value = float(x) * 100.0 if 0 < float(x) <= 1.0 else float(x)
    if not 50.0 <= value <= 100.0:
        raise ValueError(f"spo2 out of range: {x!r}")
    return value


def number(value: Any) -> float | None:
    """A finite JSON number, else None (bools are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def day_ceil(ts: str) -> str:
    """First date whose 00:00 is at or after ts; same rule as the store's date columns."""
    if len(ts) == 10:
        return Date.fromisoformat(ts).isoformat()
    dt = datetime.fromisoformat(ts)
    day = dt.date() if dt.time() == datetime.min.time() else dt.date() + timedelta(days=1)
    return day.isoformat()


def at_noon(day: str, tz: str) -> str:
    """'<day>T12:00:00<offset>' in tz; daily values without a time of day are dated here."""
    d = Date.fromisoformat(day)
    return iso(datetime(d.year, d.month, d.day, 12, tzinfo=ZoneInfo(tz)))


def midpoint(start: str, end: str) -> str:
    a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    return iso(a + (b - a) / 2)


def hours_between(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600.0


def stage_hours(segments: list[SleepSegment]) -> dict[str, float]:
    out: dict[str, float] = {}
    for seg in segments:
        out[seg.stage] = out.get(seg.stage, 0.0) + hours_between(seg.start, seg.end)
    return out


# --------------------------------------------------------------------------
# Row collector


@dataclass
class Rows:
    """Rows of one poll for one source; ``batch`` keeps only those inside the window."""

    name: str
    vendor_keys: frozenset[str]
    samples: list[Sample] = field(default_factory=list)
    segments: list[SleepSegment] = field(default_factory=list)
    nights: list[SleepNight] = field(default_factory=list)
    workouts: list[Workout] = field(default_factory=list)
    scores: list[DailyValue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True

    def sample(self, metric: str, unit: str | None, ts: str, fld: str, value: float) -> None:
        self.samples.append(Sample(metric, unit, ts, fld, float(value), self.name))

    def score(self, day: str, key: str, value: float) -> None:
        full = f"{self.name}.{key}"
        if full not in self.vendor_keys:
            raise ValueError(f"vendor key {full!r} is not declared")
        self.scores.append(DailyValue(day, full, float(value), self.name, 1, "ok"))

    def warn(self, text: str) -> None:
        self.warnings.append(f"{self.name}:{text}")

    def extend(self, other: Rows) -> None:
        for attr in ("samples", "segments", "nights", "workouts", "scores", "warnings"):
            getattr(self, attr).extend(getattr(other, attr))
        self.complete = self.complete and other.complete

    def batch(self, since: str, until: str) -> TrackerBatch:
        lo, hi = day_ceil(since), day_ceil(until)
        return TrackerBatch(
            samples=_inside(self.samples, "ts", since, until),
            sleep_segments=_inside(self.segments, "start", since, until),
            sleep_nights=_inside(self.nights, "wake_date", lo, hi),
            workouts=_inside(self.workouts, "start", since, until),
            daily_vendor_scores=_inside(self.scores, "date", lo, hi),
        )


def _inside(rows: list[Any], col: str, lo: str, hi: str) -> tuple[Any, ...]:
    """Rows with lo <= row.col < hi, compared as strings like the store does."""
    return tuple(r for r in rows if lo <= getattr(r, col) < hi)


# --------------------------------------------------------------------------
# Adapter base


class OAuthSource:
    """TrackerSource for vendors with an OAuth2 authorization-code flow.

    Subclasses set the ClassVars, ``oauth_config`` and ``fetch_chunk``; they
    may override ``needs_refresh``, ``decode``, ``after_authorize``,
    ``begin_poll`` and ``cursor_extra``.
    """

    name: ClassVar[str]
    hosts: ClassVar[frozenset[str]]
    api_hosts: ClassVar[frozenset[str]]  # hosts requests go to; the authorize host is browser-only
    auth_kind: ClassVar[str] = "oauth2"
    overlap_days: ClassVar[int] = 3
    backfill_chunk_days: ClassVar[int] = 28
    max_history_days: ClassVar[int | None] = None
    vendor_keys: ClassVar[frozenset[str]]
    secret_env: ClassVar[tuple[str, str]]
    allow_manual: ClassVar[bool] = True

    def __init__(self, *, env: Mapping[str, str], options: Mapping[str, Any], tz: str, transport: Transport,
                 token_store: TokenStore, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, code_waiter: CodeWaiter = wait_for_code,
                 lock_dir: Path | None = None) -> None:
        client_id, client_secret = require_env(env, *self.secret_env)
        self.options, self.tz = dict(options or {}), tz
        ZoneInfo(tz)  # fail at startup on an unknown zone
        self._clock, self._store, self._waiter = clock, token_store, code_waiter
        self.http = HttpClient(transport, allow_hosts=self.api_hosts, user_agent=USER_AGENT, clock=clock,
                               sleep=sleep)
        self.oauth = self.oauth_config(client_id, client_secret, redirect_uri(self.options))
        locks = lock_dir if lock_dir is not None else default_secrets_root(env) / "locks"
        self.tokens = TokenManager(self.name, token_store, self.http, self.oauth, clock=clock, lock_dir=locks)

    # -- hooks ---------------------------------------------------------------

    def oauth_config(self, client_id: str, client_secret: str, redirect: str) -> OAuthConfig:
        raise NotImplementedError

    def fetch_chunk(self, rows: Rows, lo: datetime, hi: datetime, *, tz: str, regular: bool,
                    cursor: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def after_authorize(self, token: Token) -> None:
        """Runs after the new token is saved (Polar registers the user here)."""

    def begin_poll(self, cursor: Mapping[str, Any], now: datetime, rows: Rows) -> None:
        """Runs once before the chunks of a poll."""

    def cursor_extra(self, plan: WindowPlan, complete: bool) -> dict[str, Any]:
        return {}

    def needs_refresh(self, resp: Response) -> bool:
        return resp.status == 401

    def decode(self, resp: Response) -> Any:
        if resp.status in (401, 403):
            raise AuthRequired(f"http_{resp.status}")
        if not 200 <= resp.status < 300:
            raise UpstreamError(f"http_{resp.status}")
        return decode_json(resp.body)

    # -- auth ----------------------------------------------------------------

    def authorize(self, ui: AuthUI, *, manual: bool = False) -> AuthStatus:
        if manual and not self.allow_manual:
            raise ValueError(f"{self.name}: manual mode is not supported, use the loopback redirect")
        verifier, challenge = pkce_pair() if self.oauth.use_pkce else (None, None)
        state = new_state(self.oauth.state_len)
        url = build_authorize_url(self.oauth, state, challenge)
        code = self._code(ui, url, state, manual)
        token = exchange_code(self.http, self.oauth, code, verifier, self._clock())
        self._store.save(self.name, token)
        self.after_authorize(token)
        return self.auth_status()

    def _code(self, ui: AuthUI, url: str, state: str, manual: bool) -> str:
        if manual:
            ui.show_url(url)
            return parse_pasted_redirect(ui.ask(PASTE_PROMPT), state)
        return self._waiter(self.oauth, state, on_ready=lambda: ui.show_url(url))

    def auth_status(self) -> AuthStatus:
        token = self._store.load(self.name)
        if token is None:
            return AuthStatus(False, None, None, "no token")
        now = self._clock()
        refreshable = bool(token.refresh_token) and (token.refresh_expires_at or math.inf) > now
        usable = token.expires_at > now or refreshable
        expires = None if math.isinf(token.expires_at) else token.expires_at
        return AuthStatus(usable, expires, token.refresh_expires_at, "ok" if usable else "expired")

    def revoke(self) -> None:
        self._store.delete(self.name)

    # -- requests ------------------------------------------------------------

    def _send(self, method: str, url: str, access: str, kw: Mapping[str, Any]) -> Response:
        return self.http.request(method, url, headers={"Authorization": f"Bearer {access}"}, **kw)

    def call(self, method: str, url: str, **kw: Any) -> Any:
        """Authorized request; one refresh when ``needs_refresh``; ``decode`` of the answer."""
        resp = self._send(method, url, self.tokens.access_token(), kw)
        if self.needs_refresh(resp):
            resp = self._send(method, url, self.tokens.force_refresh(), kw)
        return self.decode(resp)

    # -- poll ----------------------------------------------------------------

    def new_rows(self) -> Rows:
        return Rows(self.name, self.vendor_keys)

    def poll(self, since_cursor: Mapping[str, Any] | None, *, now: datetime, tz: str,
             backfill_days: int = 0) -> PollResult:
        cursor = dict(since_cursor or {})
        self.http.requests_made = 0
        plan = plan_window(cursor, now=now.astimezone(ZoneInfo(tz)), overlap_days=self.overlap_days,
                           backfill_days=backfill_days, chunk_days=self.backfill_chunk_days,
                           max_history_days=self.max_history_days)
        rows = self.new_rows()
        self.begin_poll(cursor, now, rows)
        done = self._run_chunks(plan, rows, tz, cursor)
        since, until = plan.window(done)
        complete = done == len(plan.chunks) and rows.complete
        new_cursor = cursor_after(plan, done, cursor, **self.cursor_extra(plan, complete))
        return PollResult(rows.batch(since, until), (since, until), new_cursor, complete,
                          self.http.requests_made, tuple(rows.warnings))

    def _run_chunks(self, plan: WindowPlan, rows: Rows, tz: str, cursor: Mapping[str, Any]) -> int:
        """Chunks newest first; a rate limit after the first one ends the run early."""
        done = 0
        for lo, hi in plan.chunks:
            part = self.new_rows()
            try:
                self.fetch_chunk(part, lo, hi, tz=tz, regular=plan.regular and done == 0, cursor=cursor)
            except RateLimited:
                if done == 0:
                    raise
                rows.warn(f"cut_short {iso(lo)}")
                break
            rows.extend(part)
            done += 1
        return done

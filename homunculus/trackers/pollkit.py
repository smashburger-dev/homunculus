"""Helpers the tracker adapters share: row accumulation, window clipping, the chunk loop, offsets.

Every function is pure apart from the HTTP calls an adapter passes in. The
poll loop runs the chunks of a ``base.WindowPlan`` newest first and stops
cleanly when the request budget or a rate limit cuts it short after at least
one complete chunk; the cursor then covers only the finished chunks.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from itertools import chain
from typing import Any
from zoneinfo import ZoneInfo

from homunculus.store import DailyValue, Sample
from homunculus.trackers.base import (
    AuthRequired,
    HttpClient,
    PollResult,
    RateLimited,
    Response,
    SchemaError,
    TrackerBatch,
    UpstreamError,
    WindowPlan,
    cursor_after,
    decode_json,
    iso,
)

UTC = timezone.utc
SKIPPABLE = (KeyError, TypeError, ValueError, AttributeError)

_BATCH_ATTRS = ("samples", "sleep_segments", "sleep_nights", "workouts", "events", "daily_vendor_scores")
# (TrackerBatch attribute, row column, is a date column) as Store.replace_source_window compares them
_CLIP = (
    ("samples", "ts", False), ("sleep_segments", "start", False), ("sleep_nights", "wake_date", True),
    ("workouts", "start", False), ("events", "start", False), ("daily_vendor_scores", "date", True),
)


# --------------------------------------------------------------------------
# Values


def num(value: Any) -> float | None:
    """Finite float or None; booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def need(value: Any) -> float:
    """num() that raises ValueError for a missing or non-numeric value."""
    out = num(value)
    if out is None:
        raise ValueError("number required")
    return out


def dig(obj: Any, *keys: str) -> Any:
    """Nested Mapping lookup; None as soon as a level is missing."""
    for key in keys:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    return obj


def as_list(obj: Any, *keys: str) -> list:
    """A top-level list, or the list under one of keys; None counts as empty; else SchemaError."""
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, Mapping):
        for key in keys:
            if isinstance(obj.get(key), list):
                return obj[key]
    raise SchemaError("unexpected top-level shape")


# --------------------------------------------------------------------------
# Time


def local_now(now: datetime, tz: str) -> datetime:
    """now in the owner's zone, so windows carry the same offset as the rows."""
    if now.tzinfo is None:
        raise ValueError("now must carry an offset")
    return now.astimezone(ZoneInfo(tz)).replace(microsecond=0)


def parse_offset(text: str) -> timedelta:
    """'+01:00' -> timedelta(hours=1); ValueError for anything else."""
    offset = datetime.fromisoformat(f"2000-01-01T00:00:00{text}").utcoffset()
    if offset is None:
        raise ValueError(f"not an offset: {text!r}")
    return offset


def iso_at(utc_dt: datetime, offset: timedelta) -> str:
    """An aware datetime shown at a fixed offset: '2026-11-20T06:42:00+01:00'."""
    return utc_dt.astimezone(timezone(offset)).isoformat(timespec="seconds")


def utc_from_iso(text: str) -> datetime:
    """Aware UTC datetime from ISO-8601 with an offset or 'Z'."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without offset: {text!r}")
    return dt.astimezone(UTC)


def utc_z(dt: datetime) -> str:
    """'2026-11-20T05:42:00.000Z'."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def noon_iso(day: str, tz: str) -> str:
    """Local noon of a calendar date, the timestamp of once-per-day samples."""
    local = datetime.combine(date.fromisoformat(day), time(12), tzinfo=ZoneInfo(tz))
    return local.isoformat(timespec="seconds")


def day_range(start: datetime, end: datetime) -> list[date]:
    """Every local calendar date from start's to end's, both included."""
    first, last = start.date(), end.date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def spans(days: Sequence[date], size: int) -> list[tuple[date, date]]:
    """Consecutive (first, last) date pairs of at most size days, for range endpoints."""
    return [(days[i], days[min(i + size, len(days)) - 1]) for i in range(0, len(days), size)]


# --------------------------------------------------------------------------
# Rows


@dataclass
class Rows:
    """Mutable row accumulator for one chunk; ``batch()`` freezes it."""

    name: str
    vendor_keys: frozenset[str]
    samples: list = field(default_factory=list)
    sleep_nights: list = field(default_factory=list)
    workouts: list = field(default_factory=list)
    daily_vendor_scores: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def sample(self, metric: str, unit: str, ts: str, value: Any, field_kind: str = "qty") -> None:
        """Adds a Sample unless value is missing or not a number."""
        v = num(value)
        if v is not None:
            self.samples.append(Sample(metric, unit, ts, field_kind, v, self.name))

    def vendor(self, key: str, day: str, value: Any) -> None:
        """Adds a '<name>.<key>' DailyValue unless value is missing."""
        full = f"{self.name}.{key}"
        if full not in self.vendor_keys:
            raise AssertionError(f"undeclared vendor key {full}")
        v = num(value)
        if v is not None:
            self.daily_vendor_scores.append(DailyValue(day, full, v, self.name, 1, "ok"))

    def fresh(self) -> Rows:
        return Rows(self.name, self.vendor_keys)

    def extend(self, other: Rows) -> None:
        for attr in ("samples", "sleep_nights", "workouts", "daily_vendor_scores", "warnings"):
            getattr(self, attr).extend(getattr(other, attr))

    def batch(self) -> TrackerBatch:
        return TrackerBatch(samples=tuple(self.samples), sleep_nights=tuple(self.sleep_nights),
                            workouts=tuple(self.workouts), daily_vendor_scores=tuple(self.daily_vendor_scores))


def each(records: Iterable[Any], mapper: Callable[[Any, Rows], None], rows: Rows, label: str) -> None:
    """Maps every record into rows; a record that fails adds only the warning '<label>_skipped'."""
    for record in records:
        part = rows.fresh()
        try:
            mapper(record, part)
        except SKIPPABLE:
            rows.warnings.append(f"{label}_skipped")
            continue
        rows.extend(part)


# --------------------------------------------------------------------------
# Windows


def day_ceil(ts: str) -> str:
    """First date whose midnight is at or after ts, as Store compares date columns."""
    dt = datetime.fromisoformat(ts)
    return (dt.date() if dt.time() == time(0) else dt.date() + timedelta(days=1)).isoformat()


def _inside(rows: Iterable[Any], col: str, lo: str, hi: str) -> tuple:
    return tuple(r for r in rows if lo <= getattr(r, col) < hi)


def clip(batch: TrackerBatch, since: str, until: str) -> TrackerBatch:
    """Only the rows Store.replace_source_window accepts for [since, until)."""
    bounds = {False: (since, until), True: (day_ceil(since), day_ceil(until))}
    return TrackerBatch(**{attr: _inside(getattr(batch, attr), col, *bounds[is_date])
                           for attr, col, is_date in _CLIP})


def merge(batches: Iterable[TrackerBatch]) -> TrackerBatch:
    """Concatenation without exact duplicates."""
    items = list(batches)
    return TrackerBatch(**{attr: tuple(dict.fromkeys(chain.from_iterable(getattr(b, attr) for b in items)))
                           for attr in _BATCH_ATTRS})


Chunk = tuple[TrackerBatch, list[str]]


def run_chunks(plan: WindowPlan, fetch: Callable[[datetime, datetime], Rows]) -> tuple[list[Chunk], bool]:
    """Fetches the plan's chunks newest first; (finished chunks, complete).

    A RateLimited during the first chunk propagates; later it ends the run
    with complete=False and the finished chunks kept.
    """
    done: list[Chunk] = []
    for start, end in plan.chunks:
        try:
            rows = fetch(start, end)
        except RateLimited:
            if not done:
                raise
            return done, False
        done.append((clip(rows.batch(), iso(start), iso(end)), rows.warnings))
    return done, True


def finish(plan: WindowPlan, cursor: Mapping[str, Any] | None, done: list[Chunk], complete: bool,
           requests_made: int) -> PollResult:
    batch = merge(b for b, _ in done)
    warnings = tuple(w for _, ws in done for w in ws)
    return PollResult(batch=batch, window=plan.window(len(done)), new_cursor=cursor_after(plan, len(done), cursor),
                      complete=complete, requests_made=requests_made, warnings=warnings)


# --------------------------------------------------------------------------
# HTTP


def authed_get(http: HttpClient, url: str, params: Mapping[str, str] | None,
               headers_for: Callable[[bool], Mapping[str, str]]) -> Response:
    """GET with the adapter's auth header; on 401 one renewed header, then AuthRequired."""
    resp = http.request("GET", url, params=params, headers=headers_for(False))
    if resp.status == 401:
        resp = http.request("GET", url, params=params, headers=headers_for(True))
    if resp.status in (401, 403):
        raise AuthRequired(f"http_{resp.status}")
    return resp


def ok_json(resp: Response, *, empty: frozenset[int] = frozenset()) -> Any:
    """Decoded JSON of a 2xx response; None for an empty body or a status in empty."""
    if resp.status in empty:
        return None
    if not 200 <= resp.status < 300:
        raise UpstreamError(f"http_{resp.status}")
    return decode_json(resp.body) if resp.body.strip() else None

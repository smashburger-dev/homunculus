"""Poll orchestration used by the CLI and Pineal (docs/TRACKER_CONTRACT.md, section run.py).

Adapters only fetch. This module loads the cursor from the store, calls
``poll``, swaps the polled window in with ``Store.replace_source_window`` and
records the bookkeeping in ``tracker_state``. Vendor errors never escape
``poll_one``: they become ``last_error`` and a ``failed`` result.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from typing import Any

from homunculus.ingest import ConfigError
from homunculus.store import Ledger, Store, TrackerState
from homunculus.trackers import registry
from homunculus.trackers.base import (
    AuthRequired, NetworkError, RateLimited, SchemaError, TrackerBatch, TrackerConfig, TrackerError,
    TrackerSource, UpstreamError, decode_cursor, encode_cursor,
)

CONFIG_PATH = "config/trackers.json"
DEFAULT_POLL_MINUTES = 60
MIN_POLL_MINUTES = 60  # Pineal's period key is at most hourly
DUE_SLACK_MIN = 5  # an hourly job at :20 must not miss a source polled at :20 plus a few seconds
BATCH_PARTS = tuple(f.name for f in fields(TrackerBatch))

_ERROR_KINDS: tuple[tuple[type[TrackerError], str], ...] = (
    (AuthRequired, "auth_required"), (RateLimited, "rate_limited"), (SchemaError, "schema"),
    (NetworkError, "network"), (UpstreamError, "http_5xx"), (TrackerError, "network"),
)

SourceFactory = Callable[[str, TrackerConfig], TrackerSource]


def _poll_minutes(name: str, block: Mapping[str, Any]) -> int:
    minutes = block.get("poll_minutes", DEFAULT_POLL_MINUTES)
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < MIN_POLL_MINUTES:
        raise ValueError(f"config/trackers.json: {name}.poll_minutes must be an integer >= {MIN_POLL_MINUTES}")
    return minutes


def load_tracker_config(ledger: Ledger, tz: str) -> TrackerConfig:
    """config/trackers.json as TrackerConfig; ValueError for unknown, blocked or too frequent trackers."""
    raw = ledger.read_json(CONFIG_PATH, default={}) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{CONFIG_PATH} must hold a JSON object")
    enabled = tuple(str(n) for n in raw.get("enabled") or ())
    per_source = {str(k): dict(v) for k, v in raw.items() if k != "enabled" and isinstance(v, Mapping)}
    for name in enabled:
        registry.check_name(name)
        _poll_minutes(name, per_source.get(name) or {})
    return TrackerConfig(enabled, per_source, tz)


def _utc(now: datetime) -> datetime:
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _stamp(now: datetime) -> str:
    return _utc(now).isoformat(timespec="seconds")


def due(cfg: TrackerConfig, states: Mapping[str, TrackerState | None], now: datetime) -> list[str]:
    """Enabled trackers whose poll_minutes (less DUE_SLACK_MIN) have passed since the last attempt."""
    out = []
    for name in cfg.enabled:
        state = states.get(name)
        last = datetime.fromisoformat(state.last_attempt) if state and state.last_attempt else None
        wait = timedelta(minutes=_poll_minutes(name, cfg.per_source.get(name) or {}) - DUE_SLACK_MIN)
        if last is None or _utc(now) - _utc(last) >= wait:
            out.append(name)
    return out


def error_kind(exc: TrackerError) -> str:
    return next(kind for cls, kind in _ERROR_KINDS if isinstance(exc, cls))


def batch_counts(batch: TrackerBatch) -> dict[str, int]:
    return {part: len(getattr(batch, part)) for part in BATCH_PARTS}


def _instant(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return _utc(moment) if moment.tzinfo else None


def newest_ts(batch: TrackerBatch, previous: str | None = None) -> str | None:
    """Latest measurement time in the batch as UTC ISO, or previous when the batch has none newer."""
    stamps = [s.ts for s in batch.samples] + [s.end for s in batch.sleep_segments] + [w.end for w in batch.workouts]
    found = [m for m in (_instant(t) for t in stamps) if m is not None]
    old = _instant(previous) if previous else None
    best = max(found + ([old] if old else []), default=None)
    return best.isoformat(timespec="seconds") if best else previous


def _requests(state: TrackerState | None, now: datetime, made: int) -> tuple[int, str]:
    day = _utc(now).date().isoformat()
    before = state.requests_today if state and state.requests_day == day else 0
    return before + made, day


def _failed_state(name: str, state: TrackerState | None, now: datetime, kind: str) -> TrackerState:
    count, day = _requests(state, now, 0)
    return TrackerState(name, state.cursor_json if state else "{}", state.last_ok if state else None, _stamp(now),
                        kind, state.newest_data_ts if state else None, count, day)


def _ok_state(name: str, state: TrackerState | None, now: datetime, result: Any) -> TrackerState:
    count, day = _requests(state, now, result.requests_made)
    newest = newest_ts(result.batch, state.newest_data_ts if state else None)
    return TrackerState(name, encode_cursor(result.new_cursor), _stamp(now), _stamp(now), None, newest, count, day)


def poll_one(store: Store, source: TrackerSource, *, now: datetime, tz: str, backfill_days: int = 0,
             dry_run: bool = False) -> dict[str, Any]:
    """One poll of one source. dry_run fetches and counts but writes neither rows nor state."""
    name = source.name
    state = store.tracker_state(name)
    try:
        result = source.poll(decode_cursor(state.cursor_json if state else None), now=now, tz=tz,
                             backfill_days=backfill_days)
    except TrackerError as exc:
        kind = error_kind(exc)
        if not dry_run:
            store.put_tracker_state(_failed_state(name, state, now, kind))
        return {"name": name, "status": "failed", "error": kind}
    counts = batch_counts(result.batch)
    if not dry_run:
        counts = store.replace_source_window(name, *result.window, result.batch)
        store.put_tracker_state(_ok_state(name, state, now, result))
    return {"name": name, "status": "ok" if result.complete else "incomplete", "window": list(result.window),
            "counts": counts, "requests": result.requests_made, "warnings": list(result.warnings),
            "dry_run": dry_run}


def default_factory(env: Mapping[str, str]) -> SourceFactory:
    def build(name: str, cfg: TrackerConfig) -> TrackerSource:
        return registry.build(name, env=env, cfg=cfg)
    return build


def poll_names(store: Store, cfg: TrackerConfig, names: Sequence[str], now: datetime, factory: SourceFactory,
               *, backfill_days: int = 0, dry_run: bool = False) -> list[dict]:
    """poll_one for each name; a source that cannot be built (missing secret) is reported, not raised."""
    out = []
    for name in names:
        try:
            source = factory(name, cfg)
        except (ValueError, ConfigError) as exc:  # missing secret, adapter not installed
            out.append({"name": name, "status": "failed", "error": "config", "detail": str(exc)})
            continue
        out.append(poll_one(store, source, now=now, tz=cfg.tz, backfill_days=backfill_days, dry_run=dry_run))
    return out


def poll_due(store: Store, cfg: TrackerConfig, factory: SourceFactory, now: datetime) -> list[dict]:
    states = {name: store.tracker_state(name) for name in cfg.enabled}
    return poll_names(store, cfg, due(cfg, states, now), now, factory)


def state_entry(state: TrackerState | None) -> dict[str, Any]:
    """The non-secret bookkeeping of one tracker for `tracker status`."""
    if state is None:
        return {"last_ok": None, "last_attempt": None, "last_error": None, "newest_data_ts": None,
                "requests_today": 0}
    return {"last_ok": state.last_ok, "last_attempt": state.last_attempt, "last_error": state.last_error,
            "newest_data_ts": state.newest_data_ts, "requests_today": state.requests_today}

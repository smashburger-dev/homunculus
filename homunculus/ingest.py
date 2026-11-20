"""Brainstem: ingest server for Health Auto Export, check-ins and owner messages.

Successor of ``docs/_seed_webhook.py``. The iOS app Health Auto Export POSTs
JSON to ``POST /health``. This module archives the raw body in a private
directory, parses metrics, sleep, workouts and events into ``store`` rows,
drops GPS routes and device names before anything is stored, and serves the
read endpoints the scraper bots use.

Routes:
    POST /health                          token; ingest a Health Auto Export payload
    GET  /metrics /latest /freshness      token
    GET  /samples?metric=&since=&limit=   token; limit capped at cfg.max_limit
    GET  /summary?days=7                  token; daily n/min/max/avg/sum per metric and field
    GET  /workouts?since=&limit=          token
    GET  /site/*                          owner login header or token; static dashboard
    POST <post_routes>                    owner login header or token; e.g. /checkin, /message

Env (see ``load_config``):
    HEALTH_TOKEN       required, at least 32 bytes
    HEALTH_HOST        bind address, default 127.0.0.1
    HEALTH_PORT        default 8787
    HEALTH_DB          SQLite path, default <HOMUNCULUS_LEDGER>/.private/health.sqlite
    HEALTH_RAW         raw payload dir, default <HOMUNCULUS_LEDGER>/.private/raw
    HEALTH_SOURCE_MAP  JSON {substring: 'watch'|'phone'|'garmin'|'other'}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import mimetypes
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from homunculus.store import SOURCES, HealthEvent, Sample, SleepNight, SleepSegment, Store, Workout

logger = logging.getLogger("homunculus.ingest")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MIN_TOKEN_BYTES = 32
PRIVATE_DIR = ".private"
OWNER_HEADER = "Tailscale-User-Login"  # header name UNVERIFIED, see ARCHITECTURE.md
MAX_FORM_BODY = 1024 * 1024
MAX_SUMMARY_DAYS = 366
DEFAULT_LIMIT = 1000

# Keys of Store.latest_ts() that name tables rather than sample metrics.
_TABLE_KEYS = frozenset({"sleep_segments", "sleep_nights", "workouts", "events", "daily", "checkins"})

_RAW_STAMP = "%Y%m%dT%H%M%S%fZ"
_RAW_NAME_RE = re.compile(r"^(\d{8}T\d{12}Z)(?:-\d+)?\.json$")


class ConfigError(Exception):
    """Configuration is missing or unsafe; the server must not start."""


@dataclass(frozen=True)
class IngestConfig:
    host: str
    port: int
    db_path: Path
    raw_dir: Path
    token: str
    raw_retention_days: int = 30
    max_body: int = 200 * 1024 * 1024
    max_limit: int = 5000
    source_map: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # never print the token
        return f"IngestConfig(host={self.host!r}, port={self.port}, db_path={self.db_path!r}, raw_dir={self.raw_dir!r})"


@dataclass
class ParsedBatch:
    samples: list[Sample]
    sleep_segments: list[SleepSegment]
    sleep_nights: list[SleepNight]
    workouts: list[Workout]
    events: list[HealthEvent]
    skipped: dict[str, int]


PostRoute = Callable[[dict], tuple[int, dict]]


# --------------------------------------------------------------------------
# Configuration


def _check_token(token: str) -> None:
    if len(token.encode("utf-8")) < MIN_TOKEN_BYTES:
        raise ConfigError(f"HEALTH_TOKEN must be at least {MIN_TOKEN_BYTES} bytes")


def _port(text: str | None) -> int:
    if not text:
        return DEFAULT_PORT
    try:
        port = int(text)
    except ValueError:
        raise ConfigError("HEALTH_PORT must be an integer") from None
    if not 0 <= port <= 65535:
        raise ConfigError("HEALTH_PORT out of range")
    return port


def _source_map(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        raise ConfigError("HEALTH_SOURCE_MAP must be a JSON object") from None
    if not isinstance(obj, dict):
        raise ConfigError("HEALTH_SOURCE_MAP must be a JSON object")
    for key, value in obj.items():
        if not key or value not in SOURCES:
            raise ConfigError(f"HEALTH_SOURCE_MAP values must be one of {SOURCES}")
    return dict(obj)


def _path_from(value: str | None, private: Path | None, name: str, var: str) -> Path:
    if value:
        return Path(value).expanduser()
    if private is None:
        raise ConfigError(f"{var} or HOMUNCULUS_LEDGER must be set")
    return private / name


def load_config(env: Mapping[str, str]) -> IngestConfig:
    """Build the config from environment variables; raise ConfigError when unsafe."""
    token = env.get("HEALTH_TOKEN", "")
    _check_token(token)
    ledger = env.get("HOMUNCULUS_LEDGER")
    private = Path(ledger).expanduser() / PRIVATE_DIR if ledger else None
    return IngestConfig(
        host=env.get("HEALTH_HOST") or DEFAULT_HOST,
        port=_port(env.get("HEALTH_PORT")),
        db_path=_path_from(env.get("HEALTH_DB"), private, "health.sqlite", "HEALTH_DB"),
        raw_dir=_path_from(env.get("HEALTH_RAW"), private, "raw", "HEALTH_RAW"),
        token=token,
        source_map=_source_map(env.get("HEALTH_SOURCE_MAP")),
    )


# --------------------------------------------------------------------------
# Sources and auth


def normalize_source(name: str | None, source_map: Mapping[str, str]) -> str:
    """Map a device name to one of store.SOURCES; the raw name is never returned."""
    lower = (name or "").lower()
    if not lower:
        return "other"
    for key in sorted(source_map, key=len, reverse=True):
        if key.lower() in lower and source_map[key] in SOURCES:
            return source_map[key]
    for needle, source in (("watch", "watch"), ("garmin", "garmin"), ("iphone", "phone")):
        if needle in lower:
            return source
    return "other"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    want = name.lower()
    for key, value in headers.items():
        if key.lower() == want:
            return value
    return None


def _presented_tokens(headers: Mapping[str, str]) -> list[str]:
    scheme, _, value = (_header(headers, "Authorization") or "").partition(" ")
    found = [value.strip()] if scheme.lower() == "bearer" else []
    found.append((_header(headers, "X-Token") or "").strip())
    return [t for t in found if t]


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def authorized(headers: Mapping[str, str], token: str) -> bool:
    """True when the Bearer or X-Token header matches token (constant-time)."""
    if not token:
        return False
    return any(_same(got, token) for got in _presented_tokens(headers))


def _owner_ok(headers: Mapping[str, str], owner_login: str | None) -> bool:
    got = _header(headers, OWNER_HEADER)
    return bool(owner_login) and got is not None and _same(got, owner_login)


# --------------------------------------------------------------------------
# Scrubbing

_DROP_KEYS = frozenset({
    "lat", "lon", "lng", "coordinate", "coordinates", "course", "altitude",
    "horizontalaccuracy", "verticalaccuracy", "courseaccuracy", "speedaccuracy",
})
_DROP_SUBSTRINGS = (
    "route", "latitude", "longitude", "gps", "location", "source", "device",
    "hardware", "firmware", "software", "manufacturer", "model", "product", "serial",
)
_LOCATION_TAGS = frozenset({"Indoor", "Outdoor", "Unknown"})


def _keep(key: Any, value: Any) -> bool:
    k = str(key).lower()
    if k == "location":
        return isinstance(value, str) and value in _LOCATION_TAGS
    return k not in _DROP_KEYS and not any(s in k for s in _DROP_SUBSTRINGS)


def _scrub(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _scrub(v) for k, v in value.items() if _keep(k, v)}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def strip_workout(w: Mapping[str, Any]) -> dict:
    """Copy of w without route, coordinates, source names or device metadata."""
    return _scrub(w)


# --------------------------------------------------------------------------
# Value helpers


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _qty(value: Any) -> float | None:
    if isinstance(value, Mapping):
        return _num(value.get("qty"))
    return _num(value)


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _first(item: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        text = _text(item.get(key))
        if text:
            return text
    return None


def _skip(batch: ParsedBatch, key: str, n: int = 1) -> None:
    batch.skipped[key] = batch.skipped.get(key, 0) + n


def _dicts(value: Any) -> list[Mapping[str, Any]] | None:
    """The mapping items of a list, or None when value is not a list."""
    if not isinstance(value, list):
        return None
    return [v for v in value if isinstance(v, Mapping)]


# --------------------------------------------------------------------------
# Metrics and sleep

_STAGES = {
    "awake": "awake", "asleep": "asleep", "inbed": "inbed", "core": "core", "rem": "rem",
    "deep": "deep", "unspecified": "unspecified", "asleepcore": "core", "asleepdeep": "deep",
    "asleeprem": "rem", "asleepunspecified": "unspecified",
}
_NIGHT_HOURS = (("total_h", "totalSleep"), ("asleep_h", "asleep"), ("core_h", "core"),
                ("deep_h", "deep"), ("rem_h", "rem"), ("awake_h", "awake"))


def _point_samples(name: str, unit: str | None, point: Mapping[str, Any], sm: Mapping[str, str]) -> list[Sample]:
    ts = _first(point, "date", "startDate", "start")
    if ts is None:
        return []
    source = normalize_source(_text(point.get("source")), sm)
    values = ((k, _num(v)) for k, v in point.items())
    return [Sample(name, unit, ts, str(k), v, source) for k, v in values if v is not None]


def _segment(point: Mapping[str, Any], sm: Mapping[str, str]) -> SleepSegment | None:
    stage = _STAGES.get(str(point.get("value")).lower().replace(" ", "").replace("_", ""))
    start, end = _first(point, "startDate", "start"), _first(point, "endDate", "end")
    if stage is None or start is None or end is None:
        return None
    return SleepSegment(start, end, stage, normalize_source(_text(point.get("source")), sm))


def _night(point: Mapping[str, Any], sm: Mapping[str, str]) -> SleepNight | None:
    wake = _first(point, "sleepEnd", "date")
    if wake is None:
        return None
    hours = {attr: _num(point.get(key)) for attr, key in _NIGHT_HOURS}
    return SleepNight(
        wake_date=wake[:10], sleep_start=_text(point.get("sleepStart")), sleep_end=_text(point.get("sleepEnd")),
        in_bed_start=_text(point.get("inBedStart")), in_bed_end=_text(point.get("inBedEnd")),
        source=normalize_source(_text(point.get("source")), sm), **hours,
    )


def _sleep_point(point: Mapping[str, Any], sm: Mapping[str, str], batch: ParsedBatch) -> None:
    if "value" in point:
        row, target = _segment(point, sm), batch.sleep_segments
    elif "sleepStart" in point or "sleepEnd" in point:
        row, target = _night(point, sm), batch.sleep_nights
    else:
        row, target = None, []
    if row is None:
        _skip(batch, "sleep")
        return
    target.append(row)


def _parse_metric(metric: Mapping[str, Any], sm: Mapping[str, str], batch: ParsedBatch) -> None:
    name, points = _text(metric.get("name")), _dicts(metric.get("data"))
    if name is None or points is None:
        _skip(batch, "metrics")
        return
    for point in points:
        if name == "sleep_analysis":
            _sleep_point(point, sm, batch)
            continue
        rows = _point_samples(name, _text(metric.get("units")), point, sm)
        batch.samples.extend(rows)
        if not rows:
            _skip(batch, "samples")


def _parse_metrics(items: list[Mapping[str, Any]], sm: Mapping[str, str], batch: ParsedBatch) -> None:
    for metric in items:
        _parse_metric(metric, sm, batch)


# --------------------------------------------------------------------------
# Workouts


def _workout_id(name: str, start: str) -> str:
    return hashlib.sha256(f"{name}|{start}".encode("utf-8")).hexdigest()[:16]


def _kcal(w: Mapping[str, Any]) -> float | None:
    for key in ("activeEnergyBurned", "activeEnergy"):
        value = w.get(key)
        qty = _qty(value)
        if qty is None:
            continue
        unit = str(value.get("units", "")).lower() if isinstance(value, Mapping) else ""
        return qty / 4.184 if unit == "kj" else qty
    return None


def _hr(w: Mapping[str, Any], flat_key: str, nested_key: str) -> float | None:
    qty = _qty(w.get(flat_key))
    nested = w.get("heartRate")
    if qty is None and isinstance(nested, Mapping):
        qty = _qty(nested.get(nested_key))
    return qty


def _hr_series(value: Any) -> tuple[tuple[str, float], ...]:
    pairs = ((_text(p.get("date")), _num(p.get("Avg", p.get("qty")))) for p in _dicts(value) or [])
    return tuple((ts, bpm) for ts, bpm in pairs if ts is not None and bpm is not None)


def _indoor(w: Mapping[str, Any]) -> bool | None:
    flag = w.get("isIndoor")
    if isinstance(flag, bool):
        return flag
    location = w.get("location")
    return {"Indoor": True, "Outdoor": False}.get(location) if isinstance(location, str) else None


def _workout(w: Mapping[str, Any], sm: Mapping[str, str]) -> Workout | None:
    name = _text(w.get("name")) or "workout"
    start, end = _text(w.get("start")), _text(w.get("end"))
    if start is None or end is None:
        return None
    return Workout(
        id=_text(w.get("id")) or _workout_id(name, start), name=name, start=start, end=end,
        duration_s=_qty(w.get("duration")), active_kcal=_kcal(w),
        hr_avg=_hr(w, "avgHeartRate", "avg"), hr_max=_hr(w, "maxHeartRate", "max"),
        hr_series=_hr_series(w.get("heartRateData")), is_indoor=_indoor(w),
        source=normalize_source(_text(w.get("source")), sm),
        raw_json=json.dumps(strip_workout(w), sort_keys=True, ensure_ascii=False, allow_nan=False),
    )


def _parse_workouts(items: list[Mapping[str, Any]], sm: Mapping[str, str], batch: ParsedBatch) -> None:
    for item in items:
        row = _workout(item, sm)
        if row is None:
            _skip(batch, "workouts")
        else:
            batch.workouts.append(row)


# --------------------------------------------------------------------------
# Events


def _event(kind: str, item: Mapping[str, Any], sm: Mapping[str, str]) -> HealthEvent | None:
    start = _first(item, "start", "startDate", "date")
    if start is None:
        return None
    payload = json.dumps(_scrub(item), sort_keys=True, ensure_ascii=False, allow_nan=False)
    source = normalize_source(_text(item.get("source")), sm)
    return HealthEvent(kind, start, _first(item, "end", "endDate"), payload, source)


def _parse_events(kind: str, items: list[Mapping[str, Any]], sm: Mapping[str, str], batch: ParsedBatch) -> None:
    for item in items:
        row = _event(kind, item, sm)
        if row is None:
            _skip(batch, kind)
        else:
            batch.events.append(row)


_SECTIONS: Mapping[str, Callable[[list, Mapping[str, str], ParsedBatch], None]] = {
    "metrics": _parse_metrics,
    "workouts": _parse_workouts,
    "heartRateNotifications": partial(_parse_events, "hr_notification"),
    "ecg": partial(_parse_events, "ecg"),
    "symptoms": partial(_parse_events, "symptom"),
    "stateOfMind": partial(_parse_events, "state_of_mind"),
}


def _data_root(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data", payload) if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        raise ValueError("payload must be a JSON object")
    return data


def parse_payload(payload: Mapping[str, Any], source_map: Mapping[str, str]) -> ParsedBatch:
    """Parse a Health Auto Export payload ({'data': {...}} or bare) into store rows."""
    batch = ParsedBatch([], [], [], [], [], {})
    for key, value in _data_root(payload).items():
        parser, items = _SECTIONS.get(key), _dicts(value)
        if parser is None or items is None:
            _skip(batch, str(key), len(value) if isinstance(value, list) else 1)
            continue
        if len(items) < len(value):
            _skip(batch, str(key), len(value) - len(items))
        parser(items, source_map, batch)
    return batch


def _store_batch(store: Store, batch: ParsedBatch) -> tuple[dict[str, int], list[HealthEvent]]:
    counts = {
        "samples": store.upsert_samples(batch.samples),
        "sleep_segments": store.upsert_sleep_segments(batch.sleep_segments),
        "sleep_nights": store.upsert_sleep_nights(batch.sleep_nights),
        "workouts": store.upsert_workouts(batch.workouts),
    }
    new = [e for e in batch.events if store.insert_events([e])]
    counts["events"] = len(new)
    return counts, new


def ingest_payload(store: Store, payload: Mapping[str, Any], cfg: IngestConfig) -> tuple[dict[str, int], list[HealthEvent]]:
    """Parse and store a payload; returns counts per table and the newly inserted events."""
    batch = parse_payload(payload, cfg.source_map)
    if batch.skipped:
        logger.info("skipped items: %s", batch.skipped)
    return _store_batch(store, batch)


# --------------------------------------------------------------------------
# Raw archive


def _utc(now: datetime) -> datetime:
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")


def _open_new(raw_dir: Path, stamp: str) -> tuple[int, Path]:
    for n in range(1000):
        path = raw_dir / (f"{stamp}.json" if n == 0 else f"{stamp}-{n}.json")
        try:
            return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), path
        except FileExistsError:
            continue
    raise FileExistsError(f"no free raw file name for {stamp}")


def save_raw(raw_dir: Path, body: bytes, now: datetime) -> Path:
    """Write body to a new file in raw_dir (dir 0o700, file 0o600, git-ignored)."""
    raw_dir = Path(raw_dir)
    _private_dir(raw_dir)
    fd, path = _open_new(raw_dir, _utc(now).strftime(_RAW_STAMP))
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(body)
    return path


def prune_raw(raw_dir: Path, days: int, now: datetime) -> int:
    """Delete raw payloads whose file-name stamp is older than days; returns the count."""
    raw_dir = Path(raw_dir)
    if not raw_dir.is_dir():
        return 0
    cutoff = _utc(now) - timedelta(days=days)
    removed = 0
    for path in raw_dir.iterdir():
        match = _RAW_NAME_RE.match(path.name)
        if match and datetime.strptime(match.group(1), _RAW_STAMP).replace(tzinfo=timezone.utc) < cutoff:
            path.unlink()
            removed += 1
    return removed


# --------------------------------------------------------------------------
# Read endpoints (pure helpers)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _q(query: Mapping[str, list[str]], name: str, default: str) -> str:
    values = query.get(name)
    return values[-1] if values else default


def _limit(query: Mapping[str, list[str]], cap: int) -> int:
    limit = int(_q(query, "limit", str(DEFAULT_LIMIT)))
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return min(limit, cap)


def _metric_names(store: Store) -> list[str]:
    return sorted(k for k in store.latest_ts() if k not in _TABLE_KEYS)


def _latest_rows(samples: Iterable[Sample]) -> list[dict]:
    best: dict[str, Sample] = {}
    for s in samples:
        if s.field not in best or s.ts >= best[s.field].ts:
            best[s.field] = s
    return [asdict(best[k]) for k in sorted(best)]


def _summarize(samples: Iterable[Sample]) -> list[dict]:
    groups: dict[tuple[str, str, str], list[Sample]] = {}
    for s in samples:
        groups.setdefault((s.metric, s.field, s.ts[:10]), []).append(s)
    rows = [_summary_row(key, rows) for key, rows in groups.items()]
    rows.sort(key=lambda r: (r["metric"], r["field"]))
    rows.sort(key=lambda r: r["day"], reverse=True)
    return rows


def _summary_row(key: tuple[str, str, str], rows: list[Sample]) -> dict:
    values = [r.value for r in rows]
    return {"metric": key[0], "unit": rows[0].unit, "field": key[1], "day": key[2], "n": len(values),
            "min": min(values), "max": max(values), "avg": sum(values) / len(values), "sum": sum(values)}


def _workout_dict(w: Workout) -> dict:
    out = asdict(w)
    out["hr_series"] = [list(p) for p in w.hr_series]
    out["raw"] = json.loads(out.pop("raw_json"))
    return out


def _get_metrics(app: _App, query: Mapping[str, list[str]]) -> list[dict]:
    latest = app.store.latest_ts()
    return [{"metric": m, "latest": latest[m]} for m in _metric_names(app.store)]


def _get_latest(app: _App, query: Mapping[str, list[str]]) -> list[dict]:
    latest = app.store.latest_ts()
    rows: list[dict] = []
    for metric in _metric_names(app.store):
        rows.extend(_latest_rows(app.store.samples(metric, latest[metric][:10])))
    return rows


def _get_samples(app: _App, query: Mapping[str, list[str]]) -> list[dict]:
    metric = _q(query, "metric", "")
    if not metric:
        raise ValueError("metric is required")
    rows = app.store.samples(metric, _q(query, "since", ""))
    return [asdict(s) for s in reversed(rows[-_limit(query, app.cfg.max_limit):])]


def _get_summary(app: _App, query: Mapping[str, list[str]]) -> list[dict]:
    days = int(_q(query, "days", "7"))
    if not 1 <= days <= MAX_SUMMARY_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_SUMMARY_DAYS}")
    since = (_utcnow().date() - timedelta(days=days)).isoformat()
    rows: list[Sample] = []
    for metric in _metric_names(app.store):
        rows.extend(app.store.samples(metric, since))
    return _summarize(rows)


def _get_workouts(app: _App, query: Mapping[str, list[str]]) -> list[dict]:
    rows = app.store.workouts(_q(query, "since", ""))
    return [_workout_dict(w) for w in reversed(rows[-_limit(query, app.cfg.max_limit):])]


def _get_freshness(app: _App, query: Mapping[str, list[str]]) -> dict:
    return {"latest": app.store.latest_ts(), "now": _utcnow().isoformat()}


_GET_ROUTES: Mapping[str, Callable[[_App, Mapping[str, list[str]]], Any]] = {
    "/metrics": _get_metrics,
    "/latest": _get_latest,
    "/samples": _get_samples,
    "/summary": _get_summary,
    "/workouts": _get_workouts,
    "/freshness": _get_freshness,
}


# --------------------------------------------------------------------------
# HTTP application

Response = tuple[int, str, bytes]


class _HttpError(Exception):
    def __init__(self, status: int, body: Mapping[str, Any]) -> None:
        super().__init__(status)
        self.status = status
        self.body = dict(body)


def _json(status: int, obj: Any) -> Response:
    body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return status, "application/json; charset=utf-8", body


def _json_object(body: bytes) -> dict:
    obj = json.loads(body)
    if not isinstance(obj, dict):
        raise ValueError("body must be a JSON object")
    return obj


def _form_dict(parsed: Mapping[str, list[str]]) -> dict:
    return {k: v[0] if len(v) == 1 else list(v) for k, v in parsed.items()}


def _parse_form(body: bytes, headers: Mapping[str, str]) -> dict:
    ctype = (_header(headers, "Content-Type") or "").split(";")[0].strip().lower()
    if ctype == "application/x-www-form-urlencoded":
        return _form_dict(parse_qs(body.decode("utf-8"), keep_blank_values=True))
    return _json_object(body)


def _content_type(path: Path) -> str:
    guess = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"{guess}; charset=utf-8" if guess.startswith("text/") else guess


class _App:
    """Routing and auth, independent of the socket layer."""

    def __init__(self, cfg: IngestConfig, store: Store, on_events: Callable[[list[HealthEvent]], None] | None,
                 post_routes: Mapping[str, PostRoute], site_dir: Path | None, owner_login: str | None) -> None:
        self.cfg, self.store, self.on_events = cfg, store, on_events
        self.post_routes, self.site_dir, self.owner_login = dict(post_routes), site_dir, owner_login

    def _require_token(self, headers: Mapping[str, str]) -> None:
        if not authorized(headers, self.cfg.token):
            raise _HttpError(401, {"error": "unauthorized"})

    def _require_owner(self, headers: Mapping[str, str]) -> None:
        if not _owner_ok(headers, self.owner_login):
            self._require_token(headers)

    def get(self, path: str, query: Mapping[str, list[str]], headers: Mapping[str, str]) -> Response:
        if path == "/site" or path.startswith("/site/"):
            self._require_owner(headers)
            return self._site(path)
        self._require_token(headers)
        handler = _GET_ROUTES.get(path)
        if handler is None:
            raise _HttpError(404, {"error": "not found", "routes": sorted(_GET_ROUTES)})
        return _json(200, handler(self, query))

    def post(self, path: str, headers: Mapping[str, str], read_body: Callable[[int], bytes]) -> Response:
        if path == "/health":
            self._require_token(headers)
            return self._ingest(read_body(self.cfg.max_body))
        route = self.post_routes.get(path)
        if route is None:
            self._require_token(headers)
            raise _HttpError(404, {"error": "not found"})
        self._require_owner(headers)
        status, obj = route(_parse_form(read_body(MAX_FORM_BODY), headers))
        return _json(status, obj)

    def _site(self, path: str) -> Response:
        if self.site_dir is None:
            raise _HttpError(404, {"error": "not found"})
        root = Path(self.site_dir).resolve()
        target = (root / unquote(path[len("/site"):]).lstrip("/")).resolve()
        if target.is_dir():
            target = target / "index.html"
        if root not in target.parents or not target.is_file():
            raise _HttpError(404, {"error": "not found"})
        return 200, _content_type(target), target.read_bytes()

    def _ingest(self, body: bytes) -> Response:
        payload = _json_object(body)
        save_raw(self.cfg.raw_dir, body, _utcnow())
        batch = parse_payload(payload, self.cfg.source_map)
        counts, new = _store_batch(self.store, batch)
        alerts = self._notify(new)
        logger.info("ingested %s, skipped %s", counts, batch.skipped)
        return _json(200, {"stored": counts, "skipped": batch.skipped, "new_events": len(new), "alerts": alerts})

    def _notify(self, events: list[HealthEvent]) -> str:
        if not events or self.on_events is None:
            return "none"
        try:
            self.on_events(events)
        except Exception:  # stored data must survive a failing alert hook
            logger.exception("on_events failed")
            return "failed"
        return "ok"


class _Handler(BaseHTTPRequestHandler):
    app: _App
    timeout = 60
    server_version = "homunculus"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        url = urlparse(self.path)
        self._respond(lambda: self.app.get(url.path, parse_qs(url.query), self.headers))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self._respond(lambda: self.app.post(path, self.headers, self._read_body))

    def _read_body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise _HttpError(400, {"error": "invalid content-length"}) from None
        if length <= 0:
            raise _HttpError(400, {"error": "empty body"})
        if length > limit:
            raise _HttpError(413, {"error": "body too large"})
        return self.rfile.read(length)

    def _respond(self, action: Callable[[], Response]) -> None:
        try:
            status, ctype, body = action()
        except _HttpError as exc:
            status, ctype, body = _json(exc.status, exc.body)
        except ValueError as exc:
            status, ctype, body = _json(400, {"error": str(exc)})
        except Exception:
            logger.exception("request failed")
            status, ctype, body = _json(500, {"error": "internal error"})
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("%s %s", self.address_string(), format % args)


def make_server(cfg: IngestConfig, store: Store, *, on_events: Callable[[list[HealthEvent]], None] | None = None,
                post_routes: Mapping[str, PostRoute] | None = None, site_dir: Path | None = None,
                owner_login: str | None = None) -> ThreadingHTTPServer:
    """Bind a Brainstem server; refuses a token shorter than 32 bytes."""
    _check_token(cfg.token)
    app = _App(cfg, store, on_events, post_routes or {}, site_dir, owner_login)
    handler = type("BrainstemHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    server.daemon_threads = True
    return server


def serve(cfg: IngestConfig, store: Store, **kwargs: Any) -> None:
    """Run make_server until interrupted."""
    server = make_server(cfg, store, **kwargs)
    host, port = server.server_address[:2]
    print(f"Brainstem listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

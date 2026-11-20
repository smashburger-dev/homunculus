"""SQLite store for raw samples and derived rows, plus ledger file IO.

Two halves:

* ``Store`` keeps raw tracker samples and code-derived rows in SQLite on the
  box. Every range query is half-open, ``since <= col < until``, using plain
  string comparison, so callers must pass timestamps in the same format the
  rows were stored in (ISO-8601 dates or the timestamp format ingest writes).
  Tracker adapters write only through ``replace_source_window`` and keep
  their poll bookkeeping (cursor, last error, never a token) in
  ``tracker_state``.
* ``Ledger`` reads and writes the private ledger repo. Writes are checked
  against per-author fnmatch globs, land through an atomic rename, and are
  committed by ``commit`` under the bot's git identity.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from dataclasses import astuple, dataclass, fields
from datetime import date as Date
from datetime import datetime, time, timedelta
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

SCHEMA_VERSION: int = 2
SOURCES = ("watch", "phone", "garmin", "whoop", "google", "polar", "withings", "oura", "other")
SLEEP_STAGES = frozenset({"awake", "asleep", "inbed", "core", "rem", "deep", "unspecified"})
EVENT_KINDS = frozenset({"hr_notification", "ecg", "symptom", "state_of_mind"})
DAILY_QUALITIES = frozenset({"ok", "sparse", "missing"})
CHECKIN_ORIGINS = frozenset({"form", "coach", "hae"})
TRACKER_ERRORS = frozenset({"auth_required", "rate_limited", "http_5xx", "schema", "network"})
MAX_CURSOR_BYTES = 4096

SUPERUSER = "pineal"
COMMITTER = ("Pineal", "pineal@homunculus.local")
HALT_PATH = "config/HALT"
LOCK_NAME = "homunculus.lock"


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8. Used for every sha256."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.encode("utf-8")


def _require(value: Any, allowed: Iterable[Any], what: str) -> None:
    if value not in allowed:
        raise ValueError(f"invalid {what}: {value!r}")


# --------------------------------------------------------------------------
# Row dataclasses


@dataclass(frozen=True)
class Sample:
    metric: str
    unit: str | None
    ts: str
    field: str
    value: float
    source: str

    def __post_init__(self) -> None:
        _require(self.source, SOURCES, "source")


@dataclass(frozen=True)
class SleepSegment:
    start: str
    end: str
    stage: str
    source: str

    def __post_init__(self) -> None:
        _require(self.stage, SLEEP_STAGES, "stage")
        _require(self.source, SOURCES, "source")


@dataclass(frozen=True)
class SleepNight:
    wake_date: str
    sleep_start: str | None
    sleep_end: str | None
    in_bed_start: str | None
    in_bed_end: str | None
    total_h: float | None
    asleep_h: float | None
    core_h: float | None
    deep_h: float | None
    rem_h: float | None
    awake_h: float | None
    source: str

    def __post_init__(self) -> None:
        _require(self.source, SOURCES, "source")


@dataclass(frozen=True)
class Workout:
    id: str
    name: str
    start: str
    end: str
    duration_s: float | None
    active_kcal: float | None
    hr_avg: float | None
    hr_max: float | None
    hr_series: tuple[tuple[str, float], ...]
    is_indoor: bool | None
    source: str
    raw_json: str

    def __post_init__(self) -> None:
        _require(self.source, SOURCES, "source")


@dataclass(frozen=True)
class HealthEvent:
    kind: str
    start: str
    end: str | None
    payload_json: str
    source: str

    def __post_init__(self) -> None:
        _require(self.kind, EVENT_KINDS, "event kind")
        _require(self.source, SOURCES, "source")


@dataclass(frozen=True)
class DailyValue:
    date: str
    key: str
    value: float | None
    source: str
    n: int
    quality: str

    def __post_init__(self) -> None:
        _require(self.quality, DAILY_QUALITIES, "quality")


@dataclass(frozen=True)
class TrackerState:
    """Poll bookkeeping for one tracker adapter; never holds a token."""

    name: str
    cursor_json: str
    last_ok: str | None
    last_attempt: str | None
    last_error: str | None
    newest_data_ts: str | None
    requests_today: int
    requests_day: str | None

    def __post_init__(self) -> None:
        _require(self.name, SOURCES, "tracker name")
        if self.last_error is not None:
            _require(self.last_error, TRACKER_ERRORS, "tracker error")
        if len(self.cursor_json.encode("utf-8")) > MAX_CURSOR_BYTES:
            raise ValueError(f"cursor_json exceeds {MAX_CURSOR_BYTES} bytes")
        json.loads(self.cursor_json)


@dataclass(frozen=True)
class CheckinItem:
    date: str
    item: str
    value: float | None
    text: str | None
    ref: str | None
    ts: str
    wording_version: str
    origin: str

    def __post_init__(self) -> None:
        _require(self.origin, CHECKIN_ORIGINS, "origin")


# --------------------------------------------------------------------------
# Table mapping


def _identity(values: tuple) -> tuple:
    return values


def _encode_workout(values: tuple) -> tuple:
    out = list(values)
    out[8] = json.dumps([list(p) for p in values[8]], allow_nan=False)
    out[9] = None if values[9] is None else int(values[9])
    return tuple(out)


def _decode_workout(values: tuple) -> tuple:
    out = list(values)
    out[8] = tuple((str(t), float(v)) for t, v in json.loads(values[8]))
    out[9] = None if values[9] is None else bool(values[9])
    return tuple(out)


def _encode_checkin(values: tuple) -> tuple:
    # SQLite treats NULLs in a primary key as distinct; store a missing ref as ''.
    out = list(values)
    out[4] = "" if values[4] is None else values[4]
    return tuple(out)


def _decode_checkin(values: tuple) -> tuple:
    out = list(values)
    out[4] = None if values[4] == "" else values[4]
    return tuple(out)


@dataclass(frozen=True)
class _Table:
    name: str
    cls: type
    order: str
    encode: Callable[[tuple], tuple] = _identity
    decode: Callable[[tuple], tuple] = _identity

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(f.name for f in fields(self.cls))


_SAMPLES = _Table("samples", Sample, '"ts", "field", "source"')
_SEGMENTS = _Table("sleep_segments", SleepSegment, '"start", "stage", "source"')
_NIGHTS = _Table("sleep_nights", SleepNight, '"wake_date", "source"')
_WORKOUTS = _Table("workouts", Workout, '"start", "id"', _encode_workout, _decode_workout)
_EVENTS = _Table("events", HealthEvent, '"start", "kind", "source"')
_DAILY = _Table("daily", DailyValue, '"date", "key"')
_CHECKINS = _Table("checkins", CheckinItem, '"date", "item", "ts", "ref"', _encode_checkin, _decode_checkin)
_TRACKERS = _Table("tracker_state", TrackerState, '"name"')

_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE samples (
        "metric" TEXT NOT NULL, "unit" TEXT, "ts" TEXT NOT NULL, "field" TEXT NOT NULL,
        "value" REAL NOT NULL, "source" TEXT NOT NULL,
        PRIMARY KEY ("metric", "ts", "field", "source"));
    CREATE TABLE sleep_segments (
        "start" TEXT NOT NULL, "end" TEXT NOT NULL, "stage" TEXT NOT NULL, "source" TEXT NOT NULL,
        PRIMARY KEY ("start", "stage", "source"));
    CREATE TABLE sleep_nights (
        "wake_date" TEXT NOT NULL, "sleep_start" TEXT, "sleep_end" TEXT,
        "in_bed_start" TEXT, "in_bed_end" TEXT, "total_h" REAL, "asleep_h" REAL,
        "core_h" REAL, "deep_h" REAL, "rem_h" REAL, "awake_h" REAL, "source" TEXT NOT NULL,
        PRIMARY KEY ("wake_date", "source"));
    CREATE TABLE workouts (
        "id" TEXT PRIMARY KEY, "name" TEXT NOT NULL, "start" TEXT NOT NULL, "end" TEXT NOT NULL,
        "duration_s" REAL, "active_kcal" REAL, "hr_avg" REAL, "hr_max" REAL,
        "hr_series" TEXT NOT NULL, "is_indoor" INTEGER, "source" TEXT NOT NULL,
        "raw_json" TEXT NOT NULL);
    CREATE TABLE events (
        "kind" TEXT NOT NULL, "start" TEXT NOT NULL, "end" TEXT, "payload_json" TEXT NOT NULL,
        "source" TEXT NOT NULL,
        PRIMARY KEY ("kind", "start", "source"));
    CREATE TABLE daily (
        "date" TEXT NOT NULL, "key" TEXT NOT NULL, "value" REAL, "source" TEXT NOT NULL,
        "n" INTEGER NOT NULL, "quality" TEXT NOT NULL,
        PRIMARY KEY ("date", "key"));
    CREATE TABLE checkins (
        "date" TEXT NOT NULL, "item" TEXT NOT NULL, "value" REAL, "text" TEXT,
        "ref" TEXT NOT NULL DEFAULT '', "ts" TEXT NOT NULL, "wording_version" TEXT NOT NULL,
        "origin" TEXT NOT NULL,
        PRIMARY KEY ("date", "item", "ref"));
    CREATE INDEX samples_metric_ts ON samples ("metric", "ts");
    CREATE INDEX sleep_segments_start ON sleep_segments ("start");
    CREATE INDEX workouts_start ON workouts ("start");
    CREATE INDEX events_start ON events ("start");
    CREATE INDEX daily_key_date ON daily ("key", "date");
    """,
    """
    CREATE TABLE tracker_state (
        "name" TEXT PRIMARY KEY, "cursor_json" TEXT NOT NULL, "last_ok" TEXT, "last_attempt" TEXT,
        "last_error" TEXT, "newest_data_ts" TEXT, "requests_today" INTEGER NOT NULL DEFAULT 0,
        "requests_day" TEXT);
    """,
)

# Latest timestamp per table, keyed by the table name.
_LATEST_SQL = (
    ("sleep_segments", 'SELECT MAX("end") FROM sleep_segments'),
    ("sleep_nights", 'SELECT MAX("wake_date") FROM sleep_nights'),
    ("workouts", 'SELECT MAX("end") FROM workouts'),
    ("events", 'SELECT MAX("start") FROM events'),
    ("daily", 'SELECT MAX("date") FROM daily'),
    ("checkins", 'SELECT MAX("ts") FROM checkins'),
)


def _insert_sql(table: _Table, verb: str) -> str:
    cols = ", ".join(f'"{c}"' for c in table.columns)
    marks = ", ".join("?" for _ in table.columns)
    return f"INSERT {verb} INTO {table.name} ({cols}) VALUES ({marks})"


def _range_sql(table: _Table, col: str, until: str | None, filters: Mapping[str, Any]) -> tuple[str, list]:
    cols = ", ".join(f'"{c}"' for c in table.columns)
    where = [f'"{col}" >= ?']
    params: list = []
    if until is not None:
        where.append(f'"{col}" < ?')
        params.append(until)
    for name, value in filters.items():
        if value is not None:
            where.append(f'"{name}" = ?')
            params.append(value)
    sql = f"SELECT {cols} FROM {table.name} WHERE {' AND '.join(where)} ORDER BY {table.order}"
    return sql, params


def _check_daily_key(row: DailyValue) -> None:
    """A dotted key is a vendor key and must carry its own vendor as source."""
    prefix, dot, rest = row.key.partition(".")
    if dot and (prefix != row.source or not rest):
        raise ValueError(f"vendor key {row.key!r} needs source {prefix!r}, got {row.source!r}")


# --------------------------------------------------------------------------
# Source windows (tracker adapters)


@dataclass(frozen=True)
class _WindowPart:
    attr: str  # TrackerBatch attribute
    table: _Table
    col: str
    is_date: bool
    replace: bool  # False: rows are only added (events), never deleted


_WINDOW_PARTS = (
    _WindowPart("samples", _SAMPLES, "ts", False, True),
    _WindowPart("sleep_segments", _SEGMENTS, "start", False, True),
    _WindowPart("sleep_nights", _NIGHTS, "wake_date", True, True),
    _WindowPart("workouts", _WORKOUTS, "start", False, True),
    _WindowPart("events", _EVENTS, "start", False, False),
    _WindowPart("daily_vendor_scores", _DAILY, "date", True, True),
)
_SOURCE_TABLES = (_SAMPLES, _SEGMENTS, _NIGHTS, _WORKOUTS, _EVENTS, _DAILY)


def _day_ceil(ts: str) -> str:
    """First date whose 00:00 is at or after ts, in ts's own clock.

    '2026-11-18' and '2026-11-18T00:00:00+01:00' give '2026-11-18';
    '2026-11-18T06:00:00+01:00' gives '2026-11-19'. Date columns use this, so a
    date lies in a window exactly when the start of that day does.
    """
    if len(ts) == 10:
        return Date.fromisoformat(ts).isoformat()
    dt = datetime.fromisoformat(ts)
    day = dt.date() if dt.time() == time(0) else dt.date() + timedelta(days=1)
    return day.isoformat()


def _part_bounds(part: _WindowPart, since: str, until: str) -> tuple[str, str]:
    return (_day_ceil(since), _day_ceil(until)) if part.is_date else (since, until)


def _check_window_row(row: Any, part: _WindowPart, source: str, lo: str, hi: str) -> None:
    if row.source != source:
        raise ValueError(f"{part.attr} row has source {row.source!r}, expected {source!r}")
    value = getattr(row, part.col)
    if not lo <= value < hi:
        raise ValueError(f"{part.attr} row {part.col}={value!r} lies outside [{lo}, {hi})")
    if part.table is _DAILY and not row.key.startswith(f"{source}."):
        raise ValueError(f"tracker daily key {row.key!r} must start with {source + '.'!r}")


def _window_values(part: _WindowPart, batch: Any, source: str, lo: str, hi: str) -> list[tuple]:
    rows = list(getattr(batch, part.attr))
    for row in rows:
        _check_window_row(row, part, source, lo, hi)
    return [part.table.encode(astuple(r)) for r in rows]


def _delete_window_sql(part: _WindowPart, source: str) -> tuple[str, list]:
    sql = f'DELETE FROM {part.table.name} WHERE "source" = ? AND "{part.col}" >= ? AND "{part.col}" < ?'
    if part.table is _DAILY:
        return sql + ' AND substr("key", 1, ?) = ?', [len(source) + 1, f"{source}."]
    return sql, []


class Store:
    """SQLite access shared by ingest, metrics and the scheduler. Thread-safe."""

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def migrate(self) -> int:
        """Apply pending migrations; refuse a database from a newer engine."""
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"database schema {version} is newer than engine schema {SCHEMA_VERSION}")
            for target in range(version + 1, SCHEMA_VERSION + 1):
                script = _MIGRATIONS[target - 1]
                self._conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;")
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # -- writes

    def _write(self, table: _Table, rows: Iterable[Any], verb: str) -> int:
        """Insert rows in one transaction; returns rows written (REPLACE counts once, IGNORE zero)."""
        values = [table.encode(astuple(r)) for r in rows]
        sql = _insert_sql(table, verb)
        with self._lock, self._conn:
            before = self._conn.total_changes
            self._conn.executemany(sql, values)
            return self._conn.total_changes - before

    def upsert_samples(self, rows: Iterable[Sample]) -> int:
        return self._write(_SAMPLES, rows, "OR REPLACE")

    def upsert_sleep_segments(self, rows: Iterable[SleepSegment]) -> int:
        return self._write(_SEGMENTS, rows, "OR REPLACE")

    def upsert_sleep_nights(self, rows: Iterable[SleepNight]) -> int:
        return self._write(_NIGHTS, rows, "OR REPLACE")

    def upsert_workouts(self, rows: Iterable[Workout]) -> int:
        return self._write(_WORKOUTS, rows, "OR REPLACE")

    def insert_events(self, rows: Iterable[HealthEvent]) -> int:
        """Insert events, ignoring known ones; returns the number of NEW rows."""
        return self._write(_EVENTS, rows, "OR IGNORE")

    def put_daily(self, rows: Iterable[DailyValue]) -> int:
        """Upsert daily rows; a dotted (vendor) key must be prefixed by its own source."""
        rows = list(rows)
        for row in rows:
            _check_daily_key(row)
        return self._write(_DAILY, rows, "OR REPLACE")

    def put_checkins(self, rows: Iterable[CheckinItem]) -> int:
        return self._write(_CHECKINS, rows, "OR REPLACE")

    def put_tracker_state(self, row: TrackerState) -> None:
        self._write(_TRACKERS, [row], "OR REPLACE")

    def replace_source_window(self, source: str, since: str, until: str, batch: Any) -> dict[str, int]:
        """Swap one source's rows in since <= col < until for the batch, in one transaction.

        ``batch`` is a ``trackers.base.TrackerBatch``. Every row must carry
        ``source`` and lie inside the window, and daily rows must use
        ``f'{source}.'`` keys, else ValueError before anything is written.
        Date columns (wake_date, date) compare by the start of the day, see
        ``_day_ceil``. Events are only added, never deleted. Returns rows
        inserted per batch attribute plus the total under 'deleted'.
        """
        _require(source, SOURCES, "source")
        if not since < until:
            raise ValueError(f"empty window: {since!r} .. {until!r}")
        plan = [(p, *_part_bounds(p, since, until)) for p in _WINDOW_PARTS]
        values = [_window_values(p, batch, source, lo, hi) for p, lo, hi in plan]
        with self._lock, self._conn:
            return self._replace_locked(source, plan, values)

    def _replace_locked(self, source: str, plan: list, values: list[list[tuple]]) -> dict[str, int]:
        counts = {"deleted": 0}
        for (part, lo, hi), rows in zip(plan, values):
            if part.replace:
                sql, extra = _delete_window_sql(part, source)
                counts["deleted"] += self._conn.execute(sql, [source, lo, hi, *extra]).rowcount
            before = self._conn.total_changes
            self._conn.executemany(_insert_sql(part.table, "OR REPLACE" if part.replace else "OR IGNORE"), rows)
            counts[part.attr] = self._conn.total_changes - before
        return counts

    def purge_source(self, source: str) -> dict[str, int]:
        """Delete every row of one source in every table, plus its tracker state."""
        _require(source, SOURCES, "source")
        with self._lock, self._conn:
            counts = {t.name: self._conn.execute(f'DELETE FROM {t.name} WHERE "source" = ?', [source]).rowcount
                      for t in _SOURCE_TABLES}
            counts["tracker_state"] = self._conn.execute(
                'DELETE FROM tracker_state WHERE "name" = ?', [source]).rowcount
        return counts

    # -- reads

    def _range(self, table: _Table, col: str, since: str, until: str | None, **filters: Any) -> list:
        sql, params = _range_sql(table, col, until, filters)
        with self._lock:
            rows = self._conn.execute(sql, [since, *params]).fetchall()
        return [table.cls(*table.decode(tuple(r))) for r in rows]

    def samples(self, metric: str, since: str, until: str | None = None, source: str | None = None) -> list[Sample]:
        return self._range(_SAMPLES, "ts", since, until, metric=metric, source=source)

    def sleep_segments(self, since: str, until: str | None = None) -> list[SleepSegment]:
        return self._range(_SEGMENTS, "start", since, until)

    def sleep_nights(self, since: str, until: str | None = None) -> list[SleepNight]:
        return self._range(_NIGHTS, "wake_date", since, until)

    def workouts(self, since: str, until: str | None = None) -> list[Workout]:
        return self._range(_WORKOUTS, "start", since, until)

    def events(self, kind: str | None, since: str) -> list[HealthEvent]:
        return self._range(_EVENTS, "start", since, None, kind=kind)

    def daily(self, key: str, since: str, until: str | None = None) -> list[DailyValue]:
        """Rows for one key, ordered by date ascending."""
        return self._range(_DAILY, "date", since, until, key=key)

    def checkins(self, since: str, until: str | None = None, item: str | None = None) -> list[CheckinItem]:
        return self._range(_CHECKINS, "date", since, until, item=item)

    def tracker_state(self, name: str) -> TrackerState | None:
        cols = ", ".join(f'"{c}"' for c in _TRACKERS.columns)
        with self._lock:
            row = self._conn.execute(f'SELECT {cols} FROM tracker_state WHERE "name" = ?', [name]).fetchone()
        return None if row is None else TrackerState(*row)

    def latest_ts(self) -> dict[str, str]:
        """Latest timestamp per sample metric and per table; empty ones are absent."""
        with self._lock:
            out = dict(self._conn.execute('SELECT "metric", MAX("ts") FROM samples GROUP BY "metric"').fetchall())
            for name, sql in _LATEST_SQL:
                value = self._conn.execute(sql).fetchone()[0]
                if value is not None:
                    out[name] = value
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------
# Ledger


class PathNotAllowed(PermissionError):
    """A ledger path escapes the root or is outside the author's write rules."""


_IDENTITY_RE = re.compile(r"^\s*([^<>\n]+?)\s*<([^<>\s]+@[^<>\s]+)>\s*$")
_BOT_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_TRAILER_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


def _normalize_rel(rel: str) -> str:
    """Return a clean posix relative path, or raise PathNotAllowed."""
    path = PurePosixPath(str(rel).replace("\\", "/"))
    parts = [p for p in path.parts if p not in ("", ".")]
    bad = path.is_absolute() or not parts or ".." in parts or parts[0] == ".git"
    if bad:
        raise PathNotAllowed(f"invalid ledger path: {rel!r}")
    return "/".join(parts)


def parse_identity(author: str) -> tuple[str, str]:
    """'amygdala' -> ('Amygdala', 'amygdala@homunculus.local'); 'Name <a@b>' passes through."""
    match = _IDENTITY_RE.match(author)
    if match:
        return match.group(1), match.group(2)
    name = author.strip().lower()
    if not _BOT_RE.match(name):
        raise ValueError(f"invalid commit author: {author!r}")
    return name.capitalize(), f"{name}@homunculus.local"


def _commit_message(message: str, trailers: Mapping[str, str]) -> str:
    lines = []
    for key, value in trailers.items():
        text = str(value)
        if not _TRAILER_KEY_RE.match(key) or "\n" in text or "\r" in text:
            raise ValueError(f"invalid trailer: {key!r}")
        lines.append(f"{key}: {text}")
    body = message.strip()
    return f"{body}\n\n" + "\n".join(lines) + "\n" if lines else body + "\n"


def _git_env(author: str) -> dict[str, str]:
    name, email = parse_identity(author)
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": COMMITTER[0],
        "GIT_COMMITTER_EMAIL": COMMITTER[1],
    })
    return env


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Ledger:
    """File IO on the private ledger repo with per-author write rules."""

    def __init__(self, root: str | Path, write_rules: Mapping[str, Sequence[str]]) -> None:
        self.root: Path = Path(root).resolve()
        self._rules = {k.strip().lower(): tuple(v) for k, v in write_rules.items()}
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    # -- path handling

    def _path(self, rel: str) -> Path:
        clean = _normalize_rel(rel)
        path = self.root / clean
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise PathNotAllowed(f"ledger path escapes root: {rel!r}")
        return path

    def _allowed(self, rel: str, author: str) -> bool:
        who = author.strip().lower()
        if who == SUPERUSER:
            return True
        return any(fnmatchcase(rel, glob) for glob in self._rules.get(who, ()))

    def _writable(self, rel: str, author: str) -> tuple[str, Path]:
        clean = _normalize_rel(rel)
        if not self._allowed(clean, author):
            raise PathNotAllowed(f"{author!r} may not write {clean!r}")
        return clean, self._path(clean)

    def _mark(self, rel: str) -> None:
        with self._lock:
            self._pending.add(rel)

    # -- reads

    def read_json(self, rel: str, default: Any = None) -> Any:
        path = self._path(rel)
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def read_text(self, rel: str) -> str:
        return self._path(rel).read_text(encoding="utf-8")

    def read_jsonl(self, rel: str) -> list[dict]:
        path = self._path(rel)
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def exists(self, rel: str) -> bool:
        return self._path(rel).exists()

    def glob(self, pattern: str) -> list[str]:
        """Sorted relative posix paths of files matching pattern, .git excluded."""
        clean = _normalize_rel(pattern)
        hits = (p.relative_to(self.root).as_posix() for p in self.root.glob(clean) if p.is_file())
        return sorted(h for h in hits if not h.startswith(".git/"))

    def sha256(self, rel: str) -> str:
        return hashlib.sha256(self._path(rel).read_bytes()).hexdigest()

    def halted(self) -> bool:
        return self._path(HALT_PATH).exists()

    # -- writes

    def write_json(self, rel: str, obj: Any, *, author: str) -> Path:
        text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        return self.write_text(rel, text, author=author)

    def write_text(self, rel: str, text: str, *, author: str) -> Path:
        clean, path = self._writable(rel, author)
        _atomic_write(path, text.encode("utf-8"))
        self._mark(clean)
        return path

    def append_jsonl(self, rel: str, obj: Mapping[str, Any], *, author: str) -> None:
        clean, path = self._writable(rel, author)
        line = canonical_json(dict(obj)) + b"\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._mark(clean)

    def pending(self) -> list[str]:
        """Paths written through this Ledger since the last commit, sorted."""
        with self._lock:
            return sorted(self._pending)

    # -- git

    def _git(self, args: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=dict(env), capture_output=True, text=True, check=False,
        )

    def _git_ok(self, args: Sequence[str], env: Mapping[str, str]) -> str:
        proc = self._git(args, env)
        if proc.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def commit(self, *, author: str, message: str, trailers: Mapping[str, str]) -> str | None:
        """Commit pending paths under the author's identity; None if nothing changed."""
        env = _git_env(author)
        text = _commit_message(message, trailers)
        git_dir = self.root / ".git"
        if not git_dir.is_dir():
            raise FileNotFoundError(f"ledger root is not a git repository: {self.root}")
        with open(git_dir / LOCK_NAME, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self._commit_locked(env, text)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _commit_locked(self, env: Mapping[str, str], text: str) -> str | None:
        paths = self.pending()
        if paths:
            self._git_ok(["add", "--", *paths], env)
        staged = self._git(["diff", "--cached", "--quiet"], env)
        if staged.returncode == 0:
            self._clear(paths)
            return None
        self._git_ok(["commit", "--quiet", "--cleanup=whitespace", "-m", text], env)
        self._clear(paths)
        return self._git_ok(["rev-parse", "HEAD"], env)

    def _clear(self, paths: Sequence[str]) -> None:
        with self._lock:
            self._pending.difference_update(paths)

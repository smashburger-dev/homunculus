#!/usr/bin/env python3
"""Local webhook for Apple Health data.

Apple Health has no webhook of its own. The iOS app "Health Auto Export"
(REST API automation) POSTs JSON to this server. The server stores every
sample in SQLite and serves it back as JSON for a scraper bot or swarm.

Standard library only. Run:
    HEALTH_TOKEN=secret python3 apple_health_webhook.py

Endpoints:
    POST /health                 ingest a Health Auto Export payload
    GET  /metrics                metric names, sample count, latest date
    GET  /latest                 latest value per metric and field
    GET  /samples?metric=step_count&since=2026-11-01&limit=500
    GET  /summary?days=7         daily count/min/max/avg/sum per metric
    GET  /workouts?since=2026-11-01

Env:
    HEALTH_HOST   bind address (default 0.0.0.0, so the iPhone reaches it on LAN)
    HEALTH_PORT   port (default 8787)
    HEALTH_DB     SQLite path (default ./health.db)
    HEALTH_RAW    folder for raw payloads (default ./health_raw)
    HEALTH_TOKEN  if set, required as "Authorization: Bearer <token>" or "X-Token: <token>"
    SWARM_URL     if set, every ingested batch is forwarded there as JSON
"""

import json
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("HEALTH_HOST", "0.0.0.0")
PORT = int(os.environ.get("HEALTH_PORT", "8787"))
DB_PATH = os.environ.get("HEALTH_DB", "health.db")
RAW_DIR = Path(os.environ.get("HEALTH_RAW", "health_raw"))
TOKEN = os.environ.get("HEALTH_TOKEN", "")
SWARM_URL = os.environ.get("SWARM_URL", "")
MAX_BODY = 200 * 1024 * 1024  # full history exports get large

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    metric TEXT NOT NULL,
    unit   TEXT,
    date   TEXT NOT NULL,
    field  TEXT NOT NULL,
    value  REAL NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (metric, date, field, source)
);
CREATE TABLE IF NOT EXISTS workouts (
    name  TEXT,
    start TEXT,
    end   TEXT,
    raw   TEXT,
    PRIMARY KEY (name, start)
);
"""

db_lock = threading.Lock()


def connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


DB = connect()


def flatten_metric(metric):
    """Yield one row per numeric field of each data point.

    Covers plain metrics (qty), heart rate (Min/Avg/Max) and sleep
    (asleep, inBed, core, deep, rem, awake).
    """
    name, unit = metric.get("name"), metric.get("units")
    for point in metric.get("data", []):
        date, source = point.get("date"), point.get("source") or ""
        for field, value in point.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                yield (name, unit, date, field, float(value), source)


def ingest(payload):
    data = payload.get("data", payload)
    rows = [row for m in data.get("metrics", []) for row in flatten_metric(m)]
    workouts = [
        (w.get("name"), str(w.get("start")), str(w.get("end")), json.dumps(w))
        for w in data.get("workouts", [])
    ]
    with db_lock, DB:
        DB.executemany("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?)", rows)
        DB.executemany("INSERT OR REPLACE INTO workouts VALUES (?,?,?,?)", workouts)
    return rows, workouts


def save_raw(body):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    (RAW_DIR / f"{stamp}.json").write_bytes(body)


def forward_to_swarm(rows, workouts):
    if not SWARM_URL:
        return
    batch = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "samples": [dict(zip(("metric", "unit", "date", "field", "value", "source"), r)) for r in rows],
        "workouts": [json.loads(w[3]) for w in workouts],
    }
    req = urllib.request.Request(
        SWARM_URL,
        data=json.dumps(batch).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30).close()
    except Exception as exc:  # the swarm being down must not lose the ingest
        print(f"[swarm] forward failed: {exc}")


def query(sql, params=()):
    with db_lock:
        return [dict(r) for r in DB.execute(sql, params).fetchall()]


def get_metrics(_q):
    return query(
        "SELECT metric, unit, COUNT(*) AS samples, MAX(date) AS latest "
        "FROM samples GROUP BY metric ORDER BY metric"
    )


def get_latest(_q):
    return query(
        "SELECT s.metric, s.unit, s.field, s.value, s.date, s.source FROM samples s "
        "JOIN (SELECT metric, field, MAX(date) AS d FROM samples GROUP BY metric, field) m "
        "ON s.metric = m.metric AND s.field = m.field AND s.date = m.d "
        "ORDER BY s.metric, s.field"
    )


def get_samples(q):
    metric = q.get("metric", [""])[0]
    since = q.get("since", ["0000"])[0]
    limit = int(q.get("limit", ["1000"])[0])
    return query(
        "SELECT * FROM samples WHERE (? = '' OR metric = ?) AND date >= ? "
        "ORDER BY date DESC LIMIT ?",
        (metric, metric, since, limit),
    )


def get_summary(q):
    days = int(q.get("days", ["7"])[0])
    return query(
        "SELECT metric, unit, field, substr(date, 1, 10) AS day, COUNT(*) AS n, "
        "MIN(value) AS min, MAX(value) AS max, AVG(value) AS avg, SUM(value) AS sum "
        "FROM samples WHERE substr(date, 1, 10) >= date('now', ?) "
        "GROUP BY metric, field, day ORDER BY day DESC, metric",
        (f"-{days} days",),
    )


def get_workouts(q):
    since = q.get("since", ["0000"])[0]
    rows = query("SELECT raw FROM workouts WHERE start >= ? ORDER BY start DESC", (since,))
    return [json.loads(r["raw"]) for r in rows]


ROUTES = {
    "/metrics": get_metrics,
    "/latest": get_latest,
    "/samples": get_samples,
    "/summary": get_summary,
    "/workouts": get_workouts,
}


class Handler(BaseHTTPRequestHandler):
    def authorized(self):
        if not TOKEN:
            return True
        bearer = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return TOKEN in (bearer, self.headers.get("X-Token", ""))

    def send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.authorized():
            return self.send_json(401, {"error": "unauthorized"})
        url = urlparse(self.path)
        handler = ROUTES.get(url.path)
        if handler is None:
            return self.send_json(404, {"error": "not found", "routes": list(ROUTES)})
        try:
            self.send_json(200, handler(parse_qs(url.query)))
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})

    def do_POST(self):
        if not self.authorized():
            return self.send_json(401, {"error": "unauthorized"})
        if urlparse(self.path).path != "/health":
            return self.send_json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= MAX_BODY:
            return self.send_json(413, {"error": "empty or too large"})
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            return self.send_json(400, {"error": f"invalid json: {exc}"})
        save_raw(body)
        rows, workouts = ingest(payload)
        threading.Thread(target=forward_to_swarm, args=(rows, workouts), daemon=True).start()
        print(f"[{time.strftime('%H:%M:%S')}] ingested {len(rows)} samples, {len(workouts)} workouts", flush=True)
        self.send_json(200, {"samples": len(rows), "workouts": len(workouts)})

    def log_message(self, fmt, *args):
        pass  # keep the console to ingest lines


if __name__ == "__main__":
    if not TOKEN:
        print("warning: HEALTH_TOKEN not set, anyone on the network can read your health data")
    print(f"listening on http://{HOST}:{PORT}  (POST /health, GET {' '.join(ROUTES)})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

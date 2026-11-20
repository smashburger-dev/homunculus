"""Pineal: scheduler, single git writer, budget guard and alert delivery.

Every minute ``tick`` runs the due jobs once per ``(job, period)``. Each run is
appended to ``ops/runs.jsonl``, leaves a heartbeat in ``ops/heartbeats/`` and is
committed under the job's bot identity. A job that raises leaves a scrubbed
incident (frames only, no locals) and, when configured, triggers the repair
webhook. ``config/HALT`` leaves only essential jobs running.

Owner-facing text here is fixed German. Red-flag text always comes from
``guardrails.TEMPLATES_DE``; no LLM sits in the alert path.
"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import os
import re
import traceback
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath
from string import Template
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from homunculus import agents, beliefs, checkins, dashboard, guardrails, ingest, insula, metrics, plans, scouts, weekly
from homunculus.agents import BotSpec, Budget, Provider
from homunculus.evidence import OwnerProfile
from homunculus.guardrails import Alert
from homunculus.metrics import MetricsConfig
from homunculus.store import CheckinItem, Ledger, Store, canonical_json

CADENCES = ("minute", "hourly", "daily", "weekly", "monthly")
STATUSES = ("ok", "skipped", "deferred", "failed")
FINAL_STATUSES = frozenset({"ok", "skipped"})
MAX_ATTEMPTS = 2  # first run plus at most one retry per period

WRITER = "pineal"
RUNS_LOG = "ops/runs.jsonl"
ALERTS_LOG = "data/alerts.jsonl"
NOTICES_LOG = "ops/notices.jsonl"
HEARTBEAT_DIR = "ops/heartbeats"
INCIDENT_DIR = "ops/incidents"
FRESHNESS_PATH = "data/freshness.json"
TICK_LOCK = "pineal-tick.lock"
PROMPTS_DIR = weekly.PROMPTS_DIR
ENGINE_ROOT = Path(__file__).resolve().parent.parent

REPAIR_URL_ENV = "HOMUNCULUS_REPAIR_URL"
REPAIR_SECRET_ENV = "HOMUNCULUS_REPAIR_SECRET"
RAW_ENV = "HEALTH_RAW"
WEBHOOK_TIMEOUT_S = 10
RAW_RETENTION_DAYS = 30
INCIDENT_MESSAGE_MAX = 200

OWNER_TARGETS = ("sartorius", "vagus", "cerebellum")
GOAL_RECIPIENTS = ("hippocampus", "motor_cortex", "sartorius", "vagus")
MESSAGE_MAX = 4000
STALE_BLOCKING = ("heart_rate_variability", "sleep_segments")
ALERT_DAYS = 2  # today and yesterday count as active
INBOX_MAX = 5
NOTES_MAX = 2000
THALAMUS_WEEKDAY = 3  # Thursday: submit; other days only collect a running batch
RED_FLAG_KEYS = ("rhr", "bp_sys", "bp_dia", "spo2_min", "af_burden", "weight_kg", "resp_rate", "wrist_temp_c")
RED_FLAG_LOOKBACK_DAYS = 91

FALLBACK_DE = "Ich kann gerade nicht antworten. Versuch es bitte später noch einmal."
PLAN_RATIFIED_DE = Template("Neuer Plan $version gilt ab jetzt. Die Änderungen stehen im Dashboard unter Plan.")
JOB_FAILED_DE = Template("Der Job $job ist im Zeitraum $period fehlgeschlagen. Details stehen unter ops/incidents.")
MONTHLY_DE = Template("Monatsbericht $month: LLM-Kosten $usd USD. Experimente mit Ergebnis: $experiments.")
BUDGET_DE: Mapping[str, Template] = MappingProxyType({
    "reduced": Template(
        "Das LLM-Budget dieser Woche ist zu $pct % verbraucht. Die Coaches antworten knapper, "
        "die Scouts pausieren bis Montag."
    ),
    "core_only": Template(
        "Das LLM-Budget dieser Woche ist aufgebraucht. Bis Montag laufen nur Datenimport, "
        "Warnungen und das Wochenreview."
    ),
})

# Normalized (guardrails.normalize_name) word prefixes per urgent symptom.
URGENT_TERMS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "chest_pain": (
        "brustschmerz", "schmerzen in der brust", "schmerz in der brust", "druck auf der brust",
        "brustdruck", "engegefuhl in der brust", "chest pain", "chest pressure", "chest tightness",
    ),
    "fainting": (
        "ohnmacht", "bewusstlos", "umgekippt", "kollabiert", "fainted", "fainting", "passed out",
        "blacked out",
    ),
    "short_breath": (
        "atemnot", "luftnot", "keine luft", "kurzatmig", "kann nicht atmen", "short of breath",
        "shortness of breath", "can t breathe", "cannot breathe", "breathless",
    ),
})
_URGENT_RES = {
    symptom: tuple(re.compile(r"(?<![0-9a-z])" + re.escape(t)) for t in terms)
    for symptom, terms in URGENT_TERMS.items()
}

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_HOURLY_RE = re.compile(r"^:([0-5]\d)$")
_STEP_RE = re.compile(r"^\*/([1-9]\d?)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")

COACH_REPLY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "reply_de": {"type": "string", "minLength": 1, "maxLength": MESSAGE_MAX},
        "checkin": checkins.COACH_CHECKIN_SCHEMA,
    },
    "required": ["reply_de", "checkin"],
    "additionalProperties": False,
}
GOAL_TALK_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "reply_de": {"type": "string", "minLength": 1, "maxLength": MESSAGE_MAX},
        "goals_md": {"type": ["string", "null"], "maxLength": 20000},
        "goal_update": {"type": ["string", "null"], "maxLength": 500},
    },
    "required": ["reply_de", "goals_md", "goal_update"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# Types


@dataclass(frozen=True)
class JobResult:
    status: str
    written: tuple[str, ...] = ()
    message: str = ""
    trailers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"invalid job status {self.status!r}")


@dataclass(frozen=True)
class Job:
    """cadence in CADENCES. at: 'HH:MM' (daily, weekly, monthly), ':MM' (hourly), '*/N' (minute).

    weekly needs weekday 0..6 (Monday = 0); monthly runs from day 1 at ``at``.
    """

    name: str
    cadence: str
    at: str | None
    weekday: int | None
    bot: str | None
    essential: bool
    fn: Callable[["Context", str], JobResult] = field(repr=False)

    def __post_init__(self) -> None:
        if self.cadence not in CADENCES:
            raise ValueError(f"job {self.name!r}: invalid cadence {self.cadence!r}")
        if self.cadence == "weekly" and self.weekday not in range(7):
            raise ValueError(f"job {self.name!r}: weekly needs weekday 0..6")
        _offset_min(self)  # validates at


class Notifier(Protocol):
    def send(self, to: str, text: str, level: str) -> None: ...


@dataclass
class Context:
    store: Store
    ledger: Ledger
    roster: Mapping[str, BotSpec]
    providers: Mapping[str, Provider]
    budget: Budget
    http: "scouts.Http"
    metrics_cfg: MetricsConfig
    notifier: Notifier
    tz: str
    now: Callable[[], datetime]
    dry_run: bool = False


# --------------------------------------------------------------------------
# Time helpers


def _localize(moment: datetime, tz: str) -> datetime:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(ZoneInfo(tz))


def _local_now(ctx: Context) -> datetime:
    return _localize(ctx.now(), ctx.tz)


def _stamp(ctx: Context) -> str:
    return _local_now(ctx).isoformat(timespec="seconds")


def _utc_z(moment: datetime) -> str:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shift(day: str, n: int) -> str:
    """Store ranges are [since, until), so a range through day ends at _shift(day, 1)."""
    return (Date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _iso_week(day: Date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _previous_month(month: str) -> str:
    year, mon = (int(x) for x in month.split("-"))
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


# --------------------------------------------------------------------------
# Schedule


def _match(rx: re.Pattern, at: str, job: Job) -> tuple[int, ...]:
    m = rx.match(at)
    if not m:
        raise ValueError(f"job {job.name!r}: invalid at {at!r} for cadence {job.cadence!r}")
    return tuple(int(g) for g in m.groups())


def _minute_step(job: Job) -> int:
    if job.at is None:
        return 1
    (step,) = _match(_STEP_RE, job.at, job)
    if 60 % step:
        raise ValueError(f"job {job.name!r}: minute step must divide 60")
    return step


def _offset_min(job: Job) -> int:
    """Minutes after the period start at which the job's slot opens."""
    if job.cadence == "minute":
        _minute_step(job)
        return 0
    if job.cadence == "hourly":
        return _match(_HOURLY_RE, job.at, job)[0] if job.at else 0
    hour, minute = _match(_HHMM_RE, job.at, job) if job.at else (0, 0)
    days = job.weekday if job.cadence == "weekly" else 0
    return days * 1440 + hour * 60 + minute


def _elapsed_min(job: Job, now: datetime) -> int:
    """Minutes since the start of the period now lies in."""
    days = {"weekly": now.weekday(), "monthly": now.day - 1}.get(job.cadence, 0)
    hours = 0 if job.cadence in ("minute", "hourly") else now.hour
    return days * 1440 + hours * 60 + now.minute


def _minute_key(job: Job, now: datetime) -> str:
    step = _minute_step(job)
    return f"{now:%Y-%m-%dT%H}:{now.minute - now.minute % step:02d}"


_PERIOD_KEYS: Mapping[str, Callable[[Job, datetime], str]] = MappingProxyType({
    "minute": _minute_key,
    "hourly": lambda _job, now: f"{now:%Y-%m-%dT%H}",
    "daily": lambda _job, now: now.date().isoformat(),
    "weekly": lambda _job, now: _iso_week(now.date()),
    "monthly": lambda _job, now: f"{now:%Y-%m}",
})


def period_key(job: Job, now: datetime) -> str:
    """'2026-11-20' daily, '2026-W47' weekly, '2026-11' monthly, '2026-11-20T07' hourly,
    '2026-11-20T07:15' minute. now is local time."""
    return _PERIOD_KEYS[job.cadence](job, now)


def _history(runs: Sequence[Mapping]) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    for row in runs:
        out.setdefault((str(row.get("job")), str(row.get("period"))), []).append(str(row.get("status")))
    return out


def _needs_run(statuses: Sequence[str]) -> bool:
    return not FINAL_STATUSES.intersection(statuses) and len(statuses) < MAX_ATTEMPTS


def due_jobs(jobs: Sequence[Job], now: datetime, runs: Sequence[Mapping]) -> list[tuple[Job, str]]:
    """Jobs whose slot in the current period has opened and that have no ok/skipped run
    and fewer than MAX_ATTEMPTS attempts there. A late tick catches up within the period."""
    history = _history(runs)
    due = []
    for job in jobs:
        period = period_key(job, now)
        if _elapsed_min(job, now) >= _offset_min(job) and _needs_run(history.get((job.name, period), [])):
            due.append((job, period))
    return due


# --------------------------------------------------------------------------
# Incidents and the repair webhook


def _scrub_path(filename: str) -> str:
    posix = PurePath(filename).as_posix()
    cut = posix.rfind("/homunculus/")
    return posix[cut + 1:] if cut >= 0 else PurePath(posix).name


def _safe_message(text: str) -> bool:
    """Only short single-line messages without digits, paths or addresses survive."""
    unsafe = any(ch.isdigit() or ch in "/\\@\n" for ch in text)
    return bool(text) and not unsafe and len(text) <= INCIDENT_MESSAGE_MAX


def scrub_incident(exc: BaseException, job: str, commit_sha: str | None) -> dict:
    """Traceback frames (file, line, function) only; no locals, no messages containing digits."""
    frames = [
        {"file": _scrub_path(f.filename), "line": f.lineno, "function": f.name}
        for f in traceback.extract_tb(exc.__traceback__)
    ]
    message = str(exc)
    body = {
        "job": job,
        "type": type(exc).__name__,
        "message": message if _safe_message(message) else None,
        "frames": frames,
        "commit_sha": commit_sha,
    }
    body["id"] = "inc-" + hashlib.sha256(canonical_json(body)).hexdigest()[:12]
    return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would forward the secret header to another URL, so refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _post(req: urllib.request.Request) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=WEBHOOK_TIMEOUT_S) as resp:
        return resp.read()


def repair_webhook(url: str, secret: str, incident: Mapping[str, Any], opener: Callable | None = None) -> None:
    """POST the scrubbed incident as JSON with an X-Webhook-Secret header; https only."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("repair webhook must be an https URL")
    if not secret:
        raise ValueError("repair webhook needs a secret")
    req = urllib.request.Request(
        url, data=canonical_json(dict(incident)), method="POST",
        headers={"Content-Type": "application/json", "X-Webhook-Secret": secret,
                 "User-Agent": "homunculus-pineal/0.1"},
    )
    (opener or _post)(req)


def _packed_ref(git: Path, ref: str) -> str | None:
    packed = git / "packed-refs"
    lines = packed.read_text(encoding="utf-8").splitlines() if packed.is_file() else []
    hits = (line.split(" ", 1)[0] for line in lines if line.endswith(" " + ref))
    return next(hits, None)


def _engine_sha() -> str | None:
    """HEAD of the engine checkout, read from .git without running git."""
    git = ENGINE_ROOT / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head if _SHA_RE.match(head) else None
        ref = head[5:]
        loose = git / ref
        sha = loose.read_text(encoding="utf-8").strip() if loose.is_file() else _packed_ref(git, ref)
    except OSError:
        return None
    return sha if sha and _SHA_RE.match(sha) else None


def _record_incident(ctx: Context, exc: BaseException, job: str, period: str, run_id: str) -> dict:
    incident = {**scrub_incident(exc, job, _engine_sha()), "period": period, "run_id": run_id}
    ctx.ledger.write_json(f"{INCIDENT_DIR}/{incident['id']}.json", incident, author=WRITER)
    return incident


def _send_repair(ctx: Context, incident: Mapping[str, Any]) -> str:
    url, secret = os.environ.get(REPAIR_URL_ENV), os.environ.get(REPAIR_SECRET_ENV, "")
    if ctx.dry_run or not url:
        return "not_configured"
    try:
        repair_webhook(url, secret, incident)
    except (ValueError, OSError):
        return "failed"
    return "sent"


# --------------------------------------------------------------------------
# Running jobs


def _run_id(job_name: str, period: str) -> str:
    return f"r-{re.sub(r'[^0-9A-Za-z]', '', period)}-{job_name}"


def _execute(ctx: Context, job: Job, period: str, run_id: str) -> tuple[JobResult, dict | None]:
    try:
        result = job.fn(ctx, period)
        if not isinstance(result, JobResult):
            raise TypeError(f"job {job.name} returned {type(result).__name__}, not JobResult")
        return result, None
    except Exception as exc:  # every job failure becomes an incident, never a crashed tick
        incident = _record_incident(ctx, exc, job.name, period, run_id)
        return JobResult("failed", (f"{INCIDENT_DIR}/{incident['id']}.json",), f"incident {incident['id']}"), incident


def _usage_trailers(ledger: Ledger, run_id: str) -> dict[str, str]:
    rows = [r for r in ledger.read_jsonl(agents.BUDGET_LOG) if r.get("run_id") == run_id]
    if not rows:
        return {}
    return {
        "Model": ",".join(sorted({str(r.get("model")) for r in rows})),
        "Tokens-In": str(sum(int(r.get("tokens_in", 0)) for r in rows)),
        "Tokens-Out": str(sum(int(r.get("tokens_out", 0)) for r in rows)),
        "Usd": f"{sum(float(r.get('usd', 0.0)) for r in rows):.6f}",
    }


def _commit(ctx: Context, job: Job, period: str, run_id: str, result: JobResult) -> tuple[str | None, dict | None]:
    if ctx.dry_run:
        return None, None
    trailers = {
        **_usage_trailers(ctx.ledger, run_id), "Run-Id": run_id, "Kind": "job", "Job": job.name,
        "Period": period, "Status": result.status, **dict(result.trailers),
    }
    message = f"{job.name} {period}: {result.status}"
    if result.message:
        message += "\n\n" + result.message
    try:
        return ctx.ledger.commit(author=job.bot or WRITER, message=message, trailers=trailers), None
    except Exception as exc:  # a failed commit leaves the paths pending for the next run
        return None, _record_incident(ctx, exc, f"commit:{job.name}", period, run_id)


def _rebuild_dashboard(ctx: Context) -> str:
    try:
        dashboard.build(ctx.store, ctx.ledger, ctx.ledger.root / "site", ctx.now(), ctx.metrics_cfg)
    except Exception:  # the dashboard is a view; it never fails a job
        return "failed"
    return "ok"


def run_job(ctx: Context, job: Job, period: str) -> dict:
    """Run once, append ops/runs.jsonl, write the heartbeat, commit under the bot identity.

    On exception: ops/incidents/<id>.json and the repair webhook. Retry limits
    live in due_jobs, so a direct call (cli run) always runs.
    """
    run_id = _run_id(job.name, period)
    started = _stamp(ctx)
    result, incident = _execute(ctx, job, period, run_id)
    row = {"job": job.name, "period": period, "status": result.status, "started": started,
           "ended": _stamp(ctx), "run_id": run_id}
    ctx.ledger.append_jsonl(RUNS_LOG, row, author=WRITER)
    ctx.ledger.write_json(f"{HEARTBEAT_DIR}/{job.name}.json", {**row, "message": result.message}, author=WRITER)
    sha, commit_incident = _commit(ctx, job, period, run_id, result)
    incident = incident or commit_incident
    out = {**row, "message": result.message, "written": list(result.written), "commit": sha}
    if incident:
        out.update(incident=incident["id"], webhook=_send_repair(ctx, incident))
    if sha:
        out["dashboard"] = _rebuild_dashboard(ctx)
    return out


@contextmanager
def _tick_lock(ledger: Ledger) -> Iterator[bool]:
    git = ledger.root / ".git"
    path = git / TICK_LOCK if git.is_dir() else ledger.root / f".{TICK_LOCK}"
    with open(path, "a", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _admitted(job: Job, halted: bool, level: str) -> bool:
    """HALT keeps essential jobs only; core_only budget also keeps code-only jobs (no LLM cost)."""
    if halted:
        return job.essential
    return level != "core_only" or job.essential or job.bot is None


def tick(ctx: Context, jobs: Sequence[Job]) -> list[dict]:
    """Entry point for launchd/systemd/cron each minute. A tick that finds another running returns []."""
    with _tick_lock(ctx.ledger) as acquired:
        if not acquired:
            return []
        halted, level = ctx.ledger.halted(), ctx.budget.level()
        due = due_jobs(jobs, _local_now(ctx), ctx.ledger.read_jsonl(RUNS_LOG))
        return [run_job(ctx, job, period) for job, period in due if _admitted(job, halted, level)]


# --------------------------------------------------------------------------
# Notification and alerts


class FileNotifier:
    """Writes mail/<to>/inbox/<id>.json envelopes; the dashboard shows the owner's."""

    def __init__(self, ledger: Ledger, *, now: Callable[[], datetime] | None = None) -> None:
        self._ledger = ledger
        self._now = now or (lambda: datetime.now(timezone.utc))

    def send(self, to: str, text: str, level: str) -> None:
        created = _utc_z(self._now())
        sha = hashlib.sha256(canonical_json({"to": to, "text": text, "level": level, "at": created})).hexdigest()
        msg = agents.make_message(
            sender=WRITER, to=[to], kind="alert" if level in guardrails.ALERT_LEVELS else "ask.owner",
            thread=f"notify/{to}", round_=0, max_rounds=0, refs=[], data_sha=sha, run_id=f"notify-{sha[:12]}",
            model="code", body=text, items=[{"level": level}], created_at=created,
        )
        self._ledger.write_json(f"mail/{to}/inbox/{msg['id']}.json", msg, author=WRITER)


def _profile(ledger: Ledger) -> dict:
    raw = ledger.read_json("profile/owner.json", default={})
    return dict(raw) if isinstance(raw, Mapping) else {}


def _delivered(ledger: Ledger) -> set[tuple[str, str]]:
    return {(str(r.get("rule_id")), str(r.get("date"))) for r in ledger.read_jsonl(ALERTS_LOG)}


def _deliver(ctx: Context, alert: Alert, profile: Mapping[str, Any]) -> None:
    text = guardrails.render_alert(alert, profile)
    ctx.notifier.send("owner", text, alert.level)
    ctx.ledger.append_jsonl(ALERTS_LOG, {
        "level": alert.level, "rule_id": alert.rule_id, "date": alert.date, "data": dict(alert.data),
        "delivered_at": _stamp(ctx),
    }, author=WRITER)


def deliver_alerts(ctx: Context, alerts: Sequence[Alert]) -> int:
    """Send each new (rule_id, date) once with its fixed template; returns how many went out."""
    seen = _delivered(ctx.ledger)
    profile = _profile(ctx.ledger)
    sent = 0
    for alert in alerts:
        key = (alert.rule_id, alert.date)
        if key in seen:
            continue
        seen.add(key)
        _deliver(ctx, alert, profile)
        sent += 1
    return sent


def _notify_once(ctx: Context, key: str, period: str, text: str, level: str) -> bool:
    """Pineal's own notices (budget, failed jobs, monthly report), deduplicated in ops/notices.jsonl."""
    if any(r.get("key") == key and r.get("period") == period for r in ctx.ledger.read_jsonl(NOTICES_LOG)):
        return False
    ctx.notifier.send("owner", text, level)
    ctx.ledger.append_jsonl(NOTICES_LOG, {"key": key, "period": period, "sent_at": _stamp(ctx)}, author=WRITER)
    return True


# --------------------------------------------------------------------------
# Context for LLM turns


def _plan_or_none(ledger: Ledger) -> dict | None:
    try:
        return plans.current(ledger)
    except plans.PlanError:
        return None


def _latest_packet(ledger: Ledger) -> tuple[str | None, dict]:
    rels = ledger.glob("reviews/*/packet.json")
    if not rels:
        return None, {}
    raw = ledger.read_json(rels[-1], default={})
    return rels[-1], dict(raw) if isinstance(raw, Mapping) else {}


def _ed_watch(ledger: Ledger) -> bool:
    guard = _latest_packet(ledger)[1].get("guard_context")
    return bool(guard.get("ed_watch")) if isinstance(guard, Mapping) else False


def _today_snapshot(ctx: Context, day: str) -> dict:
    """Provisional rows for today (finalize at 06:30 overwrites them), then the baselines."""
    metrics.finalize_day(ctx.store, day, ctx.metrics_cfg)
    return metrics.baselines(ctx.store, day, ctx.metrics_cfg)


def _recent_alerts(ledger: Ledger, day: str) -> list[dict]:
    days = {_shift(day, -k) for k in range(ALERT_DAYS)}
    rows = (r for r in ledger.read_jsonl(ALERTS_LOG) if str(r.get("date")) in days)
    return [{"level": r.get("level"), "rule_id": r.get("rule_id"), "date": r.get("date")} for r in rows]


def _modulation(plan: Mapping | None, now: datetime, snap: Mapping, fresh: Mapping, alerts: Sequence[Mapping]) -> dict | None:
    if plan is None:
        return None
    day = now.date().isoformat()
    band = (snap.get("readiness") or {}).get("band")
    red = any(a.get("level") == "urgent" or a.get("date") == day for a in alerts)
    stale = any(fresh.get(k, {}).get("stale", True) for k in STALE_BLOCKING)
    return plans.modulate_day(plan, now.weekday(), band, red, stale)


def _inbox(ledger: Ledger, bot: str) -> list[dict]:
    msgs = [ledger.read_json(rel, default={}) for rel in ledger.glob(f"mail/{bot}/inbox/*.json")]
    msgs = sorted((m for m in msgs if isinstance(m, Mapping)), key=lambda m: str(m.get("created_at")))
    keys = ("from", "kind", "created_at", "body")
    return [{k: m.get(k) for k in keys} for m in msgs[-INBOX_MAX:]]


def _checkins_7d(store: Store, day: str) -> list[dict]:
    rows = store.checkins(_shift(day, -6), _shift(day, 1))
    return [{"date": c.date, "item": c.item, "value": c.value, "ref": c.ref}
            for c in rows if c.item != checkins.NOTE_ITEM]


def _recent_notes(store: Store, day: str) -> str:
    notes = [c.text for c in store.checkins(_shift(day, -6), _shift(day, 1), item=checkins.NOTE_ITEM) if c.text]
    return "\n".join(notes)[-NOTES_MAX:]


def _today_de(now: datetime) -> str:
    return f"{now.date().isoformat()}, {plans.WEEKDAYS_DE[now.weekday()]}, {now:%H:%M}"


def _coach_context(ctx: Context, bot: str, task: str, now: datetime) -> dict:
    day = now.date().isoformat()
    snap = _today_snapshot(ctx, day)
    fresh = metrics.freshness(ctx.store.latest_ts(), now, weekly.FRESHNESS_H)
    alerts = _recent_alerts(ctx.ledger, day)
    plan = _plan_or_none(ctx.ledger)
    return {
        "task": task, "today": _today_de(now), "plan_current": plan,
        "modulation": _modulation(plan, now, snap, fresh, alerts), "daily": snap, "freshness": fresh,
        "alerts": alerts, "inbox": _inbox(ctx.ledger, bot), "checkins_7d": _checkins_7d(ctx.store, day),
        "ed_watch": _ed_watch(ctx.ledger),
    }


def _ask(ctx: Context, name: str, context: Mapping[str, Any], schema: Mapping[str, Any], run_id: str,
         untrusted: Mapping[str, str] | None = None) -> tuple[dict, agents.LLMResponse]:
    bot = ctx.roster[name]
    system, user = agents.render_prompt(bot, PROMPTS_DIR, context, untrusted)
    req = agents.LLMRequest(bot=name, system=system, messages=({"role": "user", "content": user},),
                            output_schema=schema, max_tokens=bot.max_tokens, effort=bot.effort)
    resp = agents.call(bot, req, ctx.providers, ctx.budget, run_id)
    return dict(resp.structured or {}), resp


def _safe_reply(structured: Mapping[str, Any]) -> str | None:
    """The coach's reply, or None when it is empty or holds banned diagnostic vocabulary."""
    text = str(structured.get("reply_de") or "").strip()
    return text if text and not guardrails.scan_text(text) else None


def _store_chat_checkin(ctx: Context, extracted: Any, now: datetime) -> int:
    if not isinstance(extracted, Mapping) or not extracted:
        return 0
    try:
        items = checkins.from_chat(extracted, date=now.date().isoformat(), ts=now.isoformat(timespec="seconds"))
    except checkins.CheckinError:
        return 0
    return ctx.store.put_checkins(items) if items else 0


def _checkin_alerts_now(ctx: Context, day: str) -> int:
    """Check-in red flags (symptoms, self-harm language) right away instead of at the next hour."""
    items = ctx.store.checkins(day, _shift(day, 1))
    return deliver_alerts(ctx, guardrails.red_flags({}, [], items, day, _profile(ctx.ledger)))


# --------------------------------------------------------------------------
# Owner messages


def _urgent_symptoms(text: str) -> list[str]:
    norm = guardrails.normalize_name(text)
    return [s for s, patterns in _URGENT_RES.items() if any(rx.search(norm) for rx in patterns)]


def _self_harm_alert(text: str, day: str, ts: str) -> Alert | None:
    """guardrails' self-harm check on the raw message, through a check-in note that is not stored."""
    note = CheckinItem(date=day, item=checkins.NOTE_ITEM, value=None, text=text, ref=None, ts=ts,
                       wording_version=checkins.WORDING_VERSION, origin="coach")
    found = [a for a in guardrails.red_flags({}, [], [note], day, {}) if a.rule_id == "self_harm"]
    return found[0] if found else None


def _message_problem(to: str, text: Any) -> str | None:
    if to not in OWNER_TARGETS:
        return f"unknown recipient {to!r}"
    if not isinstance(text, str) or not text.strip():
        return "empty message"
    return "message too long" if len(text) > MESSAGE_MAX else None


def _code_reply(ctx: Context, alert: Alert, symptoms: Sequence[str], now: datetime) -> dict:
    """Fixed template reply without an LLM call; symptoms are stored and the alert goes out."""
    stored = _store_chat_checkin(ctx, {"symptoms": list(symptoms)} if symptoms else {}, now)
    deliver_alerts(ctx, [alert])
    return {"reply_de": guardrails.render_alert(alert, _profile(ctx.ledger)), "checkin_stored": stored}


def _short_circuit(ctx: Context, text: str, now: datetime) -> dict | None:
    day, ts = now.date().isoformat(), now.isoformat(timespec="seconds")
    urgent = _urgent_symptoms(text)
    if urgent:
        return _code_reply(ctx, Alert("urgent", "symptom_urgent", day, {"symptoms": urgent}), urgent, now)
    harm = _self_harm_alert(text, day, ts)
    return _code_reply(ctx, harm, (), now) if harm else None


def _message_run_id(to: str, now: datetime) -> str:
    return f"r-msg-{now:%Y%m%dT%H%M%S}-{to}"


def _coach_reply(ctx: Context, bot: str, text: str, now: datetime) -> tuple[str, int]:
    context = _coach_context(ctx, bot, "reply", now)
    untrusted = {"owner_message": text}
    if bot == "vagus":
        untrusted["checkin_notes"] = _recent_notes(ctx.store, now.date().isoformat())
    out, _ = _ask(ctx, bot, context, COACH_REPLY_SCHEMA, _message_run_id(bot, now), untrusted)
    return _safe_reply(out) or FALLBACK_DE, _store_chat_checkin(ctx, out.get("checkin"), now)


def _goal_update(ctx: Context, update: str, resp: agents.LLMResponse, now: datetime) -> None:
    sha = hashlib.sha256(update.encode("utf-8")).hexdigest()
    msg = agents.make_message(
        sender="cerebellum", to=list(GOAL_RECIPIENTS), kind="goal.update", thread="goals", round_=0,
        max_rounds=0, refs=["config/goals.md"], data_sha=sha, run_id=_message_run_id("cerebellum", now),
        model=resp.model, body=update, created_at=_utc_z(now),
    )
    for bot in GOAL_RECIPIENTS:
        ctx.ledger.write_json(f"mail/{bot}/inbox/{msg['id']}.json", msg, author="cerebellum")


def _apply_goal_talk(ctx: Context, out: Mapping[str, Any], resp: agents.LLMResponse, now: datetime) -> None:
    goals, update = out.get("goals_md"), out.get("goal_update")
    if isinstance(goals, str) and goals.strip() and not guardrails.scan_text(goals):
        ctx.ledger.write_text("config/goals.md", goals.rstrip() + "\n", author="cerebellum")
    if isinstance(update, str) and update.strip() and not guardrails.scan_text(update):
        _goal_update(ctx, update.strip(), resp, now)


def _cerebellum_reply(ctx: Context, _bot: str, text: str, now: datetime) -> tuple[str, int]:
    goals = ctx.ledger.read_text("config/goals.md") if ctx.ledger.exists("config/goals.md") else ""
    context = {
        "task": "goal_talk", "week": _iso_week(now.date()), "goals_md": goals,
        "plan_current": _plan_or_none(ctx.ledger),
        "review": {"guard_context": _latest_packet(ctx.ledger)[1].get("guard_context")}, "known_refs": [],
    }
    out, resp = _ask(ctx, "cerebellum", context, GOAL_TALK_SCHEMA, _message_run_id("cerebellum", now),
                     {"owner_message": text})
    _apply_goal_talk(ctx, out, resp, now)
    return _safe_reply(out) or FALLBACK_DE, 0


_ROUTES: Mapping[str, Callable[[Context, str, str, datetime], tuple[str, int]]] = MappingProxyType({
    "sartorius": _coach_reply, "vagus": _coach_reply, "cerebellum": _cerebellum_reply,
})


def _commit_pending(ctx: Context, author: str, run_id: str) -> None:
    """The server process writes alerts, mail and budget rows; commit them under the bot identity."""
    if ctx.dry_run or not ctx.ledger.pending():
        return
    try:
        ctx.ledger.commit(author=author, message=f"owner message to {author}",
                          trailers={"Run-Id": run_id, "Kind": "owner.message"})
    except Exception:  # noqa: BLE001 - a git failure must not lose the reply; paths stay pending
        return


def _routed(ctx: Context, to: str, text: str, now: datetime) -> tuple[int, dict]:
    try:
        reply, stored = _ROUTES[to](ctx, to, text, now)
    except (agents.BudgetExceeded, agents.ProviderError):
        return 503, {"reply_de": FALLBACK_DE, "checkin_stored": 0}
    if stored:
        _checkin_alerts_now(ctx, now.date().isoformat())
    return 200, {"reply_de": reply, "checkin_stored": stored}


def handle_owner_message(ctx: Context, to: str, text: str) -> tuple[int, dict]:
    """POST /message: to in OWNER_TARGETS; reply {'reply_de', 'checkin_stored'}.

    Urgent symptom keywords and self-harm language short-circuit to the fixed
    template (112, Telefonseelsorge) without an LLM call.
    """
    problem = _message_problem(to, text)
    if problem:
        return 400, {"error": problem}
    now = _local_now(ctx)
    fixed = _short_circuit(ctx, text, now)
    status, body = (200, fixed) if fixed else _routed(ctx, to, text, now)
    _commit_pending(ctx, to, _message_run_id(to, now))
    return status, body


# --------------------------------------------------------------------------
# Job functions


def _llm_job(fn: Callable[[Context, str], JobResult]) -> Callable[[Context, str], JobResult]:
    """Budget refusals and unreachable providers defer the job; they are not incidents."""

    @functools.wraps(fn)
    def run(ctx: Context, period: str) -> JobResult:
        try:
            return fn(ctx, period)
        except agents.BudgetExceeded:
            return JobResult("deferred", (), "budget")
        except agents.ProviderUnavailable:
            return JobResult("deferred", (), "provider unavailable")

    return run


def _llm_args(ctx: Context) -> dict:
    return {"roster": ctx.roster, "providers": ctx.providers, "budget": ctx.budget, "prompts_dir": PROMPTS_DIR}


def _job_ingest_prune(ctx: Context, _period: str) -> JobResult:
    raw_dir = Path(os.environ.get(RAW_ENV) or ctx.ledger.root / ingest.PRIVATE_DIR / "raw")
    removed = ingest.prune_raw(raw_dir, RAW_RETENTION_DAYS, ctx.now())
    return JobResult("ok", (), f"{removed} raw payloads pruned")


def _job_freshness(ctx: Context, _period: str) -> JobResult:
    fresh = metrics.freshness(ctx.store.latest_ts(), ctx.now(), weekly.FRESHNESS_H)
    ctx.ledger.write_json(FRESHNESS_PATH, fresh, author=WRITER)
    stale = sorted(k for k, v in fresh.items() if v["stale"])
    return JobResult("ok", (FRESHNESS_PATH,), "stale: " + (", ".join(stale) or "none"))


def _job_heartbeat(ctx: Context, _period: str) -> JobResult:
    day = _local_now(ctx).date().isoformat()
    beats = [ctx.ledger.read_json(rel, default={}) for rel in ctx.ledger.glob(f"{HEARTBEAT_DIR}/*.json")]
    failing = [b for b in beats if isinstance(b, Mapping) and b.get("status") == "failed"]
    for beat in failing:
        text = JOB_FAILED_DE.substitute(job=beat.get("job"), period=beat.get("period"))
        _notify_once(ctx, f"job_failed:{beat.get('job')}", day, text, "info")
    return JobResult("ok", (), f"{len(failing)} failing jobs")


def _job_budget(ctx: Context, _period: str) -> JobResult:
    level = ctx.budget.level()
    if level in BUDGET_DE:
        text = BUDGET_DE[level].substitute(pct=round(ctx.budget.reduce_at * 100))
        _notify_once(ctx, f"budget_{level}", _iso_week(_local_now(ctx).date()), text, "info")
    return JobResult("ok", (), f"budget {level}")


def _job_red_flags(ctx: Context, _period: str) -> JobResult:
    day = _local_now(ctx).date().isoformat()
    metrics.finalize_day(ctx.store, day, ctx.metrics_cfg)
    since = _shift(day, -RED_FLAG_LOOKBACK_DAYS)
    daily = {key: ctx.store.daily(key, since, _shift(day, 1)) for key in RED_FLAG_KEYS}
    rhr = [metrics.rhr_state(daily["rhr"], _shift(day, -k)) for k in (2, 1, 0)]
    alerts = guardrails.red_flags(daily, rhr, ctx.store.checkins(day, _shift(day, 1)), day, _profile(ctx.ledger))
    alerts += guardrails.event_alerts(ctx.store.events("hr_notification", _shift(day, -1)))
    sent = deliver_alerts(ctx, alerts)
    return JobResult("ok", (ALERTS_LOG,) if sent else (), f"{sent} alerts delivered")


def _scout_job(scout: str) -> Callable[[Context, str], JobResult]:
    def run(ctx: Context, _period: str) -> JobResult:
        keywords = ctx.ledger.read_json("config/keywords.json")
        if not isinstance(keywords, Mapping) or not keywords:
            return JobResult("skipped", (), "no keywords")
        if ctx.budget.level() != "normal":
            return JobResult("skipped", (), "scouts paused by budget")
        now = _local_now(ctx)
        found = scouts.run_scout(scout, keywords, ctx.http, ctx.ledger, now)
        rel = f"evidence/candidates/{now.date().isoformat()}.jsonl"
        return JobResult("ok", (rel,) if found else (), f"{len(found)} candidates")

    run.__name__ = f"scout_{scout}"
    return run


def _job_finalize_day(ctx: Context, period: str) -> JobResult:
    """At 06:30: yesterday's daily rows, the week's baselines (with readiness) and conflicts, belief views."""
    day = _shift(period, -1)
    week = _iso_week(Date.fromisoformat(day))
    rows = metrics.finalize_day(ctx.store, day, ctx.metrics_cfg)
    paths = (f"data/daily/{day[:4]}/{day}.json", f"data/baselines/{week}.json", f"data/conflicts/{week}.json")
    daily = {r.key: {k: v for k, v in asdict(r).items() if k not in ("date", "key")} for r in rows}
    ctx.ledger.write_json(paths[0], {"date": day, "rows": daily}, author=WRITER)
    ctx.ledger.write_json(paths[1], metrics.baselines(ctx.store, day, ctx.metrics_cfg), author=WRITER)
    ctx.ledger.write_json(paths[2], insula.weekly_reconciliation(ctx.store, week, ctx.metrics_cfg), author=WRITER)
    views = beliefs.rebuild_views(ctx.ledger, day)
    return JobResult("ok", (*paths, *views), f"finalized {day}")


def _coach_job(name: str, bot: str, task: str) -> Callable[[Context, str], JobResult]:
    @_llm_job
    def run(ctx: Context, period: str) -> JobResult:
        now = _local_now(ctx)
        context = _coach_context(ctx, bot, task, now)
        if bot == "sartorius" and context["plan_current"] is None:
            return JobResult("skipped", (), "no plan")
        out, resp = _ask(ctx, bot, context, COACH_REPLY_SCHEMA, _run_id(name, period))
        text = _safe_reply(out)
        if text is None:
            return JobResult("failed", (), "reply rejected by scan_text")
        ctx.notifier.send("owner", text, "info")
        stored = _store_chat_checkin(ctx, out.get("checkin"), now)
        return JobResult("ok", (), f"{bot} {task} sent, {stored} check-in items")

    run.__name__ = name
    return run


@_llm_job
def _job_thalamus(ctx: Context, _period: str) -> JobResult:
    now = _local_now(ctx)
    state = ctx.ledger.read_json(weekly.THALAMUS_BATCH)
    running = isinstance(state, Mapping) and state.get("status") == "submitted"
    if not running and now.weekday() != THALAMUS_WEEKDAY:
        return JobResult("skipped", (), "no batch running")
    written = weekly.run_thalamus(ctx.ledger, ctx.http, _owner_profile(ctx.ledger, now), now, **_llm_args(ctx))
    return JobResult("ok", tuple(written), f"{len(written)} papers scored")


def _owner_profile(ledger: Ledger, now: datetime) -> OwnerProfile:
    p = _profile(ledger)
    birth, swc = p.get("birth_year"), p.get("swc")
    age = now.year - birth if isinstance(birth, int) and not isinstance(birth, bool) else None
    return OwnerProfile(
        sex=p.get("sex"), age=age, training_status=p.get("training_status"),
        conditions=tuple(str(c) for c in p.get("conditions") or ()),
        swc={str(k): float(v) for k, v in swc.items()} if isinstance(swc, Mapping) else {},
    )


@_llm_job
def _job_insula(ctx: Context, period: str) -> JobResult:
    result = weekly.run_insula(period, ctx.ledger, ctx.store, ctx.metrics_cfg, **_llm_args(ctx))
    rel = f"reviews/{period}/{weekly.INSULA_FILE}"
    return JobResult("ok", (result.get("ref", ""), rel), f"{len(result.get('items', []))} conflicts explained")


@_llm_job
def _job_motor_cortex(ctx: Context, period: str) -> JobResult:
    if _plan_or_none(ctx.ledger) is None:
        return JobResult("skipped", (), "no plan")
    result = weekly.run_motor_cortex(period, ctx.ledger, guardrails.load_rules(ctx.ledger), **_llm_args(ctx))
    rel = f"reviews/{period}/{weekly.MOTOR_CORTEX_FILE}"
    return JobResult("ok", (rel,), f"{len(result.get('admitted', []))} proposals admitted")


def _job_weekly_review(ctx: Context, period: str) -> JobResult:
    now = ctx.now()
    packet = weekly.build_packet(ctx.store, ctx.ledger, period, ctx.metrics_cfg, now)
    outcome = weekly.run_review(
        period, packet, **_llm_args(ctx), ledger=ctx.ledger, guard_ctx=weekly.guard_context_from_packet(packet),
        rules=guardrails.load_rules(ctx.ledger), now=now,
    )
    status = "deferred" if outcome.aborted in ("budget", "provider") else "ok"
    message = f"ruling {outcome.ruling}" + (f", aborted {outcome.aborted}" if outcome.aborted else "")
    return JobResult(status, (f"reviews/{period}/packet.json", *outcome.messages), message, {"Kind": "ruling"})


def _job_ratify_due(ctx: Context, _period: str) -> JobResult:
    rel, _ = _latest_packet(ctx.ledger)
    versions = plans.ratify_due(ctx.ledger, ctx.now(), ctx.ledger.sha256(rel) if rel else None)
    for version in versions:
        ctx.notifier.send("owner", PLAN_RATIFIED_DE.substitute(version=f"v{version:04d}"), "info")
    written = tuple(f"plans/v{v:04d}.json" for v in versions)
    return JobResult("ok" if versions else "skipped", written, f"{len(versions)} versions ratified")


def _job_monthly_report(ctx: Context, period: str) -> JobResult:
    month = _previous_month(period)
    rows = ctx.ledger.read_jsonl(agents.BUDGET_LOG)
    usd = sum(float(r.get("usd", 0.0)) for r in rows if str(r.get("date", "")).startswith(month))
    done = len(ctx.ledger.glob("experiments/*/result.json"))
    text = MONTHLY_DE.substitute(month=month, usd=f"{usd:.2f}".replace(".", ","), experiments=done)
    sent = _notify_once(ctx, "monthly_report", month, text, "info")
    return JobResult("ok" if sent else "skipped", (), f"report {month}")


def default_jobs() -> list[Job]:
    """The standing schedule, in the owner's time zone."""
    return [
        Job("ingest-prune", "daily", "02:30", None, None, True, _job_ingest_prune),
        Job("freshness", "hourly", ":05", None, None, False, _job_freshness),
        Job("heartbeat", "hourly", ":05", None, None, False, _job_heartbeat),
        Job("budget", "hourly", ":05", None, None, False, _job_budget),
        Job("red_flags", "hourly", ":05", None, None, True, _job_red_flags),
        *(Job(s, "daily", "03:00", None, s, False, _scout_job(s)) for s in ("retina", "spindle", "papilla")),
        Job("thalamus", "daily", "04:00", None, "thalamus", False, _job_thalamus),
        Job("finalize_day", "daily", "06:30", None, None, True, _job_finalize_day),
        Job("sartorius_morning", "daily", "07:00", None, "sartorius", False,
            _coach_job("sartorius_morning", "sartorius", "morning")),
        Job("vagus_morning", "daily", "07:00", None, "vagus", False, _coach_job("vagus_morning", "vagus", "morning")),
        Job("vagus_evening", "daily", "21:00", None, "vagus", False, _coach_job("vagus_evening", "vagus", "evening")),
        Job("insula", "weekly", "08:00", 5, "insula", False, _job_insula),
        Job("motor_cortex", "weekly", "09:00", 5, "motor_cortex", False, _job_motor_cortex),
        Job("weekly_review", "weekly", "17:00", 6, None, True, _job_weekly_review),
        Job("ratify_due", "minute", "*/15", None, None, False, _job_ratify_due),
        Job("monthly_report", "monthly", "08:00", None, None, False, _job_monthly_report),
    ]

"""Command line entry point: ``python -m homunculus <command>``.

Wires config, Store, Ledger, providers and the Brainstem routes. Every LLM
path takes ``--dry-run``, which swaps every provider for ``FakeProvider``.

Exit codes: 0 ok, 1 runtime error, 2 usage or config error, 3 halted.

The ledger path comes from ``--ledger`` or ``HOMUNCULUS_LEDGER`` and must lie
outside this engine repo, which is public. ``init-ledger`` creates the private
ledger skeleton; ``demo`` runs the whole pipeline on synthetic data in a fresh
temporary ledger and never reads the configured one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from homunculus import (
    agents, beliefs, checkins, dashboard, deepdive, evidence, experiments, guardrails, ingest, lexicon, metrics, plans,
    scouts, stats, weekly,
)
from homunculus.evidence import OwnerProfile
from homunculus.ingest import ParsedBatch
from homunculus.metrics import MetricsConfig
from homunculus.store import SOURCES, DailyValue, Ledger, Store, canonical_json
from homunculus.trackers import registry as tracker_registry
from homunculus.trackers import run as tracker_run
from homunculus.trackers.base import TrackerConfig, TrackerError

EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, EXIT_HALTED = 0, 1, 2, 3
ENGINE_ROOT = Path(__file__).resolve().parent.parent
ENGINE_ROSTER = ENGINE_ROOT / "config" / "roster.json"
CODE_AUTHOR = "pineal"
DEFAULT_TZ = "UTC"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
BASELINE_DAYS = 28
MEAN90_DAYS = 90
EXTENSION_PAIRS = 2
FINAL_DECISIONS = frozenset({"adopt", "reject", "inconclusive"})
PUSH_URL_ENV = "HOMUNCULUS_PUSH_URL"
PUSH_TOKEN_ENV = "HOMUNCULUS_PUSH_TOKEN"
PUSH_TIMEOUT_S = 10.0
_PUSH_PRIORITY = {"urgent": "urgent", "prompt": "high"}
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
_VERSION_RE = re.compile(r"^v?(\d+)$")

LEDGER_DIRS: tuple[str, ...] = (
    "config", "profile", "data/daily", "data/baselines", "data/conflicts", "checkins",
    "evidence/candidates", "evidence/papers", "evidence/cache", "quarantine", "beliefs/views",
    "experiments", "plans/pending", "reviews", "mail/owner/inbox", "ops/heartbeats", "ops/incidents",
)
LEDGER_GITIGNORE = (
    "# Raw samples, the SQLite store and the rendered dashboard stay on the box.\n"
    ".private/\nraw/\nsite/\n*.sqlite\n*.sqlite-journal\n*.sqlite-wal\n*.sqlite-shm\n"
)
LEDGER_README = """# homunculus ledger

This repository holds personal health data for one person. It must stay private.

- Never make it public, never fork it into a public account, never publish it with GitHub Pages.
- Only the box running homunculus has a deploy key. Only Pineal commits here.
- Raw samples stay in `.private/` (SQLite, git-ignored). Git holds aggregates computed by code.
- Deleting data later means rewriting history, so think before you commit anything new.

Dieses Repo enthält persönliche Gesundheitsdaten und muss privat bleiben.
"""
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-fable-5-2": {"input": 8.0, "output": 40.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
}
OWNER_TEMPLATE: dict[str, Any] = {
    "tz": DEFAULT_TZ, "sex": None, "birth_year": None, "training_status": None, "sleep_target_h": 7.5,
    "hrmax": None, "hrrest": None, "work_days": [0, 1, 2, 3, 4], "conditions": [], "swc": {},
    "injury_constraints": {}, "doctor_contact": None, "ffm_kg": None,
}


def _config_templates() -> dict[str, Any]:
    """Ledger-relative path -> JSON object or text for a fresh ledger."""
    roster = json.loads(ENGINE_ROSTER.read_text(encoding="utf-8"))
    empty_scout = {"queries": [], "terms": [], "pub_type_weights": {}}
    return {
        "config/goals.md": "# Ziele\n\nNoch keine Ziele festgelegt. Das Zielgespräch mit Cerebellum füllt diese Datei.\n",
        "config/roster.json": roster,
        "config/cadence.json": {"schema": 1, "jobs": {}},
        "config/budget.json": {"weekly_cap_usd": 40.0, "reduce_at": 0.8, "prices": PRICES_USD_PER_MTOK},
        "config/keywords.json": {s: dict(empty_scout) for s in sorted(agents.SCOUTS)},
        "config/readiness.json": dict(metrics.DEFAULT_READINESS_WEIGHTS),
        "config/guardrails.json": {},
        "config/forbidden.json": {},
        "config/supplements.json": {"allowlist": {}, "interactions": []},
        "config/exercises.json": {},
        "profile/owner.json": dict(OWNER_TEMPLATE),
        "profile/meds.json": [],
    }


class UsageError(Exception):
    """Bad arguments or configuration (exit 2)."""


class Halted(Exception):
    """config/HALT is set and the command is not essential (exit 3)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _print(*parts: Any) -> None:
    print(*parts, flush=True)


def _inside(path: Path, root: Path) -> bool:
    resolved, base = path.resolve(), root.resolve()
    return resolved == base or base in resolved.parents


def _iso_week(day: str) -> str:
    year, week, _ = date.fromisoformat(day).isocalendar()
    return f"{year}-W{week:02d}"


def _shift(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _day_arg(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise UsageError(f"expected a date YYYY-MM-DD, got {text!r}") from None


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read JSON from {path}: {exc}") from None


# --------------------------------------------------------------------------
# Shared pipeline helpers (also used by homunculus.demo)


def store_batch(store: Store, batch: ParsedBatch) -> dict[str, int]:
    """Store a parsed Health Auto Export batch; counts per table."""
    return {
        "samples": store.upsert_samples(batch.samples),
        "sleep_segments": store.upsert_sleep_segments(batch.sleep_segments),
        "sleep_nights": store.upsert_sleep_nights(batch.sleep_nights),
        "workouts": store.upsert_workouts(batch.workouts),
        "events": store.insert_events(batch.events),
    }


def batch_days(batch: ParsedBatch) -> list[str]:
    """Every local date the batch touches, plus the following wake date, in order."""
    stamps = [s.ts for s in batch.samples] + [s.start for s in batch.sleep_segments]
    stamps += [w.start for w in batch.workouts] + [n.wake_date for n in batch.sleep_nights]
    days = sorted({str(ts)[:10] for ts in stamps if str(ts)[:10]})
    if not days:
        return []
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1]) + timedelta(days=1)
    return [(first + timedelta(days=k)).isoformat() for k in range((last - first).days + 1)]


def _daily_doc(day: str, rows: Sequence[DailyValue]) -> dict:
    return {"date": day, "values": {r.key: {"value": r.value, "source": r.source, "n": r.n, "quality": r.quality}
                                    for r in rows}}


def finalize_days(store: Store, ledger: Ledger, cfg: MetricsConfig, days: Sequence[str],
                  now: datetime) -> dict[str, list[DailyValue]]:
    """Daily rows for each day, data/daily files, baselines for the last day's week and freshness."""
    out: dict[str, list[DailyValue]] = {}
    for day in days:
        out[day] = metrics.finalize_day(store, day, cfg)
        ledger.write_json(f"data/daily/{day[:4]}/{day}.json", _daily_doc(day, out[day]), author=CODE_AUTHOR)
    if days:
        last = days[-1]
        ledger.write_json(f"data/baselines/{_iso_week(last)}.json", metrics.baselines(store, last, cfg),
                          author=CODE_AUTHOR)
    fresh = metrics.freshness(store.latest_ts(), now, weekly.FRESHNESS_H)
    ledger.write_json("data/freshness.json", fresh, author=CODE_AUTHOR)
    return out


def metrics_config(owner: Mapping[str, Any], readiness: Any, tz: str) -> MetricsConfig:
    weights = {k: float(v) for k, v in (readiness or {}).items() if isinstance(v, (int, float))}
    return MetricsConfig.from_dict({
        "tz": tz, "source_priority": owner.get("source_priority") or {},
        "sleep_target_h": owner.get("sleep_target_h") or 7.5, "hrmax": owner.get("hrmax"),
        "hrrest": owner.get("hrrest"), "sex": owner.get("sex"),
        "work_days": owner.get("work_days") or (0, 1, 2, 3, 4),
        "readiness_weights": weights or dict(metrics.DEFAULT_READINESS_WEIGHTS),
    })


def _owner_age(owner: Mapping[str, Any], today: date) -> int | None:
    if isinstance(owner.get("age"), int):
        return int(owner["age"])
    year = owner.get("birth_year")
    return today.year - int(year) if isinstance(year, int) else None


def owner_profile(owner: Mapping[str, Any], today: date) -> OwnerProfile:
    swc = {str(k): float(v) for k, v in (owner.get("swc") or {}).items() if isinstance(v, (int, float))}
    return OwnerProfile(sex=owner.get("sex"), age=_owner_age(owner, today),
                        training_status=owner.get("training_status"),
                        conditions=tuple(str(c) for c in owner.get("conditions") or ()), swc=swc)


def known_certainties(ledger: Ledger) -> dict[str, float]:
    """dedupe key -> thal-2 certainty of every scored paper in the ledger, for deepdive.decide."""
    out: dict[str, float] = {}
    for rel in ledger.glob("evidence/papers/*.json"):
        rec = ledger.read_json(rel) if not rel.endswith(".audit.json") else None
        certainty = ((rec or {}).get("score") or {}).get("certainty") if isinstance(rec, Mapping) else None
        if isinstance(certainty, (int, float)):
            try:
                out[evidence.dedupe_key(rec["extraction"])] = float(certainty)
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _paper_key(extraction: Mapping[str, Any]) -> str | None:
    try:
        return evidence.dedupe_key(extraction)
    except ValueError:
        return None


def appraise_record(ledger: Ledger, record: Mapping[str, Any], owner: OwnerProfile) -> tuple[Any, Any]:
    """(Score, Appraisal) of a paper record: lexicon replications, and deep-dive promise inputs when a dive exists."""
    extraction = record["extraction"]
    key = _paper_key(extraction)
    swc = _paper_swc(extraction, owner)
    replications = lexicon.replications(lexicon.load_view(ledger), key) if key else 0
    score = evidence.score(extraction, owner, update_to=record.get("update_to") or [], swc_paper_units=swc,
                           replications=replications)
    dive = ledger.read_json(deepdive.record_rel(key), default=None) if key else None
    base = evidence.PromiseInput(swc_paper_units=swc)
    inputs = deepdive.promise_inputs(dive, base) if isinstance(dive, Mapping) else base
    return score, evidence.appraise(extraction, score, inputs)


def _dive_check(dive: Any) -> list[str]:
    return deepdive.validate(dive)


def run_deep_dive(ledger: Ledger, llm: Mapping[str, Any], paper_rel: str, source_text: str,
                  now: datetime) -> dict:
    """Thalamus deep dive of the paper at paper_rel over source_text; code decides and stores the record.

    The Thalamus answer is re-asked once when it fails the schema or chain checks.
    """
    record = ledger.read_json(paper_rel)
    if not isinstance(record, Mapping) or not isinstance(record.get("extraction"), Mapping):
        raise UsageError(f"{paper_rel} holds no paper extraction")
    key = evidence.dedupe_key(record["extraction"])
    view = lexicon.load_view(ledger)
    meta = {"key": key, "doi": record.get("doi"), "title": (record.get("meta") or {}).get("title")}
    context = {"paper_meta": deepdive.request_meta(meta, view)}
    run_id = f"dd-{now:%Y%m%dT%H%M%SZ}-{evidence.slug(key)}"[:120]
    try:
        dive, _ = weekly._caller(llm).ask("thalamus", context, deepdive.DEEPDIVE_SCHEMA, run_id, _dive_check,
                                          {"abstract": source_text})
    except weekly._Rejected as exc:
        raise RuntimeError(f"deep dive rejected after one re-ask: {exc}") from None
    decided = deepdive.decide(dive, source_text=source_text, rules=guardrails.load_rules(ledger), paper_key=key,
                              paper_ref=paper_rel, at=now.isoformat(timespec="seconds"),
                              known=known_certainties(ledger), view=view)
    deepdive.store(ledger, decided)
    return decided


def commit(ledger: Ledger, kind: str, message: str, *, author: str = CODE_AUTHOR) -> str | None:
    """Commit pending ledger writes when the ledger is a git repo; None otherwise."""
    if not (ledger.root / ".git").is_dir() or not ledger.pending():
        return None
    stamp = _utcnow().strftime("%Y%m%dT%H%M%SZ")
    return ledger.commit(author=author, message=message, trailers={"Kind": kind, "Run-Id": f"cli-{kind}-{stamp}"})


# --------------------------------------------------------------------------
# init-ledger


def _check_new_ledger(root: Path) -> None:
    if _inside(root, ENGINE_ROOT):
        raise UsageError(f"refusing a ledger inside the public engine repo: {root}")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise UsageError(f"refusing to initialize a non-empty path: {root}")


def _write_template(root: Path, rel: str, content: Any) -> None:
    text = content if isinstance(content, str) else json.dumps(content, indent=2, sort_keys=True,
                                                                ensure_ascii=False) + "\n"
    (root / rel).write_text(text, encoding="utf-8")


def _git_init(root: Path) -> bool:
    if shutil.which("git") is None:
        return False
    proc = subprocess.run(["git", "init", "--quiet", str(root)], capture_output=True, text=True, check=False)
    return proc.returncode == 0


def init_ledger(path: str | Path, *, git: bool = True) -> list[str]:
    """Create the private ledger skeleton at path and git-init it; returns the files written."""
    root = Path(path).expanduser().absolute()
    _check_new_ledger(root)
    for rel in LEDGER_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
        (root / rel / ".gitkeep").touch()
    private = root / ingest.PRIVATE_DIR
    private.mkdir(mode=0o700, exist_ok=True)
    os.chmod(private, 0o700)
    files = {".gitignore": LEDGER_GITIGNORE, "README.md": LEDGER_README, **_config_templates()}
    for rel, content in files.items():
        _write_template(root, rel, content)
    if git and not _git_init(root):
        raise RuntimeError(f"git init failed in {root}")
    return sorted(files)


# --------------------------------------------------------------------------
# Application context


def _ledger_root(args: argparse.Namespace, env: Mapping[str, str]) -> Path:
    raw = args.ledger or env.get("HOMUNCULUS_LEDGER")
    if not raw:
        raise UsageError("no ledger: pass --ledger PATH or set HOMUNCULUS_LEDGER")
    root = Path(raw).expanduser().absolute()
    if not root.is_dir():
        raise UsageError(f"ledger not found: {root} (create it with init-ledger)")
    if _inside(root, ENGINE_ROOT):
        raise UsageError(f"refusing a ledger inside the public engine repo: {root}")
    return root


class App:
    """Lazily built objects shared by the commands of one invocation."""

    def __init__(self, args: argparse.Namespace, env: Mapping[str, str]) -> None:
        self.args = args
        self.env = env

    def now(self) -> datetime:
        return _utcnow()

    @cached_property
    def root(self) -> Path:
        return _ledger_root(self.args, self.env)

    @cached_property
    def roster(self) -> dict[str, agents.BotSpec]:
        path = self.root / "config" / "roster.json"
        raw = _read_json_file(path if path.is_file() else ENGINE_ROSTER)
        try:
            return agents.load_roster(raw)
        except ValueError as exc:
            raise UsageError(f"invalid roster: {exc}") from None

    @cached_property
    def ledger(self) -> Ledger:
        return Ledger(self.root, agents.write_rules(self.roster))

    @cached_property
    def db_path(self) -> Path:
        raw = self.args.db or self.env.get("HEALTH_DB")
        return Path(raw).expanduser() if raw else self.root / ingest.PRIVATE_DIR / "health.sqlite"

    @cached_property
    def store(self) -> Store:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(self.db_path)
        store.migrate()
        return store

    @cached_property
    def owner(self) -> dict:
        raw = self.ledger.read_json("profile/owner.json", default={})
        return dict(raw) if isinstance(raw, Mapping) else {}

    @cached_property
    def tz(self) -> str:
        return self.args.tz or self.owner.get("tz") or DEFAULT_TZ

    @cached_property
    def cfg(self) -> MetricsConfig:
        try:
            return metrics_config(self.owner, self.ledger.read_json("config/readiness.json"), self.tz)
        except (KeyError, ValueError) as exc:
            raise UsageError(f"invalid metrics config: {exc}") from None

    @cached_property
    def budget_config(self) -> dict:
        raw = self.ledger.read_json("config/budget.json", default={})
        return dict(raw) if isinstance(raw, Mapping) else {}

    @cached_property
    def providers(self) -> dict[str, agents.Provider]:
        if self.args.dry_run:
            fake = agents.FakeProvider()
            return {"anthropic": fake, "gemini": fake, "devin": fake}
        prices = self.budget_config.get("prices", {})
        out: dict[str, agents.Provider] = {}
        if self.env.get("ANTHROPIC_API_KEY"):
            out["anthropic"] = agents.AnthropicProvider(self.env["ANTHROPIC_API_KEY"], prices)
        if self.env.get("GEMINI_API_KEY"):
            out["gemini"] = agents.GeminiProvider(self.env["GEMINI_API_KEY"], prices)
        return out

    @cached_property
    def budget(self) -> agents.Budget:
        return agents.Budget(self.ledger, self.budget_config, now=self.now)

    @cached_property
    def http(self) -> scouts.Http:
        return scouts.Http(self.root / "evidence" / "cache", mailto=self.env.get("HOMUNCULUS_MAILTO"), keys={},
                           rate_per_s={}, offline=bool(self.args.dry_run))

    def llm(self) -> dict[str, Any]:
        return {"roster": self.roster, "providers": self.providers, "budget": self.budget,
                "prompts_dir": weekly.PROMPTS_DIR}

    def require_running(self) -> None:
        if self.ledger.halted():
            raise Halted("config/HALT is set; remove it with 'resume'")

    def close(self) -> None:
        if "store" in self.__dict__:
            self.store.close()


# --------------------------------------------------------------------------
# Pineal wiring


def _pineal() -> ModuleType:
    try:
        from homunculus import pineal
    except ImportError as exc:
        raise RuntimeError(f"homunculus.pineal is not available: {exc}") from None
    return pineal


class PushNotifier:
    """Pushes owner texts to an ntfy-style URL (POST, body = text), then keeps the inbox copy.

    A failed push raises, so Pineal does not log the alert or notice as delivered and retries it next tick.
    """

    def __init__(self, url: str, inner: Any, *, token: str | None = None,
                 opener: Callable[..., Any] | None = None) -> None:
        self._url, self._inner, self._token = url, inner, token
        self._open = opener or urllib.request.urlopen

    def send(self, to: str, text: str, level: str) -> None:
        if to == "owner":
            self._push(text, level)
        self._inner.send(to, text, level)

    def _push(self, text: str, level: str) -> None:
        headers = {"Title": "homunculus", "Priority": _PUSH_PRIORITY.get(level, "default"),
                   "Content-Type": "text/plain; charset=utf-8"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(self._url, data=text.encode("utf-8"), headers=headers, method="POST")
        with self._open(req, timeout=PUSH_TIMEOUT_S) as resp:
            status = getattr(resp, "status", 200)
        if not 200 <= int(status) < 300:
            raise RuntimeError(f"push to owner failed: HTTP {status}")


def _push_url(env: Mapping[str, str]) -> str | None:
    url = (env.get(PUSH_URL_ENV) or "").strip()
    if not url:
        return None
    if not url.startswith("https://"):
        raise UsageError(f"{PUSH_URL_ENV} must be an https:// URL")
    return url


def _notifier(app: App, pineal: ModuleType) -> Any:
    """The inbox file notifier, wrapped in a PushNotifier when HOMUNCULUS_PUSH_URL is set (path b's phone channel)."""
    inner = pineal.FileNotifier(app.ledger)
    url = None if app.args.dry_run else _push_url(app.env)
    return PushNotifier(url, inner, token=app.env.get(PUSH_TOKEN_ENV) or None) if url else inner


def _pineal_context(app: App, pineal: ModuleType) -> Any:
    return pineal.Context(
        store=app.store, ledger=app.ledger, roster=app.roster, providers=app.providers, budget=app.budget,
        http=app.http, metrics_cfg=app.cfg, notifier=_notifier(app, pineal), tz=app.tz,
        now=app.now, dry_run=bool(app.args.dry_run),
    )


def _message_route(pineal: ModuleType, ctx: Any) -> Callable[[dict], tuple[int, dict]]:
    def route(body: dict) -> tuple[int, dict]:
        to, text = body.get("to"), body.get("text")
        if not isinstance(to, str) or not isinstance(text, str) or not text.strip():
            return 400, {"error": "need 'to' and 'text'"}
        return pineal.handle_owner_message(ctx, to, text)
    return route


def _checkin_route(app: App) -> Callable[[dict], tuple[int, dict]]:
    def route(body: dict) -> tuple[int, dict]:
        return checkins.record(app.store, body, app.now(), app.tz)
    return route


def _events_hook(pineal: ModuleType, ctx: Any) -> Callable[[list], None]:
    def on_events(events: list) -> None:
        alerts = guardrails.event_alerts(events)
        if alerts:
            pineal.deliver_alerts(ctx, alerts)
    return on_events


def serve_kwargs(app: App, pineal: ModuleType, ctx: Any) -> dict[str, Any]:
    """Keyword arguments for ingest.serve: routes, event hook, site dir and owner login."""
    return {
        "post_routes": {"/checkin": _checkin_route(app), "/message": _message_route(pineal, ctx)},
        "on_events": _events_hook(pineal, ctx),
        "site_dir": app.root / "site",
        "owner_login": app.env.get("HOMUNCULUS_OWNER_LOGIN") or None,
    }


def _ingest_config(app: App) -> ingest.IngestConfig:
    env = {**app.env, "HOMUNCULUS_LEDGER": str(app.root), "HEALTH_DB": str(app.db_path)}
    try:
        return ingest.load_config(env)
    except ingest.ConfigError as exc:
        raise UsageError(str(exc)) from None


def _source_map(env: Mapping[str, str]) -> dict[str, str]:
    raw = env.get("HEALTH_SOURCE_MAP")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise UsageError("HEALTH_SOURCE_MAP must be a JSON object") from None
    if not isinstance(parsed, dict) or any(v not in SOURCES for v in parsed.values()):
        raise UsageError(f"HEALTH_SOURCE_MAP values must be one of {SOURCES}")
    return parsed


def _api_sources(env: Mapping[str, str]) -> frozenset[str]:
    """HEALTH_API_SOURCES: sources polled directly, whose Apple Health copies ingest drops (double-count guard)."""
    return ingest._api_sources(env.get("HEALTH_API_SOURCES"))


# --------------------------------------------------------------------------
# Commands: server and scheduler


def cmd_serve(app: App) -> int:
    cfg = _ingest_config(app)
    pineal = _pineal()
    ctx = _pineal_context(app, pineal)
    ingest.serve(cfg, app.store, **serve_kwargs(app, pineal, ctx))
    return EXIT_OK


def _report_runs(results: Sequence[Mapping[str, Any]]) -> int:
    for r in results:
        _print(f"{r.get('job')} {r.get('period')}: {r.get('status')}")
    return EXIT_RUNTIME if any(r.get("status") == "failed" for r in results) else EXIT_OK


def cmd_tick(app: App) -> int:
    pineal = _pineal()
    jobs = pineal.default_jobs()
    if app.args.devin_path:
        from homunculus import devin_layer

        jobs = devin_layer.box_jobs(jobs, app.args.devin_path)
    if app.args.devin_path == "b" and not _push_url(app.env):
        print(f"warning: {PUSH_URL_ENV} is not set; red flags and owner notices on path b reach only "
              "mail/owner/inbox, not the owner's phone", file=sys.stderr)
    code = _report_runs(pineal.tick(_pineal_context(app, pineal), jobs))
    return EXIT_HALTED if code == EXIT_OK and app.ledger.halted() else code


def cmd_run(app: App) -> int:
    pineal = _pineal()
    jobs = {j.name: j for j in pineal.default_jobs()}
    job = jobs.get(app.args.job)
    if job is None:
        raise UsageError(f"unknown job {app.args.job!r}; known: {', '.join(sorted(jobs))}")
    if app.ledger.halted() and not job.essential:
        raise Halted(f"config/HALT is set; {job.name} is not essential")
    period = app.args.period or pineal.period_key(job, app.now())
    return _report_runs([pineal.run_job(_pineal_context(app, pineal), job, period)])


# --------------------------------------------------------------------------
# Commands: data


def cmd_import_history(app: App) -> int:
    payload = _read_json_file(Path(app.args.file))
    sources = _source_map(app.env), _api_sources(app.env)
    try:
        batch = ingest.parse_payload(payload, *sources)
    except ValueError as exc:
        raise UsageError(f"not a Health Auto Export payload: {exc}") from None
    counts = store_batch(app.store, batch)
    days = batch_days(batch)
    finalize_days(app.store, app.ledger, app.cfg, days, app.now())
    commit(app.ledger, "import-history", f"Import history: {len(days)} days")
    _print("imported " + ", ".join(f"{k}={v}" for k, v in counts.items()) + f"; finalized {len(days)} days")
    if batch.skipped:
        _print("skipped " + ", ".join(f"{k}={v}" for k, v in sorted(batch.skipped.items())))
    return EXIT_OK


def cmd_finalize(app: App) -> int:
    day = _day_arg(app.args.day)
    rows = finalize_days(app.store, app.ledger, app.cfg, [day], app.now())[day]
    commit(app.ledger, "finalize", f"Finalize {day}")
    ok = sum(1 for r in rows if r.quality == "ok")
    _print(f"finalized {day}: {ok} of {len(rows)} keys ok")
    return EXIT_OK


def cmd_review(app: App) -> int:
    week = app.args.week
    if not _WEEK_RE.match(week):
        raise UsageError(f"week must look like 2026-W47, got {week!r}")
    app.require_running()
    now = app.now()
    packet = weekly.build_packet(app.store, app.ledger, week, app.cfg, now)
    outcome = weekly.run_review(
        week, packet, roster=app.roster, providers=app.providers, budget=app.budget, ledger=app.ledger,
        guard_ctx=weekly.guard_context_from_packet(packet), rules=guardrails.load_rules(app.ledger),
        prompts_dir=weekly.PROMPTS_DIR, now=now,
    )
    commit(app.ledger, "ruling", f"Weekly review {week}: {outcome.ruling}", author="cerebellum")
    tier = outcome.verdict.tier if outcome.verdict else "-"
    _print(f"review {week}: ruling={outcome.ruling} tier={tier} pending={outcome.pending_id or '-'} "
           f"messages={len(outcome.messages)} aborted={outcome.aborted or '-'}")
    if outcome.aborted == "halted":
        return EXIT_HALTED
    return EXIT_RUNTIME if outcome.aborted else EXIT_OK


# --------------------------------------------------------------------------
# Commands: plan


def _open_pendings(ledger: Ledger) -> list[dict]:
    rows = (ledger.read_json(p) for p in ledger.glob("plans/pending/*.json"))
    return sorted((r for r in rows if isinstance(r, dict) and r.get("status") == "pending"),
                  key=lambda r: (str(r.get("created_at")), str(r.get("id"))))


def cmd_pending(app: App) -> int:
    rows = _open_pendings(app.ledger)
    for r in rows:
        veto = f" veto_until={r['veto_until']}" if r.get("veto_until") else ""
        _print(f"{r['id']} tier={r['tier']} base=v{int(r['base_version']):04d}{veto}: " + "; ".join(r["reasons"]))
    if not rows:
        _print("no pending changes")
    return EXIT_OK


def _plan_call(app: App, fn: Callable[[], Any], kind: str, message: str) -> Any:
    try:
        result = fn()
    except plans.PlanError as exc:
        raise RuntimeError(str(exc)) from None
    commit(app.ledger, kind, message)
    return result


def cmd_approve(app: App) -> int:
    pid = app.args.id
    version = _plan_call(app, lambda: plans.approve(app.ledger, pid, app.now()), "plan.ratified", f"Approve {pid}")
    _print(f"approved {pid}: plan v{version:04d}")
    return EXIT_OK


def cmd_veto(app: App) -> int:
    pid = app.args.id
    _plan_call(app, lambda: plans.veto(app.ledger, pid, app.now()), "plan.vetoed", f"Veto {pid}")
    _print(f"vetoed {pid}")
    return EXIT_OK


def cmd_rollback(app: App) -> int:
    match = _VERSION_RE.match(app.args.version)
    if match is None:
        raise UsageError(f"version must look like v0012 or 12, got {app.args.version!r}")
    target = int(match.group(1))
    version = _plan_call(app, lambda: plans.rollback(app.ledger, target, app.now()), "plan.rollback",
                         f"Rollback to v{target:04d}")
    _print(f"rolled back to v{target:04d} as v{version:04d}")
    return EXIT_OK


def cmd_halt(app: App) -> int:
    app.ledger.write_text("config/HALT", f"halted at {app.now().isoformat(timespec='seconds')}\n", author=CODE_AUTHOR)
    commit(app.ledger, "halt", "Halt: config/HALT set")
    _print("halted: only ingest and red flags keep running")
    return EXIT_OK


def _unstage(root: Path, rel: str) -> None:
    if (root / ".git").is_dir() and shutil.which("git"):
        subprocess.run(["git", "-C", str(root), "rm", "--cached", "--quiet", "--ignore-unmatch", "--", rel],
                       capture_output=True, text=True, check=False)


def cmd_resume(app: App) -> int:
    halt = app.root / "config" / "HALT"
    if not halt.exists():
        _print("not halted")
        return EXIT_OK
    halt.unlink()
    _unstage(app.root, "config/HALT")
    if (app.root / ".git").is_dir():
        app.ledger.commit(author=CODE_AUTHOR, message="Resume: config/HALT removed", trailers={"Kind": "resume"})
    _print("resumed")
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands: experiments


def _locked_values(store: Store, p: experiments.Protocol, first: str, last: str) -> dict[str, float | None]:
    """Outcome values of the locked source only, keyed by date; other sources count as missing."""
    rows = store.daily(p.outcome_key, first, _shift(last, 1))
    return {r.date: (r.value if r.source == p.source else None) for r in rows}


def _dense(values: Mapping[str, float | None], first: str, n: int) -> list[float | None]:
    return [values.get(_shift(first, k)) for k in range(n)]


def _mean90(store: Store, p: experiments.Protocol) -> tuple[float, float]:
    first = _shift(p.start_date, -MEAN90_DAYS)
    known = [v for v in _locked_values(store, p, first, _shift(p.start_date, -1)).values() if v is not None]
    if len(known) < 2:
        raise experiments.ProtocolError("fewer than 2 outcome values in the 90 days before start")
    return stats.mean(known), stats.sd(known)


def _protocol(raw: Any) -> experiments.Protocol:
    if not isinstance(raw, Mapping):
        raise UsageError("protocol file missing or not a JSON object")
    try:
        return experiments.Protocol.from_dict(raw)
    except (experiments.ProtocolError, KeyError, TypeError) as exc:
        raise UsageError(f"invalid protocol: {exc}") from None


def _exp_design(app: App, exp_id: str) -> int:
    p = _protocol(app.ledger.read_json(f"experiments/{exp_id}/draft.json"))
    first = _shift(p.start_date, -BASELINE_DAYS)
    baseline = _dense(_locked_values(app.store, p, first, _shift(p.start_date, -1)), first, BASELINE_DAYS)
    try:
        mean90, sd90 = _mean90(app.store, p)
        sigma, rho, _ = experiments.baseline_stats(baseline)
        problems = experiments.eligibility(p, baseline, mean90, sd90)
    except experiments.ProtocolError as exc:
        _print(f"experiment {exp_id}: not eligible: {exc}")
        return EXIT_RUNTIME
    mde = experiments.mde(sigma, rho, p.block_days, p.washout_days, p.pairs)
    _print(f"experiment {exp_id}: sigma={sigma:.4g} rho={rho:.3f} mde={mde:.4g} swc={p.swc:.4g}")
    if problems:
        _print("not eligible: " + ", ".join(problems))
        return EXIT_RUNTIME
    sha = experiments.write_protocol(app.ledger, p)
    commit(app.ledger, "experiment.protocol", f"Protocol {exp_id}", author="hippocampus")
    _print(f"protocol written: experiments/{exp_id}/protocol.json sha256={sha}")
    return EXIT_OK


def _frozen(app: App, exp_id: str) -> tuple[experiments.Protocol, list[tuple[str, str]], bool]:
    """Protocol, allocation and extension state; experiments/<id>/extension.json is written once, on 'extend'."""
    p = _protocol(app.ledger.read_json(f"experiments/{exp_id}/protocol.json"))
    extended = app.ledger.read_json(f"experiments/{exp_id}/extension.json", default=None) is not None
    extra = EXTENSION_PAIRS if extended else 0
    return p, experiments.allocate(p, canonical_json(p.to_dict()), extra_pairs=extra), extended


def _write_csv(app: App, exp_id: str, allocation: Sequence[tuple[str, str]], data: Mapping[str, float | None]) -> None:
    lines = ["date,arm,value"] + [f"{d},{arm},{'' if data.get(d) is None else data[d]}" for d, arm in allocation]
    app.ledger.write_text(f"experiments/{exp_id}/data.csv", "\n".join(lines) + "\n", author=CODE_AUTHOR)


def _exp_allocate(app: App, exp_id: str) -> int:
    _, allocation, _ = _frozen(app, exp_id)
    _write_csv(app, exp_id, allocation, {})
    commit(app.ledger, "experiment.allocation", f"Allocation {exp_id}")
    arms = {arm: sum(1 for _, a in allocation if a == arm) for arm in ("A", "B", "washout")}
    _print(f"experiment {exp_id}: {len(allocation)} days from {allocation[0][0]}: "
           + ", ".join(f"{k}={v}" for k, v in arms.items()))
    return EXIT_OK


def _belief(app: App, belief_id: str) -> beliefs.Belief:
    folded = beliefs.fold(app.ledger.read_jsonl(beliefs.EVENTS_PATH), app.now().date().isoformat())
    belief = folded.get(belief_id)
    if belief is None or belief.prior is None:
        raise UsageError(f"belief {belief_id!r} has no prior in {beliefs.EVENTS_PATH}")
    return belief


def _exclusion_inputs(app: App, p: experiments.Protocol, first: str, last: str) -> tuple[dict, dict]:
    keys = {p.outcome_key} | {str(r["key"]) for r in p.exclusions if "key" in r}
    daily: dict[str, dict[str, float | None]] = {}
    for key in sorted(keys):
        for r in app.store.daily(key, first, _shift(last, 1)):
            daily.setdefault(r.date, {})[key] = r.value
    items = app.store.checkins(first, _shift(last, 1))
    flags = {d: checkins.symptom_flags([i for i in items if i.date == d]) for d in {i.date for i in items}}
    return daily, flags


def _analyze_blocked(app: App, exp_id: str, last: str) -> str | None:
    """Why analyze must not run now: a final result exists, or the allocation has not ended."""
    previous = app.ledger.read_json(f"experiments/{exp_id}/result.json", default=None)
    decision = previous.get("decision") if isinstance(previous, Mapping) else None
    if decision in FINAL_DECISIONS:
        return f"result.json already holds the final decision {decision!r}"
    today = app.now().astimezone(ZoneInfo(app.tz)).date().isoformat()
    return f"allocation runs through {last}; analyze after that day" if today <= last else None


def _record_extension(app: App, exp_id: str, record: Mapping[str, Any]) -> None:
    rel = f"experiments/{exp_id}/extension.json"
    if record["decision"] == "extend" and app.ledger.read_json(rel, default=None) is None:
        app.ledger.write_json(rel, {"extra_pairs": EXTENSION_PAIRS, "decided_at": record["analyzed_at"]},
                              author=CODE_AUTHOR)


def _exp_analyze(app: App, exp_id: str) -> int:
    p, allocation, extended = _frozen(app, exp_id)
    first, last = allocation[0][0], allocation[-1][0]
    blocked = _analyze_blocked(app, exp_id, last)
    if blocked:
        _print(f"experiment {exp_id}: cannot analyze: {blocked}")
        return EXIT_RUNTIME
    data = _locked_values(app.store, p, first, last)
    daily, flags = _exclusion_inputs(app, p, first, last)
    belief = _belief(app, p.belief_id)
    try:
        result = experiments.analyze(p, allocation, data, experiments.excluded_days(p, daily, flags), belief.prior,
                                     extended=extended, direction=belief.claim.direction)
    except experiments.ProtocolError as exc:
        _print(f"experiment {exp_id}: cannot analyze: {exc}")
        return EXIT_RUNTIME
    record = {**asdict(result), "experiment": exp_id, "extended": extended,
              "analyzed_at": app.now().isoformat(timespec="seconds")}
    app.ledger.write_json(f"experiments/{exp_id}/result.json", record, author=CODE_AUTHOR)
    _record_extension(app, exp_id, record)
    _write_csv(app, exp_id, allocation, data)
    commit(app.ledger, "experiment.result", f"Result {exp_id}: {result.decision}")
    _print(f"experiment {exp_id}: decision={result.decision} d_bar={result.d_bar:.4g} p={result.p_value:.4g} "
           f"P(benefit)={result.p_benefit:.3f} P(harm)={result.p_harm:.3f}")
    return EXIT_OK


_EXPERIMENT_ACTIONS: Mapping[str, Callable[[App, str], int]] = {
    "design": _exp_design, "allocate": _exp_allocate, "analyze": _exp_analyze,
}


def cmd_experiment(app: App) -> int:
    app.require_running()
    return _EXPERIMENT_ACTIONS[app.args.action](app, app.args.id)


# --------------------------------------------------------------------------
# Commands: evidence, dashboard, doctor


def _paper_location(app: App, raw: str) -> tuple[Path, str | None]:
    """(file, ledger-relative path or None when the file lies outside the ledger)."""
    candidate = Path(raw).expanduser()
    path = candidate if candidate.is_absolute() else (app.root / candidate)
    if not path.is_file():
        path = candidate.absolute()
    if not path.is_file():
        raise UsageError(f"paper file not found: {raw}")
    rel = path.resolve().relative_to(app.root.resolve()).as_posix() if _inside(path, app.root) else None
    return path, rel


def _field_value(group: Any, key: str) -> Any:
    node = group.get(key) if isinstance(group, Mapping) else None
    return node.get("value") if isinstance(node, Mapping) and set(node) <= {"value", "quote"} else node


def _paper_swc(extraction: Mapping[str, Any], owner: OwnerProfile) -> float | None:
    """owner.swc (raw outcome units) in the paper's effect units: MD as is, SMD / sd_pop, None for RR or unknown."""
    name = _field_value(extraction.get("outcome"), "name")
    swc = owner.swc.get(name) if isinstance(name, str) else None
    effect = extraction.get("effect")
    kind = _field_value(effect, "type")
    if swc is None or kind == "MD":
        return swc
    sd_pop = _field_value(effect, "sd_pop")
    ok = kind == "SMD" and isinstance(sd_pop, (int, float)) and not isinstance(sd_pop, bool) and sd_pop > 0
    return swc / float(sd_pop) if ok else None


def cmd_score_paper(app: App) -> int:
    path, rel = _paper_location(app, app.args.path)
    record = _read_json_file(path)
    extraction = record.get("extraction", record) if isinstance(record, Mapping) else None
    if not isinstance(extraction, Mapping) or agents.validate_json(extraction, evidence.PAPER_SCHEMA):
        raise UsageError(f"{path} holds no schema-valid paper extraction")
    if record.get("blocked"):
        _print(f"{path}: blocked by audit disagreements, not scored")
        return EXIT_RUNTIME
    owner = owner_profile(app.owner, app.now().date())
    score, appraisal = appraise_record(app.ledger, {**record, "extraction": extraction}, owner)
    shown = {**asdict(score), "appraisal": asdict(appraisal)} if app.args.appraise else asdict(score)
    _print(json.dumps(shown, sort_keys=True, ensure_ascii=False))
    if rel is not None and "extraction" in record:
        extra = {"appraisal": asdict(appraisal)} if app.args.appraise else {}
        updated = {**record, "score": asdict(score), "rubric_version": evidence.RUBRIC_VERSION, **extra}
        app.ledger.write_json(rel, updated, author=CODE_AUTHOR)
        commit(app.ledger, "evidence.appraisal", f"Rescore {rel} with {evidence.RUBRIC_VERSION}")
    return EXIT_OK


def cmd_dashboard(app: App) -> int:
    paths = dashboard.build(app.store, app.ledger, app.root / "site", app.now(), app.cfg)
    for p in paths:
        _print(p)
    return EXIT_OK


Check = tuple[str, str, str]  # (status ok|info|warn|fail, name, detail)


def _check_token(env: Mapping[str, str]) -> Check:
    n = len(env.get("HEALTH_TOKEN", "").encode("utf-8"))
    if n >= ingest.MIN_TOKEN_BYTES:
        return ("ok", "token", f"{n} bytes")
    return ("fail", "token", f"HEALTH_TOKEN has {n} bytes, needs >= {ingest.MIN_TOKEN_BYTES}")


def _check_host(env: Mapping[str, str]) -> Check:
    host = env.get("HEALTH_HOST") or ingest.DEFAULT_HOST
    if host in LOOPBACK_HOSTS:
        return ("ok", "bind", f"{host}")
    return ("fail", "bind", f"HEALTH_HOST={host}; Brainstem must bind 127.0.0.1")


def _check_git(root: Path) -> Check:
    if not (root / ".git").is_dir():
        return ("fail", "git", "ledger is not a git repository")
    proc = subprocess.run(["git", "-C", str(root), "remote"], capture_output=True, text=True, check=False)
    remotes = proc.stdout.split()
    if not remotes:
        return ("warn", "git", "no remote; aggregates exist only on this box")
    return ("ok", "git", "remotes: " + ", ".join(remotes) + " (keep the repo private)")


def _check_roster(app: App) -> Check:
    try:
        return ("ok", "roster", f"{len(app.roster)} bots")
    except UsageError as exc:
        return ("fail", "roster", str(exc))


def _check_freshness(app: App) -> Check:
    if not app.db_path.exists():
        return ("warn", "freshness", f"no database at {app.db_path}")
    fresh = metrics.freshness(app.store.latest_ts(), app.now(), weekly.FRESHNESS_H)
    stale = sorted(k for k, v in fresh.items() if v["stale"])
    return ("warn", "freshness", "stale: " + ", ".join(stale)) if stale else ("ok", "freshness", "all fresh")


def _check_budget(app: App) -> Check:
    level = app.budget.level()
    status = "ok" if level == "normal" else "warn"
    return (status, "budget", f"{level}: {app.budget.spent_week():.2f} of {app.budget.cap:.2f} USD this week")


def cmd_doctor(app: App) -> int:
    checks = [
        _check_token(app.env), _check_host(app.env),
        ("info", "exposure", "expose only through 'tailscale serve' inside the tailnet, never 'tailscale funnel'"),
        _check_git(app.root), _check_roster(app), _check_freshness(app), _check_budget(app),
        ("warn", "halt", "config/HALT is set") if app.ledger.halted() else ("ok", "halt", "not halted"),
    ]
    for status, name, detail in checks:
        _print(f"{status:4} {name}: {detail}")
    return EXIT_USAGE if any(c[0] == "fail" for c in checks) else EXIT_OK


# --------------------------------------------------------------------------
# Commands: trackers
#
# `tracker status --json` prints a list of objects, one per tracker, with the
# keys name, enabled, auth_ok, expires_at, refresh_expires_at (epoch seconds or
# null), auth_detail, last_ok, last_attempt, last_error, newest_data_ts and
# requests_today. No command prints a token, key or code.


class ConsoleUI:
    """AuthUI on the terminal: prints the URL, reads a pasted redirect URL from stdin."""

    def show_url(self, url: str) -> None:
        _print("Diese Adresse im Browser öffnen und zustimmen:")
        _print(url)

    def ask(self, prompt: str) -> str:
        return input(prompt)

    def info(self, text: str) -> None:
        _print(text)


def _tracker_name(name: str) -> str:
    try:
        return tracker_registry.check_name(name)
    except ValueError as exc:
        raise UsageError(str(exc)) from None


def _tracker_config(app: App) -> TrackerConfig:
    try:
        return tracker_run.load_tracker_config(app.ledger, app.tz)
    except ValueError as exc:
        raise UsageError(str(exc)) from None


def _with_options(cfg: TrackerConfig, name: str, options: Mapping[str, Any]) -> TrackerConfig:
    merged = {**dict(cfg.per_source), name: {**dict(cfg.per_source.get(name) or {}), **options}}
    return TrackerConfig(cfg.enabled, merged, cfg.tz)


def _tracker_source(app: App, name: str, options: Mapping[str, Any] | None = None) -> Any:
    cfg = _with_options(_tracker_config(app), name, options or {})
    try:
        return tracker_run.default_factory(app.env)(name, cfg)
    except (ValueError, ingest.ConfigError) as exc:  # missing secret or adapter not installed
        raise UsageError(str(exc)) from None


def _port_options(port: int | None) -> dict[str, Any]:
    if port is None:
        return {}
    return {"redirect_port": port, "redirect_uri": f"http://127.0.0.1:{port}/callback"}


def cmd_tracker_auth(app: App) -> int:
    name = _tracker_name(app.args.name)
    source = _tracker_source(app, name, _port_options(app.args.port))
    try:
        status = source.authorize(ConsoleUI(), manual=bool(app.args.manual))
    except TrackerError as exc:
        _print(f"{name}: authorization failed ({type(exc).__name__})")
        return EXIT_USAGE
    except ValueError as exc:  # e.g. Withings refuses --manual
        raise UsageError(f"{name}: {exc}") from None
    _print(f"{name}: auth {'ok' if status.ok else 'not ok'} ({status.detail})")
    return EXIT_OK if status.ok else EXIT_USAGE


def _poll_line(r: Mapping[str, Any]) -> str:
    if r["status"] == "failed":
        return f"{r['name']}: failed {r.get('error')}" + (f" ({r['detail']})" if r.get("detail") else "")
    counts = ", ".join(f"{k}={v}" for k, v in r["counts"].items())
    prefix = "would write" if r.get("dry_run") else "wrote"
    return f"{r['name']}: {r['status']} {r['window'][0]} .. {r['window'][1]}; {prefix} {counts}"


def _poll_exit(results: Sequence[Mapping[str, Any]]) -> int:
    if any(r.get("error") == "auth_required" for r in results):
        return EXIT_USAGE
    return EXIT_OK if all(r["status"] == "ok" for r in results) else EXIT_HALTED


def cmd_tracker_poll(app: App) -> int:
    names = [_tracker_name(n) for n in app.args.names]
    cfg = _tracker_config(app)
    if app.args.all or not names:
        names = list(dict.fromkeys([*names, *tracker_registry.enabled(cfg)]))
    if not names:
        _print("no trackers enabled in config/trackers.json")
        return EXIT_OK
    dry = bool(app.args.dry_run or app.args.poll_dry_run)
    results = tracker_run.poll_names(app.store, cfg, names, app.now(), tracker_run.default_factory(app.env),
                                     backfill_days=app.args.backfill, dry_run=dry)
    for r in results:
        _print(_poll_line(r))
    return _poll_exit(results)


def _auth_entry(app: App, cfg: TrackerConfig, name: str) -> dict[str, Any]:
    try:
        auth = tracker_run.default_factory(app.env)(name, cfg).auth_status()
    except (ValueError, ingest.ConfigError, TrackerError) as exc:
        return {"auth_ok": False, "expires_at": None, "refresh_expires_at": None,
                "auth_detail": f"not configured: {exc}"}
    return {"auth_ok": auth.ok, "expires_at": auth.expires_at, "refresh_expires_at": auth.refresh_expires_at,
            "auth_detail": auth.detail}


def tracker_status(app: App) -> list[dict[str, Any]]:
    cfg = _tracker_config(app)
    names = [n for n in tracker_registry.REGISTRY if n in cfg.enabled or n in cfg.per_source]
    return [{"name": n, "enabled": n in cfg.enabled, **_auth_entry(app, cfg, n),
             **tracker_run.state_entry(app.store.tracker_state(n))} for n in names]


def cmd_tracker_status(app: App) -> int:
    rows = tracker_status(app)
    if app.args.json:
        _print(json.dumps(rows, sort_keys=True, ensure_ascii=False))
        return EXIT_OK
    for r in rows:
        _print(f"{r['name']}: enabled={r['enabled']} auth={'ok' if r['auth_ok'] else 'no'} ({r['auth_detail']}) "
               f"last_ok={r['last_ok'] or '-'} last_error={r['last_error'] or '-'} "
               f"newest={r['newest_data_ts'] or '-'} requests_today={r['requests_today']}")
    if not rows:
        _print("no trackers configured in config/trackers.json")
    return EXIT_OK


def cmd_tracker_revoke(app: App) -> int:
    name = _tracker_name(app.args.name)
    _tracker_source(app, name).revoke()
    _print(f"{name}: token deleted")
    if not app.args.purge:
        return EXIT_OK
    if not app.args.yes and input(f"Alle {name}-Daten im Store löschen? [y/N] ").strip().lower() != "y":
        _print("purge skipped")
        return EXIT_OK
    counts = app.store.purge_source(name)
    _print("purged " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands: Devin layer


def _devin() -> ModuleType:
    from homunculus import devin_layer  # lazy: it imports pineal

    return devin_layer


def _json_out(obj: Any) -> None:
    _print(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str))


def cmd_devin_task(app: App) -> int:
    pineal = _pineal()
    try:
        out = _devin().run_task(_pineal_context(app, pineal), app.args.automation, path=app.args.path)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    _json_out(out)
    return EXIT_RUNTIME if any(r.get("status") == "failed" for r in out.get("runs") or ()) else EXIT_OK


def cmd_message(app: App) -> int:
    path = Path(app.args.text_file).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc}") from None
    pineal = _pineal()
    status, body = _devin().reply(_pineal_context(app, pineal), app.args.bot, text, path=app.args.path)
    _json_out({"status": status, **body})
    return EXIT_OK if status == 200 else EXIT_RUNTIME


def cmd_ingest(app: App) -> int:
    if bool(app.args.stdin) == bool(app.args.file):
        raise UsageError("pass exactly one of --stdin or --file PATH")
    source_map, api_sources = _source_map(app.env), _api_sources(app.env)
    try:
        out = _devin().ingest_stdin(app.store, app.ledger, app.cfg, stream=sys.stdin if app.args.stdin else None,
                                    path=app.args.file, source_map=source_map, now=app.now(), api_sources=api_sources)
    except ValueError as exc:
        raise UsageError(f"no Health Auto Export payload: {exc}") from None
    _json_out({"counts": out["counts"], "days": len(out["days"]), "skipped": out["skipped"],
               "restored": out["restored"], "commit": out["commit"]})
    return EXIT_OK


def _json_object(raw: str, what: str) -> dict:
    obj = _read_json_file(Path(raw).expanduser())
    if not isinstance(obj, Mapping):
        raise UsageError(f"{what} file must hold a JSON object")
    return dict(obj)


def cmd_run_bot(app: App) -> int:
    app.require_running()
    context, schema = _json_object(app.args.context, "--context"), _json_object(app.args.schema, "--schema")
    run_id = app.args.run_id or f"cli-{app.args.bot}-{app.now():%Y%m%dT%H%M%SZ}"
    try:
        res = _devin().run_bot(app.args.bot, app.ledger, roster=app.roster, budget=app.budget, context=context,
                               schema=schema, out_rel=app.args.out, run_id=run_id,
                               providers=app.providers if app.args.dry_run else None, env=app.env, now=app.now)
    except (ValueError, PermissionError) as exc:
        raise UsageError(str(exc)) from None
    _json_out({"path": res.path, "model": res.model, "usd": res.usd, "commit": res.commit})
    return EXIT_OK


def cmd_devin_export(app: App) -> int:
    devin = _devin()
    cadence = app.ledger.read_json("config/cadence.json", default=None)
    keywords = app.ledger.read_json("config/keywords.json", default=None)
    specs = devin.export_automations(cadence, path=app.args.path, tz=app.args.tz, roster=app.roster,
                                     keywords=keywords, max_items=app.args.max_items)
    if not app.args.out:
        _json_out(specs)
        return EXIT_OK
    out = Path(app.args.out).expanduser().absolute()
    if _inside(out, ENGINE_ROOT):
        raise UsageError(f"refusing to write generated specs into the public engine repo: {out}")
    for written in devin.write_automations(out, specs):
        _print(written)
    return EXIT_OK


def cmd_scout_script(app: App) -> int:
    keywords = app.ledger.read_json("config/keywords.json", default=None)
    _print(_devin().scout_poll_script(app.args.scout, keywords, max_items=app.args.max_items))
    return EXIT_OK


def cmd_devin_restore(app: App) -> int:
    rows = _devin().restore_store(app.store, app.ledger, app.now())
    _print(f"restored {rows} rows into {app.db_path}")
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands: frontier lane, full text, lexicon


def cmd_fulltext(app: App) -> int:
    from homunculus import fulltext

    try:
        ids = fulltext.parse_id(app.args.ident)
        access = fulltext.load_access(fulltext.default_access_path(app.env))
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    http = fulltext.FulltextHttp(app.root / "evidence" / "cache", mailto=app.env.get("HOMUNCULUS_MAILTO"),
                                 access=access, offline=bool(app.args.dry_run))
    result = fulltext.resolve_fulltext(http, app.ledger, ids, author=CODE_AUTHOR, now=app.now(),
                                       known_urls=app.args.pdf_url or (), cand_id=app.args.id)
    commit(app.ledger, "evidence.fulltext", f"Full text {result.id}: {result.source or 'not found'}")
    _print(f"{result.id}: source={result.source or '-'} access={result.access or '-'} "
           f"license={result.license or '-'} path={result.path or '-'} text_extraction={result.text_extraction or '-'}")
    return EXIT_OK if result.path else EXIT_RUNTIME


def _source_text(app: App, raws: Sequence[str]) -> str:
    parts = []
    for raw in raws:
        path, _ = _paper_location(app, raw)
        parts.append(f"[{path.name}]\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def cmd_deep_dive(app: App) -> int:
    app.require_running()
    _, rel = _paper_location(app, app.args.paper)
    if rel is None:
        raise UsageError(f"paper must lie inside the ledger: {app.args.paper}")
    record = run_deep_dive(app.ledger, app.llm(), rel, _source_text(app, app.args.source), app.now())
    commit(app.ledger, "evidence.deepdive", f"Deep dive {rel}: {record['decision']}", author="thalamus")
    chain = record["chain"]
    _print(f"{deepdive.record_rel(record['paper_key'])}: decision={record['decision']} "
           f"weakest={chain['weakest']} coverage={chain['coverage']} holds={chain['holds']} "
           f"open_questions={len(record['open_questions'])}")
    for reason in record["reasons"]:
        _print(f"  reason: {reason}")
    return EXIT_OK


def cmd_hed(app: App) -> int:
    try:
        value = evidence.human_equivalent_dose(app.args.dose, app.args.species)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    _print(f"HED {value:.4g} mg/kg ({app.args.dose:g} mg/kg in {app.args.species})")
    return EXIT_OK


def cmd_frontier(app: App) -> int:
    week = app.args.week or _iso_week(app.now().date().isoformat())
    if not _WEEK_RE.match(week):
        raise UsageError(f"week must look like 2026-W47, got {week!r}")
    block = weekly._frontier(app.ledger, week)
    _json_out({k: v for k, v in block.items() if k != "beliefs"})
    return EXIT_OK


def _lexicon_show(app: App) -> int:
    view = lexicon.load_view(app.ledger)
    _print(f"lexicon: {len(view['nodes'])} nodes, {len(view['links'])} links, "
           f"{len(view['questions'])} open questions, {len(view['retracted'])} retracted papers")
    for link in sorted(view["links"].values(), key=lambda x: (x["src"], x["dst"])):
        _print(f"  {link['src']} -> {link['dst']}: {link['verdict']} support={link['support']} "
               f"contra={link['contra']} human={link['human']}")
    return EXIT_OK


def _lexicon_questions(app: App) -> int:
    questions = lexicon.load_view(app.ledger)["questions"]
    for q in questions:
        _print(f"{q['src']} -> {q['dst']}: {q['text']} [{', '.join(q['keywords'])}]")
    if not questions:
        _print("no open questions")
    return EXIT_OK


def _lexicon_rebuild(app: App) -> int:
    rel = lexicon.rebuild_view(app.ledger)
    commit(app.ledger, "evidence.lexicon", "Rebuild lexicon view")
    _print(rel)
    return EXIT_OK


def _lexicon_lookup(app: App) -> int:
    if len(app.args.nodes) != 2:
        raise UsageError("lexicon lookup needs SRC_NODE_ID DST_NODE_ID")
    verdict = lexicon.lookup(lexicon.load_view(app.ledger), *app.args.nodes)
    _json_out(asdict(verdict) if verdict else None)
    return EXIT_OK


def _lexicon_export(app: App) -> int:
    export = lexicon.export_keywords(lexicon.load_view(app.ledger))
    if not app.args.write:
        _json_out(export)
        return EXIT_OK
    config = app.ledger.read_json("config/keywords.json", default={}) or {}
    app.ledger.write_json("config/keywords.json", lexicon.merge_keywords(config, export), author=CODE_AUTHOR)
    commit(app.ledger, "config.keywords", "Scout keywords from lexicon open questions")
    _print("config/keywords.json updated")
    return EXIT_OK


_LEXICON_ACTIONS: Mapping[str, Callable[[App], int]] = {
    "show": _lexicon_show, "questions": _lexicon_questions, "rebuild": _lexicon_rebuild,
    "lookup": _lexicon_lookup, "export-keywords": _lexicon_export,
}


def cmd_lexicon(app: App) -> int:
    return _LEXICON_ACTIONS[app.args.action](app)


# --------------------------------------------------------------------------
# Commands without a configured ledger


def cmd_init_ledger(app: App) -> int:
    root = Path(app.args.path).expanduser().absolute()
    files = init_ledger(root)
    _print(f"ledger skeleton at {root} ({len(files)} files, git initialized). Keep this repository private.")
    _print(f"next: export HOMUNCULUS_LEDGER={root}")
    return EXIT_OK


def cmd_demo(app: App) -> int:
    from homunculus import demo

    if app.args.days < demo.MIN_DAYS:
        raise UsageError(f"--days must be >= {demo.MIN_DAYS}")
    out = Path(app.args.out).expanduser().absolute() if app.args.out else None
    summary = demo.run_demo(out, days=app.args.days, seed=app.args.seed)
    for line in demo.summary_lines(summary):
        _print(line)
    return EXIT_OK


# --------------------------------------------------------------------------
# Parser and main


def _sub(subs: Any, name: str, func: Callable[[App], int], help_text: str) -> argparse.ArgumentParser:
    parser = subs.add_parser(name, help=help_text, description=help_text)
    parser.set_defaults(func=func)
    return parser


def _add_plan_commands(subs: Any) -> None:
    _sub(subs, "pending", cmd_pending, "list pending plan changes")
    _sub(subs, "approve", cmd_approve, "approve a pending change").add_argument("id")
    _sub(subs, "veto", cmd_veto, "veto a pending change").add_argument("id")
    _sub(subs, "rollback", cmd_rollback, "roll the plan back to VERSION").add_argument("version")
    _sub(subs, "halt", cmd_halt, "create config/HALT")
    _sub(subs, "resume", cmd_resume, "remove config/HALT")


def _add_data_commands(subs: Any) -> None:
    _sub(subs, "serve", cmd_serve, "run Brainstem (ingest server, check-in and message routes, dashboard)")
    _sub(subs, "tick", cmd_tick, "run due Pineal jobs once").add_argument(
        "--devin-path", choices=("a", "b"), default=None, help="leave out the jobs Devin Automations run on that path")
    run = _sub(subs, "run", cmd_run, "run one Pineal job now")
    run.add_argument("job")
    run.add_argument("--period", default=None)
    _sub(subs, "import-history", cmd_import_history, "ingest a full Health Auto Export JSON export").add_argument("file")
    _sub(subs, "finalize", cmd_finalize, "build daily rows and baselines for DAY").add_argument("day")
    _sub(subs, "review", cmd_review, "build the packet and run the weekly review").add_argument("week")


def _add_tool_commands(subs: Any) -> None:
    exp = _sub(subs, "experiment", cmd_experiment, "N-of-1 experiment: design, allocate or analyze")
    exp.add_argument("action", choices=sorted(_EXPERIMENT_ACTIONS))
    exp.add_argument("id")
    score = _sub(subs, "score-paper", cmd_score_paper, "rescore an extraction with the current rubric (offline)")
    score.add_argument("path")
    score.add_argument("--appraise", action="store_true", help="add promise, quality gate and frontier status")
    _sub(subs, "dashboard", cmd_dashboard, "rebuild site/")
    _sub(subs, "doctor", cmd_doctor, "check token, bind host, exposure, git, roster, freshness, budget")
    _sub(subs, "init-ledger", cmd_init_ledger, "create the private ledger skeleton and git init it"
         ).add_argument("path")
    demo = _sub(subs, "demo", cmd_demo, "run the whole pipeline on synthetic data in a temporary ledger")
    demo.add_argument("--out", default=None, help="new or empty directory for the demo ledger")
    demo.add_argument("--days", type=int, default=56)
    demo.add_argument("--seed", type=int, default=7)


def _add_tracker_commands(subs: Any) -> None:
    tracker = subs.add_parser("tracker", help="direct tracker pollers: auth, poll, status, revoke",
                              description="direct tracker pollers: auth, poll, status, revoke")
    actions = tracker.add_subparsers(dest="tracker_action", required=True, metavar="ACTION")
    auth = _sub(actions, "auth", cmd_tracker_auth, "authorize a tracker (Garmin: mint a JWT from the key)")
    auth.add_argument("name")
    auth.add_argument("--manual", action="store_true", help="paste the redirect URL instead of the loopback")
    auth.add_argument("--port", type=int, default=None, help="loopback redirect port")
    poll = _sub(actions, "poll", cmd_tracker_poll, "poll trackers now (default: every enabled one)")
    poll.add_argument("names", nargs="*")
    poll.add_argument("--all", action="store_true")
    poll.add_argument("--backfill", type=int, default=0, metavar="DAYS")
    poll.add_argument("--dry-run", dest="poll_dry_run", action="store_true", help="fetch and count, write nothing")
    _sub(actions, "status", cmd_tracker_status, "auth and poll state per tracker").add_argument(
        "--json", action="store_true")
    revoke = _sub(actions, "revoke", cmd_tracker_revoke, "delete the stored token (vendor revoke where one exists)")
    revoke.add_argument("name")
    revoke.add_argument("--purge", action="store_true", help="also delete every row of this source from the store")
    revoke.add_argument("--yes", action="store_true", help="skip the y/N prompt of --purge")


def _add_devin_commands(subs: Any) -> None:
    task = _sub(subs, "devin-task", cmd_devin_task, "run one Devin Automation's Pineal jobs; prints JSON")
    task.add_argument("automation")
    task.add_argument("--path", choices=("a", "b"), default="a")
    message = _sub(subs, "message", cmd_message, "an owner message from a Devin Bot chat; prints JSON")
    message.add_argument("bot", choices=("sartorius", "vagus", "cerebellum"))
    message.add_argument("--path", choices=("a", "b"), default="a")
    message.add_argument("--text-file", required=True)
    ing = subs.add_parser("ingest", aliases=["ingest-stdin"], help="ingest one Health Auto Export payload",
                          description="ingest one Health Auto Export payload (bare, wrapped or inside prompt text)")
    ing.set_defaults(func=cmd_ingest)
    ing.add_argument("--stdin", action="store_true")
    ing.add_argument("--file", default=None)
    bot = _sub(subs, "run-bot", cmd_run_bot, "one validated LLM turn of a bot, written as that bot")
    bot.add_argument("bot")
    bot.add_argument("--context", required=True)
    bot.add_argument("--schema", required=True)
    bot.add_argument("--out", required=True, help="ledger-relative output path")
    bot.add_argument("--run-id", default=None)
    export = _sub(subs, "devin-export", cmd_devin_export, "Devin Automation specs (JSON to stdout or --out DIR)")
    export.add_argument("--path", choices=("a", "b"), default="a")
    export.add_argument("--out", default=None)
    export.add_argument("--max-items", type=int, default=10)
    script = _sub(subs, "scout-script", cmd_scout_script, "Python source of a scout's Devin script step")
    script.add_argument("scout", choices=sorted(agents.SCOUTS))
    script.add_argument("--max-items", type=int, default=10)
    _sub(subs, "devin-restore", cmd_devin_restore, "rebuild the SQLite store from the ledger (Devin path a)")


def _add_evidence_commands(subs: Any) -> None:
    ft = _sub(subs, "fulltext", cmd_fulltext, "fetch a legal full text into quarantine (DOI, PMID or arXiv id)")
    ft.add_argument("ident")
    ft.add_argument("--pdf-url", action="append", default=None, help="known PDF URL, looked up in the Wayback Machine")
    ft.add_argument("--id", default=None, help="candidate id c-xxxx")
    dd = subs.add_parser("deep-dive", aliases=["deepdive"], help="Thalamus deep dive of a frontier paper",
                         description="Thalamus deep dive of a frontier paper; code decides the N-of-1 candidacy")
    dd.set_defaults(func=cmd_deep_dive)
    dd.add_argument("paper", help="evidence/papers/<slug>.json")
    dd.add_argument("--source", action="append", required=True, help="quarantined text (repeatable)")
    hed = _sub(subs, "hed", cmd_hed, "human equivalent dose from an animal dose (Km factors)")
    hed.add_argument("dose", type=float, help="mg/kg")
    hed.add_argument("species")
    _sub(subs, "frontier", cmd_frontier, "frontier lane status of a week (default: this week)").add_argument(
        "week", nargs="?", default=None)
    lex = _sub(subs, "lexicon", cmd_lexicon, "knowledge lexicon: show, questions, rebuild, lookup, export-keywords")
    lex.add_argument("action", choices=sorted(_LEXICON_ACTIONS))
    lex.add_argument("nodes", nargs="*", help="lookup: SRC_NODE_ID DST_NODE_ID")
    lex.add_argument("--write", action="store_true", help="export-keywords: merge into config/keywords.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="homunculus", description="Personal health swarm engine.")
    parser.add_argument("--ledger", default=None, help="private ledger repo (env HOMUNCULUS_LEDGER)")
    parser.add_argument("--db", default=None, help="SQLite store (env HEALTH_DB)")
    parser.add_argument("--tz", default=None, help="owner time zone (default: profile/owner.json)")
    parser.add_argument("--dry-run", action="store_true", help="use FakeProvider for every LLM call")
    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    _add_data_commands(subs)
    _add_plan_commands(subs)
    _add_tool_commands(subs)
    _add_tracker_commands(subs)
    _add_devin_commands(subs)
    _add_evidence_commands(subs)
    return parser


_ERRORS: tuple[tuple[type[BaseException], int], ...] = (
    (UsageError, EXIT_USAGE), (ingest.ConfigError, EXIT_USAGE), (Halted, EXIT_HALTED), (Exception, EXIT_RUNTIME),
)


def _exit_for(exc: BaseException) -> int:
    return next(code for kind, code in _ERRORS if isinstance(exc, kind))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    app = App(args, os.environ)
    try:
        return int(args.func(app))
    except Exception as exc:  # noqa: BLE001 - the CLI maps every failure to an exit code
        print(f"homunculus: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return _exit_for(exc)
    finally:
        app.close()

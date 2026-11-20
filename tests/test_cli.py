"""Tests for homunculus.cli. Synthetic values only, temp ledgers, no network; Pineal is stubbed."""

from __future__ import annotations

import io
import json
import os
import random
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from homunculus import agents, beliefs, cli, demo, plans
from homunculus.store import DailyValue, HealthEvent, Ledger, Store

NOW = datetime(2026, 11, 22, 16, 0, tzinfo=timezone.utc)  # Sunday of 2026-W47
WEEK = "2026-W47"
TOKEN = "t" * 40
HAS_GIT = shutil.which("git") is not None


def _plan() -> dict:
    return {
        "version": 1, "parent": None, "effective_from": "2026-11-16T00:00:00Z", "valid_until": "2026-11-23T00:00:00Z",
        "author": "pineal", "ratified_by": "owner", "data_snapshot_sha": None, "rationale_refs": [],
        "sessions": [{"id": "s-thu", "weekday": 3, "kind": "run", "duration_min": 40, "target_rpe": 5,
                      "exercises": []}],
        "nutrition": {"kcal_target": None, "protein_g_per_kg": None, "notes_de": ""},
        "supplements": [], "experiments": [],
        "modulation": {"max_daily_load_cut_pct": 30, "allow_swap": True, "readiness_low_action": "shorten"},
    }


def _hae_payload() -> dict:
    """Two synthetic nights and one workout in Health Auto Export shape."""
    src = "Test Watch"
    hrv = [{"date": f"2026-11-{d} 0{h}:00:00 +0100", "qty": 50.0 + h, "source": src} for d in (20, 21) for h in (2, 4)]
    rhr = [{"date": f"2026-11-{d} 08:00:00 +0100", "qty": 53.0, "source": src} for d in (20, 21)]
    sleep = [{"startDate": f"2026-11-{d - 1} 23:30:00 +0100", "endDate": f"2026-11-{d} 06:30:00 +0100",
              "value": "Core", "source": src} for d in (20, 21)]
    hr = [{"date": f"2026-11-20 18:{m:02d}:00 +0100", "Avg": 140.0} for m in range(30)]
    workout = {"id": "w-test-1", "name": "Outdoor Run", "start": "2026-11-20 18:00:00 +0100",
               "end": "2026-11-20 18:30:00 +0100", "duration": 1800.0, "heartRateData": hr, "source": src,
               "route": [{"lat": 1.0, "lon": 2.0}]}
    metrics_ = [{"name": "heart_rate_variability", "units": "ms", "data": hrv},
                {"name": "resting_heart_rate", "units": "count/min", "data": rhr},
                {"name": "sleep_analysis", "units": "hr", "data": sleep}]
    return {"data": {"metrics": metrics_, "workouts": [workout]}}


@dataclass
class StubPineal:
    """Records what the CLI hands to Pineal; mirrors the public names of the pineal contract."""

    runs: list = field(default_factory=list)
    delivered: list = field(default_factory=list)
    messages: list = field(default_factory=list)

    def __post_init__(self) -> None:
        job = SimpleNamespace(name="finalize_day", essential=True)
        self.jobs = [job, SimpleNamespace(name="scouts", essential=False)]

    def Context(self, **kwargs: Any) -> SimpleNamespace:  # noqa: N802 - mirrors the dataclass name
        return SimpleNamespace(**kwargs)

    def FileNotifier(self, ledger: Ledger) -> str:  # noqa: N802
        return "notifier"

    def default_jobs(self) -> list:
        return list(self.jobs)

    def period_key(self, job: Any, now: datetime) -> str:
        return now.date().isoformat()

    def run_job(self, ctx: Any, job: Any, period: str) -> dict:
        self.runs.append((job.name, period))
        return {"job": job.name, "period": period, "status": "ok"}

    def tick(self, ctx: Any, jobs: list) -> list[dict]:
        return [self.run_job(ctx, j, "p") for j in jobs]

    def deliver_alerts(self, ctx: Any, alerts: list) -> int:
        self.delivered.extend(alerts)
        return len(alerts)

    def handle_owner_message(self, ctx: Any, to: str, text: str) -> tuple[int, dict]:
        self.messages.append((to, text))
        return 200, {"reply_de": "ok", "checkin_stored": 0}


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp)}
        env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        now_patch = mock.patch.object(cli, "_utcnow", lambda: NOW)
        now_patch.start()
        self.addCleanup(now_patch.stop)
        self.root = self.tmp / "ledger"

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def cli_ok(self, *argv: str) -> str:
        code, out, err = self.run_cli("--ledger", str(self.root), *argv)
        self.assertEqual(code, 0, msg=err + out)
        return out

    def init(self) -> Ledger:
        cli.init_ledger(self.root, git=HAS_GIT)
        roster = agents.load_roster(json.loads((self.root / "config/roster.json").read_text(encoding="utf-8")))
        return Ledger(self.root, agents.write_rules(roster))

    def store(self) -> Store:
        store = Store(self.root / ".private" / "health.sqlite")
        store.migrate()
        self.addCleanup(store.close)
        return store

    def seed_plan(self, ledger: Ledger) -> None:
        ledger.write_json("plans/v0001.json", _plan(), author="pineal")
        ledger.write_text("plans/CURRENT", "v0001\n", author="pineal")


# --------------------------------------------------------------------------
# Parser, exit codes and ledger resolution


class ParserTest(CliCase):
    def test_help_and_usage_codes(self) -> None:
        self.assertEqual(self.run_cli("--help")[0], 0)
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.run_cli("no-such-command")[0], 2)
        self.assertEqual(self.run_cli("experiment", "frobnicate", "x")[0], 2)

    def test_missing_ledger_is_usage_error(self) -> None:
        code, _, err = self.run_cli("pending")
        self.assertEqual(code, 2)
        self.assertIn("HOMUNCULUS_LEDGER", err)
        self.assertEqual(self.run_cli("--ledger", str(self.tmp / "missing"), "pending")[0], 2)

    def test_env_ledger_is_used(self) -> None:
        self.init()
        with mock.patch.dict(os.environ, {"HOMUNCULUS_LEDGER": str(self.root)}):
            code, out, _ = self.run_cli("pending")
        self.assertEqual(code, 0)
        self.assertIn("no pending changes", out)

    def test_ledger_inside_engine_repo_is_refused(self) -> None:
        code, _, err = self.run_cli("--ledger", str(cli.ENGINE_ROOT / "docs"), "pending")
        self.assertEqual(code, 2)
        self.assertIn("engine repo", err)

    def test_dry_run_uses_fake_providers(self) -> None:
        self.init()
        app = cli.App(cli.build_parser().parse_args(["--ledger", str(self.root), "--dry-run", "pending"]), {})
        self.assertTrue(all(isinstance(p, agents.FakeProvider) for p in app.providers.values()))
        live = cli.App(cli.build_parser().parse_args(["--ledger", str(self.root), "pending"]), {})
        self.assertEqual(live.providers, {})  # no keys: no provider, calls fail closed


# --------------------------------------------------------------------------
# init-ledger


class InitLedgerTest(CliCase):
    def test_skeleton(self) -> None:
        code, out, err = self.run_cli("init-ledger", str(self.root))
        self.assertEqual(code, 0, msg=err)
        self.assertIn("private", out)
        for rel in ("config/roster.json", "config/budget.json", "config/readiness.json", "config/exercises.json",
                    "config/goals.md", "profile/owner.json", "profile/meds.json", "README.md", ".gitignore"):
            self.assertTrue((self.root / rel).is_file(), rel)
        for rel in ("data/daily", "evidence/papers", "plans/pending", "mail/owner/inbox", "ops/incidents"):
            self.assertTrue((self.root / rel).is_dir(), rel)
        self.assertIn("must stay private", (self.root / "README.md").read_text(encoding="utf-8"))
        ignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".private/", ignore)
        self.assertIn("*.sqlite", ignore)
        self.assertEqual(oct((self.root / ".private").stat().st_mode & 0o777), oct(0o700))
        if HAS_GIT:
            self.assertTrue((self.root / ".git").is_dir())

    def test_templates_are_loadable(self) -> None:
        ledger = self.init()
        roster = agents.load_roster(ledger.read_json("config/roster.json"))
        self.assertIn("cerebellum", roster)
        from homunculus import guardrails
        guardrails.load_rules(ledger)  # every guardrail template parses
        self.assertEqual(plans.load_exercises(ledger), {})
        owner = ledger.read_json("profile/owner.json")
        self.assertIsNone(owner["sex"])  # template holds no personal values

    def test_refuses_engine_repo_and_non_empty_dirs(self) -> None:
        self.assertEqual(self.run_cli("init-ledger", str(cli.ENGINE_ROOT / "ledger-x"))[0], 2)
        self.assertFalse((cli.ENGINE_ROOT / "ledger-x").exists())
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "file.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.run_cli("init-ledger", str(busy))[0], 2)


# --------------------------------------------------------------------------
# Data commands


class DataTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()

    def test_import_history_and_finalize(self) -> None:
        export = self.tmp / "export.json"
        export.write_text(json.dumps(_hae_payload()), encoding="utf-8")
        out = self.cli_ok("--tz", "Europe/Berlin", "import-history", str(export))
        self.assertIn("samples=6", out)
        self.assertIn("workouts=1", out)
        self.assertTrue((self.root / "data/daily/2026/2026-11-20.json").is_file())
        self.assertTrue((self.root / "data/baselines/2026-W47.json").is_file())
        self.assertTrue((self.root / "data/freshness.json").is_file())
        store = self.store()
        rows = {r.key: r for r in store.daily("tst_h", "2026-11-20", "2026-11-21")}
        self.assertAlmostEqual(rows["tst_h"].value, 7.0)
        raw = store.workouts("2026-11-20")[0].raw_json
        self.assertNotIn("lat", raw)  # GPS stripped at ingest
        out = self.cli_ok("--tz", "Europe/Berlin", "finalize", "2026-11-21")
        self.assertIn("finalized 2026-11-21", out)

    def test_finalize_rejects_bad_date_and_import_bad_file(self) -> None:
        self.assertEqual(self.run_cli("--ledger", str(self.root), "finalize", "21.11.2026")[0], 2)
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_cli("--ledger", str(self.root), "import-history", str(bad))[0], 2)

    def test_source_map_env_is_validated(self) -> None:
        export = self.tmp / "export.json"
        export.write_text(json.dumps(_hae_payload()), encoding="utf-8")
        with mock.patch.dict(os.environ, {"HEALTH_SOURCE_MAP": '{"Test": "laptop"}'}):
            self.assertEqual(self.run_cli("--ledger", str(self.root), "import-history", str(export))[0], 2)

    def test_dashboard(self) -> None:
        self.seed_plan(self.ledger)
        out = self.cli_ok("dashboard")
        self.assertIn("index.html", out)
        self.assertTrue((self.root / "site/checkin.html").is_file())


# --------------------------------------------------------------------------
# Review, plan and halt


class ReviewTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()

    def test_dry_run_review_keeps_plan(self) -> None:
        self.seed_plan(self.ledger)
        out = self.cli_ok("--dry-run", "review", WEEK)
        self.assertIn("ruling=keep", out)
        self.assertTrue((self.root / f"reviews/{WEEK}/packet.json").is_file())
        self.assertTrue((self.root / f"reviews/{WEEK}/06-cerebellum-ruling.json").is_file())

    def test_review_without_plan_or_providers_fails_closed(self) -> None:
        code, out, _ = self.run_cli("--ledger", str(self.root), "--dry-run", "review", WEEK)
        self.assertEqual(code, 1)
        self.assertIn("aborted=no_plan", out)
        self.seed_plan(self.ledger)
        code, out, _ = self.run_cli("--ledger", str(self.root), "review", WEEK)  # no API key
        self.assertEqual(code, 1)
        self.assertIn("aborted=provider", out)

    def test_bad_week(self) -> None:
        self.assertEqual(self.run_cli("--ledger", str(self.root), "review", "47")[0], 2)

    def test_halt_blocks_review_and_resume_clears(self) -> None:
        self.seed_plan(self.ledger)
        self.cli_ok("halt")
        self.assertTrue((self.root / "config/HALT").is_file())
        self.assertEqual(self.run_cli("--ledger", str(self.root), "--dry-run", "review", WEEK)[0], 3)
        self.assertEqual(self.run_cli("--ledger", str(self.root), "experiment", "design", "exp-1")[0], 3)
        self.assertIn("resumed", self.cli_ok("resume"))
        self.assertFalse((self.root / "config/HALT").exists())
        self.assertIn("not halted", self.cli_ok("resume"))
        if HAS_GIT:
            log = subprocess.run(["git", "-C", str(self.root), "log", "--format=%an|%s"], capture_output=True,
                                 text=True, check=True).stdout
            self.assertIn("Pineal|Halt", log)
            self.assertIn("Pineal|Resume", log)
            tracked = subprocess.run(["git", "-C", str(self.root), "ls-files", "config/HALT"], capture_output=True,
                                     text=True, check=True).stdout
            self.assertEqual(tracked, "")


class PlanCommandsTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()
        self.seed_plan(self.ledger)

    def propose(self, minutes: int, tier: str = "approve") -> str:
        op = {"op": "set", "path": "/sessions/0/duration_min", "value": minutes, "refs": []}
        return plans.propose(self.ledger, [op], tier, ["test"], [], "reviews/x/06.json", NOW).id

    def test_pending_approve_rollback(self) -> None:
        pid = self.propose(45)
        self.assertIn(pid, self.cli_ok("pending"))
        self.assertIn("plan v0002", self.cli_ok("approve", pid))
        self.assertEqual(plans.current(self.ledger)["sessions"][0]["duration_min"], 45)
        self.assertIn("no pending changes", self.cli_ok("pending"))
        self.assertIn("as v0003", self.cli_ok("rollback", "v0001"))
        self.assertEqual(plans.current(self.ledger)["sessions"][0]["duration_min"], 40)

    def test_veto_and_errors(self) -> None:
        pid = self.propose(35, tier="notify")
        self.assertIn("vetoed", self.cli_ok("veto", pid))
        self.assertEqual(self.run_cli("--ledger", str(self.root), "approve", pid)[0], 1)  # no longer pending
        self.assertEqual(self.run_cli("--ledger", str(self.root), "approve", "p-000000000000")[0], 1)
        self.assertEqual(self.run_cli("--ledger", str(self.root), "rollback", "latest")[0], 2)
        self.assertEqual(self.run_cli("--ledger", str(self.root), "rollback", "1")[0], 1)  # already current


# --------------------------------------------------------------------------
# Experiments and evidence


class ExperimentTest(CliCase):
    START = "2026-11-02"

    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()
        self.db = self.store()
        self.rng = random.Random(3)

    def put(self, first: str, n: int, arm_effect: dict[str, float] | None = None, allocation=None) -> None:
        labels = dict(allocation or [])
        rows = []
        for k in range(n):
            day = (date.fromisoformat(first) + timedelta(days=k)).isoformat()
            bump = (arm_effect or {}).get(labels.get(day, ""), 0.0)
            rows.append(DailyValue(day, "hrv_sdnn_ln", 4.0 + bump + self.rng.gauss(0.0, 0.05), "watch", 3, "ok"))
        self.db.put_daily(rows)

    def draft(self) -> dict:
        return {"id": "exp-1", "belief_id": "b-sleep", "intervention": "Abendlicht dimmen",
                "comparator": "wie bisher", "outcome_key": "hrv_sdnn_ln", "source": "watch",
                "design": "paired_blocks", "block_days": 7, "washout_days": 2, "pairs": 6, "a_days_per_week": None,
                "swc": 0.03, "swc_harm": 0.03, "exclusions": [{"kind": "checkin_flag", "flag": "fever"}],
                "covariates": [], "start_date": self.START, "created_at": "2026-11-01T12:00:00+00:00"}

    def belief(self) -> None:
        claim = {"population": "adult", "intervention": "dim light", "comparator": "usual", "outcome": "hrv",
                 "unit": "ln ms", "direction": 1}
        prior = {"mu0": 0.0, "sd0": 0.1, "sources": ["doi:10.5555/demo.0001"], "certainty": 3.0,
                 "rubric_version": "thal-1"}
        event = beliefs.make_event("prior_set", "b-sleep", "2026-11-01", claim=claim, swc=0.03, prior=prior)
        self.ledger.append_jsonl(beliefs.EVENTS_PATH, event, author="pineal")

    def test_design_allocate_analyze(self) -> None:
        self.put("2026-08-01", 93)  # baseline through 2026-11-01
        self.ledger.write_json("experiments/exp-1/draft.json", self.draft(), author="pineal")
        out = self.cli_ok("experiment", "design", "exp-1")
        self.assertIn("protocol written", out)
        self.assertTrue((self.root / "experiments/exp-1/protocol.json").is_file())
        self.assertEqual(self.run_cli("--ledger", str(self.root), "experiment", "design", "exp-1")[0], 1)  # frozen
        self.assertIn("A=", self.cli_ok("experiment", "allocate", "exp-1"))
        csv = (self.root / "experiments/exp-1/data.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(csv[0], "date,arm,value")
        self.assertEqual(len(csv), 1 + 6 * 14)
        allocation = [tuple(line.split(",")[:2]) for line in csv[1:]]
        self.put(self.START, 6 * 14, {"A": 0.08}, allocation)
        self.assertEqual(self.run_cli("--ledger", str(self.root), "experiment", "analyze", "exp-1")[0], 2)  # no prior
        self.belief()
        out = self.cli_ok("experiment", "analyze", "exp-1")
        self.assertIn("decision=", out)
        result = json.loads((self.root / "experiments/exp-1/result.json").read_text(encoding="utf-8"))
        self.assertEqual(len(result["pair_diffs"]), 6)
        self.assertGreater(result["d_bar"], 0)

    def test_ineligible_draft_is_not_frozen(self) -> None:
        self.put("2026-08-01", 93)
        self.ledger.write_json("experiments/exp-1/draft.json", {**self.draft(), "pairs": 4}, author="pineal")
        code, out, _ = self.run_cli("--ledger", str(self.root), "experiment", "design", "exp-1")
        self.assertEqual(code, 1)
        self.assertIn("pairs_lt_6", out)
        self.assertFalse((self.root / "experiments/exp-1/protocol.json").exists())
        self.assertEqual(self.run_cli("--ledger", str(self.root), "experiment", "allocate", "exp-1")[0], 2)


class ScorePaperTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()
        self.ledger.write_json("profile/owner.json", demo.OWNER, author="pineal")

    def test_rescore_ledger_record(self) -> None:
        rec = {"schema": 1, "extraction": demo.extraction(demo.PAPERS[0]), "update_to": [], "score": None,
               "blocked": False}
        self.ledger.write_json("evidence/papers/10-5555-demo-0001.json", rec, author="pineal")
        out = self.cli_ok("score-paper", "evidence/papers/10-5555-demo-0001.json")
        self.assertEqual(json.loads(out)["gate"], "actionable")
        stored = self.ledger.read_json("evidence/papers/10-5555-demo-0001.json")
        self.assertEqual(stored["score"]["rubric_version"], "thal-1")

    def test_retraction_vetoes_and_blocked_is_not_scored(self) -> None:
        retracted = {"extraction": demo.extraction(demo.PAPERS[0]), "update_to": [{"type": "retraction"}]}
        path = self.tmp / "outside.json"
        path.write_text(json.dumps(retracted), encoding="utf-8")
        out = self.cli_ok("score-paper", str(path))
        self.assertEqual(json.loads(out)["certainty"], 0.0)
        blocked = {**retracted, "update_to": [], "blocked": True}
        path.write_text(json.dumps(blocked), encoding="utf-8")
        self.assertEqual(self.run_cli("--ledger", str(self.root), "score-paper", str(path))[0], 1)
        path.write_text(json.dumps({"extraction": {"doi": 1}}), encoding="utf-8")
        self.assertEqual(self.run_cli("--ledger", str(self.root), "score-paper", str(path))[0], 2)


# --------------------------------------------------------------------------
# Doctor


class DoctorTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()

    def test_fails_without_token(self) -> None:
        code, out, _ = self.run_cli("--ledger", str(self.root), "doctor")
        self.assertEqual(code, 2)
        self.assertIn("fail token", out)
        self.assertIn("tailscale serve", out)

    def test_passes_with_safe_config(self) -> None:
        with mock.patch.dict(os.environ, {"HEALTH_TOKEN": TOKEN, "HEALTH_HOST": "127.0.0.1"}):
            code, out, _ = self.run_cli("--ledger", str(self.root), "doctor")
        self.assertEqual(code, 0 if HAS_GIT else 2, msg=out)
        self.assertIn("ok   token", out)
        self.assertIn("ok   roster", out)
        self.assertIn("budget", out)

    def test_public_bind_fails(self) -> None:
        with mock.patch.dict(os.environ, {"HEALTH_TOKEN": TOKEN, "HEALTH_HOST": "0.0.0.0"}):
            code, out, _ = self.run_cli("--ledger", str(self.root), "doctor")
        self.assertEqual(code, 2)
        self.assertIn("fail bind", out)


# --------------------------------------------------------------------------
# Pineal wiring (stubbed)


class PinealWiringTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()
        self.stub = StubPineal()
        patch = mock.patch.object(cli, "_pineal", lambda: self.stub)
        patch.start()
        self.addCleanup(patch.stop)

    def app(self, *extra: str) -> cli.App:
        app = cli.App(cli.build_parser().parse_args(["--ledger", str(self.root), *extra, "serve"]), os.environ)
        self.addCleanup(app.close)
        return app

    def test_tick_and_run(self) -> None:
        self.assertIn("finalize_day p: ok", self.cli_ok("tick"))
        self.assertIn("scouts 2026-11-22: ok", self.cli_ok("run", "scouts"))
        self.assertIn("finalize_day 2026-11-01", self.cli_ok("run", "finalize_day", "--period", "2026-11-01"))
        self.assertEqual(self.run_cli("--ledger", str(self.root), "run", "nope")[0], 2)

    def test_halt_lets_only_essential_jobs_run(self) -> None:
        self.cli_ok("halt")
        self.assertEqual(self.run_cli("--ledger", str(self.root), "run", "scouts")[0], 3)
        self.cli_ok("run", "finalize_day")
        self.assertEqual(self.run_cli("--ledger", str(self.root), "tick")[0], 3)

    def test_serve_kwargs_routes(self) -> None:
        app = self.app("--tz", "Europe/Berlin")
        ctx = cli._pineal_context(app, self.stub)
        self.assertIs(ctx.ledger, app.ledger)
        kwargs = cli.serve_kwargs(app, self.stub, ctx)
        self.assertEqual(kwargs["site_dir"], self.root / "site")
        status, body = kwargs["post_routes"]["/checkin"]({"mood": 5, "wellbeing": 7})
        self.assertEqual((status, body["stored"]), (200, 2))
        self.assertEqual(kwargs["post_routes"]["/checkin"]({"mood": 99})[0], 400)
        self.assertEqual(kwargs["post_routes"]["/message"]({"to": "vagus", "text": "Hallo"})[0], 200)
        self.assertEqual(self.stub.messages, [("vagus", "Hallo")])
        self.assertEqual(kwargs["post_routes"]["/message"]({"to": "vagus"})[0], 400)
        event = HealthEvent("hr_notification", "2026-11-22T03:00:00+01:00", None, "{}", "watch")
        kwargs["on_events"]([event])
        self.assertEqual([a.level for a in self.stub.delivered], ["urgent"])

    def test_serve_refuses_short_token(self) -> None:
        with mock.patch.dict(os.environ, {"HEALTH_TOKEN": "short"}):
            code, _, err = self.run_cli("--ledger", str(self.root), "serve")
        self.assertEqual(code, 2)
        self.assertIn("32", err)

    def test_serve_starts_with_safe_config(self) -> None:
        with mock.patch.dict(os.environ, {"HEALTH_TOKEN": TOKEN}), mock.patch.object(cli.ingest, "serve") as serve:
            self.cli_ok("serve")
        cfg = serve.call_args.args[0]
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.db_path, self.root / ".private" / "health.sqlite")
        self.assertEqual(set(serve.call_args.kwargs["post_routes"]), {"/checkin", "/message"})


if __name__ == "__main__":
    unittest.main()

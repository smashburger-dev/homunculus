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

from homunculus import agents, beliefs, cli, demo, pineal, plans
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


class InitPlanTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()

    def test_fresh_ledger_gets_a_starter_plan_once(self) -> None:
        self.assertIn("plan v0001", self.cli_ok("init-plan"))
        plan = plans.current(self.ledger)
        self.assertEqual((plan["version"], plan["ratified_by"], plan["sessions"]), (1, "owner", []))
        self.assertEqual(plan["rationale_refs"], ["config/goals.md"])
        self.assertEqual(plan["valid_until"], "2026-11-29T16:00:00Z")
        self.assertTrue((self.root / "plans/v0001.md").is_file())
        self.assertEqual(self.run_cli("--ledger", str(self.root), "init-plan")[0], 2)  # never overwrites

    def test_from_file_is_validated_and_refuses_a_kcal_target(self) -> None:
        content = {k: v for k, v in _plan().items() if k not in cli.plans.PROTECTED_KEYS | {"valid_until"}}
        path = self.tmp / "plan.json"
        path.write_text(json.dumps({**content, "nutrition": {**content["nutrition"], "kcal_target": 1800}}),
                        encoding="utf-8")
        code, _, err = self.run_cli("--ledger", str(self.root), "init-plan", "--from", str(path))
        self.assertEqual(code, 2)
        self.assertIn("kcal_target", err)
        path.write_text(json.dumps({**content, "sessions": [{"id": "x"}]}), encoding="utf-8")
        self.assertEqual(self.run_cli("--ledger", str(self.root), "init-plan", "--from", str(path))[0], 2)
        self.assertFalse((self.root / "plans/CURRENT").exists())
        path.write_text(json.dumps(content), encoding="utf-8")
        self.cli_ok("init-plan", "--from", str(path))
        self.assertEqual(plans.current(self.ledger)["sessions"][0]["id"], "s-thu")


class CoachingCliTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()
        for rule in ("bp_crisis", "chest_pain"):
            self.ledger.append_jsonl(pineal.ALERTS_LOG, {"level": "urgent", "rule_id": rule, "date": "2026-11-20",
                                                         "data": {}, "text_de": "synthetisch"}, author="pineal")

    def test_clear_one_rule_then_all(self) -> None:
        out = self.cli_ok("coaching", "show")
        self.assertIn("bp_crisis 2026-11-20", out)
        self.assertIn("chest_pain 2026-11-20", out)
        self.assertEqual(self.run_cli("--ledger", str(self.root), "coaching", "clear", "nope")[0], 2)
        self.assertIn("cleared 1", self.cli_ok("coaching", "clear", "bp_crisis"))
        self.assertEqual([a["rule_id"] for a in pineal.coaching_holds(self.ledger, "2026-11-22")], ["chest_pain"])
        self.assertIn("cleared 1", self.cli_ok("coaching", "clear"))
        self.assertEqual(pineal.coaching_holds(self.ledger, "2026-11-22"), [])
        self.assertIn("no coaching hold", self.cli_ok("coaching", "show"))
        self.assertEqual(len(self.ledger.read_jsonl(pineal.COACHING_CLEARED_LOG)), 2)


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
        code, out, _ = self.run_cli("--ledger", str(self.root), "experiment", "analyze", "exp-1")
        self.assertEqual(code, 1)  # NOW lies inside the allocation, which runs through 2027-01-24
        self.assertIn("allocation runs through 2027-01-24", out)
        self.at("2027-01-25")
        self.assertEqual(self.run_cli("--ledger", str(self.root), "experiment", "analyze", "exp-1")[0], 2)  # no prior
        self.belief()
        out = self.cli_ok("experiment", "analyze", "exp-1")
        self.assertIn("decision=", out)
        result = json.loads((self.root / "experiments/exp-1/result.json").read_text(encoding="utf-8"))
        self.assertEqual(len(result["pair_diffs"]), 6)
        self.assertGreater(result["d_bar"], 0)
        self.assertEqual(result["decision"], "adopt")
        events = [e for e in self.ledger.read_jsonl(beliefs.EVENTS_PATH) if e["kind"] == "personal_result"]
        self.assertEqual([e["experiment"] for e in events], ["exp-1"])
        view = self.ledger.read_json("beliefs/views/b-sleep.json")
        self.assertGreater(view["mu"], 0.0)  # moved off the literature prior mu0 = 0

    def at(self, day: str) -> None:
        moment = datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc)
        patch = mock.patch.object(cli, "_utcnow", lambda: moment)
        patch.start()
        self.addCleanup(patch.stop)

    def allocation(self) -> list[tuple[str, ...]]:
        csv = (self.root / "experiments/exp-1/data.csv").read_text(encoding="utf-8").splitlines()
        return [tuple(line.split(",")[:2]) for line in csv[1:]]

    def analyze(self) -> tuple[int, str]:
        code, out, _ = self.run_cli("--ledger", str(self.root), "experiment", "analyze", "exp-1")
        return code, out

    def test_extension_is_write_once_and_final_result_is_kept(self) -> None:
        self.put("2026-08-01", 93)
        self.ledger.write_json("experiments/exp-1/draft.json", self.draft(), author="pineal")
        self.cli_ok("experiment", "design", "exp-1")
        self.cli_ok("experiment", "allocate", "exp-1")
        self.put(self.START, 6 * 14, {"A": 0.02}, self.allocation())
        self.belief()
        # Every posterior lands between reject and adopt, so the decision is extend, then inconclusive.
        for name, value in (("ADOPT_BENEFIT", 1.01), ("REJECT_BENEFIT_BELOW", -1.0)):
            patch = mock.patch.object(cli.experiments, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.at("2027-01-25")
        self.assertIn("decision=extend", self.analyze()[1])
        self.assertTrue((self.root / "experiments/exp-1/extension.json").is_file())
        self.assertFalse(any(e["kind"] == "personal_result" for e in self.ledger.read_jsonl(beliefs.EVENTS_PATH)))
        self.at("2027-01-26")  # the two extension pairs have no data yet
        code, out = self.analyze()
        self.assertEqual(code, 1)
        self.assertIn("allocation runs through 2027-02-21", out)
        result = json.loads((self.root / "experiments/exp-1/result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["decision"], "extend")
        self.cli_ok("experiment", "allocate", "exp-1")
        allocation = self.allocation()
        self.assertEqual(len(allocation), 8 * 14)
        self.put("2027-01-25", 2 * 14, {"A": 0.02}, allocation)
        self.at("2027-02-22")
        self.assertIn("decision=inconclusive", self.analyze()[1])
        result = json.loads((self.root / "experiments/exp-1/result.json").read_text(encoding="utf-8"))
        self.assertEqual((result["extended"], len(result["pair_diffs"])), (True, 8))
        events = [e for e in self.ledger.read_jsonl(beliefs.EVENTS_PATH) if e["kind"] == "personal_result"]
        self.assertEqual(len(events), 1)
        code, out = self.analyze()
        self.assertEqual(code, 1)
        self.assertIn("final decision 'inconclusive'", out)
        self.assertEqual(json.loads((self.root / "experiments/exp-1/result.json").read_text(encoding="utf-8")),
                         result)

    def test_extension_waits_for_its_data(self) -> None:
        self.put("2026-08-01", 93)
        self.ledger.write_json("experiments/exp-1/draft.json", self.draft(), author="pineal")
        self.cli_ok("experiment", "design", "exp-1")
        self.cli_ok("experiment", "allocate", "exp-1")
        self.put(self.START, 6 * 14 - 1, {"A": 0.02}, self.allocation())  # last day not ingested yet
        self.belief()
        for name, value in (("ADOPT_BENEFIT", 1.01), ("REJECT_BENEFIT_BELOW", -1.0)):
            patch = mock.patch.object(cli.experiments, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.at("2027-01-25")
        code, out = self.analyze()
        self.assertEqual(code, 1)
        self.assertIn("no stored data yet for 1 day(s) from 2027-01-24", out)
        self.put(self.START, 6 * 14, {"A": 0.02}, self.allocation())
        self.assertIn("decision=extend", self.analyze()[1])
        self.cli_ok("experiment", "allocate", "exp-1")
        self.at("2027-02-22")  # extension over, none of its days stored
        code, out = self.analyze()
        self.assertEqual(code, 1)
        self.assertIn("no stored data yet", out)
        self.assertFalse(any(e["kind"] == "personal_result" for e in self.ledger.read_jsonl(beliefs.EVENTS_PATH)))
        self.put("2027-01-25", 2 * 14, {"A": 0.02}, self.allocation())
        self.assertIn("decision=inconclusive", self.analyze()[1])
        result = json.loads((self.root / "experiments/exp-1/result.json").read_text(encoding="utf-8"))
        self.assertEqual((result["extended"], len(result["pair_diffs"])), (True, 8))

    def test_extension_without_data_counts_missing_after_lag(self) -> None:
        self.put("2026-08-01", 93)
        self.ledger.write_json("experiments/exp-1/draft.json", self.draft(), author="pineal")
        self.cli_ok("experiment", "design", "exp-1")
        self.cli_ok("experiment", "allocate", "exp-1")
        self.put(self.START, 6 * 14, {"A": 0.02}, self.allocation())
        self.belief()
        for name, value in (("ADOPT_BENEFIT", 1.01), ("REJECT_BENEFIT_BELOW", -1.0)):
            patch = mock.patch.object(cli.experiments, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.at("2027-01-25")
        self.assertIn("decision=extend", self.analyze()[1])
        self.at((date(2027, 2, 21) + timedelta(days=cli.FINALIZE_LAG_DAYS + 1)).isoformat())
        self.assertIn("decision=inconclusive", self.analyze()[1])

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
        self.assertEqual(stored["score"]["rubric_version"], "thal-2")

    def test_status_follows_the_appraisal_not_the_raw_gate(self) -> None:
        ext = demo.extraction(demo.PAPERS[0])
        ext["outcome_switched"] = {"value": True, "quote": "The primary outcome differed from the registered one."}
        rel = "evidence/papers/10-5555-demo-0001.json"
        self.ledger.write_json(rel, {"schema": 1, "extraction": ext, "update_to": [], "score": None,
                                     "blocked": False}, author="pineal")
        out = json.loads(self.cli_ok("score-paper", rel))
        self.assertEqual((out["gate"], out["status"]), ("actionable", "n_of_1_only"))  # weak methods cap it
        self.assertEqual(self.ledger.read_json(rel)["appraisal"]["status"], "n_of_1_only")  # stored without --appraise

    def believe(self, bid: str, direction: int, sources: list[str]) -> None:
        claim = {"population": "adult", "intervention": "x", "comparator": "usual", "outcome": "sleep",
                 "unit": "SD", "direction": direction}
        prior = {"mu0": 0.0, "sd0": 0.1, "sources": sources, "certainty": 1.0, "rubric_version": "thal-2"}
        self.ledger.append_jsonl(beliefs.EVENTS_PATH, beliefs.make_event(
            "prior_set", bid, "2026-11-01", claim=claim, swc=0.1, prior=prior), author="pineal")

    def test_promise_benefit_takes_the_linked_belief_direction(self) -> None:
        rec = {"extraction": demo.extraction(demo.PAPERS[1]), "update_to": []}
        owner = cli.owner_profile(demo.OWNER, NOW.date())
        key = "doi:10.5555/demo.0002"
        self.assertEqual(cli.appraise_record(self.ledger, rec, owner, as_of="2026-11-22")[1].promise, 0.0)
        self.believe("b-up", 1, [key])
        self.assertEqual(cli._linked_direction(self.ledger, key, "2026-11-22"), 1)
        self.assertGreater(cli.appraise_record(self.ledger, rec, owner, as_of="2026-11-22")[1].promise, 0.0)
        self.believe("b-down", -1, [key])  # linked beliefs disagree: no direction, no benefit
        self.assertIsNone(cli._linked_direction(self.ledger, key, "2026-11-22"))
        self.believe(cli.deepdive.belief_id_for(key), 1, [])  # the paper's own frontier belief wins
        self.assertEqual(cli._linked_direction(self.ledger, key, "2026-11-22"), 1)

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


class PaperSwcTest(unittest.TestCase):
    OWNER = cli.OwnerProfile(sex=None, age=None, training_status=None, conditions=(), swc={"rmssd": 3.0})

    @staticmethod
    def paper(kind: str, sd_pop: float | None) -> dict:
        f = lambda v: {"value": v, "quote": None}  # noqa: E731
        return {"outcome": {"name": f("rmssd")}, "effect": {"type": f(kind), "sd_pop": f(sd_pop)}}

    def test_owner_swc_is_converted_to_paper_units(self) -> None:
        self.assertEqual(cli._paper_swc(self.paper("MD", 10.0), self.OWNER), 3.0)
        self.assertAlmostEqual(cli._paper_swc(self.paper("SMD", 10.0), self.OWNER), 0.3)
        self.assertIsNone(cli._paper_swc(self.paper("SMD", None), self.OWNER))  # scale unknown
        self.assertIsNone(cli._paper_swc(self.paper("RR", None), self.OWNER))  # no relative SWC
        self.assertIsNone(cli._paper_swc({**self.paper("MD", None), "outcome": {"name": {"value": "x", "quote": None}}},
                                         self.OWNER))


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
        self.cli_ok("init-plan")
        with mock.patch.dict(os.environ, {"HEALTH_TOKEN": TOKEN, "HEALTH_HOST": "127.0.0.1"}):
            code, out, _ = self.run_cli("--ledger", str(self.root), "doctor")
        self.assertEqual(code, 0 if HAS_GIT else 2, msg=out)
        self.assertIn("ok   token", out)
        self.assertIn("ok   roster", out)
        self.assertIn("ok   plan: v0001", out)
        self.assertIn("budget", out)
        names = [line[5:].split(":")[0] for line in out.splitlines() if line[:4].strip() in ("ok", "warn", "fail", "info")]
        help_text = cli.build_parser()._subparsers._group_actions[0].choices["doctor"].description
        self.assertEqual(", ".join(names).replace("bind", "bind host"), help_text.removeprefix("check "))

    def test_reports_missing_plan_and_push_channel(self) -> None:
        env = {"HEALTH_TOKEN": TOKEN}
        with mock.patch.dict(os.environ, env):
            code, out, _ = self.run_cli("--ledger", str(self.root), "doctor")
        self.assertEqual(code, 2)
        self.assertIn("fail plan", out)
        self.assertIn("init-plan", out)
        self.assertIn(f"warn push: {cli.PUSH_URL_ENV} not set", out)
        with mock.patch.dict(os.environ, {**env, cli.PUSH_URL_ENV: "https://push.example.test/topic"}):
            self.assertIn(f"warn push: {cli.PUSH_TOKEN_ENV} not set", self.run_cli("--ledger", str(self.root), "doctor")[1])
        with mock.patch.dict(os.environ, {**env, cli.PUSH_URL_ENV: "http://push.example.test/topic"}):
            self.assertIn("fail push", self.run_cli("--ledger", str(self.root), "doctor")[1])
        with mock.patch.dict(os.environ, {**env, cli.PUSH_URL_ENV: "https://push.example.test/topic",
                                          cli.PUSH_TOKEN_ENV: "synthetic-push-token"}):
            out = self.run_cli("--ledger", str(self.root), "doctor")[1]
        self.assertIn("ok   push", out)
        self.assertNotIn("synthetic-push-token", out)

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

    def test_push_url_makes_owner_notifier_push_before_inbox(self) -> None:
        self.assertEqual(cli._pineal_context(self.app(), self.stub).notifier, "notifier")  # no URL: inbox only
        with mock.patch.dict(os.environ, {cli.PUSH_URL_ENV: "https://push.example.test/topic"}):
            notifier = cli._pineal_context(self.app(), self.stub).notifier
        self.assertIsInstance(notifier, cli.PushNotifier)
        sent: list = []
        inner = SimpleNamespace(send=lambda to, text, level: sent.append((to, text, level)))
        requests: list = []

        def opener(req: Any, timeout: float) -> Any:
            requests.append(req)
            resp = mock.MagicMock()
            resp.__enter__.return_value.status = 200
            return resp

        cli.PushNotifier("https://push.example.test/topic", inner, token="tok", opener=opener).send(
            "owner", "Blutdruck sehr hoch", "urgent")
        self.assertEqual(requests[0].data.decode("utf-8"), "Blutdruck sehr hoch")
        self.assertEqual(requests[0].get_header("Priority"), "urgent")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer tok")
        self.assertEqual(sent, [("owner", "Blutdruck sehr hoch", "urgent")])

    def test_failed_push_raises_so_alert_is_not_marked_delivered(self) -> None:
        sent: list = []
        inner = SimpleNamespace(send=lambda *a: sent.append(a))

        def opener(req: Any, timeout: float) -> Any:
            raise OSError("offline")

        with self.assertRaises(OSError):
            cli.PushNotifier("https://push.example.test/topic", inner, opener=opener).send("owner", "x", "urgent")
        self.assertEqual(sent, [])

    def test_push_url_must_be_https(self) -> None:
        with mock.patch.dict(os.environ, {cli.PUSH_URL_ENV: "http://push.example.test/topic"}):
            with self.assertRaises(cli.UsageError):
                cli._pineal_context(self.app(), self.stub)

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

    def test_serve_warns_without_push_url(self) -> None:
        env = {"HEALTH_TOKEN": TOKEN}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli.ingest, "serve"):
            code, _, err = self.run_cli("--ledger", str(self.root), "serve")
        self.assertEqual(code, 0)
        self.assertIn(cli.PUSH_URL_ENV, err)
        with mock.patch.dict(os.environ, {**env, cli.PUSH_URL_ENV: "https://push.example.test/topic"}), \
                mock.patch.object(cli.ingest, "serve"):
            self.assertNotIn(cli.PUSH_URL_ENV, self.run_cli("--ledger", str(self.root), "serve")[2])


# --------------------------------------------------------------------------
# Trackers, Devin layer, frontier lane


class FakeTracker:
    """TrackerSource double: one synthetic sample per poll, or the error it was given."""

    name = "garmin"

    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.revoked = error, False

    def poll(self, cursor, *, now, tz, backfill_days=0):  # noqa: ANN001
        from homunculus.store import Sample
        from homunculus.trackers.base import PollResult, TrackerBatch

        if self.error:
            raise self.error
        sample = Sample("resting_heart_rate", "count/min", "2026-11-22T06:00:00+00:00", "qty", 51.0, "garmin")
        return PollResult(TrackerBatch(samples=(sample,)), ("2026-11-21T00:00:00+00:00", "2026-11-22T16:00:00+00:00"),
                          {"watermark": "2026-11-22T16:00:00+00:00"}, True, 2)

    def auth_status(self):  # noqa: ANN201
        from homunculus.trackers.base import AuthStatus

        return AuthStatus(True, None, None, "fake")

    def authorize(self, ui, *, manual=False):  # noqa: ANN001, ANN201
        ui.info("fake auth")
        return self.auth_status()

    def revoke(self) -> None:
        self.revoked = True


class TrackerCliTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()
        self.ledger.write_json("config/trackers.json", {"enabled": ["garmin"], "garmin": {"poll_minutes": 60}},
                               author="pineal")
        self.fake = FakeTracker()
        patch = mock.patch.object(cli.tracker_registry, "build", lambda name, **kw: self.fake)
        patch.start()
        self.addCleanup(patch.stop)

    def test_blocked_and_unknown_names_fail_before_any_io(self) -> None:
        code, _, err = self.run_cli("tracker", "auth", "oura")  # no ledger needed
        self.assertEqual(code, 2)
        self.assertIn("API data may not be AI input", err)
        self.assertEqual(self.run_cli("tracker", "poll", "nope")[0], 2)

    def test_poll_dry_run_then_write_status_and_purge(self) -> None:
        out = self.cli_ok("tracker", "poll", "--dry-run")
        self.assertIn("garmin: ok", out)
        self.assertIn("would write samples=1", out)
        store = self.store()
        self.assertIsNone(store.tracker_state("garmin"))
        self.assertIn("wrote deleted=0, samples=1", self.cli_ok("tracker", "poll", "garmin"))
        self.assertEqual(store.tracker_state("garmin").requests_today, 2)
        rows = json.loads(self.cli_ok("tracker", "status", "--json"))
        self.assertEqual([(r["name"], r["enabled"], r["auth_ok"]) for r in rows], [("garmin", True, True)])
        self.assertEqual(rows[0]["last_ok"], "2026-11-22T16:00:00+00:00")
        self.assertIn("purged", self.cli_ok("tracker", "revoke", "garmin", "--purge", "--yes"))
        self.assertTrue(self.fake.revoked)
        self.assertEqual(store.samples("resting_heart_rate", "2026-11-21"), [])

    def test_auth_required_exits_2_and_records_the_kind(self) -> None:
        from homunculus.trackers.base import AuthRequired

        self.fake.error = AuthRequired("token expired")
        code, out, _ = self.run_cli("--ledger", str(self.root), "tracker", "poll", "--all")
        self.assertEqual(code, 2)
        self.assertIn("failed auth_required", out)
        self.assertEqual(self.store().tracker_state("garmin").last_error, "auth_required")
        self.assertIn("auth ok", self.cli_ok("tracker", "auth", "garmin", "--port", "8765"))

    def test_poll_minutes_below_60_is_a_config_error(self) -> None:
        self.ledger.write_json("config/trackers.json", {"enabled": ["garmin"], "garmin": {"poll_minutes": 5}},
                               author="pineal")
        self.assertEqual(self.run_cli("--ledger", str(self.root), "tracker", "poll")[0], 2)


class IngestCliTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.payload = self.tmp / "inbox.txt"
        self.payload.write_text("Webhook body follows:\n" + json.dumps(_hae_payload()), encoding="utf-8")

    def test_ingest_file_prints_counts_only(self) -> None:
        out = json.loads(self.cli_ok("ingest", "--file", str(self.payload)))
        self.assertEqual(out["counts"]["workouts"], 1)
        self.assertGreaterEqual(out["days"], 2)
        self.assertNotIn("53.0", json.dumps(out))
        self.assertEqual(self.run_cli("--ledger", str(self.root), "ingest")[0], 2)

    def test_llm_copy_ingest_needs_the_opt_in(self) -> None:
        code, out, _ = self.run_cli("--ledger", str(self.root), "ingest", "--llm-copy", "--file", str(self.payload))
        self.assertEqual(code, 1)
        self.assertIn("ingest-relay", out)
        self.assertEqual(self.store().daily("rhr", "2026-01-01"), [])  # nothing ingested
        ledger = Ledger(self.root, {})
        ledger.write_json("config/devin.json", {"llm_copy_ingest": True}, author="pineal")
        out = json.loads(self.cli_ok("ingest", "--llm-copy", "--file", str(self.payload)))
        self.assertEqual(out["counts"]["workouts"], 1)

    def test_api_sources_guard_drops_the_vendor_copy(self) -> None:
        env = {"HEALTH_SOURCE_MAP": json.dumps({"Test Watch": "garmin"}), "HEALTH_API_SOURCES": "garmin"}
        with mock.patch.dict(os.environ, env):
            out = json.loads(self.cli_ok("ingest-stdin", "--file", str(self.payload)))
            self.assertEqual(out["counts"]["workouts"], 0)
            self.assertGreater(out["skipped"]["api_source"], 0)
            path = self.tmp / "history.json"
            path.write_text(json.dumps(_hae_payload()), encoding="utf-8")
            self.assertIn("api_source=", self.cli_ok("import-history", str(path)))
        with mock.patch.dict(os.environ, {"HEALTH_API_SOURCES": "fitbitx"}):
            self.assertEqual(self.run_cli("--ledger", str(self.root), "ingest", "--file", str(self.payload))[0], 2)


class DevinCliTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()

    def test_export_scout_script_and_restore(self) -> None:
        specs = json.loads(self.cli_ok("devin-export", "--path", "b"))
        self.assertIn("homunculus-weekly-review", [s["name"] for s in specs])
        out_dir = self.tmp / "automations"
        self.assertIn("homunculus-weekly-review.json", self.cli_ok("devin-export", "--out", str(out_dir)))
        inside = cli.ENGINE_ROOT / "devin" / "automations" / "tmp-test"
        self.assertEqual(self.run_cli("--ledger", str(self.root), "devin-export", "--out", str(inside))[0], 2)
        self.assertFalse(inside.exists())
        self.assertIn("STATE_FILE", self.cli_ok("scout-script", "retina", "--max-items", "3"))
        self.assertIn("restored 0 rows", self.cli_ok("devin-restore"))

    def test_unknown_automation_and_tick_on_path_b(self) -> None:
        self.assertEqual(self.run_cli("--ledger", str(self.root), "devin-task", "nope")[0], 2)
        stub = StubPineal()
        with mock.patch.object(cli, "_pineal", lambda: stub):
            code, out, err = self.run_cli("--ledger", str(self.root), "tick", "--devin-path", "b")
            self.assertIn("finalize_day p: ok", out)  # the jobs still ran and wrote the inbox copy
            self.assertIn(cli.PUSH_URL_ENV, err)
            self.assertEqual(code, cli.EXIT_RUNTIME)  # file-only delivery on path b fails the run
            with mock.patch.dict(os.environ, {cli.PUSH_URL_ENV: "https://push.example.test/topic"}):
                code, out, err = self.run_cli("--ledger", str(self.root), "tick", "--devin-path", "b")
            self.assertEqual((code, err), (0, ""))

    def test_message_short_circuits_urgent_text(self) -> None:
        text = self.tmp / "message.txt"
        text.write_text("Ich habe seit einer Stunde Brustschmerzen.", encoding="utf-8")
        out = json.loads(self.cli_ok("--dry-run", "message", "vagus", "--path", "b", "--text-file", str(text)))
        self.assertEqual(out["status"], 200)
        self.assertIn("112", out["reply_de"])

    def test_run_bot_dry_run_writes_as_the_bot(self) -> None:
        context, schema = self.tmp / "context.json", self.tmp / "schema.json"
        context.write_text(json.dumps({"paper_meta": {"task": "test"}}), encoding="utf-8")
        schema.write_text(json.dumps({"type": "object", "properties": {"note": {"type": "string"}},
                                      "required": ["note"], "additionalProperties": False}), encoding="utf-8")
        out = json.loads(self.cli_ok("--dry-run", "run-bot", "thalamus", "--context", str(context), "--schema",
                                     str(schema), "--out", "evidence/papers/run-bot-test.json", "--run-id", "t-1"))
        self.assertEqual(out["path"], "evidence/papers/run-bot-test.json")
        self.assertEqual(self.ledger.read_json(out["path"])["bot"], "thalamus")
        code = self.run_cli("--ledger", str(self.root), "--dry-run", "run-bot", "thalamus", "--context",
                            str(context), "--schema", str(schema), "--out", "plans/CURRENT")[0]
        self.assertEqual(code, 2)  # outside thalamus's write rules


class FrontierCliTest(CliCase):
    PAPER = "evidence/papers/10-5555-demo-0004.json"

    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.init()
        self.ledger.write_json("profile/owner.json", demo.OWNER, author="pineal")
        paper = next(p for p in demo.PAPERS if p["doi"] == demo.FRONTIER_DOI)
        rec = {"schema": 1, "doi": paper["doi"], "meta": {"title": paper["title"]},
               "extraction": demo.extraction(paper), "update_to": [], "score": None, "blocked": False}
        self.ledger.write_json(self.PAPER, rec, author="pineal")
        self.ledger.write_text("quarantine/demo-c4.txt", demo.abstract(paper) + "\n\n" + demo.MECHANISM_SOURCES,
                               author="pineal")
        demo.seed_mechanism_papers(self.ledger, cli.owner_profile(demo.OWNER, date(2026, 11, 15)))

    def dive(self, fixtures: dict | None) -> tuple[int, str, str]:
        real = agents.FakeProvider
        with mock.patch.object(agents, "FakeProvider", lambda: real(fixtures)):
            return self.run_cli("--ledger", str(self.root), "--dry-run", "deepdive", self.PAPER,
                                "--source", "quarantine/demo-c4.txt")

    def test_deep_dive_lexicon_and_appraisal(self) -> None:
        before = json.loads(self.cli_ok("score-paper", self.PAPER, "--appraise"))
        self.assertEqual(before["appraisal"]["status"], "watchlist")
        code, out, err = self.dive({"thalamus": demo.DEEP_DIVE})
        self.assertEqual(code, 0, err)
        self.assertIn("decision=n_of_1_candidate", out)
        after = json.loads(self.cli_ok("score-paper", self.PAPER, "--appraise"))
        self.assertEqual(after["appraisal"]["status"], "frontier")
        self.assertEqual(self.ledger.read_json(self.PAPER)["appraisal"]["status"], "frontier")
        self.assertIn("3 links, 1 open questions", self.cli_ok("lexicon", "show"))
        self.assertIn("vagal tone", self.cli_ok("lexicon", "questions"))
        verdict = json.loads(self.cli_ok("lexicon", "lookup", "intervention:time-restricted-eating",
                                         "mechanism:circadian-clock-alignment"))
        self.assertEqual(verdict["verdict"], "supported")
        self.assertEqual(self.run_cli("--ledger", str(self.root), "lexicon", "lookup", "x")[0], 2)
        self.cli_ok("lexicon", "export-keywords", "--write")
        self.assertIn("vagal tone", self.ledger.read_json("config/keywords.json")["retina"]["terms"])
        frontier = json.loads(self.cli_ok("frontier", WEEK))
        self.assertEqual(frontier["candidates"][0]["decision"], "n_of_1_candidate")
        self.assertIn("lexicon", self.cli_ok("lexicon", "rebuild"))

    def test_ledger_design_overrides_the_cited_design(self) -> None:
        mislabeled = json.loads(json.dumps(demo.DEEP_DIVE))
        mislabeled["links"][1]["evidence"][0]["design"] = "rct_parallel"  # the ledger scored 0103 as animal
        code, out, err = self.dive({"thalamus": mislabeled})
        self.assertEqual(code, 0, err)
        record = self.ledger.read_json(self.ledger.glob("evidence/deepdives/*.json")[0])
        item = record["links"][1]["evidence"][0]
        self.assertEqual((item["design"], item["human"]), ("animal", False))
        self.assertAlmostEqual(record["chain"]["coverage"], 2 / 3)

    def test_hard_blocker_aborts_before_any_llm_call(self) -> None:
        self.ledger.write_json(self.PAPER, {**self.ledger.read_json(self.PAPER), "blocked": True}, author="pineal")
        with mock.patch.object(cli.weekly, "_caller", side_effect=AssertionError("no LLM call")):
            code, _, err = self.dive({"thalamus": demo.DEEP_DIVE})
        self.assertEqual(code, 1)
        self.assertIn("audit_blocked", err)
        self.assertEqual(list((self.root / "evidence").glob("deepdives/*.json")), [])

    def test_plain_dry_run_stays_on_watch(self) -> None:
        code, out, err = self.dive(None)  # FakeProvider's minimal instance: one link, no evidence
        self.assertEqual(code, 0, err)
        self.assertIn("decision=watch", out)

    def test_invalid_dive_is_rejected_after_one_reask(self) -> None:
        broken = {**demo.DEEP_DIVE, "links": demo.DEEP_DIVE["links"][:2]}  # chain stops before the outcome
        code, _, err = self.dive({"thalamus": broken})
        self.assertEqual(code, 1)
        self.assertIn("re-ask", err)
        self.assertEqual(list((self.root / "evidence").glob("deepdives/*.json")), [])

    def test_hed_and_fulltext_usage(self) -> None:
        self.assertIn("HED 8,1".replace(",", "."), self.cli_ok("hed", "50", "rat"))
        self.assertEqual(self.run_cli("--ledger", str(self.root), "hed", "50", "dragon")[0], 2)
        self.assertEqual(self.run_cli("--ledger", str(self.root), "fulltext", "not an id")[0], 2)
        code, out, _ = self.run_cli("--ledger", str(self.root), "--dry-run", "fulltext", "10.5555/demo.0004")
        self.assertEqual(code, 1)
        self.assertIn("source=-", out)


if __name__ == "__main__":
    unittest.main()

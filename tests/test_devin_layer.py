"""Tests for homunculus.devin_layer. leak-guard: synthetic

Every value is a tiny synthetic fixture built here. No network, no sleeping:
LLM calls go to FakeProvider, the scout script gets a fake fetch. Git runs only
in LedgerSyncTest, on throwaway repos in a temp dir, and skips without git.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from homunculus import agents, devin_layer, guardrails, pineal, scouts
from homunculus.agents import Budget, FakeProvider
from homunculus.metrics import MetricsConfig
from homunculus.pineal import Context, FileNotifier
from homunculus.store import CheckinItem, Ledger, PathNotAllowed, Store

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 7, 31, tzinfo=timezone.utc)  # Friday of 2026-W47
SHA = "b" * 40
SCHEMA = {
    "type": "object",
    "properties": {"summary_de": {"type": "string", "minLength": 1}},
    "required": ["summary_de"],
    "additionalProperties": False,
}


def _roster() -> dict[str, agents.BotSpec]:
    return agents.load_roster(json.loads((ROOT / "config" / "roster.json").read_text(encoding="utf-8")))


def _payload() -> dict:
    """Health Auto Export shape, two days, three metrics, synthetic values."""
    def point(day: int, hour: int, qty: float) -> dict:
        return {"date": f"2026-11-{day:02d} {hour:02d}:00:00 +0000", "qty": qty, "source": "Synthetic Watch"}

    return {"data": {"metrics": [
        {"name": "heart_rate_variability", "units": "ms", "data": [point(19, 2, 51.0), point(19, 4, 55.0)]},
        {"name": "resting_heart_rate", "units": "count/min", "data": [point(19, 8, 52.0)]},
        {"name": "step_count", "units": "count", "data": [point(18, 12, 4000.0), point(19, 12, 6000.0)]},
    ]}}


class RecordingLedger(Ledger):
    """Ledger whose commit records instead of running git."""

    def __init__(self, root, rules) -> None:  # noqa: ANN001
        super().__init__(root, rules)
        self.commits: list[dict] = []

    def commit(self, *, author, message, trailers):  # noqa: ANN001
        self.commits.append({"author": author, "message": message, "trailers": dict(trailers),
                             "paths": self.pending()})
        self._pending.clear()
        return SHA


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "ledger" / ".git").mkdir(parents=True)
        self.roster = _roster()
        self.ledger = RecordingLedger(self.tmp / "ledger", agents.write_rules(self.roster))
        self.store = Store(self.tmp / "health.sqlite")
        self.store.migrate()
        self.budget = Budget(self.ledger, {"weekly_cap_usd": 40.0, "prices": {}}, lambda: NOW)
        self.fake = FakeProvider()

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fresh_store(self, name: str) -> Store:
        store = Store(self.tmp / f"{name}.sqlite")
        store.migrate()
        self.addCleanup(store.close)
        return store


# --------------------------------------------------------------------------
# run_bot


class RunBotTest(Case):
    def setUp(self) -> None:
        super().setUp()
        self.prompts = self.tmp / "prompts"
        self.prompts.mkdir()
        for bot in ("hippocampus", "sartorius"):
            (self.prompts / f"{bot}.md").write_text(f"System {bot} $task\n<!-- user -->\nWoche $week\n",
                                                   encoding="utf-8")

    def run_bot(self, bot: str = "hippocampus", out_rel: str = "experiments/exp-0001/protocol.json", **kw):
        args = {"roster": self.roster, "budget": self.budget, "context": {"task": "design", "week": "2026-W47"},
                "schema": SCHEMA, "out_rel": out_rel, "run_id": "r-test-1", "providers": {"anthropic": self.fake},
                "prompts_dir": self.prompts, "now": lambda: NOW, **kw}
        return devin_layer.run_bot(bot, self.ledger, **args)

    def test_writes_validated_output_with_the_roster_model(self) -> None:
        self.fake = FakeProvider({"hippocampus": {"summary_de": "Plan bleibt."}})
        run = self.run_bot()
        req, model = self.fake.calls[0]
        self.assertEqual(model, self.roster["hippocampus"].model)
        self.assertEqual((req.effort, req.max_tokens), ("high", self.roster["hippocampus"].max_tokens))
        self.assertIn("System hippocampus design", req.system)
        self.assertEqual(req.messages[0]["content"], "Woche 2026-W47")
        record = self.ledger.read_json(run.path)
        self.assertEqual(record["output"], {"summary_de": "Plan bleibt."})
        self.assertEqual((record["bot"], record["model"], record["created_at"]),
                         ("hippocampus", "claude-opus-5-5", "2026-11-20T07:31:00Z"))
        commit = self.ledger.commits[-1]
        self.assertEqual((commit["author"], commit["trailers"]["Run-Id"]), ("hippocampus", "r-test-1"))
        self.assertIn(run.path, commit["paths"])
        self.assertEqual(len(self.ledger.read_jsonl(agents.BUDGET_LOG)), 1)

    def test_forbidden_path_fails_before_any_call(self) -> None:
        with self.assertRaises(PathNotAllowed):
            self.run_bot(out_rel="plans/v0001.json")
        self.assertEqual(self.fake.calls, [])

    def test_schema_violation_writes_nothing(self) -> None:
        self.fake = FakeProvider({"hippocampus": {"summary_de": ""}})
        with self.assertRaises(agents.SchemaViolation):
            self.run_bot()
        self.assertFalse(self.ledger.exists("experiments/exp-0001/protocol.json"))

    def test_banned_vocabulary_is_rejected_for_owner_facing_bots(self) -> None:
        self.fake = FakeProvider({"sartorius": {"summary_de": f"Klingt nach {guardrails.BANNED_TERMS[0]}."}})
        with self.assertRaises(agents.SchemaViolation):
            self.run_bot("sartorius", out_rel="mail/owner/inbox/m-1.json")
        self.assertFalse(self.ledger.exists("mail/owner/inbox/m-1.json"))

    def test_code_and_devin_providers_are_refused(self) -> None:
        with self.assertRaises(agents.ProviderError):
            self.run_bot("pineal", out_rel="ops/x.json")
        self.roster["hippocampus"] = dataclasses.replace(self.roster["hippocampus"], provider="devin")
        with self.assertRaises(agents.ProviderError):
            self.run_bot()
        self.assertEqual(self.fake.calls, [])

    def test_providers_come_from_env(self) -> None:
        with self.assertRaises(agents.ProviderUnavailable):
            self.run_bot(providers=None, env={"DEVIN_API_KEY": "synthetic"})
        built = devin_layer.providers_from_env({"ANTHROPIC_API_KEY": "synthetic", "DEVIN_API_KEY": "synthetic"}, {})
        self.assertEqual(sorted(built), ["anthropic"])
        self.assertIsInstance(built["anthropic"], agents.AnthropicProvider)


# --------------------------------------------------------------------------
# ingest_stdin and store persistence


class IngestTest(Case):
    cfg = MetricsConfig(tz="UTC", source_priority={})

    def ingest(self, **kw) -> dict:
        return devin_layer.ingest_stdin(self.store, self.ledger, self.cfg, now=NOW, **kw)

    def test_stdin_payload_is_stored_and_aggregated(self) -> None:
        out = self.ingest(stream=io.StringIO(json.dumps(_payload())))
        self.assertEqual(out["counts"]["samples"], 5)
        self.assertEqual(out["days"], ["2026-11-18", "2026-11-19", "2026-11-20"])
        for rel in ("data/daily/2026/2026-11-19.json", "data/baselines/2026-W47.json", "data/freshness.json",
                    out["summary"]):
            self.assertTrue(self.ledger.exists(rel), rel)
        self.assertEqual(out["summary"], "data/summaries/2026/20261120T073100Z.json")
        self.assertEqual(out["commit"], SHA)
        self.assertEqual(self.ledger.commits[-1]["author"], "pineal")
        self.assertNotIn("Synthetic Watch", self.ledger.read_text(out["summary"]))

    def test_prompt_text_around_a_wrapper_from_a_file(self) -> None:
        wrapped = {"headers": {"x": "y"}, "payload": json.dumps(_payload())}
        path = self.tmp / "prompt.txt"
        path.write_text("Webhook triggered.\n" + json.dumps(wrapped) + "\nDo the task.", encoding="utf-8")
        self.assertEqual(self.ingest(path=path)["counts"]["samples"], 5)

    def test_extract_payload_shapes(self) -> None:
        bare = _payload()["data"]
        self.assertEqual(devin_layer.extract_payload(json.dumps(bare)), bare)
        nested = {"body": {"payload": _payload()}}
        self.assertEqual(devin_layer.extract_payload("x {not json} " + json.dumps(nested)), _payload())
        for bad in ("", "no json here", json.dumps({"payload": {"other": 1}}), "[1, 2]"):
            with self.assertRaises(ValueError):
                devin_layer.extract_payload(bad)

    def test_oversized_input_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            devin_layer.read_input(io.StringIO("x" * (devin_layer.MAX_INPUT_BYTES + 1)))

    def test_oversized_inbox_file_is_refused(self) -> None:
        """A --file inbox far above one webhook body is not a faithful copy and never reaches the parser."""
        path = self.tmp / "inbox.txt"
        path.write_text("x" * (devin_layer.FILE_MAX_BYTES + 1), encoding="utf-8")
        self.assertLess(devin_layer.FILE_MAX_BYTES, devin_layer.MAX_INPUT_BYTES)
        with self.assertRaises(ValueError):
            devin_layer.read_input(path=path)
        path.write_text("x" * devin_layer.FILE_MAX_BYTES, encoding="utf-8")
        self.assertEqual(len(devin_layer.read_input(path=path)), devin_layer.FILE_MAX_BYTES)

    def test_fresh_store_is_rebuilt_from_the_ledger(self) -> None:
        self.ingest(stream=io.StringIO(json.dumps(_payload())))
        item = CheckinItem("2026-11-20", "wellbeing", 7.0, None, None, "2026-11-20T07:00:00+00:00", "v1", "coach")
        self.store.put_checkins([item])
        self.assertEqual(devin_layer.save_checkins(self.store, self.ledger, ["2026-11-19", "2026-11-20"]),
                         ["checkins/2026/2026-11-20.json"])
        fresh = self.fresh_store("fresh")
        self.assertGreater(devin_layer.restore_store(fresh, self.ledger, NOW), 0)
        self.assertEqual(len(fresh.samples("step_count", "2026-11-18")), 2)
        self.assertEqual(fresh.checkins("2026-11-20", "2026-11-21"), [item])
        expected = self.store.daily("steps", "2026-11-18", "2026-11-20")
        self.assertEqual([r.value for r in expected], [4000.0, 6000.0])
        self.assertEqual(fresh.daily("steps", "2026-11-18", "2026-11-20"), expected)

    def test_restore_skips_old_and_broken_files(self) -> None:
        self.ledger.write_json("data/summaries/2025/20250101T000000Z.json", {"rows": {"samples": [{}]}},
                               author="pineal")
        self.ledger.write_json("data/summaries/2026/20261119T000000Z.json", {"rows": {"samples": [{"x": 1}]}},
                               author="pineal")
        self.ledger.write_json("data/daily/2026/2026-11-19.json", {"date": "2026-11-19", "rows": {
            "rhr": {"value": 52.0, "source": "watch", "n": 1, "quality": "ok"}, "bad": {"quality": "nope"}}},
            author="pineal")
        fresh = self.fresh_store("broken")
        self.assertEqual(devin_layer.restore_store(fresh, self.ledger, NOW), 1)
        self.assertEqual([r.value for r in fresh.daily("rhr", "2026-11-19")], [52.0])


# --------------------------------------------------------------------------
# Automations

_RRULE_RE = re.compile(
    r"^FREQ=(DAILY|MONTHLY;BYMONTHDAY=1|WEEKLY;BYDAY=(MO|TU|WE|TH|FR|SA|SU)(,(MO|TU|WE|TH|FR|SA|SU))*);"
    r"BYHOUR=([01]?\d|2[0-3]);BYMINUTE=[0-5]?\d;BYSECOND=0$"
)
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _by_name(specs: list[dict]) -> dict[str, dict]:
    return {s["name"].removeprefix("homunculus-"): s for s in specs}


class AutomationTest(unittest.TestCase):
    def test_specs_are_valid_for_both_paths(self) -> None:
        jobs = {j.name for j in pineal.default_jobs()}
        for path in devin_layer.PATHS:
            specs = devin_layer.export_automations(path=path)
            names = [s["name"] for s in specs]
            self.assertEqual(len(names), len(set(names)), path)
            for spec in specs:
                self.assertIn(spec["bot"], devin_layer.DEVIN_BOTS)
                self.assertGreaterEqual(spec["max_acu_limit"], 1)
                self.assertIn("github.com", spec["network_allowlist"])
                self.assertIn(spec["name"].removeprefix("homunculus-"), spec["prompt"])
                trigger = spec["trigger"]
                if trigger["type"] == "schedule":
                    self.assertRegex(trigger["rrule"], _RRULE_RE)
                    ZoneInfo(trigger["timezone"])
            for defn in devin_layer.AUTOMATIONS:
                self.assertTrue(set(defn.jobs) <= jobs, defn.name)

    def test_default_schedules(self) -> None:
        specs = _by_name(devin_layer.export_automations(path="a"))
        rules = {name: s["trigger"].get("rrule") for name, s in specs.items()}
        self.assertEqual(rules["sartorius-morning"], "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYSECOND=0")
        self.assertEqual(rules["vagus-evening"], "FREQ=DAILY;BYHOUR=21;BYMINUTE=0;BYSECOND=0")
        self.assertEqual(rules["pineal-daily"], "FREQ=DAILY;BYHOUR=6;BYMINUTE=30;BYSECOND=0")
        self.assertEqual(rules["weekly-review"], "FREQ=WEEKLY;BYDAY=SU;BYHOUR=17;BYMINUTE=0;BYSECOND=0")
        self.assertEqual(rules["saturday"], "FREQ=WEEKLY;BYDAY=SA;BYHOUR=8;BYMINUTE=0;BYSECOND=0")
        self.assertEqual(rules["thalamus"], "FREQ=WEEKLY;BYDAY=TH,FR;BYHOUR=4;BYMINUTE=0;BYSECOND=0")
        self.assertEqual(rules["cerebellum-monthly"], "FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=8;BYMINUTE=0;BYSECOND=0")
        webhook = specs["ingest-health"]["trigger"]
        self.assertEqual((webhook["type"], webhook["secret_header"]), ("webhook", "X-Webhook-Secret"))
        self.assertIn("ingest --llm-copy --file", specs["ingest-health"]["prompt"])  # refused without the opt-in
        self.assertTrue(specs["ingest-health"]["prompt"].split("\n\n")[1].startswith("UNRELIABLE, opt-in only"))

    def test_path_b_runs_on_the_outpost_without_ingest_or_scouts(self) -> None:
        specs = _by_name(devin_layer.export_automations(path="b"))
        self.assertEqual(sorted(specs), ["cerebellum-monthly", "cerebellum-sunday", "sartorius-morning",
                                         "vagus-evening", "vagus-morning", "weekly-review"])
        self.assertEqual({s["runs_on"] for s in specs.values()}, {"outpost"})
        self.assertNotIn("git -C", specs["sartorius-morning"]["prompt"])
        kept = {j.name for j in devin_layer.box_jobs(pineal.default_jobs(), "b")}
        self.assertTrue({"red_flags", "finalize_day", "retina", "thalamus", "insula", "ratify_due"} <= kept)
        self.assertFalse({"sartorius_morning", "vagus_evening", "weekly_review", "monthly_report"} & kept)

    def test_cadence_overrides(self) -> None:
        cadence = {"schema": 1, "tz": "Europe/Berlin", "jobs": {"sartorius_morning": {"at": "06:45"},
                                                                "weekly_review": {"weekday": [5, 6]}},
                   "devin": {"max_acu": {"weekly-review": 4}}}
        specs = _by_name(devin_layer.export_automations(cadence, path="b"))
        morning = specs["sartorius-morning"]["trigger"]
        self.assertEqual((morning["rrule"], morning["timezone"]),
                         ("FREQ=DAILY;BYHOUR=6;BYMINUTE=45;BYSECOND=0", "Europe/Berlin"))
        self.assertIn("BYDAY=SA,SU", specs["weekly-review"]["trigger"]["rrule"])
        self.assertEqual(specs["weekly-review"]["max_acu_limit"], 4)

    def test_bad_config_is_refused(self) -> None:
        bad = ({"tz": "Mars/Base"}, {"jobs": {"sartorius_morning": {"at": "7:00"}}},
               {"jobs": {"weekly_review": {"weekday": 7}}}, {"jobs": []})
        for cadence in bad:
            with self.assertRaises(ValueError, msg=cadence):
                devin_layer.export_automations(cadence)
        with self.assertRaises(ValueError):
            devin_layer.export_automations(path="c")
        with self.assertRaises(ValueError):
            devin_layer.export_automations(max_items=51)

    def test_rrule(self) -> None:
        self.assertEqual(devin_layer.rrule("weekly", "09:05", (0, 2)),
                         "FREQ=WEEKLY;BYDAY=MO,WE;BYHOUR=9;BYMINUTE=5;BYSECOND=0")
        for args in (("hourly", ":05"), ("weekly", "09:00", ()), ("daily", "25:00"), ("minute", "*/15")):
            with self.assertRaises(ValueError, msg=args):
                devin_layer.rrule(*args)

    def test_secret_names_only(self) -> None:
        fake_key = "sk-" + "ant-" + "synthetic" * 4
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": fake_key, "GEMINI_API_KEY": "synthetic-gemini"}):
            specs = devin_layer.export_automations(path="a") + devin_layer.export_automations(path="b")
        text = json.dumps(specs)
        for needle in (fake_key, "synthetic-gemini", "sk-" + "ant-", "ghp" + "_", "AIza"):
            self.assertNotIn(needle, text)
        by_name = _by_name(devin_layer.export_automations(path="a"))
        for name, spec in by_name.items():
            self.assertTrue(all(_SECRET_NAME_RE.match(s) for s in spec["secrets"]), name)
        self.assertEqual(by_name["vagus-morning"]["secrets"], ["ANTHROPIC_API_KEY"])
        self.assertIn("api.anthropic.com", by_name["weekly-review"]["network_allowlist"])
        for code_only in ("ingest-health", "pineal-daily", "cerebellum-monthly"):
            self.assertEqual(by_name[code_only]["secrets"], [], code_only)
            self.assertNotIn("api.anthropic.com", by_name[code_only]["network_allowlist"])
        self.assertTrue(scouts.ALLOWED_HOSTS <= set(by_name["retina-scout"]["network_allowlist"]))

    def test_committed_specs_match_the_generator(self) -> None:
        """devin/automations/<path>/ is regenerated with write_automations; hosts follow scouts.ALLOWED_HOSTS."""
        for path in devin_layer.PATHS:
            folder = ROOT / "devin" / "automations" / path
            committed = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")}
            generated = {f"{s['name']}.json": s for s in devin_layer.export_automations(path=path)}
            self.assertEqual(sorted(committed), sorted(generated), path)
            for name, spec in generated.items():
                strip = lambda s: {k: v for k, v in s.items() if k != "network_allowlist"}  # noqa: E731
                self.assertEqual(strip(committed[name]), strip(spec), name)

    def test_write_automations(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        specs = devin_layer.export_automations(path="b")
        written = devin_layer.write_automations(tmp / "out", specs)
        self.assertEqual(len(written), len(specs))
        self.assertEqual(json.loads(written[0].read_text(encoding="utf-8")), specs[0])


class LedgerSyncTest(unittest.TestCase):
    """Parallel path-a sessions from one head; every row and summary must reach the remote."""

    cfg = MetricsConfig(tz="UTC", source_priority={})

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "pineal", "GIT_AUTHOR_EMAIL": "pineal@example.invalid",
                    "GIT_COMMITTER_NAME": "pineal", "GIT_COMMITTER_EMAIL": "pineal@example.invalid"}
        self.rules = agents.write_rules(_roster())
        self.git("init", "-q", "--bare", "-b", "main", str(self.tmp / "origin.git"))
        seed = self.clone("seed")
        (seed / "ops").mkdir()
        (seed / "ops" / "runs.jsonl").write_text('{"run": 0}\n', encoding="utf-8")
        (seed / ".gitignore").write_text(".private/\n", encoding="utf-8")
        self.git("-C", str(seed), "add", "-A")
        self.git("-C", str(seed), "commit", "-qm", "seed")
        self.git("-C", str(seed), "push", "-q", "origin", "main")

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], check=True, capture_output=True, env=self.env)

    def clone(self, name: str) -> Path:
        self.git("clone", "-q", str(self.tmp / "origin.git"), str(self.tmp / name))
        return self.tmp / name

    def sh(self, ledger: Path, command: str) -> int:
        env = {**self.env, "HOMUNCULUS_LEDGER": str(ledger)}
        return subprocess.run(["sh", "-c", command], env=env, capture_output=True).returncode

    def session(self, name: str) -> tuple[Path, Ledger, Store]:
        root = self.clone(name)
        for command in devin_layer.LEDGER_SYNC_START:
            self.assertEqual(self.sh(root, command), 0, command)
        (root / ".private").mkdir()
        store = Store(root / ".private" / "health.sqlite")
        store.migrate()
        self.addCleanup(store.close)
        return root, Ledger(root, self.rules), store

    def push(self, ledger: Ledger, store: Store) -> bool:
        return devin_layer.push_ledger(store, ledger, self.cfg, lambda: NOW, env=self.env)

    def rows(self, check: Path, rel: str) -> list[dict]:
        return [json.loads(line) for line in (check / rel).read_text(encoding="utf-8").splitlines()]

    def test_racing_appends_both_land(self) -> None:
        sessions = [self.session(n) for n in ("a", "b")]
        for (_, ledger, _), name in zip(sessions, "ab"):
            ledger.append_jsonl("ops/runs.jsonl", {"run": name}, author="pineal")
            ledger.commit(author="pineal", message=name, trailers={})
        self.assertTrue(all(self.push(ledger, store) for _, ledger, store in sessions))
        self.assertEqual(sorted(str(r["run"]) for r in self.rows(self.clone("check"), "ops/runs.jsonl")),
                         ["0", "a", "b"])

    def test_parallel_ingests_rebuild_conflicting_snapshots(self) -> None:
        """Two real ingests for one day plus run_job's writes: heartbeat, freshness, daily and baselines clash."""
        moments = {"a": NOW, "b": NOW + timedelta(minutes=5)}
        sessions = {}
        for name, rhr in (("a", 52.0), ("b", 60.0)):
            root, ledger, store = self.session(name)
            payload = {"data": {"metrics": [{"name": "resting_heart_rate", "units": "count/min", "data": [
                {"date": f"2026-11-19 0{8 if name == 'a' else 9}:00:00 +0000", "qty": rhr, "source": "Watch"}]}]}}
            devin_layer.ingest_stdin(store, ledger, self.cfg, stream=io.StringIO(json.dumps(payload)),
                                     now=moments[name])
            row = {"job": "red_flags", "period": f"2026-11-20T0{name == 'a' and 6 or 7}", "status": "ok",
                   "ended": moments[name].isoformat(), "run_id": f"r-{name}"}
            ledger.append_jsonl(pineal.RUNS_LOG, row, author="pineal")
            ledger.write_json("ops/heartbeats/red_flags.json", {**row, "message": name}, author="pineal")
            ledger.append_jsonl(pineal.ALERTS_LOG, {"rule_id": f"rule_{name}", "date": "2026-11-19"}, author="pineal")
            ledger.commit(author="pineal", message=f"session {name}", trailers={})
            sessions[name] = (ledger, store)
        self.assertTrue(self.push(*sessions["a"]))
        self.assertTrue(self.push(*sessions["b"]))
        check = self.clone("check")
        self.assertEqual(sorted(r.get("run_id") for r in self.rows(check, pineal.RUNS_LOG) if "run_id" in r),
                         ["r-a", "r-b"])
        self.assertEqual(sorted(r["rule_id"] for r in self.rows(check, pineal.ALERTS_LOG)), ["rule_a", "rule_b"])
        self.assertEqual(len(list((check / "data" / "summaries").rglob("*.json"))), 2)
        daily = json.loads((check / "data/daily/2026/2026-11-19.json").read_text(encoding="utf-8"))
        self.assertEqual(daily["values"]["rhr"]["value"], 56.0)  # both sessions' samples
        beat = json.loads((check / "ops/heartbeats/red_flags.json").read_text(encoding="utf-8"))
        self.assertEqual(beat["message"], "b")  # the later run
        self.assertTrue((check / "data/freshness.json").is_file())

    def test_push_reports_failure_when_rebase_cannot_resolve(self) -> None:
        sessions = [self.session(n) for n in ("a", "b")]
        for (_, ledger, _), text in zip(sessions, ("one", "two")):
            ledger.write_text("config/goals.md", text + "\n", author="cerebellum")
            ledger.commit(author="pineal", message=text, trailers={})
        self.assertTrue(self.push(sessions[0][1], sessions[0][2]))
        self.assertFalse(self.push(sessions[1][1], sessions[1][2]))
        status = subprocess.run(["git", "-C", str(sessions[1][0]), "status", "--porcelain"], env=self.env,
                                capture_output=True, text=True).stdout
        self.assertEqual(status, "")  # the rebase was aborted, nothing half-resolved

    def test_main_push_ledger_fails_loudly(self) -> None:
        with mock.patch.object(devin_layer, "_push_ledger_cmd", side_effect=RuntimeError("ledger push failed")):
            with redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
                self.assertEqual(devin_layer.main(["push-ledger"], env={}), 1)
        self.assertIn("ledger push failed", err.getvalue())

    def test_path_a_prompts_sync_first_and_retry_the_push_last(self) -> None:
        for spec in devin_layer.export_automations(path="a"):
            prompt = spec["prompt"]
            self.assertIn(devin_layer.LEDGER_SYNC_START[0], prompt, spec["name"])
            self.assertIn(devin_layer.LEDGER_PUSH, prompt, spec["name"])
            self.assertNotRegex(prompt, r'git -C "\$HOMUNCULUS_LEDGER" push\n', spec["name"])


# --------------------------------------------------------------------------
# Relay route: pointer in the prompt, verified body from the relay

RELAY_URL = "https://relay.invalid"
RELAY_TOKEN = "synthetic-fetch-token"
RELAY_KEY = "synthetic_key_0123456789"


def _pointer(body: bytes, **over) -> dict:
    return {"object_key": RELAY_KEY, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), **over}


class FakeRelay:
    """Stands in for the Worker: serves one object, records every request."""

    def __init__(self, body: bytes, fail_delete: bool = False) -> None:
        self.objects = {RELAY_KEY: body}
        self.fail_delete = fail_delete
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, req) -> bytes:  # noqa: ANN001
        self.calls.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        key = req.full_url.rsplit("/", 1)[-1]
        if req.get_method() == "DELETE":
            if self.fail_delete:
                raise urllib.error.URLError("offline")
            self.objects.pop(key, None)
            return b""
        if key not in self.objects:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO())
        return self.objects[key]


class RelayTest(Case):
    cfg = MetricsConfig(tz="UTC", source_priority={})

    def setUp(self) -> None:
        super().setUp()
        self.body = json.dumps(_payload()).encode("utf-8")

    def ingest(self, text: str, relay: FakeRelay) -> dict:
        return devin_layer.ingest_relay(self.store, self.ledger, self.cfg, text, base_url=RELAY_URL,
                                        token=RELAY_TOKEN, opener=relay, now=NOW)

    def test_body_is_fetched_verified_ingested_and_deleted(self) -> None:
        relay = FakeRelay(self.body)
        prompt = "Webhook triggered.\n" + json.dumps({"body": json.dumps(_pointer(self.body))}) + "\nDo the task."
        out = self.ingest(prompt, relay)
        self.assertEqual(out["counts"]["samples"], 5)
        self.assertTrue(out["deleted"])
        self.assertEqual(relay.objects, {})
        url = f"{RELAY_URL}/objects/{RELAY_KEY}"
        self.assertEqual(relay.calls, [("GET", url, f"Bearer {RELAY_TOKEN}"), ("DELETE", url, f"Bearer {RELAY_TOKEN}")])

    def test_size_or_digest_mismatch_is_refused_and_the_object_kept(self) -> None:
        for over in ({"bytes": len(self.body) - 1}, {"sha256": "0" * 64}):
            relay = FakeRelay(self.body)
            with self.assertRaises(devin_layer.RelayError, msg=over):
                self.ingest(json.dumps(_pointer(self.body, **over)), relay)
            self.assertIn(RELAY_KEY, relay.objects)
            self.assertEqual([c[0] for c in relay.calls], ["GET"])
        self.assertEqual(self.store.samples("step_count", "2026-11-18"), [])

    def test_missing_object_fails_loudly(self) -> None:
        relay = FakeRelay(self.body)
        relay.objects.clear()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.ingest(json.dumps(_pointer(self.body)), relay)
        caught.exception.close()

    def test_extract_pointer_shapes(self) -> None:
        good = _pointer(self.body)
        expected = devin_layer.RelayPointer(RELAY_KEY, good["sha256"], good["bytes"])
        self.assertEqual(devin_layer.extract_pointer(json.dumps(good)), expected)
        self.assertEqual(devin_layer.extract_pointer("x {no} " + json.dumps({"payload": good})), expected)
        bad = ({"object_key": "short"}, {"object_key": "../../etc/passwd_xxxxxxxx"}, {"sha256": "A" * 64},
               {"bytes": True}, {"bytes": 0}, {"bytes": devin_layer.MAX_INPUT_BYTES + 1})
        for over in bad:
            with self.assertRaises(devin_layer.RelayError, msg=over):
                devin_layer.extract_pointer(json.dumps({**good, **over}))
        with self.assertRaises(devin_layer.RelayError):
            devin_layer.extract_pointer(json.dumps(_payload()))

    def test_relay_needs_https_and_a_token(self) -> None:
        pointer = devin_layer.extract_pointer(json.dumps(_pointer(self.body)))
        relay = FakeRelay(self.body)
        for url, token in (("http://relay.invalid", RELAY_TOKEN), (RELAY_URL, "")):
            with self.assertRaises(devin_layer.RelayError):
                devin_layer.fetch_relay_object(pointer, url, token, relay)
        self.assertEqual(relay.calls, [])

    def test_failed_delete_is_reported_not_raised(self) -> None:
        out = self.ingest(json.dumps(_pointer(self.body)), FakeRelay(self.body, fail_delete=True))
        self.assertFalse(out["deleted"])
        self.assertEqual(out["counts"]["samples"], 5)

    def test_module_command_prints_counts_only(self) -> None:
        from homunculus import cli

        root = self.tmp / "cli-ledger"
        cli.init_ledger(root, git=False)
        inbox = self.tmp / "inbox.txt"
        inbox.write_text("Pointer:\n" + json.dumps(_pointer(self.body)), encoding="utf-8")
        env = {"HOME": str(self.tmp), "HOMUNCULUS_LEDGER": str(root), devin_layer.RELAY_URL_NAME: RELAY_URL,
               devin_layer.RELAY_TOKEN_NAME: RELAY_TOKEN}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(devin_layer, "_relay_open", FakeRelay(self.body)), \
                redirect_stdout(out), redirect_stderr(err):
            code = devin_layer.main(["relay-ingest", "--file", str(inbox)], env)
        self.assertEqual(code, 0, err.getvalue())
        printed = json.loads(out.getvalue())
        self.assertEqual((printed["counts"]["samples"], printed["deleted"]), (5, True))
        self.assertNotIn("6000", out.getvalue())
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = devin_layer.main(["relay-ingest", "--file", str(inbox)], {**env, devin_layer.RELAY_TOKEN_NAME: ""})
        self.assertEqual(code, 1)
        self.assertIn("RelayError", err.getvalue())


class RelayWorkerTest(unittest.TestCase):
    """relay/ is an optional Cloudflare Worker: its JS tests run when node is installed; it holds no secrets."""

    def test_worker_tests_pass_under_node(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node not installed")
        result = subprocess.run(["node", "--test", str(ROOT / "relay" / "relay.test.mjs")],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])

    def test_no_secret_values_in_the_relay_folder(self) -> None:
        for path in (ROOT / "relay").iterdir():
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"(?i)(sk-ant-|ghp_|AIza)", path.name)
                self.assertNotRegex(text, r'(?im)^\s*(INGEST_TOKEN|FETCH_TOKEN|DEVIN_WEBHOOK_\w+)\s*=', path.name)
        self.assertIn("wrangler.toml", (ROOT / "relay" / ".gitignore").read_text(encoding="utf-8"))

    def test_relay_automation_carries_only_the_pointer(self) -> None:
        spec = _by_name(devin_layer.export_automations(path="a"))["ingest-relay"]
        self.assertIn("python -m homunculus.devin_layer relay-ingest --file", spec["prompt"])
        self.assertNotIn("homunculus ingest --file", spec["prompt"])
        self.assertEqual(spec["trigger"]["max_payload_bytes"], devin_layer.RELAY_POINTER_MAX_BYTES)
        self.assertEqual(spec["secrets"], sorted([devin_layer.RELAY_TOKEN_NAME, devin_layer.RELAY_URL_NAME]))
        self.assertIn(devin_layer.RELAY_HOST, spec["network_allowlist"])
        self.assertNotIn(devin_layer.RELAY_HOST, _by_name(devin_layer.export_automations(path="a"))
                         ["ingest-health"]["network_allowlist"])


# --------------------------------------------------------------------------
# Scout script


def _load_script(source: str) -> dict:
    namespace: dict = {"__name__": "devin_scout_script"}
    exec(compile(source, "<scout>", "exec"), namespace)  # noqa: S102 - the source is our own rendered template
    return namespace


class ScoutScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.urls: list[str] = []
        self.ids = ["105", "101", "103", "x9"]

    def fetch(self, url: str) -> dict:
        self.urls.append(url)
        return {"esearchresult": {"idlist": list(self.ids)}}

    def run_script(self, ns: dict, state: Path) -> tuple[list[str], dict]:
        out = io.StringIO()
        ns["main"](fetch=self.fetch, state_path=str(state), out=out)
        return json.loads(out.getvalue())["items"], json.loads(state.read_text(encoding="utf-8"))

    def test_cursor_drains_the_backlog_in_steps(self) -> None:
        source = devin_layer.scout_poll_script("retina", {"retina": {"queries": ["sleep AND exercise"]}},
                                               max_items=2)
        ns = _load_script(source)
        state = self.tmp / "state.json"
        self.assertEqual(self.run_script(ns, state), (["pmid:101", "pmid:103"], {"scout": "retina", "cursor": 103}))
        self.assertEqual(self.run_script(ns, state)[0], ["pmid:105"])
        self.assertEqual(self.run_script(ns, state), ([], {"scout": "retina", "cursor": 105}))
        self.assertTrue(all(u.startswith("https://eutils.ncbi.nlm.nih.gov/") for u in self.urls))
        self.assertIn("sleep+AND+exercise", self.urls[0])

    def test_state_file_from_env_and_broken_state(self) -> None:
        state = self.tmp / "env-state.json"
        state.write_text("not json", encoding="utf-8")
        ns = _load_script(devin_layer.scout_poll_script("papilla", {"queries": ["creatine"]}))
        with mock.patch.dict(os.environ, {"STATE_FILE": str(state)}):
            ns["main"](fetch=self.fetch, out=io.StringIO())
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["cursor"], 105)

    def test_queries_cannot_break_out_of_the_literal(self) -> None:
        nasty = ['a"""; import os #', "b\nc", "d'e"]
        ns = _load_script(devin_layer.scout_poll_script("spindle", {"spindle": {"queries": nasty}}))
        self.assertEqual(ns["QUERIES"], nasty)

    def test_stdlib_only(self) -> None:
        source = devin_layer.scout_poll_script("retina")
        imports = set(re.findall(r"^import (\S+)$", source, re.M))
        self.assertEqual(imports, {"json", "os", "sys", "urllib.parse", "urllib.request"})
        self.assertEqual(_load_script(source)["QUERIES"], [])

    def test_bad_arguments(self) -> None:
        for kwargs in ({"max_items": 0}, {"max_items": 51}, {"max_items": True}):
            with self.assertRaises(ValueError):
                devin_layer.scout_poll_script("retina", **kwargs)
        with self.assertRaises(ValueError):
            devin_layer.scout_poll_script("thalamus")
        with self.assertRaises(ValueError):
            devin_layer.scout_poll_script("retina", {"queries": [1]})


# --------------------------------------------------------------------------
# run_task and reply


class RunTaskTest(Case):
    def setUp(self) -> None:
        super().setUp()
        http = scouts.Http(self.tmp / "cache", mailto=None, keys={}, rate_per_s={}, offline=True)
        self.ctx = Context(
            store=self.store, ledger=self.ledger, roster=self.roster, providers={"anthropic": self.fake},
            budget=self.budget, http=http, metrics_cfg=MetricsConfig(tz="UTC", source_priority={}),
            notifier=FileNotifier(self.ledger, now=lambda: NOW), tz="UTC", now=lambda: NOW,
        )

    def test_clear_hold_lifts_urgent_holds_and_commits(self) -> None:
        from homunculus.guardrails import Alert

        pineal.deliver_alerts(self.ctx, [Alert("urgent", "symptom_urgent", "2026-11-18", {"symptoms": ["chest_pain"]})])
        self.assertEqual(len(pineal.coaching_holds(self.ledger, "2026-11-20")), 1)
        self.assertEqual(devin_layer.clear_hold(dataclasses.replace(self.ctx, dry_run=True))["cleared"], 0)
        out = devin_layer.clear_hold(self.ctx)
        self.assertEqual((out["cleared"], out["commit"]), (1, SHA))
        self.assertEqual(pineal.coaching_holds(self.ledger, "2026-11-20"), [])
        self.assertIn("data/coaching_cleared.jsonl", self.ledger.commits[-1]["paths"])

    def test_owner_texts_are_captured_and_runs_are_idempotent(self) -> None:
        out = devin_layer.run_task(self.ctx, "homunculus-vagus-morning", path="b")
        self.assertEqual([r["status"] for r in out["runs"]], ["ok"])
        self.assertEqual(out["owner_texts"], ["x"])  # FakeProvider's minimal reply_de
        self.assertEqual(self.fake.calls[0][1], self.roster["vagus"].model)
        self.assertEqual(len(self.ledger.glob("mail/owner/inbox/*.json")), 1)
        again = devin_layer.run_task(self.ctx, "vagus-morning", path="b")
        self.assertEqual((again["runs"][0]["message"], again["owner_texts"]), ("already done", []))
        self.assertEqual(len(self.fake.calls), 1)

    def test_webhook_reruns_red_flags_in_a_done_hour(self) -> None:
        from datetime import datetime as dt
        from homunculus.store import DailyValue

        def at(hour: int, minute: int) -> Context:
            moment = dt(2026, 11, 20, hour, minute, tzinfo=timezone.utc)
            return dataclasses.replace(self.ctx, now=lambda: moment, notifier=FileNotifier(self.ledger, now=lambda: moment))

        daily = at(6, 30)  # pineal-daily's red_flags marks the hour 2026-11-20T06 done
        first = devin_layer._run_one(daily, devin_layer._job_index()["red_flags"], devin_layer._local_now(daily))
        self.assertEqual(first["status"], "ok")
        self.store.put_daily([DailyValue("2026-11-19", "bp_sys", 190.0, "other", 1, "ok"),
                              DailyValue("2026-11-19", "bp_dia", 125.0, "other", 1, "ok")])
        out = devin_layer.run_task(at(6, 50), "ingest-relay", path="a")  # the summary lands in the same hour
        self.assertEqual(out["runs"][0]["status"], "ok")
        self.assertIn(("bp_high", "2026-11-19"),
                      [(r["rule_id"], r["date"]) for r in self.ledger.read_jsonl(pineal.ALERTS_LOG)])
        again = devin_layer.run_task(at(6, 55), "ingest-relay", path="a")
        self.assertEqual(len([r for r in self.ledger.read_jsonl(pineal.ALERTS_LOG) if r["rule_id"] == "bp_high"]), 1)
        self.assertEqual(again["owner_texts"], [])  # delivered once per (rule_id, date)

    def test_path_a_restores_and_saves_state(self) -> None:
        item = CheckinItem("2026-11-20", "mood", 5.0, None, None, "2026-11-20T07:00:00+00:00", "v1", "coach")
        self.store.put_checkins([item])
        out = devin_layer.run_task(self.ctx, "vagus-morning", path="a")
        self.assertEqual(out["state_commit"], SHA)
        self.assertTrue(self.ledger.exists("checkins/2026/2026-11-20.json"))

    def test_halt_skips_non_essential_jobs(self) -> None:
        self.ledger.write_text("config/HALT", "stop\n", author="pineal")
        out = devin_layer.run_task(self.ctx, "vagus-morning", path="b")
        self.assertEqual(out["runs"][0]["message"], "halted or budget")
        self.assertEqual(self.fake.calls, [])

    def test_unknown_or_foreign_automation(self) -> None:
        with self.assertRaises(ValueError):
            devin_layer.run_task(self.ctx, "nope")
        with self.assertRaises(ValueError):
            devin_layer.run_task(self.ctx, "ingest-health", path="b")

    def test_reply_routes_like_post_message(self) -> None:
        status, body = devin_layer.reply(self.ctx, "vagus", "Wie war mein Schlaf?", path="b")
        self.assertEqual((status, body["reply_de"]), (200, "x"))
        self.assertEqual(devin_layer.reply(self.ctx, "hippocampus", "Hallo", path="b")[0], 400)


if __name__ == "__main__":
    unittest.main()

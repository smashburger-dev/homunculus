"""Tests for homunculus.pineal. Synthetic values only; no git, no network, every LLM call goes to FakeProvider."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from homunculus import agents, checkins, guardrails, pineal, scouts
from homunculus.agents import Budget, FakeProvider
from homunculus.guardrails import Alert
from homunculus.metrics import MetricsConfig
from homunculus.pineal import Context, FileNotifier, Job, JobResult
from homunculus.store import Ledger, Store

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 7, 31, tzinfo=timezone.utc)  # Friday of 2026-W47
DAY = "2026-11-20"
SHA = "a" * 40


def _roster() -> dict[str, agents.BotSpec]:
    return agents.load_roster(json.loads((ROOT / "config" / "roster.json").read_text(encoding="utf-8")))


class RecordingLedger(Ledger):
    """Ledger whose commit records instead of running git."""

    def __init__(self, root, rules) -> None:  # noqa: ANN001
        super().__init__(root, rules)
        self.commits: list[dict] = []

    def commit(self, *, author, message, trailers):  # noqa: ANN001
        self.commits.append({"author": author, "message": message, "trailers": dict(trailers),
                             "paths": self.pending()})
        return SHA


class Clock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def __call__(self) -> datetime:
        return self.value


def _ok(_ctx, _period) -> JobResult:  # noqa: ANN001
    return JobResult("ok", (), "done")


def _job(name: str = "j", cadence: str = "daily", at: str | None = "07:00", weekday: int | None = None,
         bot: str | None = None, essential: bool = False, fn=_ok) -> Job:  # noqa: ANN001
    return Job(name, cadence, at, weekday, bot, essential, fn)


class PinealCase(unittest.TestCase):
    cap = 40.0
    tz = "UTC"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "ledger").mkdir()
        self.roster = _roster()
        self.ledger = RecordingLedger(self.tmp / "ledger", agents.write_rules(self.roster))
        self.store = Store(self.tmp / "health.sqlite")
        self.clock = Clock(NOW)
        self.budget = Budget(self.ledger, {"weekly_cap_usd": self.cap, "prices": {}}, self.clock)
        self.fake = FakeProvider()
        http = scouts.Http(self.tmp / "cache", mailto=None, keys={}, rate_per_s={}, offline=True)
        self.ctx = Context(
            store=self.store, ledger=self.ledger, roster=self.roster, providers={"anthropic": self.fake},
            budget=self.budget, http=http, metrics_cfg=MetricsConfig(tz=self.tz, source_priority={}),
            notifier=FileNotifier(self.ledger, now=self.clock), tz=self.tz, now=self.clock,
        )

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use_fixtures(self, fixtures: dict) -> None:
        self.fake = FakeProvider(fixtures)
        self.ctx.providers = {"anthropic": self.fake}

    def owner_mail(self) -> list[dict]:
        return [self.ledger.read_json(rel) for rel in self.ledger.glob("mail/owner/inbox/*.json")]


# --------------------------------------------------------------------------
# Schedule


class PeriodTest(unittest.TestCase):
    def test_period_keys(self) -> None:
        cases = {
            ("minute", "*/15", None): "2026-11-20T07:30",
            ("minute", None, None): "2026-11-20T07:31",
            ("hourly", ":05", None): "2026-11-20T07",
            ("daily", "06:30", None): "2026-11-20",
            ("weekly", "17:00", 6): "2026-W47",
            ("monthly", "08:00", None): "2026-11",
        }
        for (cadence, at, weekday), key in cases.items():
            self.assertEqual(pineal.period_key(_job(cadence=cadence, at=at, weekday=weekday), NOW), key)

    def test_job_validation(self) -> None:
        bad = [("yearly", "07:00", None), ("daily", "7:00", None), ("weekly", "17:00", None),
               ("minute", "*/7", None), ("hourly", "07:00", None), ("weekly", "17:00", 7)]
        for cadence, at, weekday in bad:
            with self.assertRaises(ValueError):
                _job(cadence=cadence, at=at, weekday=weekday)

    def test_slot_opens_at_time(self) -> None:
        job = _job(at="07:45")
        self.assertEqual(pineal.due_jobs([job], NOW, []), [])
        self.assertEqual(pineal.due_jobs([job], NOW + timedelta(minutes=14), []), [(job, DAY)])

    def test_weekly_catch_up_within_week(self) -> None:
        sunday = _job(cadence="weekly", at="17:00", weekday=6)
        thursday = _job(name="thu", cadence="weekly", at="04:00", weekday=3)
        due = pineal.due_jobs([sunday, thursday], NOW, [])
        self.assertEqual(due, [(thursday, "2026-W47")])

    def test_ok_and_skipped_are_final(self) -> None:
        job = _job()
        for status in ("ok", "skipped"):
            runs = [{"job": "j", "period": DAY, "status": status}]
            self.assertEqual(pineal.due_jobs([job], NOW, runs), [])
        other_day = [{"job": "j", "period": "2026-11-19", "status": "ok"}]
        self.assertEqual(pineal.due_jobs([job], NOW, other_day), [(job, DAY)])

    def test_at_most_one_retry(self) -> None:
        job = _job()
        once = [{"job": "j", "period": DAY, "status": "failed"}]
        self.assertEqual(pineal.due_jobs([job], NOW, once), [(job, DAY)])
        twice = once + [{"job": "j", "period": DAY, "status": "deferred"}]
        self.assertEqual(pineal.due_jobs([job], NOW, twice), [])

    def test_minute_buckets(self) -> None:
        job = _job(cadence="minute", at="*/15")
        runs = [{"job": "j", "period": "2026-11-20T07:30", "status": "skipped"}]
        self.assertEqual(pineal.due_jobs([job], NOW, runs), [])
        later = NOW.replace(minute=45)
        self.assertEqual(pineal.due_jobs([job], later, runs), [(job, "2026-11-20T07:45")])

    def test_default_jobs(self) -> None:
        jobs = {j.name: j for j in pineal.default_jobs()}
        self.assertEqual(len(jobs), len(pineal.default_jobs()))
        self.assertEqual({n for n, j in jobs.items() if j.essential},
                         {"ingest-prune", "red_flags", "finalize_day", "weekly_review"})
        for name in ("freshness", "heartbeat", "budget", "red_flags"):
            self.assertEqual(jobs[name].cadence, "hourly")
        for scout in ("retina", "spindle", "papilla"):
            self.assertEqual((jobs[scout].at, jobs[scout].bot), ("03:00", scout))
        self.assertEqual(jobs["finalize_day"].at, "06:30")
        self.assertEqual({jobs["sartorius_morning"].at, jobs["vagus_morning"].at}, {"07:00"})
        self.assertEqual(jobs["vagus_evening"].at, "21:00")
        self.assertEqual((jobs["insula"].weekday, jobs["motor_cortex"].weekday), (5, 5))
        self.assertEqual((jobs["weekly_review"].weekday, jobs["weekly_review"].at), (6, "17:00"))
        self.assertEqual((jobs["ratify_due"].cadence, jobs["ratify_due"].at), ("minute", "*/15"))
        self.assertEqual(jobs["monthly_report"].cadence, "monthly")


# --------------------------------------------------------------------------
# run_job and tick


class RunJobTest(PinealCase):
    def test_success_records_run_heartbeat_and_commit(self) -> None:
        def fn(ctx, period):  # noqa: ANN001
            resp = agents.LLMResponse("x", None, "claude-haiku-5-5", 10, 2, 0, 0, 0.001, "end_turn")
            ctx.budget.record("retina", resp, "r-20261120-retina")
            return JobResult("ok", ("evidence/x.jsonl",), "3 candidates", {"Kind": "evidence.candidate"})

        out = pineal.run_job(self.ctx, _job(name="retina", bot="retina", fn=fn), DAY)
        rows = self.ledger.read_jsonl(pineal.RUNS_LOG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"job", "period", "status", "started", "ended", "run_id"})
        self.assertEqual((rows[0]["status"], rows[0]["run_id"]), ("ok", "r-20261120-retina"))
        beat = self.ledger.read_json("ops/heartbeats/retina.json")
        self.assertEqual((beat["status"], beat["message"]), ("ok", "3 candidates"))
        commit = self.ledger.commits[-1]
        self.assertEqual(commit["author"], "retina")
        self.assertEqual(commit["trailers"]["Kind"], "evidence.candidate")
        self.assertEqual(commit["trailers"]["Tokens-In"], "10")
        self.assertEqual(commit["trailers"]["Model"], "claude-haiku-5-5")
        self.assertIn(pineal.RUNS_LOG, commit["paths"])
        self.assertEqual((out["commit"], out["written"]), (SHA, ["evidence/x.jsonl"]))
        self.assertEqual(out["dashboard"], "ok")
        self.assertTrue((self.ledger.root / "site" / "index.html").is_file())

    def test_code_job_commits_as_pineal(self) -> None:
        pineal.run_job(self.ctx, _job(), DAY)
        self.assertEqual(self.ledger.commits[-1]["author"], "pineal")

    def test_exception_writes_scrubbed_incident_and_calls_webhook(self) -> None:
        def fn(_ctx, _period):  # noqa: ANN001
            raise ValueError("value 42 out of range")

        calls = []
        env = {pineal.REPAIR_URL_ENV: "https://repair.invalid/hook", pineal.REPAIR_SECRET_ENV: "s" * 32}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(pineal, "repair_webhook", lambda *a, **k: calls.append(a)):
            out = pineal.run_job(self.ctx, _job(fn=fn), DAY)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["webhook"], "sent")
        incident = self.ledger.read_json(f"ops/incidents/{out['incident']}.json")
        self.assertIsNone(incident["message"])
        self.assertEqual(incident["type"], "ValueError")
        self.assertEqual((incident["job"], incident["period"]), ("j", DAY))
        for frame in incident["frames"]:
            self.assertEqual(set(frame), {"file", "line", "function"})
        url, secret, sent = calls[0]
        self.assertEqual((url, secret, sent["id"]), ("https://repair.invalid/hook", "s" * 32, out["incident"]))
        job = _job(fn=fn)
        self.assertEqual(pineal.due_jobs([job], NOW, self.ledger.read_jsonl(pineal.RUNS_LOG)), [(job, DAY)])

    def test_failure_retried_once(self) -> None:
        def fn(_ctx, _period):  # noqa: ANN001
            raise RuntimeError("boom")

        job = _job(fn=fn)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(pineal.REPAIR_URL_ENV, None)
            first = pineal.tick(self.ctx, [job])
            second = pineal.tick(self.ctx, [job])
            third = pineal.tick(self.ctx, [job])
        self.assertEqual([r["status"] for r in first + second], ["failed", "failed"])
        self.assertEqual(first[0]["webhook"], "not_configured")
        self.assertEqual(third, [])

    def test_bad_return_value_fails(self) -> None:
        out = pineal.run_job(self.ctx, _job(fn=lambda _c, _p: {"status": "ok"}), DAY)
        self.assertEqual(out["status"], "failed")
        incident = self.ledger.read_json(f"ops/incidents/{out['incident']}.json")
        self.assertEqual(incident["type"], "TypeError")

    def test_dry_run_does_not_commit(self) -> None:
        self.ctx.dry_run = True
        out = pineal.run_job(self.ctx, _job(), DAY)
        self.assertIsNone(out["commit"])
        self.assertEqual(self.ledger.commits, [])


class TickTest(PinealCase):
    def jobs(self) -> list[Job]:
        return [_job(name="essential", essential=True), _job(name="scout", bot="retina"), _job(name="code")]

    def test_runs_due_jobs_once(self) -> None:
        first = pineal.tick(self.ctx, self.jobs())
        self.assertEqual([r["job"] for r in first], ["essential", "scout", "code"])
        self.assertEqual(pineal.tick(self.ctx, self.jobs()), [])

    def test_halt_keeps_essential_only(self) -> None:
        self.ledger.write_text("config/HALT", "", author="pineal")
        self.assertEqual([r["job"] for r in pineal.tick(self.ctx, self.jobs())], ["essential"])

    def test_overlapping_tick_returns_nothing(self) -> None:
        with open(self.ledger.root / f".{pineal.TICK_LOCK}", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self.assertEqual(pineal.tick(self.ctx, self.jobs()), [])
            fcntl.flock(fh, fcntl.LOCK_UN)
        self.assertEqual(len(pineal.tick(self.ctx, self.jobs())), 3)

    def test_local_time_zone(self) -> None:
        self.ctx.tz = "Europe/Berlin"
        self.clock.value = datetime(2026, 11, 20, 23, 30, tzinfo=timezone.utc)  # 00:30 local
        out = pineal.tick(self.ctx, [_job(at="00:15")])
        self.assertEqual(out[0]["period"], "2026-11-21")


class CoreOnlyTickTest(PinealCase):
    cap = 0.0

    def test_core_only_skips_llm_jobs(self) -> None:
        jobs = [_job(name="essential", essential=True), _job(name="scout", bot="retina"), _job(name="code")]
        self.assertEqual([r["job"] for r in pineal.tick(self.ctx, jobs)], ["essential", "code"])

    def test_budget_job_notifies_once_per_week(self) -> None:
        job = {j.name: j for j in pineal.default_jobs()}["budget"]
        pineal.run_job(self.ctx, job, "2026-11-20T07")
        pineal.run_job(self.ctx, job, "2026-11-20T08")
        mail = self.owner_mail()
        self.assertEqual(len(mail), 1)
        self.assertIn("aufgebraucht", mail[0]["body"])


# --------------------------------------------------------------------------
# Notifier and alerts


class AlertTest(PinealCase):
    def test_file_notifier_writes_valid_envelope(self) -> None:
        self.ctx.notifier.send("owner", "Hallo", "info")
        (msg,) = self.owner_mail()
        self.assertEqual(agents.validate_message(msg), [])
        self.assertEqual((msg["kind"], msg["body"], msg["from"]), ("ask.owner", "Hallo", "pineal"))

    def test_deliver_alerts_dedupes_by_rule_and_date(self) -> None:
        alert = Alert("prompt", "dark_urine", DAY, {})
        self.assertEqual(pineal.deliver_alerts(self.ctx, [alert, alert]), 1)
        self.assertEqual(pineal.deliver_alerts(self.ctx, [alert]), 0)
        self.assertEqual(pineal.deliver_alerts(self.ctx, [Alert("prompt", "dark_urine", "2026-11-21", {})]), 1)
        (row, _) = self.ledger.read_jsonl(pineal.ALERTS_LOG)
        self.assertEqual((row["rule_id"], row["date"], row["level"]), ("dark_urine", DAY, "prompt"))
        bodies = {m["body"] for m in self.owner_mail()}
        self.assertIn(guardrails.render_alert(alert, {}), bodies)
        self.assertEqual({m["kind"] for m in self.owner_mail()}, {"alert"})

    def test_red_flags_job_sends_symptom_template(self) -> None:
        self.store.put_checkins(checkins.validate({"symptoms": ["chest_pain"]}, date=DAY, ts=NOW.isoformat()))
        job = {j.name: j for j in pineal.default_jobs()}["red_flags"]
        out = pineal.run_job(self.ctx, job, "2026-11-20T07")
        self.assertEqual(out["status"], "ok", out)
        rules = [r["rule_id"] for r in self.ledger.read_jsonl(pineal.ALERTS_LOG)]
        self.assertEqual(rules, ["symptom_urgent"])
        self.assertIn("112", self.owner_mail()[0]["body"])


# --------------------------------------------------------------------------
# Owner messages


class OwnerMessageTest(PinealCase):
    def test_rejects_unknown_recipient_and_empty_text(self) -> None:
        self.assertEqual(pineal.handle_owner_message(self.ctx, "amygdala", "Hallo")[0], 400)
        self.assertEqual(pineal.handle_owner_message(self.ctx, "vagus", "  ")[0], 400)
        self.assertEqual(pineal.handle_owner_message(self.ctx, "vagus", "x" * 5000)[0], 400)

    def test_urgent_symptom_short_circuits_without_llm(self) -> None:
        status, body = pineal.handle_owner_message(self.ctx, "sartorius", "Seit dem Lauf habe ich Brustschmerzen.")
        self.assertEqual(status, 200)
        self.assertIn("112", body["reply_de"])
        self.assertEqual(body["checkin_stored"], 1)
        self.assertEqual(self.fake.calls, [])
        stored = self.store.checkins(DAY, "2026-11-21")
        self.assertEqual([c.item for c in stored], ["symptom:chest_pain"])
        self.assertEqual([r["rule_id"] for r in self.ledger.read_jsonl(pineal.ALERTS_LOG)], ["symptom_urgent"])
        self.assertEqual(self.ledger.commits[-1]["author"], "sartorius")

    def test_self_harm_short_circuits_to_helpline(self) -> None:
        status, body = pineal.handle_owner_message(self.ctx, "vagus", "Ich will nicht mehr leben.")
        self.assertEqual(status, 200)
        self.assertIn("0800 111 0 111", body["reply_de"])
        self.assertEqual((body["checkin_stored"], self.fake.calls), (0, []))

    def test_coach_reply_and_checkin(self) -> None:
        self.use_fixtures({"sartorius": {"reply_de": "Heute 40 Minuten locker.", "checkin": {"energy": 5}}})
        status, body = pineal.handle_owner_message(self.ctx, "sartorius", "Energie heute eine 5.")
        self.assertEqual((status, body), (200, {"reply_de": "Heute 40 Minuten locker.", "checkin_stored": 1}))
        (req, model) = self.fake.calls[0]
        self.assertEqual(model, "claude-opus-5-5")
        self.assertIn('<data source="owner_message">', req.messages[0]["content"])
        self.assertEqual([c.item for c in self.store.checkins(DAY, "2026-11-21")], ["energy"])

    def test_banned_vocabulary_gets_fallback(self) -> None:
        self.use_fixtures({"vagus": {"reply_de": "Das klingt nach Insomnie.", "checkin": {}}})
        status, body = pineal.handle_owner_message(self.ctx, "vagus", "Ich schlafe schlecht.")
        self.assertEqual((status, body["reply_de"]), (200, pineal.FALLBACK_DE))

    def test_budget_refusal_returns_503(self) -> None:
        self.ledger.write_text("config/HALT", "", author="pineal")
        status, body = pineal.handle_owner_message(self.ctx, "vagus", "Wie war meine Woche?")
        self.assertEqual((status, body["reply_de"]), (503, pineal.FALLBACK_DE))

    def test_cerebellum_goal_talk_writes_goals_and_update(self) -> None:
        self.use_fixtures({"cerebellum": {
            "reply_de": "Notiert.", "goals_md": "# Ziele\n\n- Halbmarathon im Mai\n",
            "goal_update": "Neues Ziel: Halbmarathon im Mai.",
        }})
        status, body = pineal.handle_owner_message(self.ctx, "cerebellum", "Ja, so passt das.")
        self.assertEqual((status, body["reply_de"]), (200, "Notiert."))
        self.assertIn("Halbmarathon", self.ledger.read_text("config/goals.md"))
        for bot in pineal.GOAL_RECIPIENTS:
            (rel,) = self.ledger.glob(f"mail/{bot}/inbox/*.json")
            msg = self.ledger.read_json(rel)
            self.assertEqual((msg["kind"], agents.validate_message(msg)), ("goal.update", []))
        self.assertEqual(self.ledger.commits[-1]["author"], "cerebellum")


# --------------------------------------------------------------------------
# Job functions


class JobFunctionTest(PinealCase):
    def job(self, name: str) -> Job:
        return {j.name: j for j in pineal.default_jobs()}[name]

    def test_freshness_job(self) -> None:
        out = pineal.run_job(self.ctx, self.job("freshness"), "2026-11-20T07")
        self.assertEqual(out["status"], "ok")
        fresh = self.ledger.read_json(pineal.FRESHNESS_PATH)
        self.assertTrue(all(v["stale"] for v in fresh.values()))

    def test_finalize_day_writes_yesterday(self) -> None:
        out = pineal.run_job(self.ctx, self.job("finalize_day"), DAY)
        self.assertEqual(out["status"], "ok", out)
        for rel in ("data/daily/2026/2026-11-19.json", "data/baselines/2026-W47.json",
                    "data/conflicts/2026-W47.json"):
            self.assertTrue(self.ledger.exists(rel), rel)
        self.assertEqual(self.ledger.read_json("data/daily/2026/2026-11-19.json")["date"], "2026-11-19")

    def test_ratify_due_without_pending_is_skipped(self) -> None:
        out = pineal.run_job(self.ctx, self.job("ratify_due"), "2026-11-20T07:30")
        self.assertEqual(out["status"], "skipped")

    def test_vagus_morning_sends_reply(self) -> None:
        self.use_fixtures({"vagus": {"reply_de": "Guten Morgen. Drei ruhige Nächte in Folge.", "checkin": {}}})
        out = pineal.run_job(self.ctx, self.job("vagus_morning"), DAY)
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual([m["body"] for m in self.owner_mail()], ["Guten Morgen. Drei ruhige Nächte in Folge."])
        self.assertIn("Aufgabe: morning", self.fake.calls[0][0].messages[0]["content"])

    def test_sartorius_morning_without_plan_is_skipped(self) -> None:
        out = pineal.run_job(self.ctx, self.job("sartorius_morning"), DAY)
        self.assertEqual((out["status"], self.fake.calls), ("skipped", []))

    def test_scout_without_keywords_is_skipped(self) -> None:
        out = pineal.run_job(self.ctx, self.job("retina"), DAY)
        self.assertEqual(out["status"], "skipped")

    def test_llm_job_defers_on_budget(self) -> None:
        self.ledger.write_text("config/HALT", "", author="pineal")
        out = pineal.run_job(self.ctx, self.job("vagus_evening"), DAY)
        self.assertEqual((out["status"], out["message"]), ("deferred", "budget"))

    def test_weekly_review_without_plan_keeps(self) -> None:
        out = pineal.run_job(self.ctx, self.job("weekly_review"), "2026-W47")
        self.assertEqual((out["status"], out["message"]), ("ok", "ruling keep, aborted no_plan"), out)
        self.assertTrue(self.ledger.exists("reviews/2026-W47/packet.json"))
        self.assertEqual(self.fake.calls, [])

    def test_saturday_and_thursday_jobs_without_inputs(self) -> None:
        insula = pineal.run_job(self.ctx, self.job("insula"), "2026-W47")
        self.assertEqual(insula["status"], "ok", insula)
        self.assertTrue(self.ledger.exists("data/conflicts/2026-W47.json"))
        motor = pineal.run_job(self.ctx, self.job("motor_cortex"), "2026-W47")
        thalamus = pineal.run_job(self.ctx, self.job("thalamus"), DAY)  # Friday, no batch running
        self.assertEqual((motor["status"], thalamus["status"]), ("skipped", "skipped"))
        self.assertEqual({req.bot for req, _ in self.fake.calls}, {"insula"})  # DATA_GAP needs an explanation

    def test_ingest_prune_removes_old_raw_payloads(self) -> None:
        raw = self.tmp / "raw"
        raw.mkdir()
        (raw / "20261001T000000000000Z.json").write_text("{}")
        (raw / "20261119T000000000000Z.json").write_text("{}")
        with mock.patch.dict(os.environ, {pineal.RAW_ENV: str(raw)}):
            out = pineal.run_job(self.ctx, self.job("ingest-prune"), DAY)
        self.assertEqual(out["message"], "1 raw payloads pruned")
        self.assertEqual([p.name for p in raw.iterdir()], ["20261119T000000000000Z.json"])

    def test_monthly_report_covers_previous_month(self) -> None:
        resp = agents.LLMResponse("x", None, "claude-haiku-5-5", 1, 1, 0, 0, 0.25, "end_turn")
        self.clock.value = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)
        self.budget.record("retina", resp, "r-1")
        self.clock.value = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
        out = pineal.run_job(self.ctx, self.job("monthly_report"), "2026-11")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(self.owner_mail()[0]["body"],
                         "Monatsbericht 2026-10: LLM-Kosten 0,25 USD. Experimente mit Ergebnis: 0.")


# --------------------------------------------------------------------------
# Incidents and webhook


def _nested_failure() -> None:
    def inner() -> None:
        raise RuntimeError("plan file missing")

    inner()


class IncidentTest(unittest.TestCase):
    def test_scrub_keeps_frames_only(self) -> None:
        try:
            _nested_failure()
        except RuntimeError as exc:
            incident = pineal.scrub_incident(exc, "finalize_day", "b" * 40)
        self.assertRegex(incident["id"], r"^inc-[0-9a-f]{12}$")
        self.assertEqual(incident["message"], "plan file missing")
        self.assertEqual([f["function"] for f in incident["frames"]][-2:], ["_nested_failure", "inner"])
        for frame in incident["frames"]:
            self.assertFalse(frame["file"].startswith("/"), frame)
        self.assertEqual(set(incident), {"id", "job", "type", "message", "frames", "commit_sha"})

    def test_scrub_drops_messages_with_digits_or_paths(self) -> None:
        for text in ("weight 81.2 kg", "cannot open /private/ledger", "mail owner@example.org"):
            self.assertIsNone(pineal.scrub_incident(ValueError(text), "j", None)["message"])

    def test_webhook_requires_https_and_secret(self) -> None:
        calls = []
        with self.assertRaises(ValueError):
            pineal.repair_webhook("http://repair.invalid/hook", "s" * 32, {"id": "inc-1"}, opener=calls.append)
        with self.assertRaises(ValueError):
            pineal.repair_webhook("https://repair.invalid/hook", "", {"id": "inc-1"}, opener=calls.append)
        self.assertEqual(calls, [])

    def test_webhook_posts_json_with_secret_header(self) -> None:
        calls = []
        incident = {"id": "inc-abc", "job": "j", "frames": []}
        pineal.repair_webhook("https://repair.invalid/hook", "s" * 32, incident, opener=calls.append)
        (req,) = calls
        self.assertEqual((req.get_method(), req.full_url), ("POST", "https://repair.invalid/hook"))
        self.assertEqual(req.get_header("X-webhook-secret"), "s" * 32)
        self.assertEqual(json.loads(req.data), incident)


if __name__ == "__main__":
    unittest.main()

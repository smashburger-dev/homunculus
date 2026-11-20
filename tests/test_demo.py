"""End-to-end test of the demo: synthetic data, temp ledger, FakeProvider, no network, no API keys."""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from homunculus import cli, demo, plans
from homunculus.store import Ledger

END = date(2026, 11, 22)  # a Sunday
WEEK = "2026-W47"


def _no_network(*_args, **_kwargs):
    raise AssertionError("the demo must not open network connections")


class DemoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp)}
        for patch in (mock.patch.dict(os.environ, env, clear=True),
                      mock.patch.object(socket.socket, "connect", _no_network),
                      mock.patch("urllib.request.urlopen", _no_network)):
            patch.start()
            self.addCleanup(patch.stop)


class DemoEndToEndTest(DemoCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp())
        with mock.patch.object(socket.socket, "connect", _no_network):
            cls.summary = demo.run_demo(cls.base / "ledger", days=56, seed=7, end=END)
        cls.root = Path(cls.summary["ledger"])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.base, True)

    def ledger(self) -> Ledger:
        return Ledger(self.root, {})

    def test_data_was_ingested_and_finalized(self) -> None:
        s = self.summary
        self.assertEqual(s["week"], WEEK)
        self.assertEqual(s["days"], ["2026-09-28", "2026-11-22"])
        self.assertGreater(s["counts"]["samples"], 300)
        self.assertGreater(s["counts"]["workouts"], 20)
        self.assertGreater(s["counts"]["checkins"], 300)
        self.assertEqual(len(list((self.root / "data/daily/2026").glob("*.json"))), 56)
        self.assertTrue((self.root / f"data/baselines/{WEEK}.json").is_file())

    def test_conflicts_follow_the_story(self) -> None:
        conflicts = self.summary["conflicts"]
        for rule in ("R1_sustained", "R3_session_residual", "DATA_GAP"):
            self.assertGreaterEqual(conflicts.get(rule, 0), 1, rule)
        insula = self.ledger().read_json(f"reviews/{WEEK}/00-insula-reconciliation.json")
        self.assertEqual(insula["errors"], [])
        self.assertEqual(len(insula["items"]), sum(conflicts.values()))
        self.assertLessEqual(sum(1 for i in insula["items"] if i["question_for_owner_de"]), 3)

    def test_papers_are_scored_by_code(self) -> None:
        gates = {Path(k).stem: v for k, v in self.summary["papers"].items()}
        self.assertEqual(gates["10-5555-demo-0001"], "actionable")
        self.assertEqual(gates["10-5555-demo-0002"], "n_of_1_only")
        self.assertIn(gates["10-5555-demo-0003"], ("watchlist", "discard"))
        audits = list((self.root / "evidence/papers").glob("*.audit.json"))
        self.assertEqual(len(audits), 1)
        self.assertEqual(json.loads(audits[0].read_text(encoding="utf-8"))["disagreements"], [])

    def test_review_ran_the_full_bounded_loop(self) -> None:
        r = self.summary["review"]
        self.assertIsNone(r["aborted"])
        self.assertEqual(r["ruling"], "ratify")
        self.assertEqual(r["tier"], "auto")
        names = [Path(m).name for m in r["messages"] if m.startswith("reviews/")]
        self.assertEqual(len(names), 6)
        r1 = self.ledger().read_json(f"reviews/{WEEK}/02-amygdala-r1.json")
        self.assertEqual([i["cid"] for i in r1["dropped"]], ["c2"])  # unresolvable ref dropped by code
        self.assertEqual(self.summary["guardrails"]["banned_terms"], [])

    def test_plan_was_ratified(self) -> None:
        plan = plans.current(self.ledger())
        self.assertEqual(plan["version"], 2)
        self.assertEqual(plan["parent"], 1)
        self.assertEqual(plan["sessions"][3]["duration_min"], 60)
        self.assertLess(plans.weekly_load(plan), plans.weekly_load(plans.get(self.ledger(), 1)))

    def test_dashboard_was_built(self) -> None:
        index = Path(self.summary["dashboard"])
        self.assertEqual(index, self.root / "site/index.html")
        html = index.read_text(encoding="utf-8")
        self.assertIn("Not intended to diagnose", html)
        self.assertNotIn("<script src", html)
        for page in ("plan.html", "week.html", "challenges.html", "budget.html", "checkin.html"):
            self.assertTrue((self.root / "site" / page).is_file(), page)

    def test_dashboard_shows_conflicts_and_experiment(self) -> None:
        site = self.root / "site"
        self.assertIn("Offene Konflikte", (site / "challenges.html").read_text(encoding="utf-8"))
        self.assertIn("Datenlücke", (site / "challenges.html").read_text(encoding="utf-8"))
        self.assertEqual(self.summary["experiment"]["problems"], [])
        self.assertTrue((self.root / "experiments/exp-abendlicht/protocol.json").is_file())
        self.assertIn("exp-abendlicht", (site / "experiments.html").read_text(encoding="utf-8"))

    def test_no_device_names_or_gps_in_ledger(self) -> None:
        tracked = [p for p in self.root.rglob("*.json") if ".private" not in p.parts and "site" not in p.parts]
        for path in tracked:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(demo.DEVICE, text, str(path))
            self.assertNotIn('"latitude"', text, str(path))

    def test_git_history_carries_bot_identities(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        import subprocess

        log = subprocess.run(["git", "-C", str(self.root), "log", "--format=%an"], capture_output=True, text=True,
                             check=True).stdout.split()
        self.assertEqual(self.summary["commits"], len(log))
        self.assertTrue({"Pineal", "Insula", "Thalamus", "Cerebellum"} <= set(log))

    def test_summary_lines(self) -> None:
        lines = list(demo.summary_lines(self.summary))
        self.assertIn(self.summary["dashboard"], lines[1])
        self.assertTrue(any("0 network requests" in line for line in lines))


class DemoDeterminismTest(DemoCase):
    def test_same_seed_same_numbers(self) -> None:
        a = demo.run_demo(self.tmp / "a", days=42, seed=11, end=END, git=False)
        b = demo.run_demo(self.tmp / "b", days=42, seed=11, end=END, git=False)
        for rel in (f"data/conflicts/{WEEK}.json", f"data/baselines/{WEEK}.json", "plans/CURRENT"):
            self.assertEqual((Path(a["ledger"]) / rel).read_bytes(), (Path(b["ledger"]) / rel).read_bytes(), rel)
        c = demo.run_demo(self.tmp / "c", days=42, seed=12, end=END, git=False)
        self.assertNotEqual((Path(a["ledger"]) / f"data/baselines/{WEEK}.json").read_bytes(),
                            (Path(c["ledger"]) / f"data/baselines/{WEEK}.json").read_bytes())


class DemoCliTest(DemoCase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_demo_ignores_configured_ledger(self) -> None:
        real = self.tmp / "real-ledger"
        real.mkdir()
        with mock.patch.dict(os.environ, {"HOMUNCULUS_LEDGER": str(real), "HEALTH_DB": str(real / "x.sqlite")}):
            code, out, err = self.run_cli("demo", "--out", str(self.tmp / "demo"), "--days", "35", "--seed", "3")
        self.assertEqual(code, 0, msg=err)
        self.assertIn("Dashboard:", out)
        self.assertIn(str(self.tmp / "demo" / "site" / "index.html"), out)
        self.assertEqual(list(real.iterdir()), [])

    def test_cli_demo_refuses_unsafe_output(self) -> None:
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "keep.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.run_cli("demo", "--out", str(busy))[0], 2)
        self.assertEqual(self.run_cli("demo", "--out", str(cli.ENGINE_ROOT / "demo-out"))[0], 2)
        self.assertFalse((cli.ENGINE_ROOT / "demo-out").exists())
        self.assertEqual(self.run_cli("demo", "--days", "7")[0], 2)


class PhaseTest(unittest.TestCase):
    def test_story_phases(self) -> None:
        phases = [demo.phase(i, 56) for i in range(56)]
        self.assertEqual(phases.count("cold"), 5)
        self.assertEqual(phases.count("travel"), 7)
        self.assertEqual(phases[-7:], ["base", "gap", "strain", "strain", "strain", "base", "base"])

    def test_calendar_ends_on_sunday(self) -> None:
        cal = demo.calendar(56, date(2026, 11, 20))  # a Friday
        self.assertEqual(cal.days[-1], "2026-11-15")
        self.assertEqual(date.fromisoformat(cal.days[-1]).weekday(), 6)
        self.assertEqual(cal.week, "2026-W46")
        self.assertEqual(len(cal.days), 56)


if __name__ == "__main__":
    unittest.main()

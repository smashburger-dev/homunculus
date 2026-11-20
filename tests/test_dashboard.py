"""Tests for homunculus.dashboard. All data are small synthetic values built here."""

from __future__ import annotations

import random
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from homunculus import dashboard
from homunculus.guardrails import Alert
from homunculus.metrics import MetricsConfig, Readiness
from homunculus.store import DailyValue, Ledger, Store

RNG_SEED = 1234
NOW = datetime(2026, 3, 4, 7, 30, tzinfo=timezone.utc)  # Wednesday, weekday 2
TODAY = "2026-03-04"
EVIL = '<script>alert("x")</script>'


def _plan(version: int = 1, parent: int | None = None, duration: int = 40) -> dict:
    return {
        "version": version, "parent": parent, "effective_from": "2026-03-01T00:00:00Z",
        "valid_until": "2026-03-08T00:00:00Z", "author": "hippocampus", "ratified_by": "cerebellum",
        "data_snapshot_sha": None, "rationale_refs": [],
        "sessions": [
            {"id": "s1", "weekday": 0, "kind": "kraft", "duration_min": 60, "target_rpe": 7,
             "exercises": [{"exercise_id": "ex-a", "sets": 3, "reps": 5, "load_kg": 40.0, "rir": 2,
                            "novel": False, "probation": False}]},
            {"id": "s2", "weekday": 2, "kind": "ausdauer", "duration_min": duration, "target_rpe": 4,
             "exercises": []},
        ],
        "nutrition": {"kcal_target": None, "protein_g_per_kg": 1.6, "notes_de": "Testnotiz"},
        "supplements": [],
        "experiments": [],
        "modulation": {"max_daily_load_cut_pct": 25, "allow_swap": True, "readiness_low_action": "shorten"},
    }


def _msg(mid: str, kind: str, items: list | None = None, *, created: str = "2026-03-01T19:00:00Z",
         body: str = "Text", thread: str = "review/2026-W09", rnd: int = 1) -> dict:
    return {"schema": 1, "id": mid, "from": "amygdala", "to": ["hippocampus"], "cc": ["cerebellum"],
            "kind": kind, "thread": thread, "round": rnd, "max_rounds": 2, "created_at": created,
            "refs": [], "data_sha": "0" * 64, "run_id": "r-1", "model": "fake", "body": body,
            "items": items or []}


def _assert_safe_page(test: unittest.TestCase, page: str) -> None:
    test.assertTrue(page.startswith("<!DOCTYPE html>"))
    test.assertIn(dashboard.INTENDED_PURPOSE_DE, page)
    test.assertIn(dashboard.INTENDED_PURPOSE_EN, page)
    test.assertIn("Content-Security-Policy", page)
    test.assertNotIn("<script", page)
    test.assertNotRegex(page, r"\bsrc=")
    urls = set(re.findall(r"https?://[^\s\"'<>]+", page))
    test.assertLessEqual(urls, {"http://www.w3.org/2000/svg"})


class SparklineTests(unittest.TestCase):
    def test_gaps_split_runs(self) -> None:
        svg = dashboard.sparkline_svg([1.0, 2.0, None, 3.0, 4.0])
        self.assertEqual(svg.count("<polyline"), 2)
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))

    def test_isolated_point_and_last_marker(self) -> None:
        svg = dashboard.sparkline_svg([None, 5.0, None])
        self.assertEqual(svg.count("<polyline"), 0)
        self.assertEqual(svg.count("<circle"), 2)  # the point plus the last-value marker

    def test_empty_and_all_missing(self) -> None:
        for values in ([], [None, None], [float("nan"), float("inf")]):
            svg = dashboard.sparkline_svg(values)
            self.assertNotIn("<polyline", svg)
            self.assertNotIn("<circle", svg)

    def test_constant_series_is_centred(self) -> None:
        svg = dashboard.sparkline_svg([3.0, 3.0, 3.0], width=60, height=20)
        self.assertIn('points="2,10 30,10 58,10"', svg)
        self.assertIn('viewBox="0 0 60 20"', svg)

    def test_points_stay_inside_box(self) -> None:
        rng = random.Random(RNG_SEED)
        values = [rng.uniform(-50, 50) if rng.random() > 0.2 else None for _ in range(40)]
        svg = dashboard.sparkline_svg(values, width=100, height=30)
        coords = [float(c) for c in re.findall(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", svg) for c in c]
        self.assertTrue(coords)
        self.assertTrue(all(0 <= c <= 100 for c in coords))

    def test_rejects_bad_size(self) -> None:
        with self.assertRaises(ValueError):
            dashboard.sparkline_svg([1.0], width=0)


class TodayTests(unittest.TestCase):
    def _render(self, **over) -> str:
        args = {
            "daily": {"rhr": [50.0, None, 52.0], "tst_h": 7.4},
            "readiness": Readiness(0.5, "normal", {"hrv": 0.3, "rhr": 0.4, "tst": -0.2}, ["wellness"]),
            "day_plan": {"session": {"id": "s2", "kind": "ausdauer", "duration_min": 40, "target_rpe": 4,
                                     "exercises": []},
                         "action": "as_planned", "reason_codes": ["readiness_normal"]},
            "freshness": {"workouts": {"latest": "2026-03-01T10:00:00+00:00", "age_h": 80.0, "stale": True},
                          "heart_rate_variability": {"latest": "2026-03-04T05:00:00+00:00", "age_h": 2.5,
                                                     "stale": False}},
            "alerts": [],
        }
        args.update(over)
        return dashboard.render_today(**args)

    def test_sections_and_values(self) -> None:
        page = self._render()
        _assert_safe_page(self, page)
        self.assertIn("+0,50", page)
        self.assertIn("normal", page)
        self.assertIn("Fehlt: Wohlbefinden", page)
        self.assertIn("52 bpm", page)
        self.assertIn("7,4 h", page)  # a scalar is accepted as a one-value series
        self.assertIn("wie geplant", page)
        self.assertIn("keine Daten seit 2026-03-01T10:00:00+00:00", page)
        self.assertIn('aria-current=page', page)

    def test_missing_readiness_and_plan(self) -> None:
        page = self._render(readiness=Readiness(None, None, {}, ["hrv", "rhr", "tst"]), day_plan={})
        self.assertIn("weniger als 3 Eingänge", page)
        self.assertIn("Kein ratifizierter Plan", page)

    def test_alerts_use_fixed_templates_and_escape(self) -> None:
        alerts = [Alert("prompt", EVIL, TODAY, {}),
                  Alert("urgent", "symptom_urgent", TODAY, {"symptoms": ["chest_pain"]})]
        page = self._render(alerts=alerts)
        _assert_safe_page(self, page)
        self.assertIn("112", page)
        self.assertLess(page.index("Dringend"), page.index("Heute klären"))
        self.assertIn("&lt;script&gt;", page)

    def test_daily_keys_escaped(self) -> None:
        page = self._render(daily={EVIL: [1.0, 2.0]})
        self.assertNotIn("<script", page)


class PlanTests(unittest.TestCase):
    def test_diff_to_parent(self) -> None:
        page = dashboard.render_plan(_plan(2, 1, duration=30), _plan(1, None))
        _assert_safe_page(self, page)
        self.assertIn("gegenüber v0001", page)
        self.assertIn("/sessions/1/duration_min", page)
        self.assertNotIn("<code>/version</code>", page)
        self.assertIn("Mittwoch", page)

    def test_no_parent_and_no_plan(self) -> None:
        self.assertIn("Kein Vorgänger", dashboard.render_plan(_plan(), None))
        self.assertIn("Noch kein ratifizierter Plan", dashboard.render_plan({}, None))

    def test_identical_content(self) -> None:
        page = dashboard.render_plan(_plan(2, 1), _plan(1, None))
        self.assertIn("Keine inhaltlichen Änderungen", page)


class WeekAndChallengeTests(unittest.TestCase):
    def test_week_orders_and_escapes(self) -> None:
        msgs = [_msg("m-2", "rebuttal", created="2026-03-01T20:00:00Z", body="zweite"),
                _msg("m-1", "review.draft", created="2026-03-01T18:00:00Z", body=EVIL),
                {"not": "a message"}]
        page = dashboard.render_week(msgs)
        _assert_safe_page(self, page)
        self.assertIn("&lt;script&gt;", page)
        self.assertLess(page.index("Entwurf"), page.index("Antwort"))
        self.assertIn("2 Nachrichten", page)

    def test_empty_week(self) -> None:
        self.assertIn("Noch kein Review-Thread", dashboard.render_week([]))

    def test_open_challenges(self) -> None:
        items = [{"cid": "c1", "severity": "minor", "claim": "klein", "ref": "a.json"},
                 {"cid": "c2", "severity": "blocker", "claim": EVIL, "ref": "b.json"},
                 {"cid": "c3", "severity": "major", "claim": "beantwortet", "ref": "c.json"}]
        rebuttal = _msg("m-2", "rebuttal", [{"cid": "c3", "action": "accept", "ref": None}])
        other = _msg("m-3", "rebuttal", [{"cid": "c1", "action": "rebut"}], thread="review/2026-W08")
        page = dashboard.render_challenges([_msg("m-1", "challenge", items), rebuttal, other])
        _assert_safe_page(self, page)
        self.assertIn("2 offen, davon 1 Blocker", page)
        self.assertNotIn("beantwortet", page)
        self.assertLess(page.index("Blocker</td>"), page.index("klein"))

    def test_no_open_challenges(self) -> None:
        self.assertIn("Keine offenen Einwände", dashboard.render_challenges([]))

    def test_conflicts_section(self) -> None:
        conflicts = [{"id": "abc", "rule_id": "R1_sustained", "window": ["2026-02-16", "2026-02-18"],
                      "interpretation_de": EVIL, "question_for_owner_de": None}]
        page = dashboard.render_challenges([], conflicts)
        _assert_safe_page(self, page)
        self.assertIn("1 Konflikte", page)
        self.assertIn("2026-02-16 bis 2026-02-18", page)
        self.assertIn("Keine offenen Konflikte", dashboard.render_challenges([]))

    def test_week_follows_turn_order_with_equal_timestamps(self) -> None:
        msgs = [_msg("m-0", "rebuttal", []), _msg("m-1", "challenge", [])]
        for m in msgs:
            m["round"] = 2
        page = dashboard.render_week(msgs)
        self.assertLess(page.index("Einwand"), page.index("Antwort"))


class ExperimentTests(unittest.TestCase):
    def test_running_and_finished(self) -> None:
        proto = {"id": "exp-0001", "intervention": "A", "comparator": "B", "outcome_key": "tst_h",
                 "source": "watch", "design": "paired_blocks", "pairs": 6, "block_days": 7,
                 "washout_days": 2, "start_date": "2026-01-01"}
        result = {"decision": "adopt", "d_bar": 0.25, "se": 0.08, "p_value": 0.031, "p_benefit": 0.86,
                  "p_harm": 0.01, "pair_diffs": [0.1, 0.3, 0.2, 0.4, 0.2, 0.3]}
        page = dashboard.render_experiments([
            {"id": "exp-0002", "protocol": dict(proto, id="exp-0002"), "result": None},
            {"id": "exp-0001", "protocol": proto, "result": result},
        ])
        _assert_safe_page(self, page)
        self.assertIn("läuft", page)
        self.assertIn("übernehmen", page)
        self.assertIn("0,031", page)
        self.assertLess(page.index("exp-0001"), page.index("exp-0002"))

    def test_none(self) -> None:
        self.assertIn("Keine Experimente", dashboard.render_experiments([]))


class BudgetTests(unittest.TestCase):
    def test_sum_and_level(self) -> None:
        rows = [{"bot": "vagus", "usd": 20.0, "date": "2026-03-02", "tokens_in": 1000, "tokens_out": 10},
                {"bot": "sartorius", "usd": 14.0, "date": "2026-03-03", "tokens_in": 2000, "tokens_out": 20},
                {"bot": EVIL, "usd": "bad", "date": "2026-03-03"}]
        page = dashboard.render_budget(rows, 40.0)
        _assert_safe_page(self, page)
        self.assertIn("34,00 $", page)
        self.assertIn("85 %", page)
        self.assertIn("reduziert", page)
        self.assertLess(page.index("vagus"), page.index("sartorius"))

    def test_levels(self) -> None:
        self.assertIn("nur Kernjobs", dashboard.render_budget([{"usd": 1.0}], 0.0))
        self.assertIn("Stufe: <span class=\"badge\">normal", dashboard.render_budget([], 40.0))
        self.assertIn("Keine Aufrufe", dashboard.render_budget([], 40.0))


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp / "health.db")
        self.store.migrate()
        (self.tmp / "ledger").mkdir()
        self.ledger = Ledger(self.tmp / "ledger", {})
        self.cfg = MetricsConfig(tz="Europe/Berlin", source_priority={})

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp)

    def _seed_ledger(self) -> None:
        w = self.ledger
        w.write_json("plans/v0001.json", _plan(1, None), author="pineal")
        w.write_json("plans/v0002.json", _plan(2, 1, duration=30), author="pineal")
        w.write_text("plans/CURRENT", "v0002\n", author="pineal")
        w.append_jsonl("data/alerts.jsonl", {"level": "prompt", "rule_id": "dark_urine", "date": TODAY,
                                             "data": {}}, author="pineal")
        w.append_jsonl("data/alerts.jsonl", {"level": "prompt", "rule_id": "dark_urine", "date": "2026-02-01",
                                             "data": {}}, author="pineal")
        w.append_jsonl("data/alerts.jsonl", {"broken": True}, author="pineal")
        challenge = _msg("m-c1", "challenge", [{"cid": "c1", "severity": "blocker", "claim": "offen",
                                                "ref": "x.json"}])
        w.write_json("mail/hippocampus/inbox/m-c1.json", challenge, author="pineal")
        w.write_json("mail/cerebellum/inbox/m-c1.json", challenge, author="pineal")
        w.write_json("mail/hippocampus/inbox/m-old.json",
                     _msg("m-old", "challenge", [{"cid": "c9", "severity": "minor", "claim": "alt"}],
                          thread="review/2026-W08"), author="pineal")
        w.write_text("mail/owner/inbox/broken.json", "{not json", author="pineal")
        w.write_json("experiments/exp-0001/protocol.json", {"id": "exp-0001", "pairs": 6}, author="pineal")
        w.write_json("config/budget.json", {"weekly_cap_usd": 30}, author="pineal")
        w.append_jsonl("ops/budget.jsonl", {"week": "2026-W10", "bot": "vagus", "usd": 1.5,
                                            "date": "2026-03-03"}, author="pineal")
        w.append_jsonl("ops/budget.jsonl", {"week": "2026-W09", "bot": "vagus", "usd": 99.0,
                                            "date": "2026-02-27"}, author="pineal")

    def _seed_store(self) -> None:
        rng = random.Random(RNG_SEED)
        rows = [DailyValue(f"2026-03-{d:02d}", "rhr", round(rng.uniform(48, 56), 1), "watch", 1, "ok")
                for d in range(1, 5)]
        rows.append(DailyValue(TODAY, "weight_kg", 70.0, "phone", 1, "ok"))
        self.store.put_daily(rows)

    def test_build_writes_all_pages(self) -> None:
        self._seed_ledger()
        self._seed_store()
        out = self.tmp / "site"
        paths = dashboard.build(self.store, self.ledger, out, NOW, self.cfg)
        self.assertEqual([p.name for p in paths], [name for name, _ in dashboard.PAGES])
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(p.name for p in paths))
        pages = {p.name: p.read_text(encoding="utf-8") for p in paths}
        for page in pages.values():
            _assert_safe_page(self, page)

        today = pages["index.html"]
        self.assertIn("Ruhetag", today)  # alert dated today -> red flag -> rest
        self.assertIn("Warnsignal aktiv", today)
        self.assertEqual(today.count("dunklen Urin"), 1)  # old and duplicate rows dropped
        self.assertIn("keine Daten", today)  # no samples -> stale
        self.assertNotIn("70,0", today)  # daily weight is never shown
        self.assertIn("Ruhepuls", today)

        self.assertIn("gegenüber v0001", pages["plan.html"])
        self.assertIn("1 offen, davon 1 Blocker", pages["challenges.html"])
        self.assertNotIn("alt", re.sub(r"<[^>]+>", " ", pages["challenges.html"]).split())
        self.assertIn("1 Nachrichten", pages["week.html"])
        self.assertIn("exp-0001", pages["experiments.html"])
        self.assertIn("1,50 $", pages["budget.html"])
        self.assertIn("30,00 $", pages["budget.html"])
        self.assertIn('action="/checkin"', pages["checkin.html"])
        self.assertIn(TODAY, pages["checkin.html"])

    def _today_page(self) -> str:
        paths = dashboard.build(self.store, self.ledger, self.tmp / "site", NOW, self.cfg)
        return paths[0].read_text(encoding="utf-8")

    def test_today_follows_pineal_coaching_holds(self) -> None:
        w = self.ledger
        w.write_json("plans/v0001.json", _plan(1, None), author="pineal")
        w.write_text("plans/CURRENT", "v0001\n", author="pineal")
        self.assertNotIn("Ruhetag", self._today_page())
        # Urgent from four days ago, never cleared: coaching stays paused and the alert stays listed.
        w.append_jsonl("data/alerts.jsonl", {"level": "urgent", "rule_id": "bp_high", "date": "2026-02-28",
                                             "data": {"sys": 190, "dia": 125}}, author="pineal")
        page = self._today_page()
        self.assertIn("Ruhetag", page)
        self.assertIn("Warnsignal aktiv", page)
        w.append_jsonl("data/coaching_cleared.jsonl", {"rule_id": "bp_high", "date": "2026-02-28"}, author="pineal")
        self.assertNotIn("Ruhetag", self._today_page())
        # A focal bone pain prompt from yesterday holds for seven days.
        w.append_jsonl("data/alerts.jsonl", {"level": "prompt", "rule_id": "focal_bone_pain", "date": "2026-03-03",
                                             "data": {}}, author="pineal")
        self.assertIn("Ruhetag", self._today_page())

    def test_build_on_empty_ledger(self) -> None:
        paths = dashboard.build(self.store, self.ledger, self.tmp / "site", NOW, self.cfg)
        self.assertEqual(len(paths), 7)
        self.assertIn("Kein ratifizierter Plan", paths[0].read_text(encoding="utf-8"))

    def test_rebuild_replaces_atomically(self) -> None:
        out = self.tmp / "site"
        dashboard.build(self.store, self.ledger, out, NOW, self.cfg)
        self._seed_ledger()
        paths = dashboard.build(self.store, self.ledger, out, NOW, self.cfg)
        self.assertEqual(len(list(out.iterdir())), 7)
        self.assertIn("gegenüber v0001", paths[1].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

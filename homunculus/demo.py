"""Showcase: the whole pipeline on synthetic data for a fictional person.

``run_demo`` creates a fresh ledger (a new temporary directory unless ``out``
names a new or empty one), generates ``days`` days of seeded, physiologically
plausible tracker data and check-ins, and runs

    ingest -> metrics -> insula -> evidence -> weekly review -> guardrails
    -> plan proposal -> dashboard

with ``FakeProvider`` for every LLM call and an in-process fake for Crossref.
It never reads HOMUNCULUS_LEDGER or HEALTH_DB, opens no socket and needs no
API key. Every value is invented; the person does not exist.

Story of the data: a stable base phase, five days of a cold (no training,
lower HRV, higher resting HR, sore throat), a travel week with late and short
nights, then the review week: one night without the watch (data gap), three
days where the owner feels great while HRV, resting HR and sleep say strain
(subjective/objective conflict), and a long run that felt much harder than
heart rate says.
"""

from __future__ import annotations

import base64
import json
import math
import random
import shutil
import tempfile
import urllib.parse
import urllib.request
from dataclasses import asdict as dataclasses_asdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from homunculus import (
    agents, checkins, cli, dashboard, deepdive, evidence, experiments, guardrails, ingest, insula, lexicon, metrics,
    plans, scouts, weekly,
)
from homunculus.store import Ledger, Store, canonical_json
from homunculus.trackers import garmin
from homunculus.trackers import run as tracker_run
from homunculus.trackers.base import Request, Response

MIN_DAYS = 28
DEMO_TZ = "Europe/Berlin"
DEVICE = "Demo Watch"  # normalized to 'watch' at ingest; the name itself is never stored
HRMAX = 188.0
REVIEW_HOUR = 17

OWNER: dict[str, Any] = {
    "_note": "Fictional demo person. Every value is synthetic.",
    "tz": DEMO_TZ, "sex": "male", "age": 34, "training_status": "recreational", "sleep_target_h": 7.5,
    "hrmax": HRMAX, "hrrest": 52.0, "work_days": [0, 1, 2, 3, 4], "conditions": [],
    "swc": {"sleep_onset_latency": 5.0, "hrv_sdnn_ln": 0.03, "resting_hr": 1.0},
    "injury_constraints": {}, "doctor_contact": "Hausarztpraxis Beispiel (fiktiv)", "ffm_kg": 62.0,
}
GOALS_MD = (
    "# Ziele (Demo, fiktiv)\n\n"
    "1. Halbmarathon im Frühjahr locker durchlaufen.\n"
    "2. Zweimal pro Woche Krafttraining beibehalten.\n"
    "3. Wohlbefinden mindestens 7 von 10 im Wochenmittel.\n"
)
EXERCISES: dict[str, dict[str, Any]] = {
    "ex-squat": {"pattern": "squat", "contraction": "dynamic", "energy_system": "strength",
                 "joint_load": {"knee": "moderate", "hip": "moderate"}},
    "ex-row": {"pattern": "pull_horizontal", "contraction": "dynamic", "energy_system": "strength",
               "joint_load": {"shoulder": "low"}},
    "ex-run": {"pattern": "run", "contraction": "cyclic", "energy_system": "aerobic",
               "joint_load": {"knee": "moderate", "ankle": "moderate"}},
}


# --------------------------------------------------------------------------
# Calendar


@dataclass(frozen=True)
class Calendar:
    days: tuple[str, ...]
    week: str
    now: datetime  # Sunday 17:00 local of the review week, as UTC

    @property
    def n(self) -> int:
        return len(self.days)


def _last_sunday(today: date) -> date:
    return today - timedelta(days=(today.weekday() + 1) % 7)


def calendar(days: int, end: date | None = None) -> Calendar:
    """days ending on the Sunday `end` (default: the most recent Sunday in DEMO_TZ)."""
    last = _last_sunday(end or datetime.now(ZoneInfo(DEMO_TZ)).date())
    dates = tuple((last - timedelta(days=days - 1 - k)).isoformat() for k in range(days))
    local = datetime.combine(last, time(REVIEW_HOUR), ZoneInfo(DEMO_TZ))
    year, week, _ = last.isocalendar()
    return Calendar(dates, f"{year}-W{week:02d}", local.astimezone(timezone.utc))


def phase(i: int, n: int) -> str:
    """Story phase of day index i out of n days (the last 7 days are the review week)."""
    phases = {n - 6: "gap", n - 5: "strain", n - 4: "strain", n - 3: "strain"}
    if i in phases:
        return phases[i]
    if n >= 35 and n - 26 <= i <= n - 22:
        return "cold"
    if n >= 21 and n - 14 <= i <= n - 8:
        return "travel"
    return "base"


# --------------------------------------------------------------------------
# Synthetic physiology

# phase -> (ln HRV offset, RHR offset, sleep hours, bedtime shift minutes)
PHYSIOLOGY: Mapping[str, tuple[float, float, float, float]] = {
    "base": (0.0, 0.0, 7.3, 0.0),
    "cold": (-0.30, 6.0, 7.8, -30.0),
    "travel": (-0.10, 2.0, 6.0, 110.0),
    "strain": (-0.28, 6.5, 6.0, 40.0),
    "gap": (0.0, 0.0, 7.2, 0.0),
}
HRV_LN_BASE = math.log(58.0)
RHR_BASE = 52.0

# weekday -> (kind, minutes, mean HR, typical RPE)
SCHEDULE: Mapping[int, tuple[str, int, float, int]] = {
    0: ("strength", 50, 118.0, 6), 2: ("easy_run", 40, 138.0, 4),
    4: ("intervals", 35, 150.0, 8), 5: ("long_run", 70, 142.0, 5),
}
WORKOUT_NAMES = {"strength": "Traditional Strength Training", "easy_run": "Outdoor Run",
                 "intervals": "Outdoor Run", "long_run": "Outdoor Run"}


def _ts(day: str, minutes: float) -> datetime:
    """Local datetime `minutes` after midnight of day (may run into the next day)."""
    base = datetime.combine(date.fromisoformat(day), time(0), ZoneInfo(DEMO_TZ))
    return (base.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(ZoneInfo(DEMO_TZ))


def _hae(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


@dataclass
class Synth:
    """Seeded generator; every list it fills is plain Health Auto Export JSON or a check-in form."""

    cal: Calendar
    rng: random.Random

    def __post_init__(self) -> None:
        self.metrics: dict[str, list[dict]] = {}
        self.sleep: list[dict] = []
        self.workouts: list[dict] = []
        self.forms: list[tuple[str, str, dict]] = []

    def point(self, name: str, dt: datetime, qty: float) -> None:
        self.metrics.setdefault(name, []).append({"date": _hae(dt), "qty": round(qty, 3), "source": DEVICE})

    def payload(self) -> dict:
        units = {"heart_rate_variability": "ms", "resting_heart_rate": "count/min", "weight_body_mass": "kg",
                 "step_count": "count", "vo2_max": "ml/(kg·min)"}
        metric_list = [{"name": k, "units": units.get(k), "data": v} for k, v in sorted(self.metrics.items())]
        metric_list.append({"name": "sleep_analysis", "units": "hr", "data": self.sleep})
        return {"data": {"metrics": metric_list, "workouts": self.workouts}}


def _night(s: Synth, day: str, ph: str) -> tuple[datetime, datetime] | None:
    """Sleep segments of the night ending on day; returns (sleep start, sleep end)."""
    _, _, hours, shift = PHYSIOLOGY[ph]
    tst = max(4.0, hours + s.rng.gauss(0.0, 0.3))
    start = _ts(day, -50 + shift + s.rng.gauss(0.0, 20.0))  # around 23:10 the evening before
    blocks = (("Core", 0.35), ("Deep", 0.15), ("Awake", None), ("Core", 0.25), ("REM", 0.25))
    cursor = start
    for stage, share in blocks:
        minutes = 8.0 if share is None else tst * 60.0 * share
        end = cursor + timedelta(minutes=minutes)
        if ph != "gap":
            s.sleep.append({"startDate": _hae(cursor), "endDate": _hae(end), "value": stage, "source": DEVICE})
        cursor = end
    return (start, cursor) if ph != "gap" else None


def _vitals(s: Synth, i: int, day: str, ph: str, window: tuple[datetime, datetime] | None) -> None:
    ln_off, rhr_off, _, _ = PHYSIOLOGY[ph]
    if window is not None:
        start, end = window
        level = HRV_LN_BASE + ln_off + s.rng.gauss(0.0, 0.06)
        for frac in (0.25, 0.55, 0.85):
            s.point("heart_rate_variability", start + (end - start) * frac, math.exp(level + s.rng.gauss(0.0, 0.05)))
        s.point("resting_heart_rate", _ts(day, 8 * 60), RHR_BASE + rhr_off + s.rng.gauss(0.0, 1.0))
    s.point("weight_body_mass", _ts(day, 7 * 60 + 15), 76.0 - 0.01 * i + s.rng.gauss(0.0, 0.25))
    steps = 3000.0 if ph == "cold" else 9000.0
    s.point("step_count", _ts(day, 21 * 60), max(500.0, steps + s.rng.gauss(0.0, 1500.0)))
    if i % 7 == 0:
        s.point("vo2_max", _ts(day, 9 * 60), 47.0 + 0.02 * i + s.rng.gauss(0.0, 0.3))


def _trains(weekday: int, ph: str) -> bool:
    if weekday not in SCHEDULE or ph == "cold":
        return False
    return ph != "travel" or weekday == 2


def _hr_series(s: Synth, start: datetime, kind: str, minutes: int, mean_hr: float) -> list[dict]:
    out = []
    for m in range(minutes):
        bpm = mean_hr + (16.0 if kind == "intervals" and (m // 3) % 2 else 0.0) + 0.08 * m * (kind == "long_run")
        out.append({"date": _hae(start + timedelta(minutes=m)), "Avg": round(bpm + s.rng.gauss(0.0, 3.0), 1)})
    return out


def _session_rpe(s: Synth, i: int, n: int, kind: str, typical: int) -> int:
    if kind == "long_run" and i == n - 2:
        return 9  # felt much harder than heart rate says (R3)
    return max(1, min(10, typical + round(s.rng.gauss(0.0, 0.6))))


def _workout(s: Synth, i: int, day: str, weekday: int) -> None:
    kind, minutes, mean_hr, typical = SCHEDULE[weekday]
    start = _ts(day, 18 * 60 if weekday < 5 else 9 * 60 + 30)
    series = _hr_series(s, start, kind, minutes, mean_hr)
    wid = f"demo-w{i:03d}"
    s.workouts.append({
        "id": wid, "name": WORKOUT_NAMES[kind], "start": _hae(start), "end": _hae(start + timedelta(minutes=minutes)),
        "duration": minutes * 60.0, "activeEnergyBurned": {"qty": round(minutes * 9.5, 1), "units": "kcal"},
        "avgHeartRate": {"qty": mean_hr, "units": "count/min"},
        "maxHeartRate": {"qty": max(p["Avg"] for p in series), "units": "count/min"},
        "heartRateData": series, "isIndoor": kind == "strength", "source": DEVICE,
    })
    rpe = _session_rpe(s, i, s.cal.n, kind, typical)
    ts = _hae(start + timedelta(minutes=minutes + 30))
    s.forms.append((day, ts, {"session_rpe": rpe, "workout_id": wid}))


# phase -> mean morning scores (sleep_quality, energy, mood, soreness, stress, motivation, wellbeing)
FEELINGS: Mapping[str, tuple[float, ...]] = {
    "base": (5, 5, 5, 3, 3, 5, 7), "cold": (4, 2, 3, 4, 3, 2, 4), "travel": (3, 4, 4, 3, 4, 4, 6),
    "strain": (7, 7, 7, 1, 1, 7, 10), "gap": (5, 5, 5, 3, 3, 5, 7),
}
FORM_ITEMS = ("sleep_quality", "energy", "mood", "soreness", "stress", "motivation", "wellbeing")


def _morning_form(s: Synth, i: int, day: str, ph: str) -> None:
    noise = 0.0 if ph == "strain" else 0.7
    form: dict[str, Any] = {}
    for key, mean in zip(FORM_ITEMS, FEELINGS[ph]):
        spec = checkins.ITEMS[key]
        form[key] = max(spec.lo, min(spec.hi, round(mean + s.rng.gauss(0.0, noise))))
    if ph == "cold":
        form["symptoms"] = ["sore_throat"]
    if ph == "travel" and day == s.cal.days[s.cal.n - 8]:
        form["note"] = "Nach der Reise noch etwas müde."
    s.forms.append((day, _hae(_ts(day, 7 * 60 + 40)), form))


def synthesize(cal: Calendar, seed: int) -> Synth:
    s = Synth(cal, random.Random(seed))
    for i, day in enumerate(cal.days):
        ph = phase(i, cal.n)
        window = _night(s, day, ph)
        _vitals(s, i, day, ph, window)
        weekday = date.fromisoformat(day).weekday()
        if _trains(weekday, ph):
            _workout(s, i, day, weekday)
        skip = ph == "base" and i < cal.n - 10 and s.rng.random() < 0.05
        if not skip:
            _morning_form(s, i, day, ph)
    return s


# --------------------------------------------------------------------------
# Synthetic papers and Crossref

PAPERS: tuple[dict[str, Any], ...] = (
    {"id": "demo-c1", "scout": "retina", "doi": "10.5555/demo.0001", "design": "meta_rct",
     "title": "Synthetic meta-analysis: dimming evening light and sleep onset latency in recreational athletes",
     "sentences": {
         "design": "This meta-analysis of randomized controlled trials pooled 9 trials.",
         "n_total": "A total of 412 participants were included.",
         "population": "Participants were recreationally trained men and women with a mean age of 31 years.",
         "effect": "Dimming evening light shortened sleep onset latency by 12 minutes (95% CI -18 to -6).",
         "registration_id": "The protocol was registered as CRD42099000001.",
         "i2": "Heterogeneity was low (I2 = 18%).",
     },
     "values": {"n_total": 412, "sex": "mixed", "age_mean": 31.0, "training": "recreational", "k": 9, "i2": 18.0,
                "outcome": "sleep_onset_latency", "unit": "min", "type": "MD", "est": -12.0, "lo": -18.0,
                "hi": -6.0, "reg": "CRD42099000001"}},
    {"id": "demo-c2", "scout": "papilla", "doi": "10.5555/demo.0002", "design": "rct_parallel",
     "title": "Synthetic randomized trial: tart cherry concentrate and overnight heart rate variability",
     "sentences": {
         "design": "In this randomized controlled trial, adults took tart cherry concentrate or placebo.",
         "n_total": "Forty adults completed the study.",
         "population": "Participants were recreationally active men and women aged 29 on average.",
         "effect": "Overnight HRV rose by 0.30 SD (95% CI -0.10 to 0.70) versus placebo.",
     },
     "values": {"n_total": 40, "sex": "mixed", "age_mean": 29.0, "training": "recreational", "k": None, "i2": None,
                "outcome": "hrv_sdnn_ln", "unit": "SD", "type": "SMD", "est": 0.30, "lo": -0.10, "hi": 0.70,
                "reg": None}},
    {"id": "demo-c3", "scout": "spindle", "doi": "10.5555/demo.0003", "design": "cross_sectional",
     "title": "Synthetic cross-sectional study: daily steps and resting heart rate in office workers",
     "sentences": {
         "design": "This cross-sectional study analysed one week of wearable data.",
         "n_total": "We included 1250 office workers.",
         "population": "Participants were untrained men and women with a mean age of 44 years.",
         "effect": "Each 1000 extra daily steps went with a 0.4 bpm lower resting heart rate (95% CI -0.6 to -0.2).",
     },
     "values": {"n_total": 1250, "sex": "mixed", "age_mean": 44.0, "training": "untrained", "k": None, "i2": None,
                "outcome": "resting_hr", "unit": "bpm", "type": "MD", "est": -0.4, "lo": -0.6, "hi": -0.2,
                "reg": None}},
    # Frontier: a clean rodent study (randomized, blinded, preregistered) with a mechanism that is partly
    # shown in humans and a low-risk timing intervention. Low certainty, high promise after the deep dive.
    {"id": "demo-c4", "scout": "retina", "doi": "10.5555/demo.0004", "design": "animal",
     "title": "Synthetic rodent study: time-restricted feeding and nocturnal heart rate variability in rats",
     "sentences": {
         "design": "In this randomized study, rats were assigned to time-restricted feeding or ad libitum feeding.",
         "n_total": "Forty-eight rats completed the protocol.",
         "population": "The animals were adult male rats.",
         "effect": "Time-restricted feeding raised nocturnal heart rate variability by 0.80 SD "
                   "(95% CI 0.35 to 1.25).",
         "mechanism": "Time-restricted feeding raised nocturnal vagal tone through realigned clock gene expression.",
         "blinded": "Outcome assessors were blinded to group allocation.",
         "registration_id": "The protocol was preregistered.",
         "dose_response": "Shorter daily feeding windows produced larger increases.",
     },
     "values": {"n_total": 48, "sex": "male", "age_mean": None, "training": None, "k": None, "i2": None,
                "outcome": "nocturnal_hrv", "unit": "SD", "type": "SMD", "est": 0.80, "lo": 0.35, "hi": 1.25,
                "reg": None, "prereg": True, "randomized": True, "blinded_outcome": True, "dose_response": True}},
    # Weak methods: a tiny unblinded, unregistered pilot with a large effect. The quality gate caps both axes.
    {"id": "demo-c5", "scout": "spindle", "doi": "10.5555/demo.0005", "design": "rct_parallel",
     "title": "Synthetic pilot trial: a bedtime breathing app and sleep onset latency",
     "sentences": {
         "design": "In this small randomized pilot, adults used a breathing app or no app before bed.",
         "n_total": "Twelve adults took part.",
         "population": "Participants were recreationally active men and women aged 33 on average.",
         "effect": "Sleep onset latency fell by 14 minutes (95% CI -26 to -2) versus control.",
         "dose_response": "Longer app sessions went with shorter sleep onset.",
     },
     "values": {"n_total": 12, "sex": "mixed", "age_mean": 33.0, "training": "recreational", "k": None, "i2": None,
                "outcome": "sleep_onset_latency", "unit": "min", "type": "MD", "est": -14.0, "lo": -26.0,
                "hi": -2.0, "reg": None, "dose_response": True}},
)
FRONTIER_DOI = "10.5555/demo.0004"
WEAK_DOI = "10.5555/demo.0005"
JOURNAL = "Journal of Synthetic Examples"


def abstract(paper: Mapping[str, Any]) -> str:
    return " ".join(paper["sentences"].values()) + " All data in this abstract are synthetic."


def _f(value: Any, quote: str | None) -> dict:
    return {"value": value, "quote": quote if value is not None else None}


def _optional_fields(v: Mapping[str, Any], q: Mapping[str, str]) -> dict:
    """Method fields only some synthetic papers report, each quoted from its own sentence."""
    out = {"randomized": _f(v["randomized"], q["design"])} if "randomized" in v else {}
    for key, sentence in (("blinded_outcome", "blinded"), ("dose_response", "dose_response")):
        if key in v:
            out[key] = _f(v[key], q[sentence])
    return out


def extraction(paper: Mapping[str, Any]) -> dict:
    """A Thalamus-shaped extraction whose quotes are verbatim spans of the synthetic abstract."""
    v, q = paper["values"], paper["sentences"]
    pop, eff = q["population"], q["effect"]
    return {**_base_extraction(paper["doi"], paper["design"], v, q, pop, eff), **_optional_fields(v, q)}


def _base_extraction(doi: str, design: str, v: Mapping[str, Any], q: Mapping[str, str], pop: str, eff: str) -> dict:
    return {
        "doi": _f(doi, None), "pmid": _f(None, None), "design": _f(design, q["design"]),
        "n_total": _f(v["n_total"], q["n_total"]), "n_per_arm": _f(None, None),
        "population": {"sex": _f(v["sex"], pop), "age_mean": _f(v["age_mean"], pop),
                       "training_status": _f(v["training"], pop), "bmi_mean": _f(None, None),
                       "condition": _f(None, None)},
        "outcome": {"name": _f(v["outcome"], eff), "unit": _f(v["unit"], eff), "is_surrogate": _f(None, None)},
        "effect": {"type": _f(v["type"], eff), "est": _f(v["est"], eff), "ci_lo": _f(v["lo"], eff),
                   "ci_hi": _f(v["hi"], eff), "sd_pop": _f(None, None)},
        "registration_id": _f(v["reg"], q.get("registration_id")),
        "preregistered": _f(True if v["reg"] or v.get("prereg") else None, q.get("registration_id")),
        "attrition_pct": _f(None, None), "blinded_outcome": _f(None, None), "outcome_switched": _f(None, None),
        "funding_industry_product": _f(None, None), "i2": _f(v["i2"], q.get("i2")),
        "k_studies": _f(v["k"], q["design"]), "peer_reviewed": _f(True, None), "dose_response": _f(None, None),
        "reported_means": [],
    }


def crossref_opener(req: urllib.request.Request) -> bytes:
    """In-process stand-in for api.crossref.org: synthetic works, no update notices, no network."""
    parts = urllib.parse.urlsplit(req.full_url)
    if parts.hostname != "api.crossref.org":
        raise scouts.HostNotAllowed(f"demo has no network: {parts.hostname}")
    doi = urllib.parse.unquote(parts.path[len("/works/"):]) if parts.path.startswith("/works/") else None
    paper = next((p for p in PAPERS if p["doi"] == doi), None)
    if paper is None:
        return json.dumps({"message": {"items": []}}).encode("utf-8")
    message = {"DOI": doi, "title": [paper["title"]], "container-title": [JOURNAL],
               "author": [{"given": "A.", "family": "Example"}], "issued": {"date-parts": [[2025, 3, 1]]}}
    return json.dumps({"message": message}).encode("utf-8")


def _write_candidates(ledger: Ledger, cal: Calendar) -> None:
    day = cal.days[-4]  # found by the scouts during the review week
    for p in PAPERS:
        rel = scouts.quarantine_text(ledger, p["id"], f"{p['title']}\n\n{abstract(p)}", author=p["scout"])
        rec = {"id": p["id"], "scout": p["scout"], "doi": p["doi"], "pmid": None, "registration_id": None,
               "title": p["title"], "year": 2025, "pub_types": [], "source_api": "europepmc", "preprint": False,
               "query": "demo", "found_at": f"{day}T03:00:00+00:00", "quarantine_path": rel,
               "triage": {"relevance": 2}, "score": 1.0}
        ledger.append_jsonl(f"evidence/candidates/{day}.jsonl", rec, author=p["scout"])


# --------------------------------------------------------------------------
# Frontier deep dive: the sources Thalamus reads and its scripted chain

MECHANISM_SOURCES = (
    "Synthetic source A (doi:10.5555/demo.0101): in a randomized crossover trial in adults, eating within "
    "an early ten-hour window improved circadian clock alignment. "
    "Synthetic source C (doi:10.5555/demo.0102): in healthy adults, higher nocturnal vagal tone went with "
    "higher overnight heart rate variability. "
    "Synthetic source B (doi:10.5555/demo.0103): in rats, realigned clock gene expression raised nocturnal "
    "vagal tone. "
    "Synthetic source D: the feeding-entrained clock pathway is conserved across mammals. "
    "All data in these sources are synthetic."
)


def _source(doi: str, design: str, title: str, s: Mapping[str, str], v: Mapping[str, Any]) -> dict:
    base = {"sex": "mixed", "age_mean": None, "training": None, "k": None, "i2": None, "unit": "SD", "type": "SMD",
            "reg": None}
    return {"doi": doi, "design": design, "title": title, "sentences": s, "values": {**base, **v}}


_PREREG = {"prereg": True, "randomized": True, "blinded_outcome": True}
_BLINDED = {"blinded": "Outcome assessors were blinded to group allocation.",
            "registration_id": "The protocol was preregistered."}

# The papers the deep dive cites, scored by code before the dive: unscored citations count nothing.
MECHANISM_PAPERS: tuple[dict[str, Any], ...] = (
    _source("10.5555/demo.0101", "rct_crossover", "Synthetic crossover trial: early eating window and clock alignment",
            {"design": "In a randomized crossover trial in adults, eating within an early ten-hour window improved "
                       "circadian clock alignment.",
             "n_total": "Thirty-six adults completed both periods.",
             "population": "Participants were recreationally active men and women aged 30 on average.",
             "effect": "Circadian alignment improved by 0.50 SD (95% CI 0.20 to 0.80)."},
            {"n_total": 36, "age_mean": 30.0, "training": "recreational", "outcome": "circadian_alignment",
             "est": 0.5, "lo": 0.2, "hi": 0.8}),
    _source("10.5555/demo.0102", "mechanistic_human", "Synthetic mechanistic study: vagal tone and overnight HRV",
            {"design": "In this randomized mechanistic study, healthy adults had nocturnal vagal tone raised or "
                       "left unchanged.",
             "n_total": "A total of 160 healthy adults took part.",
             "population": "Participants were recreationally active men and women aged 32 on average.",
             "effect": "Higher nocturnal vagal tone went with higher overnight heart rate variability "
                       "(0.50 SD, 95% CI 0.25 to 0.75).", **_BLINDED},
            {"n_total": 160, "age_mean": 32.0, "training": "recreational", "outcome": "nocturnal_hrv",
             "est": 0.5, "lo": 0.25, "hi": 0.75, **_PREREG}),
    _source("10.5555/demo.0103", "animal", "Synthetic rodent study: clock alignment and nocturnal vagal tone in rats",
            {"design": "In this randomized study, rats were assigned to clock-aligned or misaligned light schedules.",
             "n_total": "Forty rats completed the protocol.", "population": "The animals were adult male rats.",
             "effect": "Realigned clock gene expression raised nocturnal vagal tone by 0.70 SD "
                       "(95% CI 0.30 to 1.10).", **_BLINDED},
            {"n_total": 40, "sex": "male", "outcome": "vagal_tone", "est": 0.7, "lo": 0.3, "hi": 1.1, **_PREREG}),
)


def seed_mechanism_papers(ledger: Ledger, owner: Any) -> list[str]:
    """Write the deep dive's cited papers as code-scored ledger records; returns their paths."""
    rels = []
    for paper in MECHANISM_PAPERS:
        ext = extraction(paper)
        score = evidence.score(ext, owner, update_to=[], swc_paper_units=None)
        rel = weekly._paper_rel(paper["doi"])
        ledger.write_json(rel, {"schema": 1, "rubric_version": evidence.RUBRIC_VERSION, "doi": paper["doi"],
                                "meta": {"title": paper["title"]}, "update_to": [], "resolved": False,
                                "extraction": ext, "blocked": False, "score": dataclasses_asdict(score)},
                          author=cli.CODE_AUTHOR)
        rels.append(rel)
    return rels


def _ev(paper: str, design: str, quote: str) -> dict:
    return {"paper": paper, "design": design, "relation": "supports", "quote": quote}


def _dive_link(lid: str, src: tuple[str, str], dst: tuple[str, str], kind: str, evidence_: list[dict],
               question: str | None = None, keywords: Sequence[str] = ()) -> dict:
    return {"id": lid, "src": src[1], "src_type": src[0], "dst": dst[1], "dst_type": dst[0], "kind": kind,
            "evidence": evidence_, "open_question": question, "keywords": list(keywords)}


_TRE = ("intervention", "Time-restricted eating")
_CLOCK = ("mechanism", "Circadian clock alignment")
_VAGAL = ("mechanism", "Nocturnal vagal tone")
_HRV = ("outcome", "Overnight heart rate variability")

DEEP_DIVE: dict[str, Any] = {
    "claim": {"intervention": _TRE[1], "category": "timing", "outcome": _HRV[1], "tracker_key": "hrv_sdnn_ln",
              "supplement": None},
    "links": [
        _dive_link("l1", _TRE, _CLOCK, "mechanism", [_ev(
            "doi:10.5555/demo.0101", "rct_crossover",
            "eating within an early ten-hour window improved circadian clock alignment")]),
        _dive_link("l2", _CLOCK, _VAGAL, "biomarker", [_ev(
            "doi:10.5555/demo.0103", "animal",
            "realigned clock gene expression raised nocturnal vagal tone")],
            "Hebt eine bessere Ausrichtung der inneren Uhr den nächtlichen Vagotonus auch beim Menschen?",
            ["vagal tone", "time-restricted eating"]),
        _dive_link("l3", _VAGAL, _HRV, "outcome", [_ev(
            "doi:10.5555/demo.0102", "mechanistic_human",
            "higher nocturnal vagal tone went with higher overnight heart rate variability")]),
    ],
    "conserved_pathway": {"value": True, "quote": "the feeding-entrained clock pathway is conserved across mammals"},
}


# --------------------------------------------------------------------------
# Scripted LLM replies (FakeProvider fixtures)


def _paper_for(req: agents.LLMRequest) -> dict:
    text = json.dumps([dict(m) for m in req.messages], ensure_ascii=False)
    paper = next((p for p in PAPERS if p["doi"] in text), PAPERS[0])
    return extraction(paper)


INSULA_DE: Mapping[str, str] = {
    "R1_sustained": ("An mehreren Tagen fühlst du dich deutlich besser, als HRV, Ruhepuls und Schlafdauer anzeigen. "
                     "Die Körperdaten sprechen eher für Belastung als für Erholung."),
    "R2_single_day": "An diesem Tag liegen Gefühl und Körperdaten weit auseinander, in entgegengesetzter Richtung.",
    "R3_session_residual": "Die Einheit hat sich viel härter angefühlt, als der Puls erwarten lässt.",
    "DATA_GAP": "In dieser Nacht fehlen Uhrdaten. Für diesen Tag lässt sich nichts vergleichen.",
}
INSULA_QUESTION_DE: Mapping[str, str] = {
    "R1_sustained": "Hast du in den letzten Tagen weniger geschlafen oder mehr Stress gehabt, als du einträgst?",
    "R3_session_residual": "War beim langen Lauf am Samstag etwas anders als sonst, etwa Hitze, Hunger oder müde Beine?",
}


def _insula_items(rec: Mapping[str, Any], ref: str) -> dict:
    items, asked = [], 0
    for c in rec["conflicts"]:
        question = INSULA_QUESTION_DE.get(c["rule_id"]) if asked < 3 else None
        asked += question is not None
        noisier = "objective" if c["rule_id"] == "R3_session_residual" else "unclear"
        items.append({"conflict_id": c["id"], "interpretation_de": INSULA_DE[c["rule_id"]], "noisier_signal": noisier,
                      "question_for_owner_de": question, "refs": [ref]})
    return {"items": items}


def _op(path: str, value: Any, refs: Sequence[str]) -> dict:
    return {"op": "set", "path": path, "value": value, "refs": list(refs)}


@dataclass
class Script:
    """Replies of Hippocampus, Amygdala and Cerebellum for one review, in turn order."""

    week: str
    turns: dict[str, int]

    def pkt(self, pointer: str) -> str:
        return f"reviews/{self.week}/packet.json#{pointer}"

    def next(self, bot: str) -> int:
        self.turns[bot] = self.turns.get(bot, 0) + 1
        return self.turns[bot]

    def ops_revised(self) -> list[dict]:
        refs = [self.pkt("/conflicts/conflicts"), self.pkt("/guard_context/weekly_loads")]
        return [_op("/sessions/3/duration_min", 60, refs), _op("/sessions/2/target_rpe", 6, refs)]

    def draft(self) -> dict:
        ref = self.pkt("/baselines/load/srpe")
        return {"summary_md": "Die Last der letzten Wochen war stabil. Vorschlag: langer Lauf von 70 auf 80 Minuten.",
                "plan_diff": [_op("/sessions/3/duration_min", 80, [ref])],
                "claims": [{"id": "k1", "text": "Die Wochenlast liegt im Rahmen der letzten vier Wochen.",
                            "refs": [ref]}],
                "experiments_proposed": []}

    def challenge(self, n: int) -> dict:
        if n == 1:
            return {"items": [
                {"cid": "c1", "severity": "major", "claim": "Konflikt zwischen Gefühl und Körperdaten in dieser Woche, "
                 "dazu Erkältungstage und Reisewoche im 4-Wochen-Fenster. Steigerung jetzt verfrüht.",
                 "ref": self.pkt("/conflicts/conflicts"), "diff_paths": ["/sessions/3/duration_min"]},
                {"cid": "c2", "severity": "minor", "claim": "Studie spricht für längere Läufe.",
                 "ref": "evidence/papers/not-in-ledger.json", "diff_paths": ["/sessions/3/duration_min"]},
            ]}
        return {"items": [{"cid": "c3", "severity": "minor",
                           "claim": "Intervalle mit RPE 6 setzen kaum noch einen Reiz.",
                           "ref": self.pkt("/baselines/load/srpe"), "diff_paths": ["/sessions/2/target_rpe"]}]}

    def answer(self, n: int) -> dict:
        if n == 1:
            return {"answers": [{"cid": "c1", "action": "accept", "ref": None, "change": self.ops_revised()}],
                    "plan_diff": self.ops_revised()}
        return {"answers": [{"cid": "c3", "action": "rebut", "ref": f"data/conflicts/{self.week}.json",
                             "change": []}], "plan_diff": self.ops_revised()}

    def ruling(self) -> dict:
        return {"ruling": "ratify", "plan_diff": self.ops_revised(),
                "rationale_md": "Entlastungswoche: langer Lauf 60 Minuten, Intervalle bei RPE 6. "
                                "Gefühl und Körperdaten widersprechen sich, deshalb keine Steigerung.",
                "ask_owner_de": ["Wie erholt fühlst du dich nach der Reisewoche wirklich, auch nachts?"]}

    def reply(self, req: agents.LLMRequest) -> dict:
        if req.output_schema is evidence.PAPER_SCHEMA:
            return _paper_for(req)  # Thalamus extraction and the Amygdala audit agree
        if req.output_schema is deepdive.DEEPDIVE_SCHEMA:
            return DEEP_DIVE
        if req.bot == "hippocampus":
            return self.draft() if req.output_schema is weekly.DRAFT_SCHEMA else self.answer(self.next("answer"))
        if req.bot == "amygdala":
            return self.challenge(self.next("challenge"))
        return self.ruling()


def fake_provider(week: str, insula_rec: Mapping[str, Any]) -> agents.FakeProvider:
    script = Script(week, {})
    conflicts_ref = f"data/conflicts/{week}.json"
    fixtures: dict[str, Any] = {bot: script.reply for bot in ("hippocampus", "amygdala", "cerebellum", "thalamus")}
    fixtures["insula"] = _insula_items(insula_rec, conflicts_ref)
    return agents.FakeProvider(fixtures)


# --------------------------------------------------------------------------
# Plan and ledger setup


def initial_plan(cal: Calendar) -> dict:
    monday = date.fromisoformat(cal.days[-7])
    ex = {"sets": 3, "reps": 8, "load_kg": None, "rir": 2, "novel": False, "probation": False}
    return {
        "version": 1, "parent": None, "effective_from": f"{monday.isoformat()}T00:00:00Z",
        "valid_until": f"{(monday + timedelta(days=7)).isoformat()}T00:00:00Z",
        "author": "pineal", "ratified_by": "owner", "data_snapshot_sha": None, "rationale_refs": [],
        "sessions": [
            {"id": "s-mon", "weekday": 0, "kind": "strength", "duration_min": 50, "target_rpe": 6,
             "exercises": [{"exercise_id": "ex-squat", **ex}, {"exercise_id": "ex-row", **ex}]},
            {"id": "s-wed", "weekday": 2, "kind": "run", "duration_min": 40, "target_rpe": 4, "exercises": []},
            {"id": "s-fri", "weekday": 4, "kind": "intervals", "duration_min": 35, "target_rpe": 7, "exercises": []},
            {"id": "s-sat", "weekday": 5, "kind": "long_run", "duration_min": 70, "target_rpe": 5, "exercises": []},
        ],
        "nutrition": {"kcal_target": None, "protein_g_per_kg": 1.6, "notes_de": "Normal essen, genug Eiweiß."},
        "supplements": [], "experiments": [],
        "modulation": {"max_daily_load_cut_pct": 30, "allow_swap": True, "readiness_low_action": "shorten"},
    }


def _demo_root(out: Path | None) -> Path:
    if out is None:
        return Path(tempfile.mkdtemp(prefix="homunculus-demo-"))
    return out


def _setup_ledger(root: Path, cal: Calendar, git: bool) -> Ledger:
    cli.init_ledger(root, git=git)
    roster = agents.load_roster(json.loads((root / "config" / "roster.json").read_text(encoding="utf-8")))
    ledger = Ledger(root, agents.write_rules(roster))
    ledger.write_json("profile/owner.json", OWNER, author="pineal")
    ledger.write_json("config/exercises.json", EXERCISES, author="pineal")
    ledger.write_text("config/goals.md", GOALS_MD, author="pineal")
    ledger.write_json("plans/v0001.json", initial_plan(cal), author="pineal")
    ledger.write_text("plans/CURRENT", "v0001\n", author="pineal")
    ledger.write_text("plans/v0001.md", plans.render_markdown_de(initial_plan(cal)), author="pineal")
    return ledger


# --------------------------------------------------------------------------
# Pipeline steps


@dataclass
class Run:
    root: Path
    cal: Calendar
    ledger: Ledger
    store: Store
    cfg: metrics.MetricsConfig
    git: bool
    commits: int = 0

    def commit(self, kind: str, message: str, author: str = "pineal") -> None:
        if self.git and cli.commit(self.ledger, kind, message, author=author):
            self.commits += 1


def step_ingest(run: Run, seed: int) -> dict:
    synth = synthesize(run.cal, seed)
    payload = synth.payload()
    ingest.save_raw(run.root / ingest.PRIVATE_DIR / "raw", json.dumps(payload).encode("utf-8"), run.cal.now)
    counts = cli.store_batch(run.store, ingest.parse_payload(payload, {}))
    items = [it for day, ts, form in synth.forms for it in checkins.validate(form, date=day, ts=ts)]
    counts["checkins"] = run.store.put_checkins(items)
    return counts


def step_metrics(run: Run) -> None:
    cli.finalize_days(run.store, run.ledger, run.cfg, list(run.cal.days), run.cal.now)
    run.commit("data.daily", f"Daily rows and baselines, {run.cal.days[0]}..{run.cal.days[-1]}")


def _llm(provider: agents.Provider, ledger: Ledger, cal: Calendar) -> dict[str, Any]:
    roster = agents.load_roster(ledger.read_json("config/roster.json"))
    budget = agents.Budget(ledger, ledger.read_json("config/budget.json"), now=lambda: cal.now)
    return {"roster": roster, "providers": {"anthropic": provider}, "budget": budget,
            "prompts_dir": weekly.PROMPTS_DIR}


def step_insula(run: Run, llm: Mapping[str, Any]) -> dict:
    result = weekly.run_insula(run.cal.week, run.ledger, run.store, run.cfg, **llm)
    run.commit("data.anomaly", f"Conflicts {run.cal.week}", author="insula")
    return result


def step_evidence(run: Run, llm: Mapping[str, Any]) -> dict[str, str]:
    _write_candidates(run.ledger, run.cal)
    http = scouts.Http(run.root / "evidence" / "cache", mailto=None, keys={}, rate_per_s={"api.crossref.org": 1e6},
                       opener=crossref_opener, clock=lambda: run.cal.now.timestamp(), sleep=lambda _s: None)
    owner = cli.owner_profile(OWNER, run.cal.now.date())
    written = weekly.run_thalamus(run.ledger, http, owner, run.cal.now, **llm)
    run.commit("evidence.appraisal", f"Extracted and scored {len(written)} papers", author="thalamus")
    gates = {}
    for rel in written:
        rec = run.ledger.read_json(rel) or {}
        gates[rel] = ((rec.get("score") or {}).get("gate")) or ("blocked" if rec.get("blocked") else "unscored")
    return gates


def _appraisal_line(rec: Mapping[str, Any]) -> dict:
    a = rec["appraisal"]
    return {"status": a["status"], "certainty": round(rec["score"]["certainty"], 2),
            "certainty_capped": round(a["certainty"], 2), "promise": round(a["promise"], 2),
            "flags": list(a["flags"]), "blockers": list(a["frontier_blockers"])}


def _appraise_paper(run: Run, rel: str) -> dict:
    rec = dict(run.ledger.read_json(rel))
    score, appraisal = cli.appraise_record(run.ledger, rec, cli.owner_profile(OWNER, run.cal.now.date()),
                                           as_of=run.cal.now.date().isoformat())
    rec.update(score=dataclasses_asdict(score), appraisal=dataclasses_asdict(appraisal))
    run.ledger.write_json(rel, rec, author=cli.CODE_AUTHOR)
    return _appraisal_line(rec)


def step_frontier(run: Run, llm: Mapping[str, Any]) -> dict:
    """Appraise the rodent study, deep-dive it into an N-of-1 candidate, then re-appraise; cap the weak pilot."""
    frontier_rel = weekly._paper_rel(FRONTIER_DOI)
    before = _appraise_paper(run, frontier_rel)
    seed_mechanism_papers(run.ledger, cli.owner_profile(OWNER, run.cal.now.date()))
    sources = scouts.quarantine_text(run.ledger, "demo-c4-sources", MECHANISM_SOURCES, author="retina")
    paper = next(p for p in PAPERS if p["doi"] == FRONTIER_DOI)
    text = f"{paper['title']}\n\n{abstract(paper)}\n\n{run.ledger.read_text(sources)}"
    record = cli.run_deep_dive(run.ledger, llm, frontier_rel, text, run.cal.now)
    after = _appraise_paper(run, frontier_rel)
    weak = _appraise_paper(run, weekly._paper_rel(WEAK_DOI))
    run.commit("evidence.deepdive", f"Deep dive {frontier_rel}: {record['decision']}", author="thalamus")
    view = lexicon.load_view(run.ledger)
    return {
        "paper": frontier_rel, "before_dive": before, "after_dive": after, "weak": weak,
        "dive": {"ref": deepdive.record_rel(record["paper_key"]), "decision": record["decision"],
                 "belief_id": record["belief_id"], "chain": record["chain"], "reasons": record["reasons"]},
        "lexicon": {"nodes": len(view["nodes"]), "links": len(view["links"]), "questions": len(view["questions"]),
                    "keywords": lexicon.open_keywords(view)},
    }


def step_review(run: Run, llm: Mapping[str, Any]) -> weekly.ReviewOutcome:
    packet = weekly.build_packet(run.store, run.ledger, run.cal.week, run.cfg, run.cal.now)
    outcome = weekly.run_review(
        run.cal.week, packet, guard_ctx=weekly.guard_context_from_packet(packet),
        rules=guardrails.load_rules(run.ledger), ledger=run.ledger, now=run.cal.now, **llm,
    )
    run.commit("ruling", f"Weekly review {run.cal.week}: {outcome.ruling}", author="cerebellum")
    return outcome


def _rhr_states(store: Store, day: str) -> list[metrics.RhrState]:
    series = store.daily("rhr", cli._shift(day, -40), cli._shift(day, 1))
    return [metrics.rhr_state(series, cli._shift(day, k)) for k in (-2, -1, 0)]


def step_guardrails(run: Run) -> dict:
    """Red flags for every review-week day (fixed German templates) and a banned-vocabulary scan."""
    first, end = run.cal.days[-7], cli._shift(run.cal.days[-1], 1)
    daily = {k: run.store.daily(k, cli._shift(first, -95), end) for k in metrics.DAILY_KEYS}
    items = run.store.checkins(first, end)
    alerts = [a for day in run.cal.days[-7:]
              for a in guardrails.red_flags(daily, _rhr_states(run.store, day), items, day, OWNER)]
    for a in alerts:
        run.ledger.append_jsonl("data/alerts.jsonl", {"level": a.level, "rule_id": a.rule_id, "date": a.date,
                                                      "data": dict(a.data),
                                                      "text_de": guardrails.render_alert(a, OWNER)}, author="pineal")
    bodies = [str((run.ledger.read_json(p) or {}).get("body", "")) for p in run.ledger.glob(f"reviews/{run.cal.week}/0*.json")]
    hits = sorted({t for b in bodies for t in guardrails.scan_text(b)})
    run.commit("alert", f"Red flags {run.cal.week}: {len(alerts)}")
    return {"alerts": [f"{a.level}:{a.rule_id}:{a.date}" for a in alerts], "banned_terms": hits}


def step_plan(run: Run) -> dict:
    sha = weekly.packet_sha(run.ledger.read_json(f"reviews/{run.cal.week}/packet.json"))
    versions = plans.ratify_due(run.ledger, run.cal.now, sha)
    run.commit("plan.ratified", f"Ratified {versions}" if versions else "No plan change due")
    pending = [p for p in (run.ledger.read_json(r) for r in run.ledger.glob("plans/pending/*.json"))
               if isinstance(p, dict) and p.get("status") == "pending"]
    return {"new_versions": versions, "current": plans.current(run.ledger)["version"],
            "still_pending": [f"{p['id']} ({p['tier']})" for p in pending]}


# N-of-1 experiment designed after the review: does dimming evening light raise the
# owner's own overnight HRV? Starts the Monday after the review week.
EXPERIMENT: Mapping[str, Any] = {
    "id": "exp-abendlicht", "belief_id": "demo-evening-light-hrv",
    "intervention": "Licht ab 21 Uhr gedimmt", "comparator": "Licht wie gewohnt",
    "outcome_key": "hrv_sdnn_ln", "source": "watch", "design": "paired_blocks",
    "block_days": 7, "washout_days": 2, "pairs": 6, "a_days_per_week": None,
    "swc": 0.1, "swc_harm": 0.1, "exclusions": [{"kind": "checkin_flag", "flag": "fever", "days_after": 2}], "covariates": [],
}


def _experiment_problems(store: Store, p: experiments.Protocol) -> list[str]:
    """The same eligibility check as `cli experiment design`, on the locked source."""
    first = cli._shift(p.start_date, -cli.BASELINE_DAYS)
    values = cli._locked_values(store, p, first, cli._shift(p.start_date, -1))
    baseline = cli._dense(values, first, cli.BASELINE_DAYS)
    try:
        mean90, sd90 = cli._mean90(store, p)
    except experiments.ProtocolError:
        return ["baseline_insufficient"]
    return experiments.eligibility(p, baseline, mean90, sd90)


def step_experiment(run: Run) -> dict:
    """Freeze the protocol and write the seeded allocation, only if the design is eligible."""
    start = cli._shift(run.cal.days[-1], 1)
    p = experiments.Protocol.from_dict({**EXPERIMENT, "start_date": start, "created_at": run.cal.now.isoformat()})
    problems = _experiment_problems(run.store, p)
    if problems:
        return {"id": p.id, "problems": problems, "days": 0, "start": start}
    experiments.write_protocol(run.ledger, p)
    run.commit("experiment.protocol", f"Protocol {p.id}", author="hippocampus")
    allocation = experiments.allocate(p, canonical_json(p.to_dict()))
    lines = ["date,arm,value"] + [f"{day},{arm}," for day, arm in allocation]
    run.ledger.write_text(f"experiments/{p.id}/data.csv", "\n".join(lines) + "\n", author=cli.CODE_AUTHOR)
    run.commit("experiment.allocation", f"Allocation {p.id}")
    return {"id": p.id, "problems": [], "days": len(allocation), "start": start}


# --------------------------------------------------------------------------
# Fake tracker poll: synthetic Garmin-like data through an in-process transport

GARMIN_ACTIVITY_ID = 9001
DEMO_GARMIN_KEY = "demo-garmin-key-not-a-secret"  # GarminSource refuses to start without a key


def _jwt(exp: float) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.demo"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _local_ms(dt: datetime) -> int:
    """Garmin's '*Local' epochs: local wall time read as if it were UTC."""
    return _ms(dt.replace(tzinfo=timezone.utc))


def _garmin_sleep(day: str) -> dict:
    start, end = _ts(day, -55), _ts(day, 6 * 60 + 20)
    return {"calendarDate": day, "sleepStartTimestampGMT": _ms(start), "sleepStartTimestampLocal": _local_ms(start),
            "sleepEndTimestampGMT": _ms(end), "sleepEndTimestampLocal": _local_ms(end), "sleepTimeSeconds": 25200,
            "deepSleepSeconds": 5000, "lightSleepSeconds": 14800, "remSleepSeconds": 5400, "awakeSleepSeconds": 1500,
            "averageRespirationValue": 14.2, "sleepScores": {"overall": {"value": 78}}}


def _garmin_daily(day: str) -> dict:
    return {"calendarDate": day, "calories": {"burnedActive": 560}, "heartRate": {"restingValue": 54},
            "bodyBattery": {"maxValue": 82, "minValue": 24}}


def _garmin_activity(cal: Calendar) -> dict:
    """The Saturday long run the watch recorded too (same session, second device)."""
    start = _ts(cal.days[-2], 9 * 60 + 30)
    gmt = start.astimezone(timezone.utc)
    return {"activityId": GARMIN_ACTIVITY_ID, "activityName": "Langer Lauf", "startTimeGMT": f"{gmt:%Y-%m-%d %H:%M:%S}",
            "startTimeLocal": f"{start:%Y-%m-%d %H:%M:%S}", "duration": 4200.0, "calories": 720, "bmrCalories": 95,
            "averageHR": 142, "maxHR": 161, "activityType": {"typeKey": "running"}, "startLatitude": 0.0,
            "startLongitude": 0.0}


class GarminFake:
    """In-process stand-in for the Garmin Personal API; only the window's days, no network."""

    def __init__(self, cal: Calendar) -> None:
        self.cal = cal
        self.calls = 0
        days = list(cal.days[-4:])
        self.routes: dict[tuple[str, str], Any] = {
            ("POST", garmin.TOKEN_PATH): {"accessToken": _jwt(cal.now.timestamp() + 86400)},
            ("GET", garmin.SLEEP_PATH): [_garmin_sleep(d) for d in days],
            ("GET", garmin.DAILY_PATH): [_garmin_daily(d) for d in days],
            ("GET", garmin.ACTIVITIES_PATH): [_garmin_activity(cal)],
        }
        for d in days:
            self.routes[("GET", garmin.HRV_PATH.format(day=d))] = {
                "hrvSummary": {"calendarDate": d, "lastNightAvg": 46, "weeklyAvg": 48, "status": "BALANCED"}}

    def __call__(self, req: Request) -> Response:
        self.calls += 1
        parts = urllib.parse.urlsplit(req.url)
        if parts.hostname != garmin.HOST:
            raise scouts.HostNotAllowed(f"demo has no network: {parts.hostname}")
        body = self.routes.get((req.method, parts.path))
        if body is None:
            return Response(404, {}, b"")
        return Response(200, {"content-type": "application/json"}, json.dumps(body).encode("utf-8"))


class _MemoryTokens:
    def __init__(self) -> None:
        self.tokens: dict[str, Any] = {}

    def load(self, name: str) -> Any:
        return self.tokens.get(name)

    def save(self, name: str, token: Any) -> None:
        self.tokens[name] = token

    def delete(self, name: str) -> None:
        self.tokens.pop(name, None)


def garmin_source(cal: Calendar, transport: GarminFake) -> garmin.GarminSource:
    return garmin.GarminSource(env={garmin.KEY_ENV: DEMO_GARMIN_KEY}, options={}, tz=DEMO_TZ, transport=transport,
                               token_store=_MemoryTokens(), clock=lambda: cal.now.timestamp(), sleep=lambda _s: None)


def garmin_connect_copy(cal: Calendar) -> dict:
    """What Garmin Connect writes back into Apple Health: the same run and resting HR, as HAE JSON."""
    run_ = _garmin_activity(cal)
    start = _ts(cal.days[-2], 9 * 60 + 30)
    rhr = [{"date": _hae(_ts(d, 12 * 60)), "qty": 54.0, "source": "Garmin Connect"} for d in cal.days[-4:]]
    workout = {"id": "hae-garmin-copy", "name": "Outdoor Run", "start": _hae(start),
               "end": _hae(start + timedelta(seconds=run_["duration"])), "duration": run_["duration"],
               "source": "Garmin Connect"}
    return {"data": {"metrics": [{"name": "resting_heart_rate", "units": "count/min", "data": rhr}],
                     "workouts": [workout]}}


def _canonical(store: Store, days: Sequence[str]) -> dict[tuple[str, str], tuple[float | None, str]]:
    return {(r.date, r.key): (r.value, r.source) for key in metrics.DAILY_KEYS
            for r in store.daily(key, days[0], cli._shift(days[-1], 1))}


def _changed(before: Mapping, after: Mapping) -> list[str]:
    """Daily keys whose canonical value existed before and differs now (a double count would show here)."""
    return sorted({k for (d, k), (v, _) in before.items() if v is not None and after.get((d, k), (None,))[0] != v})


def _inserted(result: Mapping[str, Any]) -> dict[str, int]:
    return {k: v for k, v in result["counts"].items() if k != "deleted"}


def step_tracker(run: Run) -> dict:
    """Poll the fake Garmin twice (overlapping windows) and ingest Garmin's Apple Health copy with the guard on."""
    days = list(run.cal.days[-4:])
    run.ledger.write_json(tracker_run.CONFIG_PATH, {"enabled": ["garmin"], "garmin": {"poll_minutes": 60}},
                          author=cli.CODE_AUTHOR)
    before = _canonical(run.store, days)
    transport = GarminFake(run.cal)
    source = garmin_source(run.cal, transport)
    first = tracker_run.poll_one(run.store, source, now=run.cal.now, tz=DEMO_TZ)
    second = tracker_run.poll_one(run.store, source, now=run.cal.now, tz=DEMO_TZ)
    copy = ingest.parse_payload(garmin_connect_copy(run.cal), {"Garmin Connect": "garmin"}, frozenset({"garmin"}))
    stored_copy = cli.store_batch(run.store, copy)
    cli.finalize_days(run.store, run.ledger, run.cfg, days, run.cal.now)
    after = _canonical(run.store, days)
    run.commit("data.daily", f"Garmin poll merged, {days[0]}..{days[-1]}")
    workouts = [w for w in run.store.workouts(f"{days[0]}T00:00") if w.source == "garmin"]
    return {
        "status": first["status"], "window": first["window"], "first": _inserted(first), "second": _inserted(second),
        "repoll_deleted": second["counts"]["deleted"],
        "requests": transport.calls, "hae_copy_dropped": copy.skipped.get("api_source", 0),
        "hae_copy_stored": sum(stored_copy.values()), "garmin_workouts": len(workouts),
        "changed_keys": _changed(before, after),
        "filled_keys": sorted({k for (d, k), (v, src) in after.items() if src == "garmin" and v is not None}),
    }


def step_dashboard(run: Run) -> list[Path]:
    return dashboard.build(run.store, run.ledger, run.root / "site", run.cal.now, run.cfg, demo=True)


# --------------------------------------------------------------------------
# Entry


def _check_out(root: Path) -> None:
    if cli._inside(root, cli.ENGINE_ROOT):
        raise cli.UsageError(f"demo output must lie outside the engine repo: {root}")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise cli.UsageError(f"demo output must be a new or empty directory: {root}")


def _git_available() -> bool:
    return shutil.which("git") is not None


def run_demo(out: Path | None = None, *, days: int = 56, seed: int = 7, end: date | None = None,
             git: bool | None = None) -> dict:
    """Run the showcase pipeline into a fresh ledger; returns a JSON-ready summary."""
    if days < MIN_DAYS:
        raise cli.UsageError(f"days must be >= {MIN_DAYS}")
    if out is not None:
        _check_out(out)
    cal = calendar(days, end)
    root = _demo_root(out)
    use_git = _git_available() if git is None else git
    ledger = _setup_ledger(root, cal, use_git)
    store = Store(root / ingest.PRIVATE_DIR / "health.sqlite")
    store.migrate()
    try:
        return _pipeline(Run(root, cal, ledger, store, cli.metrics_config(OWNER, None, DEMO_TZ), use_git), seed)
    finally:
        store.close()


def _pipeline(run: Run, seed: int) -> dict:
    run.commit("setup", "Demo ledger: fictional profile, goals and plan v0001")
    counts = step_ingest(run, seed)
    step_metrics(run)
    rec = insula.weekly_reconciliation(run.store, run.cal.week, run.cfg)
    provider = fake_provider(run.cal.week, rec)
    llm = _llm(provider, run.ledger, run.cal)
    insula_out = step_insula(run, llm)
    gates = step_evidence(run, llm)
    frontier = step_frontier(run, llm)
    outcome = step_review(run, llm)
    guards = step_guardrails(run)
    plan = step_plan(run)
    experiment = step_experiment(run)
    tracker = step_tracker(run)
    pages = step_dashboard(run)
    return {
        "ledger": str(run.root), "dashboard": str(pages[0]), "week": run.cal.week,
        "days": [run.cal.days[0], run.cal.days[-1]], "counts": counts,
        "conflicts": _rule_counts(rec), "insula_items": len(insula_out["items"]), "papers": gates,
        "review": {"ruling": outcome.ruling, "tier": outcome.verdict.tier if outcome.verdict else None,
                   "pending_id": outcome.pending_id, "messages": list(outcome.messages),
                   "open_blockers": list(outcome.open_blockers), "aborted": outcome.aborted},
        "guardrails": guards, "plan": plan, "experiment": experiment, "frontier": frontier, "tracker": tracker,
        "llm_calls": len(provider.calls), "commits": run.commits,
    }


def _rule_counts(rec: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in rec["conflicts"]:
        out[c["rule_id"]] = out.get(c["rule_id"], 0) + 1
    return out


def summary_lines(s: Mapping[str, Any]) -> Iterator[str]:
    yield f"Demo ledger: {s['ledger']} (synthetic data, fictional person)"
    yield f"Dashboard:   {s['dashboard']}"
    yield f"Data:        {s['days'][0]} .. {s['days'][1]}; " + ", ".join(f"{k}={v}" for k, v in s["counts"].items())
    yield "Conflicts:   " + (", ".join(f"{k}={v}" for k, v in sorted(s["conflicts"].items())) or "none")
    yield "Papers:      " + ", ".join(f"{Path(k).stem}={v}" for k, v in sorted(s["papers"].items()))
    r = s["review"]
    yield (f"Review {s['week']}: ruling={r['ruling']} tier={r['tier']} messages={len(r['messages'])}"
           + (f" aborted={r['aborted']}" if r["aborted"] else ""))
    yield "Red flags:   " + (", ".join(s["guardrails"]["alerts"]) or "none")
    p = s["plan"]
    yield f"Plan:        v{p['current']:04d} current; new {p['new_versions'] or 'none'}; pending {p['still_pending'] or 'none'}"
    e = s["experiment"]
    yield (f"Experiment:  {e['id']} " + (f"not eligible: {', '.join(e['problems'])}" if e["problems"]
                                          else f"protocol frozen, {e['days']} days from {e['start']}"))
    yield from _frontier_lines(s["frontier"])
    yield _tracker_line(s["tracker"])
    yield f"LLM calls:   {s['llm_calls']} to FakeProvider, 0 network requests; git commits: {s['commits']}"


def _frontier_lines(f: Mapping[str, Any]) -> Iterator[str]:
    yield (f"Frontier:    {Path(f['paper']).stem} {f['before_dive']['status']} -> {f['after_dive']['status']} "
           f"(certainty {f['after_dive']['certainty']}, promise {f['before_dive']['promise']} -> "
           f"{f['after_dive']['promise']}); deep dive: {f['dive']['decision']} {f['dive']['belief_id']} "
           f"(weakest link {f['dive']['chain']['weakest']}, human coverage {f['dive']['chain']['coverage']:.2f})")
    w = f["weak"]
    yield (f"Weak paper:  {WEAK_DOI} certainty {w['certainty']} -> {w['certainty_capped']}, "
           f"promise {w['promise']}, flags {', '.join(w['flags']) or 'none'}")
    lx = f["lexicon"]
    yield (f"Lexicon:     {lx['nodes']} nodes, {lx['links']} links, {lx['questions']} open questions; "
           f"scout keywords {', '.join(lx['keywords']) or 'none'}")


def _tracker_line(t: Mapping[str, Any]) -> str:
    return (f"Tracker:     garmin {t['status']}, " + ", ".join(f"{k}={v}" for k, v in t["first"].items() if v)
            + f"; repoll replaced {t['repoll_deleted']} rows with "
            + f"{'the same' if t['first'] == t['second'] else 'different'} counts; Apple Health copy dropped "
            f"{t['hae_copy_dropped']} rows; canonical keys changed: {', '.join(t['changed_keys']) or 'none'}")

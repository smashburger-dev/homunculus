"""Deterministic conflict detection between check-ins and tracker data.

Code finds every conflict; the Insula LLM only explains them. Rules:

- R1_sustained: |S - O| >= 1.5 on at least 3 of 5 consecutive days, same sign.
- R2_single_day: one day with |S - O| >= 2.5, S and O of opposite sign, each |.| >= 1.
- R3_session_residual: sRPE load regressed on Edwards TRIMP over 60 days;
  a session whose residual z is >= 2 felt much harder than heart rate says.
- DATA_GAP: a day whose inputs fail the quality check. Such days never take
  part in R1 or R2, so a gap is reported instead of a guess.

O_d is the mean of the HRV m7 z, the negated resting-HR z and the TST z.
S_d is the mean item z of the morning check-in against each item's own
28-day baseline. Pure functions, except weekly_reconciliation, which reads
the store. No LLM calls.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from homunculus import checkins, metrics, stats
from homunculus.metrics import MetricsConfig
from homunculus.store import CheckinItem, Store, Workout

RULE_R1 = "R1_sustained"
RULE_R2 = "R2_single_day"
RULE_R3 = "R3_session_residual"
RULE_GAP = "DATA_GAP"
RULE_IDS = (RULE_R1, RULE_R2, RULE_R3, RULE_GAP)

OBJECTIVE_KEYS = ("hrv_m7_z", "rhr_z", "tst_z")
SUBJECTIVE_PREFIX = "subj_"

R1_GAP = 1.5
R1_WINDOW = 5
R1_MIN_HITS = 3
R2_GAP = 2.5
R2_SIDE = 1.0
R3_Z = 2.0
R3_MIN_SESSIONS = 15
R3_WINDOW_DAYS = 60
SUBJECTIVE_WINDOW = 28
MAX_OWNER_QUESTIONS = 3

_Z_CAP = 3.0
_TST_MIN_BASE = 14
_ROUND = 4
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


# --------------------------------------------------------------------------
# Records


@dataclass(frozen=True)
class DayScore:
    date: str
    objective: float | None
    subjective: float | None
    gap: float | None
    quality_ok: bool
    inputs: dict[str, float | None]


@dataclass(frozen=True)
class Conflict:
    id: str
    rule_id: str
    window: tuple[str, str]
    numbers: dict[str, float | None]
    sign: int
    session_id: str | None


def conflict_id(rule_id: str, window: tuple[str, str], session_id: str | None) -> str:
    """sha256('rule_id|start|end|session_id')[:10]; session_id None hashes as ''."""
    key = "|".join((rule_id, window[0], window[1], session_id or ""))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


def _conflict(rule_id: str, window: tuple[str, str], numbers: dict[str, float | None],
              sign: int, session_id: str | None = None) -> Conflict:
    return Conflict(conflict_id(rule_id, window, session_id), rule_id, window, numbers, sign, session_id)


def _shift(day: str, n: int) -> str:
    return (Date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


# --------------------------------------------------------------------------
# Day scores


def _objective(objective_z: Mapping[str, float | None]) -> float | None:
    values = [objective_z.get(k) for k in OBJECTIVE_KEYS]
    if any(v is None for v in values):
        return None
    hrv, rhr, tst = values
    return stats.mean([hrv, -rhr, tst])


def day_score(objective_z: Mapping[str, float | None], subjective_z: Mapping[str, float | None],
              date: str) -> DayScore:
    """O = mean(hrv_m7_z, -rhr_z, tst_z), needs all 3; S = checkins.composite (needs 3 items).

    quality_ok is False when either side is None; gap = S - O otherwise.
    """
    objective = _objective(objective_z)
    subjective = checkins.composite(subjective_z)
    ok = objective is not None and subjective is not None
    gap = subjective - objective if ok else None
    inputs = {k: objective_z.get(k) for k in OBJECTIVE_KEYS}
    inputs.update({SUBJECTIVE_PREFIX + k: subjective_z[k] for k in sorted(subjective_z)})
    return DayScore(date, objective, subjective, gap, ok, inputs)


# --------------------------------------------------------------------------
# Day rules


def _r1_window(by_date: Mapping[str, DayScore], end: str) -> tuple[int, list[str]]:
    """(sign, hit dates) when the 5 days ending at end fire R1, else (0, [])."""
    window = [by_date.get(_shift(end, k - R1_WINDOW + 1)) for k in range(R1_WINDOW)]
    if any(s is None or not s.quality_ok for s in window):
        return 0, []
    for sign in (1, -1):
        hits = [s.date for s in window if abs(s.gap) >= R1_GAP and _sign(s.gap) == sign]
        if len(hits) >= R1_MIN_HITS:
            return sign, hits
    return 0, []


def _r1_episodes(by_date: Mapping[str, DayScore]) -> list[tuple[int, set[str]]]:
    """Firing windows of one sign that overlap merge into one episode."""
    episodes: list[tuple[int, set[str]]] = []
    last_end: str | None = None
    for end in sorted(by_date):
        sign, hits = _r1_window(by_date, end)
        if not sign:
            continue
        start = _shift(end, 1 - R1_WINDOW)
        if episodes and episodes[-1][0] == sign and last_end is not None and start <= last_end:
            episodes[-1][1].update(hits)
        else:
            episodes.append((sign, set(hits)))
        last_end = end
    return episodes


def _r1_conflict(sign: int, hit_dates: set[str], by_date: Mapping[str, DayScore]) -> Conflict:
    days = sorted(hit_dates)
    gaps = [by_date[d].gap for d in days]
    numbers = {
        "hit_days": float(len(days)),
        "mean_gap": stats.mean(gaps),
        "max_abs_gap": max(abs(g) for g in gaps),
        "threshold": R1_GAP,
    }
    return _conflict(RULE_R1, (days[0], days[-1]), numbers, sign)


def _r2_fires(s: DayScore) -> bool:
    if not s.quality_ok or abs(s.gap) < R2_GAP:
        return False
    opposite = _sign(s.subjective) * _sign(s.objective) < 0
    return opposite and abs(s.subjective) >= R2_SIDE and abs(s.objective) >= R2_SIDE


def _r2_conflict(s: DayScore) -> Conflict:
    numbers = {"gap": s.gap, "subjective": s.subjective, "objective": s.objective, "threshold": R2_GAP}
    return _conflict(RULE_R2, (s.date, s.date), numbers, _sign(s.gap))


def _gap_runs(scores: Sequence[DayScore]) -> list[list[DayScore]]:
    """Runs of calendar-consecutive days that fail the quality check."""
    runs: list[list[DayScore]] = []
    for s in scores:
        if s.quality_ok:
            continue
        if runs and _shift(runs[-1][-1].date, 1) == s.date:
            runs[-1].append(s)
        else:
            runs.append([s])
    return runs


def _gap_conflict(run: Sequence[DayScore]) -> Conflict:
    numbers = {"days": float(len(run))}
    for key in OBJECTIVE_KEYS:
        numbers[f"missing_{key}"] = float(sum(s.inputs.get(key) is None for s in run))
    numbers["missing_subjective"] = float(sum(s.subjective is None for s in run))
    return _conflict(RULE_GAP, (run[0].date, run[-1].date), numbers, 0)


def detect_day_conflicts(scores: Sequence[DayScore]) -> list[Conflict]:
    """R1, R2 and DATA_GAP over day scores, sorted by window then rule.

    A 5-day window with any day missing or failing quality never fires R1;
    those failing days are reported as DATA_GAP runs instead.
    """
    by_date = {s.date: s for s in scores}
    ordered = [by_date[d] for d in sorted(by_date)]
    found = [_r1_conflict(sign, hits, by_date) for sign, hits in _r1_episodes(by_date)]
    found += [_r2_conflict(s) for s in ordered if _r2_fires(s)]
    found += [_gap_conflict(run) for run in _gap_runs(ordered)]
    return sorted(found, key=lambda c: (c.window, c.rule_id))


# --------------------------------------------------------------------------
# Session rule


def _finite(*xs: object) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def _recent_sessions(sessions: Sequence[tuple[str, str, float, float]]) -> list[tuple[str, str, float, float]]:
    """Finite rows within R3_WINDOW_DAYS of the newest session, sorted by (date, id)."""
    rows = [(w, d, float(y), float(x)) for w, d, y, x in sessions if _finite(y, x)]
    if not rows:
        return []
    cutoff = _shift(max(d for _, d, _, _ in rows), 1 - R3_WINDOW_DAYS)
    return sorted((r for r in rows if r[1] >= cutoff), key=lambda r: (r[1], r[0]))


def _fit(rows: Sequence[tuple[str, str, float, float]]) -> tuple[float, float, float] | None:
    """OLS srpe ~ trimp; None without spread in TRIMP or in the residuals."""
    try:
        slope, intercept, rsd = stats.ols([r[3] for r in rows], [r[2] for r in rows])
    except ValueError:
        return None
    return (slope, intercept, rsd) if rsd > 0 else None


def _session_conflict(row: tuple[str, str, float, float], fit: tuple[float, float, float],
                      n: int) -> Conflict | None:
    workout_id, day, srpe, trimp = row
    slope, intercept, rsd = fit
    predicted = intercept + slope * trimp
    z = (srpe - predicted) / rsd
    if z < R3_Z:
        return None
    numbers = {
        "srpe_load": srpe, "edwards_trimp": trimp, "predicted_srpe": predicted,
        "residual": srpe - predicted, "residual_z": z, "slope": slope, "intercept": intercept,
        "residual_sd": rsd, "n_sessions": float(n), "threshold": R3_Z,
    }
    return _conflict(RULE_R3, (day, day), numbers, 1, workout_id)


def detect_session_conflicts(sessions: Sequence[tuple[str, str, float, float]]) -> list[Conflict]:
    """R3 over (workout_id, date, srpe_load, edwards_trimp) rows.

    Uses the 60 days ending at the newest session, needs >= 15 sessions and
    flags residual z >= 2 (felt harder than heart rate says).
    """
    rows = _recent_sessions(sessions)
    if len(rows) < R3_MIN_SESSIONS:
        return []
    fit = _fit(rows)
    if fit is None:
        return []
    found = (_session_conflict(r, fit, len(rows)) for r in rows)
    return [c for c in found if c is not None]


# --------------------------------------------------------------------------
# Store readers


def _capped(z: float | None) -> float | None:
    return None if z is None else max(-_Z_CAP, min(_Z_CAP, z))


def _hrv_z(store: Store, day: str) -> float | None:
    """HRV m7 z with illness days left out of the baseline, as in metrics.baselines."""
    since = _shift(day, -67)
    ill = metrics.illness_days(store, since, day)
    st = metrics.hrv_state(store.daily("hrv_sdnn_ln", since, _shift(day, 1)), day, exclude=ill)
    return stats.zscore(st.m7, st.mu60, st.sd60, cap=_Z_CAP)


def _rhr_z(store: Store, day: str) -> float | None:
    return _capped(metrics.rhr_state(store.daily("rhr", _shift(day, -36), _shift(day, 1)), day).z)


def _tst_z(store: Store, day: str) -> float | None:
    """Today's TST z against days -28..-1, needs 14 baseline nights."""
    rows = store.daily("tst_h", _shift(day, -28), _shift(day, 1))
    by_date = {r.date: r.value for r in rows if r.value is not None}
    today = by_date.pop(day, None)
    base = list(by_date.values())
    if today is None or len(base) < _TST_MIN_BASE:
        return None
    return stats.zscore(today, stats.mean(base), stats.sd(base), cap=_Z_CAP)


def _score_for(store: Store, day: str) -> DayScore:
    objective_z = {"hrv_m7_z": _hrv_z(store, day), "rhr_z": _rhr_z(store, day), "tst_z": _tst_z(store, day)}
    return day_score(objective_z, checkins.subjective_scores(store, day, SUBJECTIVE_WINDOW), day)


def _parse_ts(ts: str, tz: str) -> datetime:
    """Aware datetime from ISO-8601 or Health Auto Export 'YYYY-MM-DD HH:MM:SS +0100'."""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S %z")
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz))


def _local_day(ts: str, tz: str) -> str:
    return _parse_ts(ts, tz).astimezone(ZoneInfo(tz)).date().isoformat()


def _minutes(w: Workout, tz: str) -> float:
    if w.duration_s is not None:
        return w.duration_s / 60.0
    return (_parse_ts(w.end, tz) - _parse_ts(w.start, tz)).total_seconds() / 60.0


def _latest_rpe(items: Sequence[CheckinItem]) -> dict[str, float]:
    best: dict[str, tuple[str, float]] = {}
    for it in sorted(items, key=lambda i: i.ts):
        if it.ref and it.value is not None:
            best[it.ref] = (it.ts, it.value)
    return {ref: value for ref, (_, value) in best.items()}


def _session_row(w: Workout, day: str, rpe: Mapping[str, float],
                 cfg: MetricsConfig) -> tuple[str, str, float, float] | None:
    trimp = metrics.edwards_trimp(w.hr_series, cfg.hrmax)
    minutes = _minutes(w, cfg.tz)
    value = rpe.get(w.id)
    if trimp is None or minutes < 0 or value is None or not 0 <= value <= 10:
        return None
    return w.id, day, metrics.srpe_load(value, minutes), trimp


def _workouts_by_day(store: Store, since: str, end: str, tz: str) -> dict[str, list[Workout]]:
    out: dict[str, list[Workout]] = {}
    for w in store.workouts(_shift(since, -1), _shift(end, 2)):
        day = _local_day(w.start, tz)
        if since <= day <= end:
            out.setdefault(day, []).append(w)
    return out


def _session_tuples(store: Store, end: str, cfg: MetricsConfig) -> list[tuple[str, str, float, float]]:
    """Canonical-source sessions with both sRPE and Edwards TRIMP over the 60 days ending at end."""
    since = _shift(end, 1 - R3_WINDOW_DAYS)
    rpe = _latest_rpe(store.checkins(_shift(since, -1), _shift(end, 3), item=checkins.SESSION_ITEM))
    rows: list[tuple[str, str, float, float]] = []
    for day, found in sorted(_workouts_by_day(store, since, end, cfg.tz).items()):
        source = metrics.pick_source(found, cfg.priority("load_srpe"))
        candidates = (_session_row(w, day, rpe, cfg) for w in found if w.source == source)
        rows += [r for r in candidates if r is not None]
    return rows


# --------------------------------------------------------------------------
# Weekly file


def _week_start(week: str) -> str:
    match = _WEEK_RE.match(week)
    if match is None:
        raise ValueError(f"week must look like 2026-W47, got {week!r}")
    return Date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1).isoformat()


def _r(x: float | None) -> float | None:
    return None if x is None else round(float(x), _ROUND)


def _day_dict(s: DayScore) -> dict:
    return {
        "date": s.date, "objective": _r(s.objective), "subjective": _r(s.subjective), "gap": _r(s.gap),
        "quality_ok": s.quality_ok, "inputs": {k: _r(v) for k, v in s.inputs.items()},
    }


def _conflict_dict(c: Conflict) -> dict:
    return {
        "id": c.id, "rule_id": c.rule_id, "window": list(c.window),
        "numbers": {k: _r(v) for k, v in c.numbers.items()}, "sign": c.sign, "session_id": c.session_id,
    }


def _flagged(c: Conflict) -> dict:
    keys = ("srpe_load", "edwards_trimp", "predicted_srpe", "residual_z")
    return {"conflict_id": c.id, "workout_id": c.session_id, "date": c.window[0],
            **{k: _r(c.numbers[k]) for k in keys}}


def weekly_reconciliation(store: Store, week: str, cfg: MetricsConfig) -> dict:
    """Content of data/conflicts/<week>.json.

    days covers the ISO week plus the 4 lead-in days R1 windows need. conflicts
    keeps day conflicts whose window ends inside or after the week start and
    session conflicts dated inside the week; rounded to 4 decimals.
    """
    start = _week_start(week)
    end = _shift(start, 6)
    scores = [_score_for(store, _shift(start, k)) for k in range(1 - R1_WINDOW, 7)]
    day_conflicts = [c for c in detect_day_conflicts(scores) if c.window[1] >= start]
    sessions = [c for c in detect_session_conflicts(_session_tuples(store, end, cfg))
                if start <= c.window[0] <= end]
    return {
        "week": week,
        "days": [_day_dict(s) for s in scores],
        "conflicts": [_conflict_dict(c) for c in day_conflicts + sessions],
        "sessions_flagged": [_flagged(c) for c in sessions],
    }


# --------------------------------------------------------------------------
# LLM output contract


def _output_schema() -> dict:
    item = {
        "type": "object",
        "properties": {
            "conflict_id": {"type": "string", "minLength": 10, "maxLength": 10},
            "interpretation_de": {"type": "string", "minLength": 1, "maxLength": 1200},
            "noisier_signal": {"type": "string", "enum": ["subjective", "objective", "unclear"]},
            "question_for_owner_de": {"type": ["string", "null"], "maxLength": 300},
            "refs": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
        },
        "required": ["conflict_id", "interpretation_de", "noisier_signal", "question_for_owner_de", "refs"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "description": f"One item per conflict id; at most {MAX_OWNER_QUESTIONS} items carry a question.",
        "properties": {"items": {"type": "array", "items": item}},
        "required": ["items"],
        "additionalProperties": False,
    }


INSULA_OUTPUT_SCHEMA: dict = _output_schema()


def _id_errors(ids: Sequence[object], expected: set[str]) -> list[str]:
    errors = [f"unknown conflict_id {i!r}" for i in ids if i not in expected]
    errors += [f"missing conflict_id {i!r}" for i in sorted(expected - set(ids))]
    duplicates = {i for i in ids if ids.count(i) > 1}
    return errors + [f"duplicate conflict_id {i!r}" for i in sorted(duplicates, key=str)]


def output_errors(output: Mapping, reconciliation: Mapping) -> list[str]:
    """Rules JSON Schema cannot express: exactly one item per conflict id, <= 3 questions."""
    items = output.get("items") or []
    expected = {c["id"] for c in reconciliation.get("conflicts", [])}
    errors = _id_errors([it.get("conflict_id") for it in items], expected)
    asked = sum(1 for it in items if it.get("question_for_owner_de"))
    if asked > MAX_OWNER_QUESTIONS:
        errors.append(f"{asked} owner questions, at most {MAX_OWNER_QUESTIONS}")
    return errors

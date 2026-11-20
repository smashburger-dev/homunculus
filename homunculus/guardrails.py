"""Safety code: plan-diff tiers, exercise admission, nutrition floors, red flags.

Everything here is pure over passed-in data except ``load_rules``, which reads
the ledger config. Every check fails closed: a value it cannot verify counts
as a violation, never as a pass.

* ``check_diff`` sorts a typed plan diff into ``auto``, ``notify``,
  ``approve`` or ``reject``. The strictest finding wins.
* ``exercise_admissible`` checks an exercise feature record against the
  owner's injury constraints.
* ``supplement_check`` and ``forbidden_match`` enforce the forbidden list,
  the tested-product allowlist and the interaction table.
* ``ed_watch`` and ``energy_availability`` guard the nutrition floor.
* ``red_flags`` and ``event_alerts`` compute alerts; ``render_alert`` turns
  them into fixed German text. No LLM sits in the alert path.
* ``scan_text`` finds diagnostic vocabulary in bot output.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from dataclasses import dataclass, field, fields
from datetime import date as Date
from datetime import timedelta
from string import Template
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence

from homunculus.checkins import SYMPTOM_LABELS_DE, URGENT_SYMPTOMS, restrictive_language, symptom_flags
from homunculus.metrics import RhrState
from homunculus.store import CheckinItem, DailyValue, HealthEvent, Ledger

TIERS = ("auto", "notify", "approve", "reject")
ALERT_LEVELS = frozenset({"urgent", "prompt"})

HARD_RPE = 7.0  # a session with target_rpe >= 7 counts as hard
KCAL_APPROVE_PCT = 5.0  # kcal changes above this need approval
KCAL_PER_KG = 7700.0  # rule-of-thumb energy content of 1 kg body mass (UNVERIFIED)
NOVEL_MAX_RPE = 6.0
NOVEL_MAX_SETS = 2
EXERCISE_FEATURES_KEY = "exercise_features"  # reserved key in GuardContext.injury_constraints
REQUIRED_FEATURES = ("pattern", "contraction", "energy_system", "joint_load")
JOINT_LEVELS = ("none", "low", "moderate", "high")
TESTED_PROGRAMS = frozenset({"kolner liste", "koelner liste", "informed sport"})
PAPER_REF_PREFIX = "evidence/papers/"

DEFAULT_FORBIDDEN: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "prescription_drug": (
        "prescription drug", "verschreibungspflichtig", "rezeptpflichtig", "metformin",
        "semaglutide", "semaglutid", "tirzepatide", "tirzepatid", "beta blocker", "betablocker",
    ),
    "wada_prohibited": (
        "anabolic steroid", "anabole steroide", "anabolika", "testosterone", "testosteron",
        "nandrolone", "nandrolon", "trenbolone", "trenbolon", "stanozolol", "clenbuterol",
        "epo", "erythropoietin", "sarm", "sarms", "ostarine", "ligandrol", "cardarine",
        "growth hormone", "wachstumshormon", "hgh", "dmaa", "ephedrine", "ephedrin",
        "meldonium", "higenamine", "higenamin", "dnp",
    ),
    "dehydration_cut": (
        "water cut", "dehydration cut", "wasserentzug", "entwassern", "entwasserung",
        "diuretic", "diuretics", "diuretika", "furosemide", "furosemid",
    ),
    "altitude_protocol": (
        "altitude tent", "hypoxic tent", "hypoxiezelt", "hohenzelt", "intermittent hypoxia",
        "intermittierende hypoxie", "acetazolamide", "acetazolamid", "diamox",
    ),
})


# --------------------------------------------------------------------------
# Rules


@dataclass(frozen=True)
class Rules:
    load_auto_pct: float = 10
    load_approve_pct: float = 20
    two_week_reject_pct: float = 30
    max_added_hard_sessions: int = 1
    return_after_days_off: int = 7
    return_cap_pct: float = 70
    cumulative_3wk_pct: float = 20
    ea_floor: float = 30
    ea_approve: float = 35
    kcal_step_pct: float = 10
    kcal_step_abs: float = 250
    max_loss_pct_bw_week: float = 1.0
    max_fast_h: float = 24
    novel_per_session: int = 1
    novel_per_week: int = 2
    pull_push: tuple[float, float] = (1.0, 2.0)
    forbidden: Mapping[str, Sequence[str]] = field(default_factory=lambda: DEFAULT_FORBIDDEN)
    supplements: Mapping[str, Mapping] = field(default_factory=lambda: MappingProxyType({}))
    interactions: Sequence[Mapping] = ()
    meds: Sequence[str] = ()


_COLLECTION_FIELDS = frozenset({"pull_push", "forbidden", "supplements", "interactions", "meds"})
_SCALAR_FIELDS: Mapping[str, type] = MappingProxyType({
    f.name: (int if f.type == "int" else float) for f in fields(Rules) if f.name not in _COLLECTION_FIELDS
})


def _number(key: str, raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError(f"guardrails.json: {key} must be a finite number")
    return raw


def _scalar_overrides(cfg: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(cfg) - set(_SCALAR_FIELDS) - {"pull_push"})
    if unknown:
        raise ValueError(f"guardrails.json: unknown keys {unknown}")
    out: dict[str, Any] = {k: _SCALAR_FIELDS[k](_number(k, v)) for k, v in cfg.items() if k != "pull_push"}
    if "pull_push" in cfg:
        lo, hi = (float(_number("pull_push", x)) for x in cfg["pull_push"])
        out["pull_push"] = (lo, hi)
    return out


def _read_object(ledger: Ledger, rel: str) -> dict:
    raw = ledger.read_json(rel, default={})
    if not isinstance(raw, dict):
        raise ValueError(f"{rel} must be a JSON object")
    return raw


def _string_list(rel: str, raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{rel}: expected a list of strings")
    return tuple(raw)


def _forbidden(ledger: Ledger) -> Mapping[str, tuple[str, ...]]:
    """Built-in categories plus config/forbidden.json; config can add terms, never remove them."""
    merged = {k: list(v) for k, v in DEFAULT_FORBIDDEN.items()}
    for cat, terms in _read_object(ledger, "config/forbidden.json").items():
        merged.setdefault(cat, []).extend(_string_list("config/forbidden.json", terms))
    return MappingProxyType({k: tuple(dict.fromkeys(v)) for k, v in merged.items()})


def _supplement_config(ledger: Ledger) -> tuple[Mapping[str, Mapping], tuple[Mapping, ...]]:
    cfg = _read_object(ledger, "config/supplements.json")
    allow, inter = cfg.get("allowlist", {}), cfg.get("interactions", [])
    if not isinstance(allow, dict) or not all(isinstance(v, dict) for v in allow.values()):
        raise ValueError("config/supplements.json: allowlist must map names to records")
    if not isinstance(inter, list) or not all(isinstance(r, dict) for r in inter):
        raise ValueError("config/supplements.json: interactions must be a list of records")
    return MappingProxyType(dict(allow)), tuple(inter)


def _meds(ledger: Ledger) -> tuple[str, ...]:
    raw = ledger.read_json("profile/meds.json", default=[])
    if isinstance(raw, dict):
        raw = raw.get("meds", [])
    names = [m.get("name") if isinstance(m, dict) else m for m in raw] if isinstance(raw, list) else None
    return _string_list("profile/meds.json", names)


def load_rules(ledger: Ledger) -> Rules:
    """Rules from config/guardrails.json, forbidden.json, supplements.json and profile/meds.json."""
    supplements, interactions = _supplement_config(ledger)
    return Rules(
        **_scalar_overrides(_read_object(ledger, "config/guardrails.json")),
        forbidden=_forbidden(ledger), supplements=supplements,
        interactions=interactions, meds=_meds(ledger),
    )


# --------------------------------------------------------------------------
# Names and term matching


def normalize_name(s: str) -> str:
    """Casefold, strip diacritics and punctuation, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", s.casefold())
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", bare).split())


def _has_phrase(haystack: str, needle: str) -> bool:
    """Whole-word phrase containment on normalized strings."""
    return bool(needle) and f" {needle} " in f" {haystack} "


def forbidden_match(name: str, rules: Rules) -> str | None:
    """Category of the first forbidden term contained in name as whole words."""
    text = normalize_name(name)
    for category, terms in rules.forbidden.items():
        if any(_has_phrase(text, normalize_name(t)) for t in terms):
            return category
    return None


# --------------------------------------------------------------------------
# Supplements and nutrition


def _allowlisted(name: str, rules: Rules) -> tuple[str, Mapping] | None:
    """(canonical name, record) for name or one of a record's aliases."""
    key = normalize_name(name)
    for canon, record in rules.supplements.items():
        aliases = [canon, *record.get("aliases", ())]
        if key in {normalize_name(a) for a in aliases}:
            return canon, record
    return None


def _dose_problems(dose: float, unit: str, record: Mapping) -> list[str]:
    if isinstance(dose, bool) or not isinstance(dose, (int, float)) or not math.isfinite(dose) or dose <= 0:
        return ["dose_invalid"]
    if normalize_name(unit) != normalize_name(str(record.get("unit", ""))):
        return ["unit_mismatch"]
    max_dose = record.get("max_dose")
    if not isinstance(max_dose, (int, float)) or dose > max_dose:
        return ["dose_above_max"]
    return []


def _interactions(names: Iterable[str], rules: Rules) -> list[str]:
    keys = {normalize_name(n) for n in names}
    meds = [normalize_name(m) for m in rules.meds]
    hits = []
    for row in rules.interactions:
        med = normalize_name(str(row.get("med", "")))
        if normalize_name(str(row.get("supplement", ""))) in keys and any(_has_phrase(m, med) for m in meds):
            hits.append(f"interaction:{med}")
    return list(dict.fromkeys(hits))


def supplement_check(name: str, dose: float, unit: str, tested_by: str | None, rules: Rules) -> list[str]:
    """Problem ids; empty means the supplement passes every check."""
    problems: list[str] = []
    category = forbidden_match(name, rules)
    if category:
        problems.append(f"forbidden:{category}")
    hit = _allowlisted(name, rules)
    if hit is None:
        problems.append("not_allowlisted")
    else:
        problems.extend(_dose_problems(dose, unit, hit[1]))
    if tested_by is None or normalize_name(tested_by) not in TESTED_PROGRAMS:
        problems.append("untested")
    problems.extend(_interactions([name, *(hit[:1] if hit else ())], rules))
    return problems


def energy_availability(intake_kcal: float, exercise_kcal: float, ffm_kg: float) -> float:
    """(intake - exercise energy) per kg fat-free mass, kcal/kg FFM/day."""
    if not ffm_kg > 0:
        raise ValueError("ffm_kg must be positive")
    return (intake_kcal - exercise_kcal) / ffm_kg


def _under_floor(intake: float | None, ea: float | None, floor: float) -> bool:
    """EA below the floor, or a logged full-day fast (intake <= 0)."""
    return (ea is not None and ea < floor) or (intake is not None and intake <= 0)


def ed_watch(
    intake_7d: Sequence[float | None], ea_7d: Sequence[float | None],
    weigh_ins_per_day: Sequence[int], checkin_texts: Sequence[str], rules: Rules,
) -> bool:
    """True on 3+ of the last 7 days under the floor, any day with > 1 weigh-in, or restrictive language."""
    intake, ea = list(intake_7d)[-7:], list(ea_7d)[-7:]
    intake = [None] * (len(ea) - len(intake)) + intake
    ea = [None] * (len(intake) - len(ea)) + ea
    days_under = sum(_under_floor(i, e, rules.ea_floor) for i, e in zip(intake, ea))
    if days_under >= 3 or any(n > 1 for n in list(weigh_ins_per_day)[-7:]):
        return True
    return any(restrictive_language(t) for t in checkin_texts if t)


# --------------------------------------------------------------------------
# Exercise admission


def _level(raw: Any) -> int | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 <= raw <= len(JOINT_LEVELS) - 1:
        return int(raw)
    key = normalize_name(raw) if isinstance(raw, str) else None
    return JOINT_LEVELS.index(key) if key in JOINT_LEVELS else None


def _excluded_joints(loads: Mapping[str, int | None], constraints: Mapping[str, Any]) -> list[str]:
    hits = (normalize_name(j) for j in constraints.get("excluded_joints", ()))
    return [f"excluded_joints:{j}" for j in hits if (loads.get(j) or 0) > 0]


def _joint_caps(loads: Mapping[str, int | None], constraints: Mapping[str, Any]) -> list[str]:
    out = []
    for joint, cap in constraints.get("joint_load_max", {}).items():
        key, cap_lv = normalize_name(joint), _level(cap)
        if cap_lv is None or (loads.get(key) or 0) > cap_lv:
            out.append(f"joint_load_max:{key}")
    return out


def _joint_violations(joint_load: Any, constraints: Mapping[str, Any]) -> list[str]:
    if not isinstance(joint_load, Mapping):
        return ["invalid:joint_load"]
    loads = {normalize_name(str(j)): _level(v) for j, v in joint_load.items()}
    unknown = [f"joint_level_unknown:{j}" for j, lv in loads.items() if lv is None]
    return unknown + _excluded_joints(loads, constraints) + _joint_caps(loads, constraints)


_EXCLUSIONS = (
    ("exercise_id", "excluded_exercises"),
    ("pattern", "excluded_patterns"),
    ("contraction", "excluded_contractions"),
    ("energy_system", "excluded_energy_systems"),
)
_KNOWN_CONSTRAINTS = frozenset(
    {ck for _, ck in _EXCLUSIONS} | {"excluded_joints", "joint_load_max", EXERCISE_FEATURES_KEY}
)


def _exclusions(features: Mapping[str, Any], constraints: Mapping[str, Any]) -> list[str]:
    out = []
    for feature, ckey in _EXCLUSIONS:
        raw = str(features.get(feature) or "")
        banned = {normalize_name(str(x)) for x in constraints.get(ckey, ())}
        if raw and normalize_name(raw) in banned:
            out.append(f"{ckey}:{raw}")
    return out


def exercise_admissible(features: Mapping[str, Any], constraints: Mapping[str, Any]) -> list[str]:
    """Violated constraint ids; empty means admissible. Unknown constraint keys count as violations."""
    unknown = [f"unknown_constraint:{k}" for k in sorted(set(constraints) - _KNOWN_CONSTRAINTS)]
    missing = [f"missing:{k}" for k in REQUIRED_FEATURES if features.get(k) in (None, "", {})]
    if unknown or missing:
        return unknown + missing
    return _exclusions(features, constraints) + _joint_violations(features["joint_load"], constraints)


# --------------------------------------------------------------------------
# Diff checks


@dataclass(frozen=True)
class GuardContext:
    weekly_loads: Sequence[float]  # completed weeks, oldest -> newest
    hard_sessions_last_week: int
    days_off: int
    ea_7d: float | None
    kcal_current: float | None  # mean daily intake
    weight_kg: float | None
    ffm_kg: float | None
    auto_changes_3wk_pct: float
    red_flag_active: bool
    ed_watch: bool
    injury_constraints: Mapping[str, Any]


@dataclass(frozen=True)
class Verdict:
    tier: str
    reasons: tuple[str, ...]
    rule_ids: tuple[str, ...]


Finding = tuple[str, str, str]  # (tier, rule_id, reason)


def _de(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


# -- load


def _plan_load(plan: Mapping) -> float:
    return math.fsum(float(s["target_rpe"]) * float(s["duration_min"]) for s in plan.get("sessions", []))


def _load_tier(pct: float, rules: Rules) -> str:
    if pct <= rules.load_auto_pct:
        return "auto"
    return "approve" if pct <= rules.load_approve_pct else "reject"


def _trailing_findings(new_load: float, ctx: GuardContext, rules: Rules) -> list[Finding]:
    recent = [float(x) for x in ctx.weekly_loads][-4:]
    if len(recent) < 4 or statistics.fmean(recent) <= 0:
        return [("approve", "load_no_baseline", "Laststeigerung ohne 4-Wochen-Basis")]
    pct = (new_load / statistics.fmean(recent) - 1) * 100
    tier = _load_tier(pct, rules)
    out = [(tier, f"load_{tier}", f"Wochenlast {_de(pct)} % über dem 4-Wochen-Mittel")]
    if tier == "auto" and ctx.auto_changes_3wk_pct + max(pct, 0.0) > rules.cumulative_3wk_pct:
        out.append(("approve", "load_cumulative_3wk", "Automatische Steigerungen über 3 Wochen zu hoch"))
    return out


def _two_week_findings(new_load: float, ctx: GuardContext, rules: Rules) -> list[Finding]:
    loads = [float(x) for x in ctx.weekly_loads]
    if len(loads) < 2 or loads[-2] <= 0:
        return []
    pct = (new_load / loads[-2] - 1) * 100
    if pct <= rules.two_week_reject_pct:
        return []
    return [("reject", "load_two_week", f"Wochenlast {_de(pct)} % über 2 Wochen")]


def _return_findings(new_load: float, ctx: GuardContext, rules: Rules) -> list[Finding]:
    if ctx.days_off < rules.return_after_days_off:
        return []
    prior = next((float(x) for x in reversed(ctx.weekly_loads) if x > 0), None)
    if prior is None:
        return [("approve", "return_no_reference", "Rückkehr nach Pause ohne Vergleichswoche")]
    if new_load > prior * rules.return_cap_pct / 100:
        return [("reject", "return_cap", f"Rückkehr nach Pause über {_de(rules.return_cap_pct)} % der Vorwoche")]
    return []


def _load_findings(cur: Mapping, new: Mapping, ctx: GuardContext, rules: Rules) -> list[Finding]:
    new_load = _plan_load(new)
    if new_load <= _plan_load(cur):
        return []
    if ctx.red_flag_active or ctx.ed_watch:
        return [("reject", "load_while_flagged", "Laststeigerung bei aktivem Warnsignal")]
    return [
        *_trailing_findings(new_load, ctx, rules),
        *_two_week_findings(new_load, ctx, rules),
        *_return_findings(new_load, ctx, rules),
    ]


def _hard_count(plan: Mapping) -> int:
    return sum(float(s["target_rpe"]) >= HARD_RPE for s in plan.get("sessions", []))


def _hard_findings(cur: Mapping, new: Mapping, ctx: GuardContext, rules: Rules) -> list[Finding]:
    n_new = _hard_count(new)
    if n_new <= _hard_count(cur) or n_new - ctx.hard_sessions_last_week <= rules.max_added_hard_sessions:
        return []
    return [("reject", "hard_sessions", "Mehr als erlaubt zusätzliche harte Einheiten")]


# -- exercises


def _exercises(plan: Mapping) -> Iterator[tuple[Mapping, Mapping]]:
    for session in plan.get("sessions", []):
        for ex in session.get("exercises", []):
            yield session, ex


def _keys(plan: Mapping) -> set[tuple[str, str]]:
    return {(str(s["id"]), str(ex["exercise_id"])) for s, ex in _exercises(plan)}


def _features(ctx: GuardContext, eid: str) -> Mapping | None:
    library = ctx.injury_constraints.get(EXERCISE_FEATURES_KEY, {})
    rec = library.get(eid) if isinstance(library, Mapping) else None
    return rec if isinstance(rec, Mapping) else None


def _novel_entry(session: Mapping, ex: Mapping, feats: Mapping) -> list[Finding]:
    out = []
    if float(session["target_rpe"]) > NOVEL_MAX_RPE:
        out.append(("reject", "novel_rpe", "Neue Übung über RPE 6"))
    if int(ex["sets"]) > NOVEL_MAX_SETS:
        out.append(("reject", "novel_sets", "Neue Übung mit mehr als 2 Sätzen"))
    if not ex.get("probation"):
        out.append(("reject", "novel_probation", "Neue Übung ohne Probephase"))
    if normalize_name(str(feats.get("contraction", ""))) == "eccentric overload" or feats.get("eccentric_overload"):
        out.append(("reject", "novel_eccentric", "Neue Übung mit exzentrischer Überlast"))
    return out or [("approve", "novel_exercise", f"Neue Übung {ex['exercise_id']}")]


def _is_swap(session: Mapping, feats: Mapping, removed: set[tuple[str, str]], ctx: GuardContext) -> bool:
    pattern = normalize_name(str(feats.get("pattern", "")))
    for sid, eid in removed:
        old = _features(ctx, eid)
        if sid == str(session["id"]) and old and normalize_name(str(old.get("pattern", ""))) == pattern:
            return True
    return False


def _added_exercise(session: Mapping, ex: Mapping, removed: set[tuple[str, str]], ctx: GuardContext) -> list[Finding]:
    eid = str(ex["exercise_id"])
    feats = _features(ctx, eid)
    if feats is None:
        return [("reject", "features_missing", f"Kein Merkmalsdatensatz für {eid}")]
    violations = exercise_admissible(feats, ctx.injury_constraints)
    if violations:
        return [("reject", f"injury:{v}", f"{eid} verletzt {v}") for v in violations]
    if ex.get("novel"):
        return _novel_entry(session, ex, feats)
    if _is_swap(session, feats, removed, ctx):
        return [("notify", "swap_same_pattern", f"Tausch innerhalb des Musters: {eid}")]
    return [("approve", "exercise_added", f"Übung {eid} hinzugefügt")]


def _novel_counts(plan: Mapping) -> dict[str, int]:
    counts: dict[str, int] = {}
    for session, ex in _exercises(plan):
        counts[str(session["id"])] = counts.get(str(session["id"]), 0) + bool(ex.get("novel"))
    return counts


def _novel_cap_findings(cur: Mapping, new: Mapping, rules: Rules) -> list[Finding]:
    before, after = _novel_counts(cur), _novel_counts(new)
    out = []
    if any(n > rules.novel_per_session and n > before.get(sid, 0) for sid, n in after.items()):
        out.append(("reject", "novel_per_session", "Zu viele neue Übungen in einer Einheit"))
    if sum(after.values()) > rules.novel_per_week and sum(after.values()) > sum(before.values()):
        out.append(("reject", "novel_per_week", "Zu viele neue Übungen pro Woche"))
    return out


def _pull_push(plan: Mapping, ctx: GuardContext) -> float | None:
    sets = {"pull": 0, "push": 0}
    for _, ex in _exercises(plan):
        words = normalize_name(str((_features(ctx, str(ex["exercise_id"])) or {}).get("pattern", ""))).split()
        for side in sets:
            sets[side] += int(ex["sets"]) if side in words else 0
    if sets["push"] == 0:
        return None if sets["pull"] == 0 else math.inf
    return sets["pull"] / sets["push"]


def _pull_push_findings(cur: Mapping, new: Mapping, ctx: GuardContext, rules: Rules) -> list[Finding]:
    ratio = _pull_push(new, ctx)
    lo, hi = rules.pull_push
    if ratio is None or lo <= ratio <= hi or ratio == _pull_push(cur, ctx):
        return []
    return [("reject", "pull_push", "Zug-zu-Druck-Verhältnis außerhalb 1:1 bis 2:1")]


def _exercise_findings(cur: Mapping, new: Mapping, ctx: GuardContext, rules: Rules) -> list[Finding]:
    before, after = _keys(cur), _keys(new)
    removed = before - after
    out: list[Finding] = []
    for session, ex in _exercises(new):
        if (str(session["id"]), str(ex["exercise_id"])) not in before:
            out.extend(_added_exercise(session, ex, removed, ctx))
    return out + _novel_cap_findings(cur, new, rules) + _pull_push_findings(cur, new, ctx, rules)


# -- nutrition


def _kcal(plan: Mapping) -> float | None:
    value = plan.get("nutrition", {}).get("kcal_target")
    return None if value is None else float(value)


def _step_finding(delta: float, ref: float, rules: Rules) -> Finding:
    limit = min(ref * rules.kcal_step_pct / 100, rules.kcal_step_abs)
    pct = abs(delta) / ref * 100
    if abs(delta) > limit:
        return ("reject", "kcal_step", f"kcal-Ziel ändert sich um {_de(abs(delta))} kcal")
    if pct > KCAL_APPROVE_PCT:
        return ("approve", "kcal_change", f"kcal-Ziel ändert sich um {_de(pct)} %")
    return ("notify", "kcal_small", f"kcal-Ziel ändert sich um {_de(pct)} %")


def _ea_finding(delta: float, ctx: GuardContext, rules: Rules) -> list[Finding]:
    if ctx.ea_7d is None or not ctx.ffm_kg:
        return [("reject", "ea_unknown", "Energieverfügbarkeit unbekannt")]
    projected = ctx.ea_7d + delta / ctx.ffm_kg
    if projected < rules.ea_floor:
        return [("reject", "ea_floor", f"Energieverfügbarkeit fiele auf {_de(projected)} kcal/kg FFM")]
    if projected < rules.ea_approve:
        return [("approve", "ea_low", f"Energieverfügbarkeit fiele auf {_de(projected)} kcal/kg FFM")]
    return []


def _loss_finding(target: float, ctx: GuardContext, rules: Rules) -> list[Finding]:
    if ctx.kcal_current is None or not ctx.weight_kg:
        return [("reject", "loss_rate_unknown", "Abnahmetempo nicht prüfbar")]
    deficit = ctx.kcal_current - target
    pct = max(deficit, 0.0) * 7 / KCAL_PER_KG / ctx.weight_kg * 100
    if pct > rules.max_loss_pct_bw_week:
        return [("reject", "loss_rate", f"Abnahme von {_de(pct)} % Körpergewicht pro Woche")]
    return []


def _deficit_findings(target: float, delta: float, ctx: GuardContext, rules: Rules) -> list[Finding]:
    if ctx.red_flag_active or ctx.ed_watch:
        return [("reject", "deficit_while_flagged", "Kaloriendefizit bei aktivem Warnsignal")]
    return _ea_finding(delta, ctx, rules) + _loss_finding(target, ctx, rules)


def _kcal_findings(cur: Mapping, new: Mapping, ctx: GuardContext, rules: Rules) -> list[Finding]:
    old, target = _kcal(cur), _kcal(new)
    if target == old:
        return []
    if target is None:
        return [("notify", "kcal_cleared", "kcal-Ziel entfernt")]
    ref = old if old else ctx.kcal_current
    out = [] if old else [("approve", "kcal_new_target", "Neues kcal-Ziel")]
    if not ref:
        return out
    delta = target - ref
    out.append(_step_finding(delta, ref, rules))
    return out + (_deficit_findings(target, delta, ctx, rules) if delta < 0 else [])


# -- supplements, text, refs, paths


def _supp_key(s: Mapping) -> tuple:
    return (normalize_name(str(s["name"])), float(s["dose"]), normalize_name(str(s["unit"])), s.get("tested_by"))


def _supplement_findings(cur: Mapping, new: Mapping, rules: Rules) -> list[Finding]:
    known = {_supp_key(s) for s in cur.get("supplements", [])}
    out: list[Finding] = []
    for s in new.get("supplements", []):
        if _supp_key(s) in known:
            continue
        problems = supplement_check(str(s["name"]), s["dose"], str(s["unit"]), s.get("tested_by"), rules)
        out.extend(("reject", f"supplement:{p}", f"{s['name']}: {p}") for p in problems)
        if not problems:
            out.append(("approve", "supplement", f"Supplement {s['name']}"))
    return out


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


_FAST_WORD_RE = re.compile(r"fasten|fasting|\bfast\b")
_DURATION_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)[\s-]*(stunden|stunde|std|hours|hour|hrs|h|tagen|tage|tag|days|day)\b"
)


def _fast_hours(text: str) -> list[float]:
    low = text.casefold()
    if not _FAST_WORD_RE.search(low):
        return []
    hours = []
    for num, unit in _DURATION_RE.findall(low):
        value = float(num.replace(",", "."))
        hours.append(value * 24 if unit.startswith(("tag", "day")) else value)
    return hours


def _text_findings(diff: Sequence[Mapping], rules: Rules) -> list[Finding]:
    out: list[Finding] = []
    for text in (t for op in diff for t in _strings(op.get("value"))):
        category = forbidden_match(text, rules)
        if category:
            out.append(("reject", f"forbidden:{category}", f"Verbotener Inhalt ({category})"))
        out.extend(("reject", "banned_term", f"Diagnosebegriff: {t}") for t in scan_text(text))
        if any(h > rules.max_fast_h for h in _fast_hours(text)):
            out.append(("reject", "fast_too_long", f"Fasten über {_de(rules.max_fast_h)} h"))
    return out


def _ref_findings(diff: Sequence[Mapping]) -> list[Finding]:
    out = []
    for op in diff:
        papers = {r for r in op.get("refs", []) if isinstance(r, str) and r.startswith(PAPER_REF_PREFIX)}
        if len(papers) == 1:
            out.append(("approve", "single_study", f"Begründung aus nur einer Studie: {op.get('path')}"))
    return out


_PATH_TIERS: Mapping[str, str] = MappingProxyType({
    "sessions": "auto", "nutrition": "notify", "supplements": "notify", "modulation": "notify",
    "valid_until": "notify", "experiments": "approve", "rationale_refs": "auto",
})


def _path_finding(op: Mapping) -> Finding:
    path = op.get("path")
    if op.get("op") not in ("set", "add", "remove") or not isinstance(path, str) or not path.startswith("/"):
        return ("reject", "malformed_op", f"Ungültige Operation: {path!r}")
    parts = path.split("/")
    top = parts[1]
    if top not in _PATH_TIERS:
        return ("reject", "path_not_allowed", f"Pfad nicht änderbar: {path}")
    if top == "sessions" and "exercises" in parts:
        return ("notify", "exercise_change", f"Übungsänderung: {path}")
    return (_PATH_TIERS[top], f"path:{top}", f"Änderung: {path}")


def _all_findings(cur: Mapping, new: Mapping, diff: Sequence[Mapping], ctx: GuardContext, rules: Rules) -> list[Finding]:
    return [
        *(_path_finding(op) for op in diff),
        *_load_findings(cur, new, ctx, rules),
        *_hard_findings(cur, new, ctx, rules),
        *_exercise_findings(cur, new, ctx, rules),
        *_kcal_findings(cur, new, ctx, rules),
        *_supplement_findings(cur, new, rules),
        *_text_findings(diff, rules),
        *_ref_findings(diff),
    ]


def _verdict(findings: Sequence[Finding]) -> Verdict:
    ranked = sorted(dict.fromkeys(findings), key=lambda f: -TIERS.index(f[0]))
    return Verdict(ranked[0][0], tuple(f[2] for f in ranked), tuple(f[1] for f in ranked))


def check_diff(current_plan: Mapping, new_plan: Mapping, diff: Sequence[Mapping], ctx: GuardContext, rules: Rules) -> Verdict:
    """Tier for a plan diff; the strictest finding wins. Malformed input is rejected."""
    if not diff:
        return Verdict("auto", ("Keine Änderung",), ("no_change",))
    try:
        findings = _all_findings(current_plan, new_plan, diff, ctx, rules)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, ZeroDivisionError) as exc:
        findings = [("reject", "malformed_plan", f"Plan nicht prüfbar: {type(exc).__name__}")]
    return _verdict(findings)


# --------------------------------------------------------------------------
# Red flags


@dataclass(frozen=True)
class Alert:
    level: str
    rule_id: str
    date: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in ALERT_LEVELS:
            raise ValueError(f"invalid alert level: {self.level!r}")


_SELF_HARM_TERMS: tuple[str, ...] = (
    # German
    "suizid", "selbstmord", "umbringen", "mich toten", "mir das leben nehmen", "nicht mehr leben",
    "will sterben", "sterben will", "mir etwas antun", "mir was antun", "ritzen", "selbstverletzung",
    # English
    "suicide", "suicidal", "kill myself", "end my life", "want to die", "self harm", "hurt myself",
)


def _self_harm(text: str) -> bool:
    norm = normalize_name(text)
    return any(_has_phrase(norm, normalize_name(t)) for t in _SELF_HARM_TERMS)


def _shift(day: str, n: int) -> str:
    return (Date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _values(daily: Mapping[str, Sequence[DailyValue]], key: str) -> dict[str, float]:
    return {r.date: float(r.value) for r in daily.get(key, ()) if r.value is not None}


def _window(by_date: Mapping[str, float], day: str, lo: int, hi: int) -> list[float]:
    """Values on days lo..hi relative to day, inclusive."""
    return [by_date[d] for d in (_shift(day, i) for i in range(lo, hi + 1)) if d in by_date]


def _rise(daily: Mapping[str, Sequence[DailyValue]], key: str, day: str) -> float | None:
    """Today's value minus the median of days -35..-8 (needs 14 baseline days)."""
    by_date = _values(daily, key)
    base = _window(by_date, day, -35, -8)
    if day not in by_date or len(base) < 14:
        return None
    return by_date[day] - statistics.median(base)


def _checkin_alerts(items: Sequence[CheckinItem], day: str) -> list[Alert]:
    flags = symptom_flags(items)
    urgent = [s for s in flags if s in URGENT_SYMPTOMS]
    out = [Alert("urgent", "symptom_urgent", day, {"symptoms": urgent})] if urgent else []
    if any(_self_harm(it.text) for it in items if it.text):
        out.append(Alert("urgent", "self_harm", day, {}))
    out.extend(Alert("prompt", s, day, {}) for s in ("dark_urine", "focal_bone_pain") if s in flags)
    return out


def _bp_alerts(daily: Mapping[str, Sequence[DailyValue]], day: str) -> list[Alert]:
    sys_v, dia_v = _values(daily, "bp_sys").get(day), _values(daily, "bp_dia").get(day)
    if (sys_v or 0) >= 180 or (dia_v or 0) >= 120:
        return [Alert("urgent", "bp_high", day, {"sys": sys_v, "dia": dia_v})]
    return []


def _rhr_3d(deltas: Sequence[float | None], day: str) -> list[Alert]:
    if len(deltas) == 3 and all(d is not None and d >= 10 for d in deltas):
        return [Alert("prompt", "rhr_up_3d", day, {"delta": min(deltas)})]
    return []


def _rhr_resp_temp(delta: float | None, daily: Mapping[str, Sequence[DailyValue]], day: str) -> list[Alert]:
    if delta is None or delta < 5:
        return []
    resp, temp = _rise(daily, "resp_rate", day), _rise(daily, "wrist_temp_c", day)
    if (resp or 0) < 2 and (temp or 0) < 0.5:
        return []
    data = {"delta": delta, "resp_delta": resp, "temp_delta": temp, "pause_intense": True}
    return [Alert("prompt", "rhr_resp_temp", day, data)]


def _rhr_alerts(rhr: Sequence[RhrState], daily: Mapping[str, Sequence[DailyValue]], day: str) -> list[Alert]:
    """rhr holds consecutive daily states, the last one for day."""
    deltas = [s.delta for s in rhr[-3:]]
    return _rhr_3d(deltas, day) + _rhr_resp_temp(deltas[-1] if deltas else None, daily, day)


def _spo2_alerts(daily: Mapping[str, Sequence[DailyValue]], day: str) -> list[Alert]:
    week = [v * 100 if v <= 1 else v for v in _window(_values(daily, "spo2_min"), day, -6, 0)]
    low = [v for v in week if v < 92]
    return [Alert("prompt", "spo2_low", day, {"days": len(low), "min": min(low)})] if len(low) >= 3 else []


def _af_alerts(daily: Mapping[str, Sequence[DailyValue]], day: str) -> list[Alert]:
    by_date = _values(daily, "af_burden")
    today = by_date.get(day, 0.0)
    if today > 0 and not any(v > 0 for v in _window(by_date, day, -90, -1)):
        return [Alert("prompt", "af_new", day, {"burden": today})]
    return []


def _weight_alerts(daily: Mapping[str, Sequence[DailyValue]], day: str, profile: Mapping[str, Any]) -> list[Alert]:
    if profile.get("weight_loss_intended"):
        return []
    by_date = _values(daily, "weight_kg")
    early, late = _window(by_date, day, -30, -22), _window(by_date, day, -2, 0)
    if len(early) < 2 or not late:
        return []
    ref = statistics.median(early)
    loss = (ref - statistics.median(late)) / ref * 100
    return [Alert("prompt", "weight_loss_30d", day, {"loss_pct": loss})] if loss >= 5 else []


def _profile_alerts(profile: Mapping[str, Any], day: str) -> list[Alert]:
    missed = profile.get("missed_periods")
    if isinstance(missed, int) and not isinstance(missed, bool) and missed >= 3:
        return [Alert("prompt", "missed_periods", day, {"count": missed})]
    return []


def red_flags(
    daily: Mapping[str, Sequence[DailyValue]], rhr: Sequence[RhrState],
    checkins: Sequence[CheckinItem], day: str, profile: Mapping[str, Any],
) -> list[Alert]:
    """Code-computed red flags for day, urgent first."""
    items = [c for c in checkins if c.date == day]
    alerts = [
        *_checkin_alerts(items, day), *_bp_alerts(daily, day), *_rhr_alerts(rhr, daily, day),
        *_spo2_alerts(daily, day), *_af_alerts(daily, day), *_weight_alerts(daily, day, profile),
        *_profile_alerts(profile, day),
    ]
    return sorted(alerts, key=lambda a: a.level != "urgent")


def event_alerts(events: Sequence[HealthEvent]) -> list[Alert]:
    """Every watch heart-rate notification becomes an urgent alert."""
    return [
        Alert("urgent", "hr_notification", e.start[:10], {"start": e.start})
        for e in events if e.kind == "hr_notification"
    ]


# --------------------------------------------------------------------------
# Templates


TEMPLATES_DE: Mapping[str, str] = MappingProxyType({
    "symptom_urgent": (
        "Du hast heute angegeben: $symptoms. Ruf jetzt den Notruf 112 an. "
        "Das Coaching pausiert, bis dich eine Ärztin oder ein Arzt gesehen hat."
    ),
    "self_harm": (
        "Danke, dass du das aufgeschrieben hast. Du musst damit nicht allein bleiben. "
        "Die Telefonseelsorge ist rund um die Uhr kostenlos erreichbar: 0800 111 0 111. "
        "Bist du in akuter Gefahr, ruf den Notruf 112 an. Das Coaching pausiert."
    ),
    "bp_high": (
        "Dein Blutdruck lag am $date bei $sys/$dia mmHg. Hast du dazu Brustschmerzen, Atemnot, "
        "Sehstörungen oder starke Kopfschmerzen, ruf sofort den Notruf 112 an. "
        "Sonst sprich heute noch mit $doctor. Das Coaching pausiert."
    ),
    "hr_notification": (
        "Deine Uhr hat am $date eine Herzfrequenz-Mitteilung gesendet. Hast du Brustschmerzen "
        "oder Atemnot oder fühlst du dich einer Ohnmacht nahe, ruf sofort den Notruf 112 an. "
        "Sonst lass die Mitteilung zeitnah von $doctor ansehen. Das Coaching pausiert."
    ),
    "rhr_up_3d": (
        "Dein Ruhepuls liegt seit 3 Tagen mindestens $delta Schläge pro Minute über deinem "
        "Ausgangswert. Intensives Training pausiert. Fühlst du dich krank oder bleibt der Wert "
        "so, sprich heute mit $doctor."
    ),
    "rhr_resp_temp": (
        "Dein Ruhepuls liegt $delta Schläge pro Minute über deinem Ausgangswert, dazu ist die "
        "Atemfrequenz ($resp_delta pro Minute) oder die Hauttemperatur ($temp_delta °C) erhöht. "
        "Intensives Training pausiert. Fühlst du dich krank, sprich heute mit $doctor."
    ),
    "spo2_low": (
        "Deine Sauerstoffsättigung lag an $days der letzten 7 Tage unter 92 % "
        "(niedrigster Wert $min %). Sprich heute mit $doctor. Bei Atemnot ruf den Notruf 112 an."
    ),
    "af_new": (
        "Deine Uhr zeigt am $date zum ersten Mal einen Wert im Herzrhythmus-Verlauf ($burden). "
        "Lass das heute von $doctor ansehen."
    ),
    "weight_loss_30d": (
        "Dein Gewicht ist in den letzten 30 Tagen um $loss_pct % gesunken, ohne dass ein "
        "Abnehmziel aktiv ist. Kaloriendefizite pausieren. Sprich heute mit $doctor."
    ),
    "dark_urine": (
        "Du hast nach dem Training dunklen Urin angegeben. Trainiere heute nicht und sprich "
        "heute mit $doctor."
    ),
    "focal_bone_pain": (
        "Du hast einen punktuellen Knochenschmerz angegeben. Belaste die Stelle nicht und "
        "sprich zeitnah mit $doctor."
    ),
    "missed_periods": (
        "Du hast $count ausgebliebene Perioden in Folge angegeben. Kaloriendefizite pausieren. "
        "Sprich zeitnah mit $doctor."
    ),
})

DOCTOR_FALLBACK_DE = "deiner Hausärztin oder deinem Hausarzt"


def _doctor(profile: Mapping[str, Any]) -> str:
    raw = profile.get("doctor_contact")
    if isinstance(raw, Mapping):
        raw = ", ".join(str(raw[k]) for k in ("name", "phone") if raw.get(k))
    return raw if isinstance(raw, str) and raw.strip() else DOCTOR_FALLBACK_DE


def _fmt(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, (list, tuple)):
        return ", ".join(SYMPTOM_LABELS_DE.get(v, str(v)) for v in value)
    if isinstance(value, float):
        return _de(value)
    return str(value)


def render_alert(alert: Alert, profile: Mapping[str, Any]) -> str:
    """Fixed German text for alert; raises KeyError for an unknown rule or missing field."""
    values = {k: _fmt(v) for k, v in alert.data.items()}
    values.update(date=alert.date, doctor=_doctor(profile))
    return Template(TEMPLATES_DE[alert.rule_id]).substitute(values)


# --------------------------------------------------------------------------
# Banned diagnostic vocabulary


BANNED_TERMS: tuple[str, ...] = (
    # English (matched as word prefixes on normalized text)
    "diagnos", "disease", "disorder", "syndrom", "arrhythmi", "atrial fibrillation", "afib",
    "tachycardi", "bradycardi", "hypertensi", "hypotensi", "myocarditis", "heart attack", "infarct",
    "stroke", "thrombos", "embolism", "infection", "pneumonia", "covid", "influenza", "anemi",
    "diabet", "hypothyroid", "hyperthyroid", "sleep apnea", "insomnia", "depressi", "anorexi",
    "bulimi", "rhabdomyoly", "fracture", "tendinitis", "tendinopath", "concussion",
    "relative energy deficiency", "amenorrh", "cancer", "tumor",
    # German
    "erkrankung", "krankheitsbild", "herzrhythmusstorung", "vorhofflimmern", "tachykardi",
    "bradykardi", "bluthochdruck", "hypertoni", "hypotoni", "herzmuskelentzundung", "myokarditis",
    "herzinfarkt", "schlaganfall", "thrombose", "embolie", "infekt", "lungenentzundung", "grippe",
    "anamie", "schilddrusenunterfunktion", "schilddrusenuberfunktion", "schlafapnoe", "insomnie",
    "angststorung", "magersucht", "essstorung", "ubertrainingssyndrom", "ermudungsbruch",
    "stressfraktur", "fraktur", "knochenbruch", "sehnenentzundung", "gehirnerschutterung",
    "amenorrho", "krebs",
)

_BANNED_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (t, re.compile(r"(?<![a-z0-9])" + re.escape(t))) for t in BANNED_TERMS
)


def scan_text(text: str) -> list[str]:
    """BANNED_TERMS entries that start a word in text (case, diacritics and punctuation ignored)."""
    norm = normalize_name(text or "")
    return [term for term, rx in _BANNED_RES if rx.search(norm)]

"""Weekly review: packet, the bounded Hippocampus/Amygdala/Cerebellum loop, and the Saturday and Thursday steps.

``build_packet`` writes ``reviews/<week>/packet.json`` from code-computed data.
Its sha256 is the ``data_sha`` every review message cites. ``run_review``
runs the argued review with a fixed shape and at most 6 messages:

    01 Hippocampus draft -> 02 Amygdala round 1 -> 03 Hippocampus answer
    -> 04 Amygdala round 2 (only on paths changed in round 1) -> 05 answer
    -> 06 Cerebellum ruling

Guardrails run before the ruling (each diff gets a ``check_diff`` tier) and
after it (the ratified subset is re-checked before ``plans.propose``). An open
blocker forces ``keep``, extends ``valid_until`` by 7 days and asks the owner.
Every LLM output is scanned for diagnostic vocabulary; a hit is re-asked once
and a second hit aborts the review. Aborts keep the plan and alert the owner.

``run_insula`` and ``run_motor_cortex`` are the Saturday steps,
``run_thalamus`` the Thursday batch extraction. All LLM calls go through
``agents.call`` or the roster provider's batch API, priced by ``Budget``.
This module never commits; Pineal commits what it wrote.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import urllib.error
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from homunculus import agents, beliefs, evidence, experiments, guardrails, insula, metrics, plans, scouts
from homunculus.agents import BotSpec, Budget, LLMRequest, LLMResponse, Provider
from homunculus.checkins import NOTE_ITEM, SESSION_ITEM, SYMPTOM_PREFIX, subjective_scores
from homunculus.evidence import OwnerProfile
from homunculus.guardrails import GuardContext, Rules, Verdict
from homunculus.metrics import MetricsConfig
from homunculus.store import Ledger, Store, canonical_json

# --------------------------------------------------------------------------
# Constants

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agents"
MAX_ROUNDS = 2
MAX_MESSAGES = 6
NOTE_MAX = 280
MAX_OWNER_ASKS = 3
EXTEND_DAYS = 7
CODE_AUTHOR = "pineal"
RED_FLAG_DAYS = 7
GUARD_HORIZON_DAYS = 28
TITLE_MATCH_MIN = 0.9
FRESHNESS_H: Mapping[str, float] = {"heart_rate_variability": 36.0, "sleep_segments": 36.0, "workouts": 72.0}
WEIGHT_METRICS = ("weight_body_mass", "body_mass")
INSULA_FILE = "00-insula-reconciliation.json"
MOTOR_CORTEX_FILE = "00-motor-cortex-proposals.json"
THALAMUS_BATCH = "evidence/batches/thalamus.json"

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_PRIVATE_KEYS = frozenset({
    "device", "device_name", "devices", "gps", "route", "routes", "location", "locations",
    "lat", "lon", "latitude", "longitude", "raw_json",
})
_REF_EXCLUDED = ("ops/", "mail/", "quarantine/", "evidence/cache/", "evidence/batches/", "site/")
_CEREBELLUM_PACKET_KEYS = ("week", "daily", "baselines", "guard_context", "freshness", "budget")
_TRANSPORT_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, scouts.CacheMiss)

HOLD_ASK_DE = (
    "Im Wochenreview ist ein Einwand offen, deshalb bleibt dein Plan eine Woche länger gültig. "
    "Schau dir die offenen Punkte im Dashboard an und sag Cerebellum, wie es weitergehen soll."
)
ABORT_DE = "Wochenreview $week abgebrochen ($reason). Der aktuelle Plan bleibt unverändert."
REASK_DE = (
    "Deine letzte Antwort wurde abgelehnt: {problems}. Antworte erneut als ein JSON-Objekt im selben "
    "Schema und vermeide genau diese Probleme."
)

# --------------------------------------------------------------------------
# Output schemas

_DIFF: dict = {k: v for k, v in plans.DIFF_SCHEMA.items() if k != "$schema"}
_STR: dict = {"type": "string"}
_STR_LIST: dict = {"type": "array", "items": {"type": "string"}}


def _obj(props: Mapping[str, Any]) -> dict:
    return {"type": "object", "properties": dict(props), "required": list(props), "additionalProperties": False}


def _array(items: Mapping[str, Any], **extra: Any) -> dict:
    return {"type": "array", "items": dict(items), **extra}


_PROTOCOL_DRAFT: dict = _obj({
    "belief_id": _STR, "intervention": _STR, "comparator": _STR, "outcome_key": _STR, "source": _STR,
    "design": {"type": "string", "enum": list(experiments.DESIGNS)},
    "block_days": {"type": "integer"}, "washout_days": {"type": "integer"}, "pairs": {"type": "integer"},
    "exclusions": _STR_LIST, "covariates": _STR_LIST,
})

DRAFT_SCHEMA: dict = _obj({
    "summary_md": _STR,
    "plan_diff": _DIFF,
    "claims": _array(_obj({"id": _STR, "text": _STR, "refs": _STR_LIST})),
    "experiments_proposed": _array(_PROTOCOL_DRAFT),
})

CHALLENGE_SCHEMA: dict = _obj({
    "items": _array(_obj({
        "cid": _STR,
        "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
        "claim": _STR,
        "ref": _STR,
        "diff_paths": _STR_LIST,
    })),
})

REBUTTAL_SCHEMA: dict = _obj({
    "answers": _array(_obj({
        "cid": _STR,
        "action": {"type": "string", "enum": ["accept", "rebut"]},
        "ref": {"type": ["string", "null"]},
        "change": _DIFF,
    })),
    "plan_diff": _DIFF,
})

RULING_SCHEMA: dict = _obj({
    "ruling": {"type": "string", "enum": ["ratify", "partial", "keep"]},
    "plan_diff": _DIFF,
    "rationale_md": _STR,
    "ask_owner_de": _array(_STR, maxItems=MAX_OWNER_ASKS),
})

_JOINT: dict = _obj({"joint": _STR, "level": {"type": "string", "enum": list(guardrails.JOINT_LEVELS)}})
_EXERCISE: dict = {
    **_obj({"exercise_id": _STR, "pattern": _STR, "contraction": _STR, "energy_system": _STR,
            "joint_load": _array(_JOINT)}),
    "type": ["object", "null"],
}
MOTOR_CORTEX_SCHEMA: dict = _obj({
    "proposals": _array(_obj({
        "session_id": _STR, "plan_diff": _DIFF, "exercise": _EXERCISE, "novel": {"type": "boolean"},
        "goal": _STR, "rationale": _STR, "refs": _STR_LIST,
    })),
})

# --------------------------------------------------------------------------
# Small helpers


def _week_start(week: str) -> date:
    match = _WEEK_RE.match(str(week))
    if match is None:
        raise ValueError(f"week must look like 2026-W47, got {week!r}")
    return date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)


def _week_days(week: str) -> list[str]:
    start = _week_start(week)
    return [(start + timedelta(days=k)).isoformat() for k in range(7)]


def _shift(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(text: str) -> datetime:
    return _utc(datetime.fromisoformat(str(text).replace("Z", "+00:00")))


def _iso_week(dt: datetime) -> str:
    year, week, _ = _utc(dt).isocalendar()
    return f"{year}-W{week:02d}"


def _run_id(week: str, bot: str, round_: int | str) -> str:
    return f"r-{week.replace('-', '')}-{bot}-{round_}"


def _review_rel(week: str, name: str) -> str:
    return f"reviews/{week}/{name}"


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _scan_problems(structured: Any) -> list[str]:
    return [f"banned_term:{t}" for t in guardrails.scan_text("\n".join(_strings(structured)))]


def _no_check(_structured: Any) -> list[str]:
    return []


def _goals(ledger: Ledger) -> str:
    return ledger.read_text("config/goals.md") if ledger.exists("config/goals.md") else ""


def _profile(ledger: Ledger) -> dict:
    raw = ledger.read_json("profile/owner.json", default={})
    return dict(raw) if isinstance(raw, Mapping) else {}


def _plain(node: Any) -> Any:
    """Paper extraction with every {value, quote} field reduced to its value."""
    if isinstance(node, Mapping):
        if set(node) == {"value", "quote"}:
            return node["value"]
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    return node


def _clean(value: Any) -> Any:
    """JSON-safe copy: private keys dropped, non-finite floats None, strings capped at NOTE_MAX."""
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items() if str(k).lower() not in _PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str) and len(value) > NOTE_MAX:
        return value[:NOTE_MAX]
    return value


# --------------------------------------------------------------------------
# Packet


def build_packet(store: Store, ledger: Ledger, week: str, cfg: MetricsConfig, now: datetime) -> dict:
    """Write reviews/<week>/packet.json and return it. Every number in it comes from code."""
    days = _week_days(week)
    plan = _plan_or_none(ledger)
    ctx = _guard_context(store, ledger, days, guardrails.load_rules(ledger), now)
    packet = {
        "week": week,
        "generated_at": _iso_utc(now),
        "daily": _daily(store, days),
        "baselines": _ledger_or(ledger, f"data/baselines/{week}.json", lambda: metrics.baselines(store, days[-1], cfg)),
        "conflicts": _ledger_or(ledger, f"data/conflicts/{week}.json",
                                lambda: insula.weekly_reconciliation(store, week, cfg)),
        "insula": ledger.read_json(_review_rel(week, INSULA_FILE)),
        "motor_cortex": ledger.read_json(_review_rel(week, MOTOR_CORTEX_FILE)),
        "papers_actionable": _papers_actionable(ledger),
        "beliefs_changed": _beliefs_changed(ledger, days),
        "experiments": _experiments(ledger),
        "guard_context": _guard_dict(ctx),
        "freshness": metrics.freshness(store.latest_ts(), now, FRESHNESS_H),
        "budget": _budget_summary(ledger, now),
        "critic_same_family": _critic_same_family(ledger),
    }
    clean = _clean(packet)
    clean["plan_current"] = plan  # the plan is copied verbatim so diff paths stay valid
    ledger.write_json(_review_rel(week, "packet.json"), clean, author=CODE_AUTHOR)
    return clean


def packet_sha(packet: Mapping) -> str:
    return _sha(packet)


def guard_context_from_packet(packet: Mapping) -> GuardContext:
    """The GuardContext build_packet serialized, so review and packet check against the same numbers."""
    raw = dict(packet["guard_context"])
    raw["weekly_loads"] = tuple(float(x) for x in raw["weekly_loads"])
    return GuardContext(**raw)


def _plan_or_none(ledger: Ledger) -> dict | None:
    try:
        return plans.current(ledger)
    except plans.PlanError:
        return None


def _ledger_or(ledger: Ledger, rel: str, compute: Callable[[], Any]) -> Any:
    found = ledger.read_json(rel)
    return found if found is not None else compute()


def _daily(store: Store, days: Sequence[str]) -> dict:
    out: dict[str, dict] = {}
    for key in metrics.DAILY_KEYS:
        rows = store.daily(key, days[0], _shift(days[-1], 1))
        out[key] = {r.date: {"value": r.value, "quality": r.quality, "source": r.source} for r in rows}
    return out


def _paper_files(ledger: Ledger) -> list[str]:
    return [p for p in ledger.glob("evidence/papers/*.json") if not p.endswith(".audit.json")]


def _actionable(rec: Mapping) -> bool:
    score = rec.get("score") or {}
    return score.get("gate") == "actionable" and not rec.get("blocked") and rec.get("resolved") is True


def _paper_summary(rel: str, rec: Mapping) -> dict:
    score, paper = rec.get("score") or {}, rec.get("extraction") or {}
    return {
        "ref": rel, "doi": rec.get("doi"), "pmid": rec.get("pmid"), "title": (rec.get("meta") or {}).get("title"),
        "certainty": score.get("certainty"), "gate": score.get("gate"), "flags": list(score.get("flags") or ()),
        **{k: _plain(paper.get(k)) for k in ("design", "n_total", "population", "outcome", "effect")},
    }


def _papers_actionable(ledger: Ledger) -> list[dict]:
    out = []
    for rel in _paper_files(ledger):
        rec = ledger.read_json(rel) or {}
        if isinstance(rec, Mapping) and _actionable(rec):
            out.append(_paper_summary(rel, rec))
    return out


_BELIEF_FIELDS = ("status", "mu", "sd", "p_benefit", "p_harm", "review_after")


def _beliefs_changed(ledger: Ledger, days: Sequence[str]) -> list[dict]:
    events = ledger.read_jsonl(beliefs.EVENTS_PATH)
    ids = sorted({str(e["belief_id"]) for e in events
                  if "belief_id" in e and days[0] <= str(e.get("at", ""))[:10] <= days[-1]})
    out = []
    for bid in ids:
        rel = f"{beliefs.VIEWS_DIR}/{bid}.json"
        view = ledger.read_json(rel)
        if isinstance(view, Mapping):
            out.append({"id": bid, "ref": rel, **{k: view.get(k) for k in _BELIEF_FIELDS}})
    return out


_RESULT_FIELDS = ("decision", "d_bar", "p_value", "p_benefit", "p_harm", "n_excluded")
_PROTOCOL_FIELDS = ("id", "belief_id", "outcome_key", "source", "design", "pairs", "start_date")


def _experiment_summary(ledger: Ledger, rel: str) -> dict:
    proto = ledger.read_json(rel) or {}
    res_rel = rel[: -len("protocol.json")] + "result.json"
    result = ledger.read_json(res_rel)
    summary = None
    if isinstance(result, Mapping):
        summary = {"ref": res_rel, **{k: result.get(k) for k in _RESULT_FIELDS}}
    return {"ref": rel, **{k: proto.get(k) for k in _PROTOCOL_FIELDS}, "result": summary}


def _experiments(ledger: Ledger) -> list[dict]:
    return [_experiment_summary(ledger, rel) for rel in ledger.glob("experiments/*/protocol.json")]


def _budget_summary(ledger: Ledger, now: datetime) -> dict:
    cfg = ledger.read_json("config/budget.json", default={}) or {}
    budget = Budget(ledger, cfg, now=lambda: now)
    return {"level": budget.level(), "spent_week_usd": round(budget.spent_week(), 6), "cap_usd": budget.cap}


def _same_family(roster: Mapping[str, BotSpec]) -> bool:
    drafter, critic = roster.get("hippocampus"), roster.get("amygdala")
    if drafter is None or critic is None:
        return True
    return drafter.provider == critic.provider


def _critic_same_family(ledger: Ledger) -> bool:
    """Unknown roster counts as same family, so Amygdala distrusts familiar reasoning (fail closed)."""
    raw = ledger.read_json("config/roster.json")
    try:
        return _same_family(agents.load_roster(raw)) if isinstance(raw, Mapping) else True
    except ValueError:
        return True


# --------------------------------------------------------------------------
# Guard context (code-computed, fail closed)


def _guard_context(store: Store, ledger: Ledger, days: Sequence[str], rules: Rules, now: datetime) -> GuardContext:
    """Intake data is not tracked yet, so ea_7d and kcal_current stay None and deficits reject."""
    profile = _profile(ledger)
    first = _shift(days[-1], 1 - GUARD_HORIZON_DAYS)
    loads = {r.date: r.value for r in store.daily("load_srpe", first, _shift(days[-1], 1))}
    return GuardContext(
        weekly_loads=tuple(_weekly_loads(loads, days[0])),
        hard_sessions_last_week=_hard_sessions(store, days),
        days_off=_days_off(loads, days[-1]),
        ea_7d=None,
        kcal_current=None,
        weight_kg=_last_value(store, "weight_kg", days),
        ffm_kg=_positive(profile.get("ffm_kg")),
        auto_changes_3wk_pct=_auto_changes_pct(ledger, now),
        red_flag_active=_red_flag_active(ledger, days[-1]),
        ed_watch=_ed_watch(store, days, rules),
        injury_constraints=_injury_constraints(ledger, profile),
    )


def _guard_dict(ctx: GuardContext) -> dict:
    raw = asdict(ctx)
    raw["weekly_loads"] = list(ctx.weekly_loads)
    return json.loads(json.dumps(raw, sort_keys=True, default=str))


def _weekly_loads(loads: Mapping[str, float | None], week_start: str) -> list[float]:
    """Last 4 weeks oldest -> newest; a week without any load row is left out."""
    out = []
    for back in (3, 2, 1, 0):
        values = [loads.get(_shift(week_start, k - 7 * back)) for k in range(7)]
        known = [float(v) for v in values if v is not None]
        if known:
            out.append(math.fsum(known))
    return out


def _hard_sessions(store: Store, days: Sequence[str]) -> int:
    rows = store.checkins(days[0], _shift(days[-1], 1), item=SESSION_ITEM)
    return sum(1 for r in rows if r.value is not None and r.value >= guardrails.HARD_RPE)


def _days_off(loads: Mapping[str, float | None], last_day: str) -> int:
    """Trailing days without training load; a missing day counts as off (stricter caps)."""
    n = 0
    while n < GUARD_HORIZON_DAYS and not loads.get(_shift(last_day, -n)):
        n += 1
    return n


def _last_value(store: Store, key: str, days: Sequence[str]) -> float | None:
    values = [r.value for r in store.daily(key, days[0], _shift(days[-1], 1)) if r.value is not None]
    return values[-1] if values else None


def _positive(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return None


def _load_increase_pct(ledger: Ledger, plan: Mapping) -> float:
    parent = plan.get("parent")
    if not isinstance(parent, int):
        return 0.0
    try:
        before = plans.weekly_load(plans.get(ledger, parent))
    except plans.PlanError:
        return 0.0
    if before <= 0:
        return 0.0
    return max(0.0, (plans.weekly_load(plan) / before - 1.0) * 100.0)


def _recent_auto(plan: Any, cutoff: datetime) -> bool:
    if not isinstance(plan, Mapping) or plan.get("ratified_by") != "auto":
        return False
    try:
        return _parse_utc(plan.get("effective_from", "")) >= cutoff
    except ValueError:
        return False


def _auto_changes_pct(ledger: Ledger, now: datetime) -> float:
    cutoff = _utc(now) - timedelta(days=21)
    total = 0.0
    for rel in ledger.glob("plans/v*.json"):
        plan = ledger.read_json(rel)
        if _recent_auto(plan, cutoff):
            total += _load_increase_pct(ledger, plan)
    return total


def _red_flag_active(ledger: Ledger, last_day: str) -> bool:
    since = _shift(last_day, 1 - RED_FLAG_DAYS)
    return any(str(a.get("date", ""))[:10] >= since for a in ledger.read_jsonl("data/alerts.jsonl"))


def _weigh_ins(store: Store, days: Sequence[str]) -> list[int]:
    stamps: dict[str, set[str]] = {d: set() for d in days}
    for metric in WEIGHT_METRICS:
        for s in store.samples(metric, days[0], _shift(days[-1], 1)):
            stamps.setdefault(s.ts[:10], set()).add(s.ts)
    return [len(stamps.get(d, ())) for d in days]


def _ed_watch(store: Store, days: Sequence[str], rules: Rules) -> bool:
    notes = [c.text for c in store.checkins(days[0], _shift(days[-1], 1), item=NOTE_ITEM) if c.text]
    return guardrails.ed_watch([], [], _weigh_ins(store, days), notes, rules)


def _injury_constraints(ledger: Ledger, profile: Mapping[str, Any]) -> dict:
    base = dict(profile.get("injury_constraints") or {})
    try:
        library = plans.load_exercises(ledger)
    except plans.PlanError:
        library = {}
    base[guardrails.EXERCISE_FEATURES_KEY] = library
    return base


# --------------------------------------------------------------------------
# Refs and challenge filters


def _pointers(node: Any, prefix: str) -> Iterator[str]:
    if isinstance(node, Mapping):
        children: Any = ((str(k), v) for k, v in node.items())
    elif isinstance(node, list):
        children = ((str(i), v) for i, v in enumerate(node))
    else:
        return
    for key, child in children:
        path = f"{prefix}/{_escape(key)}"
        yield path
        yield from _pointers(child, path)


def known_refs(packet: Mapping, ledger: Ledger) -> set[str]:
    """Every JSON pointer into the packet plus existing ledger files outside ops, mail and quarantine."""
    base = f"reviews/{packet['week']}/packet.json#"
    refs = {base + p for p in _pointers(packet, "")}
    refs.update(p for p in ledger.glob("**/*") if not p.startswith(_REF_EXCLUDED))
    return refs


def drop_unreferenced(items: Sequence[Mapping], refs: set[str]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped): an item survives only when its ref resolves."""
    kept: list[dict] = []
    dropped: list[dict] = []
    for item in items:
        ref = item.get("ref")
        (kept if isinstance(ref, str) and ref in refs else dropped).append(dict(item))
    return kept, dropped


def restrict_round2(items: Sequence[Mapping], changed: set[str]) -> list[dict]:
    """Keep only items whose diff_paths intersect the paths changed in round 1."""
    return [dict(i) for i in items if set(i.get("diff_paths") or ()) & set(changed)]


def _filter_challenges(items: Sequence[Mapping], refs: set[str], round_: int,
                       changed: set[str] | None) -> tuple[list[dict], list[dict]]:
    kept, dropped = drop_unreferenced(items, refs)
    if round_ < MAX_ROUNDS:
        return kept, dropped
    narrowed = restrict_round2(kept, changed or set())
    return narrowed, dropped + [i for i in kept if i not in narrowed]


def _changed(old: Sequence[Mapping], new: Sequence[Mapping]) -> set[str]:
    """Paths of ops present in only one of the two diffs."""
    a, b = {_sha(op) for op in old}, {_sha(op) for op in new}
    return plans.changed_paths([op for op in [*old, *new] if (_sha(op) in a) != (_sha(op) in b)])


# --------------------------------------------------------------------------
# LLM calls


class _Rejected(Exception):
    """An output failed its checks twice."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class _Caller:
    roster: Mapping[str, BotSpec]
    providers: Mapping[str, Provider]
    budget: Budget
    prompts_dir: Path

    def request(self, bot: BotSpec, context: Mapping[str, Any], schema: Mapping[str, Any],
                untrusted: Mapping[str, str] | None = None) -> LLMRequest:
        system, user = agents.render_prompt(bot, self.prompts_dir, context, untrusted)
        return LLMRequest(bot=bot.name, system=system, messages=({"role": "user", "content": user},),
                          output_schema=schema, max_tokens=bot.max_tokens, effort=bot.effort)

    def ask(self, name: str, context: Mapping[str, Any], schema: Mapping[str, Any], run_id: str,
            check: Callable[[Any], list[str]] = _scan_problems,
            untrusted: Mapping[str, str] | None = None) -> tuple[dict, LLMResponse]:
        """One validated turn; outputs failing check are re-asked once, then rejected."""
        bot = self.roster[name]
        req = self.request(bot, context, schema, untrusted)
        resp = agents.call(bot, req, self.providers, self.budget, run_id)
        problems = check(resp.structured)
        if not problems:
            return dict(resp.structured or {}), resp
        retry = dataclasses.replace(req, messages=(
            *req.messages,
            {"role": "assistant", "content": resp.text},
            {"role": "user", "content": REASK_DE.format(problems=", ".join(problems))},
        ))
        resp = agents.call(bot, retry, self.providers, self.budget, run_id)
        problems = check(resp.structured)
        if problems:
            raise _Rejected(problems)
        return dict(resp.structured or {}), resp

    def provider(self, bot: BotSpec) -> Provider:
        found = self.providers.get(bot.provider)
        if found is None:
            raise agents.ProviderUnavailable(f"provider {bot.provider!r} is not configured")
        return found


def _caller(llm: Mapping[str, Any]) -> _Caller:
    missing = sorted({"roster", "providers", "budget"} - set(llm))
    if missing:
        raise TypeError(f"missing LLM arguments: {missing}")
    return _Caller(llm["roster"], llm["providers"], llm["budget"], Path(llm.get("prompts_dir") or PROMPTS_DIR))


# --------------------------------------------------------------------------
# Review loop


@dataclass(frozen=True)
class ReviewOutcome:
    week: str
    ruling: str
    verdict: Verdict | None
    pending_id: str | None
    open_blockers: tuple[str, ...]
    asks_owner: tuple[str, ...]
    messages: tuple[str, ...]
    aborted: str | None


@dataclass(frozen=True)
class _Step:
    name: str  # file stem
    bot: str
    kind: str
    round: int
    to: tuple[str, ...]


STEPS: Mapping[str, _Step] = {
    "draft": _Step("01-hippocampus-draft", "hippocampus", "review.draft", 0, ("amygdala",)),
    "challenge1": _Step("02-amygdala-r1", "amygdala", "challenge", 1, ("hippocampus",)),
    "answer1": _Step("03-hippocampus-r1", "hippocampus", "rebuttal", 1, ("amygdala",)),
    "challenge2": _Step("04-amygdala-r2", "amygdala", "challenge", 2, ("hippocampus",)),
    "answer2": _Step("05-hippocampus-r2", "hippocampus", "rebuttal", 2, ("amygdala",)),
    "ruling": _Step("06-cerebellum-ruling", "cerebellum", "ruling", 2, ("hippocampus", "amygdala")),
}


class _Abort(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


Shape = Callable[[Mapping[str, Any]], tuple[str, list[dict], dict]]


@dataclass
class _Review:
    week: str
    packet: Mapping[str, Any]
    sha: str
    refs: set[str]
    caller: _Caller
    ledger: Ledger
    guard_ctx: GuardContext
    rules: Rules
    plan: dict
    now: datetime
    thread: list[dict] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    hashes: set[str] = field(default_factory=set)

    @property
    def thread_id(self) -> str:
        return f"review/{self.week}"

    @property
    def ruling_ref(self) -> str:
        return _review_rel(self.week, STEPS["ruling"].name + ".json")


def run_review(
    week: str,
    packet: Mapping,
    *,
    roster: Mapping[str, BotSpec],
    providers: Mapping[str, Provider],
    budget: Budget,
    ledger: Ledger,
    guard_ctx: GuardContext,
    rules: Rules,
    prompts_dir: Path,
    now: datetime,
) -> ReviewOutcome:
    """Bounded argued review. Fails closed: any abort keeps the current plan."""
    if packet.get("week") != week:
        raise ValueError(f"packet is for {packet.get('week')!r}, not {week!r}")
    if ledger.halted():
        return ReviewOutcome(week, "keep", None, None, (), (), (), "halted")
    plan = _plan_or_none(ledger)
    if plan is None:
        return ReviewOutcome(week, "keep", None, None, (), (), (), "no_plan")
    rv = _Review(week, packet, packet_sha(packet), known_refs(packet, ledger),
                 _Caller(roster, providers, budget, Path(prompts_dir)), ledger, guard_ctx, rules, plan, now)
    return _guarded(rv)


def _guarded(rv: _Review) -> ReviewOutcome:
    try:
        return _loop(rv)
    except _Abort as exc:
        return _abort(rv, exc.reason)
    except _Rejected:
        return _abort(rv, "scan_text")
    except agents.BudgetExceeded:
        return _abort(rv, "budget")
    except agents.ProviderError:
        return _abort(rv, "provider")


def _loop(rv: _Review) -> ReviewOutcome:
    draft = _turn(rv, STEPS["draft"], {"task": "draft"}, DRAFT_SCHEMA, _draft_shape(rv))
    diff = [dict(op) for op in draft["plan_diff"]]
    blockers: dict[str, dict] = {}
    diff, changed = _round(rv, 1, diff, None, blockers)
    if changed:
        diff, _ = _round(rv, 2, diff, changed, blockers)
    return _rule(rv, diff, blockers)


def _round(rv: _Review, round_: int, diff: list[dict], changed: set[str] | None,
           blockers: dict[str, dict]) -> tuple[list[dict], set[str]]:
    """One challenge and, when anything survived the filters, one answer."""
    extra = {"task": "challenge", "round": round_, "changed_paths": sorted(changed) if changed else None}
    out = _turn(rv, STEPS[f"challenge{round_}"], extra, CHALLENGE_SCHEMA, _challenge_shape(rv, round_, changed))
    items, _ = _filter_challenges(out["items"], rv.refs, round_, changed)
    if not items:
        return diff, set()
    blockers.update({f"r{round_}:{i['cid']}": i for i in items if i["severity"] == "blocker"})
    answer = _turn(rv, STEPS[f"answer{round_}"], {"task": "answer"}, REBUTTAL_SCHEMA, _answer_shape(rv))
    _resolve(blockers, round_, answer["answers"], rv.refs)
    revised = [dict(op) for op in answer["plan_diff"]]
    return revised, _changed(diff, revised)


def _closes(answer: Mapping[str, Any], refs: set[str]) -> bool:
    if answer.get("action") == "accept":
        return True
    return answer.get("action") == "rebut" and answer.get("ref") in refs


def _resolve(blockers: dict[str, dict], round_: int, answers: Sequence[Mapping], refs: set[str]) -> None:
    for answer in answers:
        key = f"r{round_}:{answer.get('cid')}"
        if key in blockers and _closes(answer, refs):
            del blockers[key]


def _context(rv: _Review, step: _Step, extra: Mapping[str, Any]) -> dict:
    """Context for every review prompt; render_prompt ignores keys a template does not use."""
    return {
        "week": rv.week, "round": step.round, "packet": rv.packet, "thread": rv.thread,
        "changed_paths": None, "paper_meta": None, "known_refs": sorted(rv.refs),
        "goals_md": _goals(rv.ledger), "plan_current": rv.plan, "review": None, **extra,
    }


def _turn(rv: _Review, step: _Step, extra: Mapping[str, Any], schema: Mapping[str, Any], shape: Shape) -> dict:
    """Ask one bot, check the loop bounds, and store its message."""
    if len(rv.paths) >= MAX_MESSAGES:
        raise _Abort("loop")
    run_id = _run_id(rv.week, step.bot, step.round)
    structured, resp = rv.caller.ask(step.bot, _context(rv, step, extra), schema, run_id)
    digest = _sha(structured)
    if digest in rv.hashes:
        raise _Abort("loop")
    rv.hashes.add(digest)
    body, items, annotations = shape(structured)
    msg = agents.make_message(
        sender=step.bot, to=step.to, kind=step.kind, thread=rv.thread_id, round_=step.round,
        max_rounds=MAX_ROUNDS, refs=sorted(set(_refs_in(structured))), data_sha=rv.sha, run_id=run_id,
        model=resp.model, body=body, items=items, cc=() if step.bot == "cerebellum" else ("cerebellum",),
        created_at=_iso_utc(rv.now),
    )
    stored = {**msg, "payload": structured, **annotations}
    rel = _review_rel(rv.week, step.name + ".json")
    rv.ledger.write_json(rel, stored, author=step.bot)
    rv.paths.append(rel)
    rv.thread.append(stored)
    return structured


def _refs_in(node: Any) -> Iterator[str]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in ("ref", "refs"):
                yield from (r for r in ([value] if isinstance(value, str) else value or ()) if isinstance(r, str))
            else:
                yield from _refs_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs_in(value)


def _verdict_dict(v: Verdict) -> dict:
    return {"tier": v.tier, "reasons": list(v.reasons), "rule_ids": list(v.rule_ids)}


def _draft_shape(rv: _Review) -> Shape:
    def shape(s: Mapping[str, Any]) -> tuple[str, list[dict], dict]:
        return str(s["summary_md"]), [dict(c) for c in s["claims"]], {"verdict": _verdict_dict(_tier(rv, s["plan_diff"]))}
    return shape


def _challenge_shape(rv: _Review, round_: int, changed: set[str] | None) -> Shape:
    def shape(s: Mapping[str, Any]) -> tuple[str, list[dict], dict]:
        kept, dropped = _filter_challenges(s["items"], rv.refs, round_, changed)
        body = "\n".join(f"{i['cid']} ({i['severity']}): {i['claim']}" for i in kept) or "Keine Einwände."
        return body, kept, {"dropped": dropped}
    return shape


def _answer_shape(rv: _Review) -> Shape:
    def shape(s: Mapping[str, Any]) -> tuple[str, list[dict], dict]:
        body = "\n".join(f"{a['cid']}: {a['action']}" for a in s["answers"]) or "Keine Antworten."
        return body, [dict(a) for a in s["answers"]], {"verdict": _verdict_dict(_tier(rv, s["plan_diff"]))}
    return shape


def _ruling_shape(s: Mapping[str, Any]) -> tuple[str, list[dict], dict]:
    return str(s["rationale_md"]), [], {}


# -- guardrails before and after the ruling


def _stale(packet: Mapping[str, Any]) -> bool:
    fresh = packet.get("freshness")
    if not isinstance(fresh, Mapping) or not fresh:
        return True
    return any(not isinstance(v, Mapping) or v.get("stale", True) for v in fresh.values())


def _tier(rv: _Review, diff: Sequence[Mapping]) -> Verdict:
    """check_diff tier of diff against the current plan; stale data turns any load increase into reject."""
    try:
        new = plans.apply_diff(rv.plan, diff)
    except plans.PlanError:
        return Verdict("reject", ("Diff nicht anwendbar",), ("plan_error",))
    verdict = guardrails.check_diff(rv.plan, new, diff, rv.guard_ctx, rv.rules)
    if _stale(rv.packet) and plans.weekly_load(new) > plans.weekly_load(rv.plan):
        return Verdict("reject", ("Laststeigerung trotz veralteter Daten", *verdict.reasons),
                       ("stale_load_increase", *verdict.rule_ids))
    return verdict


def _subset(proposed: Sequence[Mapping], diff: Sequence[Mapping]) -> list[dict]:
    """Ops of the ruling that appear unchanged in the latest diff; Cerebellum may only drop ops."""
    allowed = {_sha(op) for op in diff}
    out: dict[str, dict] = {}
    for op in proposed:
        key = _sha(op)
        if key in allowed:
            out.setdefault(key, dict(op))
    return list(out.values())


def _op_refs(ops: Sequence[Mapping]) -> list[str]:
    return sorted({r for op in ops for r in op.get("refs", ()) if isinstance(r, str)})


def _review_view(rv: _Review, verdict: Verdict, blockers: Mapping[str, Any]) -> dict:
    return {
        "packet": {k: rv.packet.get(k) for k in _CEREBELLUM_PACKET_KEYS},
        "thread": rv.thread,
        "verdict": _verdict_dict(verdict),
        "open_blockers": sorted(blockers),
    }


def _rule(rv: _Review, diff: list[dict], blockers: dict[str, dict]) -> ReviewOutcome:
    extra = {"task": "ruling", "review": _review_view(rv, _tier(rv, diff), blockers)}
    out = _turn(rv, STEPS["ruling"], extra, RULING_SCHEMA, _ruling_shape)
    asks = tuple(str(q) for q in out["ask_owner_de"][:MAX_OWNER_ASKS])
    if blockers:
        return _hold(rv, blockers, asks)
    ops = _subset(out["plan_diff"], diff)
    if out["ruling"] == "keep" or not ops:
        return _finish(rv, "keep", None, None, asks)
    verdict = _tier(rv, ops)
    pending_id = _propose(rv, ops, verdict)
    if pending_id is None:
        return _finish(rv, "keep", verdict, None, asks)
    return _finish(rv, "ratify" if len(ops) == len(diff) else "partial", verdict, pending_id, asks)


def _propose(rv: _Review, ops: Sequence[Mapping], verdict: Verdict) -> str | None:
    if verdict.tier not in plans.PROPOSABLE_TIERS:
        return None
    try:
        pending = plans.propose(rv.ledger, ops, verdict.tier, verdict.reasons, _op_refs(ops), rv.ruling_ref, rv.now)
    except plans.PlanError:
        return None
    return pending.id


def _extended_valid_until(plan: Mapping[str, Any], now: datetime) -> str:
    raw = plan.get("valid_until")
    try:
        base = _parse_utc(raw) if raw else _utc(now)
    except ValueError:
        base = _utc(now)
    return _iso_utc(base + timedelta(days=EXTEND_DAYS))


def _hold(rv: _Review, blockers: Mapping[str, Any], asks: tuple[str, ...]) -> ReviewOutcome:
    """Open blocker: keep the plan, extend valid_until by 7 days, ask the owner."""
    ops = [{"op": "set", "path": "/valid_until", "value": _extended_valid_until(rv.plan, rv.now),
            "refs": [rv.ruling_ref]}]
    verdict = _tier(rv, ops)
    pending_id = _propose(rv, ops, verdict)
    return _finish(rv, "keep", verdict, pending_id, asks or (HOLD_ASK_DE,), tuple(sorted(blockers)))


def _owner_message(rv: _Review, sender: str, kind: str, body: str, items: Sequence[Mapping], model: str) -> str:
    msg = agents.make_message(
        sender=sender, to=["owner"], kind=kind, thread=rv.thread_id, round_=MAX_ROUNDS, max_rounds=MAX_ROUNDS,
        refs=[rv.ruling_ref] if rv.ruling_ref in rv.paths else [], data_sha=rv.sha,
        run_id=_run_id(rv.week, sender, kind), model=model, body=body, items=items, created_at=_iso_utc(rv.now),
    )
    rel = f"mail/owner/inbox/{msg['id']}.json"
    rv.ledger.write_json(rel, msg, author=sender)
    return rel


def _finish(rv: _Review, ruling: str, verdict: Verdict | None, pending_id: str | None,
            asks: tuple[str, ...], open_blockers: tuple[str, ...] = ()) -> ReviewOutcome:
    if asks:
        body = "\n".join(f"- {q}" for q in asks)
        model = rv.caller.roster["cerebellum"].model
        rv.paths.append(_owner_message(rv, "cerebellum", "ask.owner", body, [{"question_de": q} for q in asks], model))
    return ReviewOutcome(rv.week, ruling, verdict, pending_id, open_blockers, asks, tuple(rv.paths), None)


def _abort(rv: _Review, reason: str) -> ReviewOutcome:
    body = ABORT_DE.replace("$week", rv.week).replace("$reason", reason)
    rv.paths.append(_owner_message(rv, CODE_AUTHOR, "alert", body, [{"reason": reason}], "none"))
    return ReviewOutcome(rv.week, "keep", None, None, (), (), tuple(rv.paths), reason)


# --------------------------------------------------------------------------
# Saturday: Insula


def _checkin_scores(store: Store, days: Sequence[str]) -> dict:
    rows = store.checkins(days[0], _shift(days[-1], 1))
    out = {}
    for day in days:
        items = [r for r in rows if r.date == day]
        out[day] = {
            "z": subjective_scores(store, day),
            "symptoms": sorted(r.item[len(SYMPTOM_PREFIX):] for r in items if r.item.startswith(SYMPTOM_PREFIX) and r.value),
            "session_rpe": [r.value for r in items if r.item == SESSION_ITEM],
        }
    return out


def _notes(store: Store, days: Sequence[str]) -> str:
    rows = store.checkins(days[0], _shift(days[-1], 1), item=NOTE_ITEM)
    return "\n".join(f"{r.date}: {r.text}" for r in rows if r.text)


def _insula_items(caller: _Caller, week: str, store: Store, rec: Mapping) -> dict:
    days = _week_days(week)
    context = {"week": week, "reconciliation": rec, "checkin_scores": _checkin_scores(store, days)}

    def check(out: Any) -> list[str]:
        return _scan_problems(out) + insula.output_errors(out or {}, rec)

    try:
        out, _ = caller.ask("insula", context, insula.INSULA_OUTPUT_SCHEMA, _run_id(week, "insula", 0), check,
                            {"checkin_notes": _notes(store, days)})
    except _Rejected as exc:
        return {"items": [], "errors": list(exc.problems)}
    return {"items": [dict(i) for i in out["items"]], "errors": []}


def run_insula(week: str, ledger: Ledger, store: Store, cfg: MetricsConfig, **llm: Any) -> dict:
    """Write data/conflicts/<week>.json (code) and Insula's explanation of each conflict.

    llm: roster, providers, budget, optional prompts_dir. Without conflicts no LLM call is made.
    """
    caller = _caller(llm)
    rec = insula.weekly_reconciliation(store, week, cfg)
    conflicts_rel = f"data/conflicts/{week}.json"
    ledger.write_json(conflicts_rel, rec, author=CODE_AUTHOR)
    result: dict[str, Any] = {"week": week, "ref": conflicts_rel, "items": [], "errors": []}
    if rec["conflicts"]:
        result.update(_insula_items(caller, week, store, rec))
    ledger.write_json(_review_rel(week, INSULA_FILE), result, author="insula")
    return result


# --------------------------------------------------------------------------
# Saturday: Motor Cortex


def _novel_counts(plan: Mapping[str, Any]) -> dict:
    sessions = {str(s["id"]): sum(1 for ex in s.get("exercises", []) if ex.get("novel"))
                for s in plan.get("sessions", [])}
    return {"sessions": sessions, "week": sum(sessions.values())}


def _spindle_candidates(ledger: Ledger, days: Sequence[str]) -> list[dict]:
    out = []
    for day in days:
        for rec in ledger.read_jsonl(f"evidence/candidates/{day}.jsonl"):
            exercise = (rec.get("triage") or {}).get("exercise")
            if rec.get("scout") == "spindle" and exercise:
                out.append({"id": rec.get("id"), "exercise": exercise})
    return out


def _mc_inputs(ledger: Ledger, week: str, plan: Mapping[str, Any], constraints: Mapping[str, Any]) -> dict:
    return {
        "baselines": ledger.read_json(f"data/baselines/{week}.json"),
        "freshness": ledger.read_json("data/freshness.json"),
        "injury_constraints": {k: v for k, v in constraints.items() if k != guardrails.EXERCISE_FEATURES_KEY},
        "novelty": _novel_counts(plan),
        "spindle_candidates": _spindle_candidates(ledger, _week_days(week)),
    }


def _features(exercise: Mapping[str, Any], library: Mapping[str, Mapping]) -> dict:
    """Library record when the id is known, else the proposed record with joint_load as a mapping."""
    eid = str(exercise.get("exercise_id"))
    if eid in library:
        return dict(library[eid])
    joints = {str(j.get("joint")): j.get("level") for j in exercise.get("joint_load") or ()}
    return {**{k: exercise.get(k) for k in ("exercise_id", "pattern", "contraction", "energy_system")},
            "joint_load": joints}


def _vocabulary_problems(feats: Mapping[str, Any], library: Mapping[str, Mapping]) -> list[str]:
    out = []
    for key in ("pattern", "contraction", "energy_system"):
        known = {str(r.get(key)) for r in library.values()}
        if str(feats.get(key)) not in known:
            out.append(f"vocabulary:{key}")
    return out


def _proposal_problems(p: Mapping[str, Any], plan: Mapping[str, Any], library: Mapping[str, Mapping],
                       constraints: Mapping[str, Any]) -> list[str]:
    problems = []
    try:
        plans.apply_diff(plan, p["plan_diff"])
    except plans.PlanError:
        problems.append("diff_invalid")
    exercise = p.get("exercise")
    if exercise is not None:
        feats = _features(exercise, library)
        problems += _vocabulary_problems(feats, library) + guardrails.exercise_admissible(feats, constraints)
    return problems


def _novel_cap_problems(p: Mapping[str, Any], counts: Mapping[str, Any], rules: Rules) -> list[str]:
    out = []
    if counts["sessions"].get(str(p["session_id"]), 0) >= rules.novel_per_session:
        out.append("novel_per_session")
    if counts["week"] >= rules.novel_per_week:
        out.append("novel_per_week")
    return out


def _solve(proposals: Sequence[Mapping], plan: Mapping[str, Any], library: Mapping[str, Mapping],
           constraints: Mapping[str, Any], rules: Rules) -> tuple[list[dict], list[dict]]:
    """Admit proposals whose diff applies, whose features satisfy the constraints and the novelty caps."""
    counts = _novel_counts(plan)
    admitted: list[dict] = []
    rejected: list[dict] = []
    for p in proposals:
        problems = _proposal_problems(p, plan, library, constraints)
        if not problems and p.get("novel"):
            problems = _novel_cap_problems(p, counts, rules)
        if problems:
            rejected.append({**p, "violations": problems})
            continue
        admitted.append({**p, "violations": []})
        if p.get("novel"):
            sid = str(p["session_id"])
            counts["sessions"][sid] = counts["sessions"].get(sid, 0) + 1
            counts["week"] += 1
    return admitted, rejected


def run_motor_cortex(week: str, ledger: Ledger, rules: Rules, **llm: Any) -> dict:
    """Motor Cortex proposals, filtered by the solver (guardrails.exercise_admissible and novelty caps).

    llm: roster, providers, budget, optional prompts_dir.
    """
    caller = _caller(llm)
    plan = plans.current(ledger)
    library = plans.load_exercises(ledger)
    constraints = _injury_constraints(ledger, _profile(ledger))
    context = {"week": week, "goals_md": _goals(ledger), "plan_current": plan, "exercises": library,
               "inputs": _mc_inputs(ledger, week, plan, constraints)}
    errors: list[str] = []
    try:
        out, _ = caller.ask("motor_cortex", context, MOTOR_CORTEX_SCHEMA, _run_id(week, "motor_cortex", 0))
    except _Rejected as exc:
        out, errors = {"proposals": []}, list(exc.problems)
    admitted, rejected = _solve(out["proposals"], plan, library, constraints, rules)
    result = {"week": week, "admitted": admitted, "rejected": rejected, "errors": errors}
    ledger.write_json(_review_rel(week, MOTOR_CORTEX_FILE), result, author="motor_cortex")
    return result


# --------------------------------------------------------------------------
# Thursday: Thalamus


def _paper_rel(key: str) -> str:
    return f"evidence/papers/{evidence.slug(key)}.json"


def _extractable(ledger: Ledger, rec: Mapping[str, Any], seen: set[str]) -> bool:
    key = rec.get("doi") or rec.get("pmid")
    path = rec.get("quarantine_path")
    if not key or not path or rec.get("id") in seen:
        return False
    if (rec.get("triage") or {}).get("relevance") == 0:
        return False
    return ledger.exists(path) and not ledger.exists(_paper_rel(str(key)))


def _thalamus_candidates(ledger: Ledger, top_n: int) -> list[dict]:
    seen: set[str] = set()
    out = []
    for rel in ledger.glob("evidence/candidates/*.jsonl"):
        for rec in ledger.read_jsonl(rel):
            if _extractable(ledger, rec, seen):
                seen.add(rec["id"])
                out.append(rec)
    out.sort(key=lambda r: (-float(r.get("score") or 0.0), str(r["id"])))
    return out[: max(0, top_n)]


def _paper_meta(cand: Mapping[str, Any]) -> dict:
    return {k: cand.get(k) for k in ("id", "doi", "pmid", "title", "source_api", "preprint")}


def _submit_thalamus(caller: _Caller, ledger: Ledger, now: datetime, top_n: int) -> dict | None:
    cands = _thalamus_candidates(ledger, top_n)
    if not cands:
        return None
    bot = caller.roster["thalamus"]
    items = [(str(c["id"]), caller.request(bot, {"paper_meta": _paper_meta(c)}, evidence.PAPER_SCHEMA,
                                            {"abstract": ledger.read_text(c["quarantine_path"])})) for c in cands]
    estimate = sum(agents.estimate_usd(req, bot.model, caller.budget.prices) for _, req in items) * 0.5
    if not caller.budget.allow(bot, estimate):
        raise agents.BudgetExceeded("budget refuses the thalamus batch")
    batch_id = caller.provider(bot).submit_batch(items, bot.model)
    state = {"schema": 1, "status": "submitted", "batch_id": batch_id, "submitted_at": _iso_utc(now),
             "candidates": {str(c["id"]): c for c in cands}}
    ledger.write_json(THALAMUS_BATCH, state, author=CODE_AUTHOR)
    return state


@dataclass(frozen=True)
class _Thalamus:
    caller: _Caller
    ledger: Ledger
    http: Any
    owner: OwnerProfile
    now: datetime


def _crossref(http: Any, cand: Mapping[str, Any]) -> dict | None:
    """Crossref work and update notices; None when the lookup failed (retry next run)."""
    doi = cand.get("doi")
    if not doi:
        return {"work": None, "update_to": [], "title_match": None}
    try:
        work = scouts.resolve_doi(http, str(doi))
        updates = scouts.crossref_updates(http, str(doi))
    except _TRANSPORT_ERRORS:
        return None
    match = scouts.title_match(work.get("title") or "", str(cand.get("title") or "")) if work else None
    return {"work": work, "update_to": updates, "title_match": match}


def _meta(cand: Mapping[str, Any], work: Mapping[str, Any] | None) -> dict:
    if not work:
        return {"title": cand.get("title"), "authors": [], "journal": None, "year": cand.get("year")}
    return {k: work.get(k) for k in ("title", "authors", "journal", "year")}


def _swc(paper: Mapping[str, Any], owner: OwnerProfile) -> float | None:
    name = _plain((paper.get("outcome") or {}).get("name"))
    return owner.swc.get(name) if isinstance(name, str) else None


def _audit(th: _Thalamus, cand: Mapping[str, Any], abstract: str, paper: Mapping[str, Any], rel: str) -> dict:
    """Amygdala re-extracts independently; any disagreeing field blocks scoring."""
    context = {"task": "audit", "week": _iso_week(th.now), "round": 0, "packet": None, "thread": [],
               "changed_paths": None, "paper_meta": _paper_meta(cand), "known_refs": []}
    other, _ = th.caller.ask("amygdala", context, evidence.PAPER_SCHEMA, _run_id(_iso_week(th.now), "amygdala", "audit"),
                             _no_check, {"abstract": abstract})
    other = evidence.void_fields(other, evidence.verify_quotes(other, abstract))
    disagreements = evidence.compare_extractions(paper, other)
    audit_rel = rel[: -len(".json")] + ".audit.json"
    th.ledger.write_json(audit_rel, {"schema": 1, "candidate_id": cand.get("id"), "extraction": other,
                                     "disagreements": disagreements}, author="amygdala")
    return {"audit_ref": audit_rel, "audit_disagreements": disagreements, "blocked": bool(disagreements)}


def _record(th: _Thalamus, cand: Mapping[str, Any], paper: dict, voided: list[str], cref: Mapping, model: str) -> dict:
    match = cref["title_match"]
    return {
        "schema": 1, "rubric_version": evidence.RUBRIC_VERSION, "candidate_id": cand.get("id"),
        "doi": cand.get("doi"), "pmid": cand.get("pmid"), "meta": _meta(cand, cref["work"]),
        "update_to": list(cref["update_to"]), "title_match": match,
        "resolved": cref["work"] is not None and match is not None and match >= TITLE_MATCH_MIN,
        "extraction": paper, "voided_fields": voided, "audit_ref": None, "audit_disagreements": [],
        "blocked": False, "score": None, "extracted_at": _iso_utc(th.now), "model": model,
    }


def _extract_one(th: _Thalamus, cand: Mapping[str, Any], resp: LLMResponse, audit: bool) -> str | None:
    paper = resp.structured
    if not isinstance(paper, Mapping) or agents.validate_json(paper, evidence.PAPER_SCHEMA):
        return None
    cref = _crossref(th.http, cand)
    if cref is None:
        return None
    abstract = th.ledger.read_text(cand["quarantine_path"])
    voided = evidence.verify_quotes(paper, abstract)
    clean = evidence.void_fields(paper, voided)
    rel = _paper_rel(str(cand.get("doi") or cand.get("pmid")))
    record = _record(th, cand, clean, voided, cref, resp.model)
    if audit:
        record.update(_audit(th, cand, abstract, clean, rel))
    if not record["blocked"]:
        score = evidence.score(clean, th.owner, update_to=record["update_to"], swc_paper_units=_swc(clean, th.owner))
        record["score"] = asdict(score)
    th.ledger.write_json(rel, record, author="thalamus")
    return rel


def _audit_ids(ids: Sequence[str], share: float) -> set[str]:
    """Deterministic sample: ceil(share * n) ids ordered by their sha256."""
    if share <= 0 or not ids:
        return set()
    n = min(len(ids), math.ceil(share * len(ids)))
    return set(sorted(ids, key=lambda i: hashlib.sha256(i.encode("utf-8")).hexdigest())[:n])


def _process_batch(th: _Thalamus, state: Mapping[str, Any], results: Mapping[str, LLMResponse],
                   audit_share: float) -> list[str]:
    run_id = _run_id(_iso_week(th.now), "thalamus", "batch")
    ids = sorted(cid for cid in state["candidates"] if cid in results)
    audited = _audit_ids(ids, audit_share)
    written = []
    for cid in ids:
        th.caller.budget.record("thalamus", results[cid], run_id)
        rel = _extract_one(th, state["candidates"][cid], results[cid], cid in audited)
        if rel is not None:
            written.append(rel)
    return written


def run_thalamus(ledger: Ledger, http: "scouts.Http", owner: OwnerProfile, now: datetime, top_n: int = 10,
                 audit_share: float = 0.1, **llm: Any) -> list[str]:
    """Batch extraction of the top candidates, then quotes, Crossref, audit and score. Returns written paper paths.

    The batch id is kept in evidence/batches/thalamus.json, so a run that finds
    the batch still processing returns [] and a later run collects it.
    llm: roster, providers, budget, optional prompts_dir.
    """
    caller = _caller(llm)
    state = ledger.read_json(THALAMUS_BATCH)
    if not isinstance(state, Mapping) or state.get("status") != "submitted":
        state = _submit_thalamus(caller, ledger, now, top_n)
        if state is None:
            return []
    results = caller.provider(caller.roster["thalamus"]).batch_results(str(state["batch_id"]))
    if results is None:
        return []
    written = _process_batch(_Thalamus(caller, ledger, http, owner, now), state, results, audit_share)
    ledger.write_json(THALAMUS_BATCH, {**state, "status": "done", "done_at": _iso_utc(now)}, author=CODE_AUTHOR)
    return written

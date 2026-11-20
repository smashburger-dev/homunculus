"""Event-sourced belief ledger.

``beliefs/events.jsonl`` is append-only. ``fold(events, as_of)`` rebuilds every
belief view from scratch, so a retraction or a late piece of evidence simply
changes what the next fold sees.

Event payloads (all events carry ``schema``, ``kind``, ``belief_id``, ``at``):

* ``prior_set``: ``claim`` (Claim fields), ``swc``, optional ``swc_harm``
  (defaults to ``swc``), optional ``prior`` (Prior fields or null) and optional
  ``q_per_day`` (variance growth override). Creates or redefines the belief.
* ``evidence_added``: ``prior`` (Prior fields), one more literature component.
* ``personal_result``: ``est`` and ``se`` (> 0) in outcome units, extra keys
  such as ``experiment`` are kept in ``Belief.personal``.
* ``source_retracted``: ``source_key``. Every prior component citing it is
  dropped and every decision citing it is flagged.
* ``decision``: ``status`` (any of STATUSES except 'stale'), optional ``cites``
  (defaults to the prior's sources at that moment) and ``review_after``.
* ``review``: optional ``review_after``; otherwise ``at`` + 90 days.

Effects are stored in raw outcome units. ``Claim.direction`` flips the sign
before P(benefit) and P(harm) are computed, and ``swc``/``swc_harm`` are
positive magnitudes. Times are ISO dates (midnight UTC) or ISO timestamps;
naive timestamps count as UTC.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from . import stats
from .store import Ledger, canonical_json

EVENT_KINDS = ("prior_set", "evidence_added", "personal_result", "source_retracted", "decision", "review")
STATUSES = ("hypothesis", "testing", "adopted", "rejected", "stale")

EVENTS_PATH = "beliefs/events.jsonl"
VIEWS_DIR = "beliefs/views"
SCHEMA = 1
REVIEW_DAYS = 90
STALE_THRESHOLD = 0.8
DAYS_PER_YEAR = 365.0

_RESERVED = frozenset({"schema", "kind", "belief_id", "at"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class Prior:
    mu0: float
    sd0: float
    sources: tuple[str, ...]
    certainty: float
    rubric_version: str


@dataclass(frozen=True)
class Claim:
    population: str
    intervention: str
    comparator: str
    outcome: str
    unit: str
    direction: int  # +1 higher is better, -1 lower is better


@dataclass
class Belief:
    id: str
    claim: Claim
    prior: Prior | None
    personal: list[dict]
    mu: float | None
    sd: float | None
    p_benefit: float | None
    p_harm: float | None
    status: str
    as_of: str
    review_after: str | None
    decisions: list[dict]
    swc: float
    swc_harm: float


# --- time ---------------------------------------------------------------------


def _day(at: str) -> float:
    """Fractional day number (proleptic ordinal, UTC) of an ISO date or timestamp."""
    if len(at) == 10:
        return float(date.fromisoformat(at).toordinal())
    dt = datetime.fromisoformat(at)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    seconds = dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6
    return dt.toordinal() + seconds / 86400.0


def _date_plus(at: str, days: int) -> str:
    return (date.fromordinal(int(_day(at))) + timedelta(days=days)).isoformat()


# --- events -------------------------------------------------------------------


def _check_id(belief_id: str) -> None:
    if not isinstance(belief_id, str) or not _ID_RE.match(belief_id):
        raise ValueError(f"invalid belief id: {belief_id!r}")


def _plain(value: Any) -> Any:
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else value


def make_event(kind: str, belief_id: str, at: str, **data: Any) -> dict:
    """Validated, JSON-ready event dict; dataclass values become plain dicts."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"invalid event kind: {kind!r}")
    _check_id(belief_id)
    _day(at)
    clash = _RESERVED & data.keys()
    if clash:
        raise ValueError(f"reserved event keys: {sorted(clash)}")
    event = {"schema": SCHEMA, "kind": kind, "belief_id": belief_id, "at": at}
    event.update({k: _plain(v) for k, v in data.items()})
    return json.loads(canonical_json(event))


# --- math ---------------------------------------------------------------------


def _check_sd(sd: float) -> None:
    if not sd > 0.0:
        raise ValueError("sd must be > 0")


def p_benefit(mu: float, sd: float, swc: float) -> float:
    """P(effect > swc) under N(mu, sd^2)."""
    _check_sd(sd)
    return 1.0 - stats.phi((swc - mu) / sd)


def p_harm(mu: float, sd: float, swc_harm: float) -> float:
    """P(effect < -swc_harm) under N(mu, sd^2)."""
    _check_sd(sd)
    return stats.phi((-swc_harm - mu) / sd)


def decayed_sd(sd1: float, q_per_day: float, days: float) -> float:
    """Random-walk variance growth: sqrt(sd1^2 + q * days)."""
    if q_per_day < 0.0 or days < 0.0:
        raise ValueError("q_per_day and days must be >= 0")
    return math.sqrt(sd1 * sd1 + q_per_day * days)


def _dedupe(priors: Sequence[Prior]) -> list[Prior]:
    """Keep each prior whose sources are not already covered by an earlier one."""
    seen: set[str] = set()
    kept: list[Prior] = []
    for prior in priors:
        if seen.isdisjoint(prior.sources):
            kept.append(prior)
            seen.update(prior.sources)
    return kept


def combine_priors(priors: Sequence[Prior]) -> Prior:
    """Precision-weighted combination; a prior repeating a source key is skipped.

    Certainty is the precision-weighted mean of the kept certainties.
    """
    kept = _dedupe(priors)
    if not kept:
        raise ValueError("combine_priors needs at least one prior")
    for prior in kept:
        _check_sd(prior.sd0)
    weights = [1.0 / (p.sd0 * p.sd0) for p in kept]
    total = sum(weights)
    mu0 = sum(w * p.mu0 for w, p in zip(weights, kept)) / total
    certainty = sum(w * p.certainty for w, p in zip(weights, kept)) / total
    sources = tuple(s for p in kept for s in p.sources)
    rubric = "+".join(sorted({p.rubric_version for p in kept}))
    return Prior(mu0, math.sqrt(1.0 / total), sources, certainty, rubric)


# --- fold state ---------------------------------------------------------------


@dataclass
class _State:
    claim: Claim
    swc: float
    swc_harm: float
    q_override: float | None
    created: str
    components: list[tuple[float, Prior]] = field(default_factory=list)
    personal: list[dict] = field(default_factory=list)
    retracted: set[str] = field(default_factory=set)
    decisions: list[dict] = field(default_factory=list)
    status: str = "hypothesis"
    review_after: str | None = None


def _prior_from(data: Mapping[str, Any]) -> Prior:
    prior = Prior(
        mu0=float(data["mu0"]),
        sd0=float(data["sd0"]),
        sources=tuple(str(s) for s in data["sources"]),
        certainty=float(data["certainty"]),
        rubric_version=str(data["rubric_version"]),
    )
    _check_sd(prior.sd0)
    return prior


def _claim_from(data: Mapping[str, Any]) -> Claim:
    direction = int(data["direction"])
    if direction not in (1, -1):
        raise ValueError(f"claim direction must be +1 or -1: {direction!r}")
    keys = ("population", "intervention", "comparator", "outcome", "unit")
    return Claim(*(str(data[k]) for k in keys), direction=direction)


def _kept_components(st: _State) -> list[tuple[float, Prior]]:
    return [(d, p) for d, p in st.components if st.retracted.isdisjoint(p.sources)]


def _current_prior(st: _State) -> Prior | None:
    kept = _kept_components(st)
    return combine_priors([p for _, p in kept]) if kept else None


def _on_prior_set(st: _State | None, ev: Mapping[str, Any], day: float) -> _State:
    swc = float(ev["swc"])
    q = ev.get("q_per_day")
    new = _State(
        claim=_claim_from(ev["claim"]),
        swc=swc,
        swc_harm=float(ev.get("swc_harm", swc)),
        q_override=None if q is None else float(q),
        created=st.created if st else ev["at"],
    )
    if st is not None:
        new.personal, new.retracted, new.decisions = st.personal, st.retracted, st.decisions
        new.status, new.review_after = st.status, st.review_after
    if ev.get("prior") is not None:
        new.components.append((day, _prior_from(ev["prior"])))
    return new


def _on_evidence(st: _State, ev: Mapping[str, Any], day: float) -> _State:
    st.components.append((day, _prior_from(ev["prior"])))
    return st


def _on_personal(st: _State, ev: Mapping[str, Any], day: float) -> _State:
    entry = {k: v for k, v in ev.items() if k not in ("schema", "kind", "belief_id")}
    entry["est"], entry["se"] = float(ev["est"]), float(ev["se"])
    _check_sd(entry["se"])
    st.personal.append(entry)
    return st


def _on_retracted(st: _State, ev: Mapping[str, Any], day: float) -> _State:
    st.retracted.add(str(ev["source_key"]))
    return st


def _review_date(ev: Mapping[str, Any]) -> str:
    explicit = ev.get("review_after")
    return str(explicit) if explicit else _date_plus(ev["at"], REVIEW_DAYS)


def _on_decision(st: _State, ev: Mapping[str, Any], day: float) -> _State:
    status = ev["status"]
    if status not in STATUSES or status == "stale":
        raise ValueError(f"invalid decision status: {status!r}")
    entry = {k: v for k, v in ev.items() if k not in ("schema", "kind", "belief_id")}
    if "cites" not in entry:
        prior = _current_prior(st)
        entry["cites"] = list(prior.sources) if prior else []
    st.decisions.append(entry)
    st.status = status
    st.review_after = _review_date(ev)
    return st


def _on_review(st: _State, ev: Mapping[str, Any], day: float) -> _State:
    st.review_after = _review_date(ev)
    return st


_HANDLERS = {
    "evidence_added": _on_evidence,
    "personal_result": _on_personal,
    "source_retracted": _on_retracted,
    "decision": _on_decision,
    "review": _on_review,
}


def _apply(states: dict[str, _State], ev: Mapping[str, Any], day: float) -> None:
    kind, bid = ev["kind"], ev["belief_id"]
    if kind not in EVENT_KINDS:
        raise ValueError(f"invalid event kind: {kind!r}")
    if kind == "prior_set":
        states[bid] = _on_prior_set(states.get(bid), ev, day)
        return
    if bid not in states:
        raise ValueError(f"{kind} event before prior_set for belief {bid!r}")
    states[bid] = _HANDLERS[kind](states[bid], ev, day)


# --- posterior ----------------------------------------------------------------


@dataclass(frozen=True)
class _Posterior:
    mu: float
    sd_last: float  # right after the last data update
    sd_now: float  # decayed to as_of


def _start(prior: Prior | None, prior_day: float, personal: list[dict]) -> tuple[float, float, float, list[dict]] | None:
    """(mu, sd, day, remaining results). Without a prior the first result seeds it."""
    if prior is not None:
        return prior.mu0, prior.sd0, prior_day, personal
    if not personal:
        return None
    first = personal[0]
    return first["est"], first["se"], _day(first["at"]), personal[1:]


def _posterior(st: _State, as_of_day: float) -> tuple[Prior | None, _Posterior | None]:
    kept = _kept_components(st)
    prior = combine_priors([p for _, p in kept]) if kept else None
    prior_day = max((d for d, _ in kept), default=0.0)
    results = sorted(st.personal, key=lambda r: _day(r["at"]))
    start = _start(prior, prior_day, results)
    if start is None:
        return prior, None
    mu, sd, t, rest = start
    q = st.q_override if st.q_override is not None else sd * sd / DAYS_PER_YEAR
    for r in rest:
        r_day = _day(r["at"])
        sd = decayed_sd(sd, q, max(0.0, r_day - t))
        mu, sd = stats.normal_update(mu, sd, r["est"], r["se"])
        t = max(t, r_day)
    return prior, _Posterior(mu, sd, decayed_sd(sd, q, max(0.0, as_of_day - t)))


def _status(st: _State, post: _Posterior | None) -> str:
    """'stale' when decay alone pushed an adopted belief below the threshold."""
    if st.status != "adopted" or post is None:
        return st.status
    mu = st.claim.direction * post.mu
    before = p_benefit(mu, post.sd_last, st.swc)
    after = p_benefit(mu, post.sd_now, st.swc)
    return "stale" if before >= STALE_THRESHOLD > after else st.status


def _flag_decisions(st: _State) -> list[dict]:
    out = []
    for d in st.decisions:
        hit = sorted(st.retracted.intersection(d.get("cites", ())))
        out.append({**d, "flagged": bool(hit), "retracted_sources": hit})
    return out


def _belief(bid: str, st: _State, as_of: str) -> Belief:
    prior, post = _posterior(st, _day(as_of))
    mu = sd = pb = ph = None
    if post is not None:
        mu, sd = post.mu, post.sd_now
        signed = st.claim.direction * mu
        pb, ph = p_benefit(signed, sd, st.swc), p_harm(signed, sd, st.swc_harm)
    return Belief(
        id=bid, claim=st.claim, prior=prior, personal=[dict(r) for r in st.personal],
        mu=mu, sd=sd, p_benefit=pb, p_harm=ph, status=_status(st, post), as_of=as_of,
        review_after=st.review_after or _date_plus(st.created, REVIEW_DAYS),
        decisions=_flag_decisions(st), swc=st.swc, swc_harm=st.swc_harm,
    )


def _timeline(events: Iterable[Mapping], as_of_day: float) -> list[tuple[float, Mapping]]:
    """Events up to as_of, stably sorted by time (file order breaks ties)."""
    timed = []
    for ev in events:
        _check_id(ev["belief_id"])
        day = _day(ev["at"])
        if day <= as_of_day:
            timed.append((day, ev))
    timed.sort(key=lambda pair: pair[0])
    return timed


def fold(events: Iterable[Mapping], as_of: str) -> dict[str, Belief]:
    """Deterministically rebuild every belief from its events up to as_of."""
    states: dict[str, _State] = {}
    for day, ev in _timeline(events, _day(as_of)):
        _apply(states, ev, day)
    return {bid: _belief(bid, states[bid], as_of) for bid in sorted(states)}


# --- queries and IO -----------------------------------------------------------


def _cites(belief: Belief, source_key: str) -> bool:
    if belief.prior is not None and source_key in belief.prior.sources:
        return True
    return any(source_key in d.get("cites", ()) for d in belief.decisions)


def retract(beliefs: Mapping[str, Belief], source_key: str, at: str) -> list[dict]:
    """One source_retracted event per belief whose prior or decisions cite source_key."""
    hits = sorted(bid for bid, b in beliefs.items() if _cites(b, source_key))
    return [make_event("source_retracted", bid, at, source_key=source_key) for bid in hits]


def _due(belief: Belief, as_of_day: float) -> bool:
    if belief.status == "stale":
        return True
    return belief.review_after is not None and _day(belief.review_after) <= as_of_day


def due_for_review(beliefs: Mapping[str, Belief], as_of: str) -> list[str]:
    """Sorted ids of stale beliefs and beliefs whose review_after has passed."""
    day = _day(as_of)
    return sorted(bid for bid, b in beliefs.items() if _due(b, day))


def rebuild_views(ledger: Ledger, as_of: str, *, author: str = "pineal") -> list[str]:
    """Fold beliefs/events.jsonl and write beliefs/views/<id>.json; returns the paths."""
    beliefs = fold(ledger.read_jsonl(EVENTS_PATH), as_of)
    written = []
    for bid, belief in beliefs.items():
        rel = f"{VIEWS_DIR}/{bid}.json"
        ledger.write_json(rel, asdict(belief), author=author)
        written.append(rel)
    return written

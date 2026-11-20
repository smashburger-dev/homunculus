"""N-of-1 experiments: eligibility, MDE, preregistered allocation, analysis.

Designs:

* ``paired_blocks``: ``pairs`` pairs of two ``block_days`` blocks, AB or BA
  per pair. The first ``washout_days`` of every block are labeled
  ``washout`` and never analyzed.
* ``daily_alternating``: ``pairs`` periods of ``block_days`` days. Each period
  gets k A days, k drawn from ``a_days_per_week = (min_a, max_a)``, placed at
  random; the rest are B days. No washout.

A is the intervention, B the comparator. A pair difference is
mean(A days) - mean(B days) in raw outcome units, the same units as the
belief prior. The allocation seed comes from the canonical protocol bytes,
so nobody can re-roll after peeking. Each pair draws from the generator in
order, so an extension by 2 pairs (``extra_pairs``) keeps the first pairs.

Exclusion rules (``Protocol.exclusions``), each a mapping with ``kind``:

* ``{"kind": "checkin_flag", "flag": str}``: the day carries that check-in flag.
* ``{"kind": "missing", "key": str}``: the daily value for ``key`` is absent.
* ``{"kind": "threshold", "key": str, "op": ">"|">="|"<"|"<="|"=="|"!=",
  "value": float}``: the daily value for ``key`` satisfies the comparison.

Every rule takes an optional ``days_after`` (int >= 0) that also excludes
that many following days.
"""

from __future__ import annotations

import hashlib
import json
import math
import operator
import random
import re
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from . import beliefs, stats
from .beliefs import Prior
from .beliefs import p_benefit as _p_benefit
from .beliefs import p_harm as _p_harm
from .store import SOURCES, Ledger, canonical_json

DESIGNS = ("paired_blocks", "daily_alternating")
LABELS = ("A", "B", "washout")
DECISIONS = ("adopt", "reject", "inconclusive", "extend")
FINAL_DECISIONS = ("adopt", "reject", "inconclusive")

MIN_PAIRS = 6
MIN_BASELINE_DAYS = 21
MIN_ANALYSIS_PAIRS = 4
EXTEND_PAIRS = 2
SIGN_FLIP_EXACT_MAX = 20
ADOPT_BENEFIT = 0.8
ADOPT_HARM_BELOW = 0.05
REJECT_BENEFIT_BELOW = 0.2
RHO_MAX = 0.95

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt, ">=": operator.ge, "<": operator.lt,
    "<=": operator.le, "==": operator.eq, "!=": operator.ne,
}
_RULE_FIELDS = {"checkin_flag": ("flag",), "missing": ("key",), "threshold": ("key", "op", "value")}
_RULE_OPTIONAL = ("kind", "days_after")


class ProtocolError(ValueError):
    """Invalid protocol, allocation or analysis input."""


# --- protocol -------------------------------------------------------------------


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _fail(cond: bool, message: str) -> None:
    if cond:
        raise ProtocolError(message)


def _check_ident(p: "Protocol") -> None:
    _fail(not isinstance(p.id, str) or not _ID_RE.match(p.id), f"invalid experiment id: {p.id!r}")
    for name in ("belief_id", "intervention", "comparator", "outcome_key", "source"):
        _fail(not isinstance(getattr(p, name), str), f"{name} must be a string")
    _fail(not p.belief_id or not p.outcome_key, "belief_id and outcome_key must be non-empty")


def _check_layout(p: "Protocol") -> None:
    _fail(p.design not in DESIGNS, f"design must be one of {DESIGNS}")
    _fail(not all(_is_int(v) for v in (p.block_days, p.washout_days, p.pairs)), "day counts must be int")
    _fail(p.block_days < 1 or p.pairs < 1, "block_days and pairs must be >= 1")
    _fail(not 0 <= p.washout_days < p.block_days, "need 0 <= washout_days < block_days")


def _check_alternating(p: "Protocol") -> None:
    if p.design == "paired_blocks":
        _fail(p.a_days_per_week is not None, "paired_blocks takes no a_days_per_week")
        return
    _fail(p.washout_days != 0, "daily_alternating has no washout days")
    a = p.a_days_per_week
    _fail(not isinstance(a, tuple) or len(a) != 2 or not all(_is_int(v) for v in a),
          "daily_alternating needs a_days_per_week = (min_a, max_a)")
    _fail(not 1 <= a[0] <= a[1] <= p.block_days - 1, "need 1 <= min_a <= max_a <= block_days - 1")


def _check_effects(p: "Protocol") -> None:
    for name in ("swc", "swc_harm"):
        v = getattr(p, name)
        _fail(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0, f"{name} must be > 0")


def _check_times(p: "Protocol") -> None:
    try:
        date.fromisoformat(p.start_date)
        datetime.fromisoformat(p.created_at)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"invalid start_date or created_at: {exc}") from None
    _fail(len(p.start_date) != 10, "start_date must be YYYY-MM-DD")


def _check_rule(rule: Any) -> None:
    _fail(not isinstance(rule, Mapping), "exclusion rule must be a mapping")
    required = _RULE_FIELDS.get(rule.get("kind"))
    _fail(required is None, f"unknown exclusion kind: {rule.get('kind')!r}")
    _fail(any(k not in rule for k in required), f"exclusion rule needs {required}")
    _fail(bool(set(rule) - set(required) - set(_RULE_OPTIONAL)), f"unknown keys in exclusion rule {dict(rule)}")
    _fail(rule["kind"] == "threshold" and rule["op"] not in _OPS, f"unknown op {rule.get('op')!r}")
    days_after = rule.get("days_after", 0)
    _fail(not _is_int(days_after) or days_after < 0, "days_after must be an int >= 0")


def _check_lists(p: "Protocol") -> None:
    _fail(not isinstance(p.exclusions, tuple) or not isinstance(p.covariates, tuple),
          "exclusions and covariates must be tuples")
    for rule in p.exclusions:
        _check_rule(rule)
    _fail(not all(isinstance(c, str) for c in p.covariates), "covariates must be strings")


@dataclass(frozen=True)
class Protocol:
    id: str
    belief_id: str
    intervention: str
    comparator: str
    outcome_key: str
    source: str
    design: str
    block_days: int
    washout_days: int
    pairs: int
    a_days_per_week: tuple[int, int] | None
    swc: float
    swc_harm: float
    exclusions: tuple[Mapping[str, Any], ...]
    covariates: tuple[str, ...]
    start_date: str
    created_at: str

    def __post_init__(self) -> None:
        for check in (_check_ident, _check_layout, _check_alternating, _check_effects, _check_times, _check_lists):
            check(self)

    def to_dict(self) -> dict:
        """JSON-native dict (tuples become lists)."""
        raw = {f.name: getattr(self, f.name) for f in fields(self)}
        raw["exclusions"] = [dict(r) for r in self.exclusions]
        return json.loads(canonical_json(raw))

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Protocol":
        names = {f.name for f in fields(cls)}
        _fail(set(d) != names, f"protocol keys differ: missing {sorted(names - set(d))}, extra {sorted(set(d) - names)}")
        kw = dict(d)
        a = d["a_days_per_week"]
        kw["a_days_per_week"] = None if a is None else tuple(a)
        kw["exclusions"] = tuple(dict(r) for r in d["exclusions"])
        kw["covariates"] = tuple(d["covariates"])
        return cls(**kw)


def _canonical(p: Protocol) -> bytes:
    return canonical_json(p.to_dict())


# --- design ------------------------------------------------------------------------


def _adjacent_products(values: Sequence[float | None], mu: float) -> list[float]:
    pairs = zip(values, values[1:])
    return [(a - mu) * (b - mu) for a, b in pairs if a is not None and b is not None]


def baseline_stats(values: Sequence[float | None]) -> tuple[float, float, float]:
    """(sigma, rho, mean) of a baseline; rho uses only adjacent non-None days."""
    xs = [float(v) for v in values if v is not None]
    _fail(len(xs) < MIN_BASELINE_DAYS, f"baseline needs >= {MIN_BASELINE_DAYS} values, got {len(xs)}")
    mu = stats.mean(xs)
    sigma = stats.sd(xs)
    products = _adjacent_products(list(values), mu)
    _fail(not products, "baseline has no two adjacent days")
    denom = math.fsum((x - mu) ** 2 for x in xs)
    if denom <= 0.0:
        return sigma, 0.0, mu
    rho = math.fsum(products) * (len(xs) - 1) / len(products) / denom
    return sigma, rho, mu


def _check_mde_args(sigma: float, rho: float, n: int, pairs: int, tau_drift: float) -> None:
    if n < 1 or pairs < 2:
        raise ValueError("need block_days - washout_days >= 1 and pairs >= 2")
    if sigma < 0.0 or tau_drift < 0.0:
        raise ValueError("sigma and tau_drift must be >= 0")
    if not -1.0 < rho < 1.0:
        raise ValueError("rho must lie in (-1, 1)")


def mde(
    sigma: float, rho: float, block_days: int, washout_days: int, pairs: int,
    tau_drift: float = 0.0, alpha: float = 0.05, power: float = 0.8,
) -> float:
    """Minimal detectable effect (t_{1-a/2,m-1} + t_{power,m-1}) * SE.

    n_eff = n(1-rho)/(1+rho) with n = block_days - washout_days and rho
    floored at 0, SE = sqrt((2 sigma^2 / n_eff + tau_drift^2) / m).
    """
    n = block_days - washout_days
    _check_mde_args(sigma, rho, n, pairs, tau_drift)
    if not (0.0 < alpha < 1.0 and 0.0 < power < 1.0):
        raise ValueError("alpha and power must lie in (0, 1)")
    r = max(rho, 0.0)
    n_eff = n * (1.0 - r) / (1.0 + r)
    se = math.sqrt((2.0 * sigma * sigma / n_eff + tau_drift * tau_drift) / pairs)
    df = pairs - 1
    return (stats.t_ppf(1.0 - alpha / 2.0, df) + stats.t_ppf(power, df)) * se


def _arm_days(p: Protocol) -> int:
    """Analyzed days per arm and pair; the smaller arm for daily_alternating."""
    if p.design == "paired_blocks":
        return p.block_days - p.washout_days
    lo, hi = p.a_days_per_week
    return min(lo, p.block_days - hi)


def _mde_problem(p: Protocol, sigma: float, rho: float) -> list[str]:
    try:
        value = mde(sigma, rho, _arm_days(p), 0, p.pairs)
    except ValueError:
        return ["mde_gt_2swc"]
    return ["mde_gt_2swc"] if value > 2.0 * p.swc else []


def _baseline_problems(p: Protocol, baseline: Sequence[float | None], mean90: float, sd90: float) -> list[str]:
    try:
        sigma, rho, mu = baseline_stats(baseline)
    except ValueError:
        return ["baseline_insufficient"]
    problems = _mde_problem(p, sigma, rho)
    if abs(mu - mean90) > sd90:
        problems.append("baseline_outside_1sd")
    return problems


def eligibility(p: Protocol, baseline: Sequence[float | None], mean90: float, sd90: float) -> list[str]:
    """Problems that refuse the experiment; an empty list means eligible."""
    problems = ["pairs_lt_6"] if p.pairs < MIN_PAIRS else []
    problems += _baseline_problems(p, baseline, mean90, sd90)
    if p.design == "paired_blocks" and p.washout_days < 1:
        problems.append("washout_lt_1")
    if p.source not in SOURCES:
        problems.append("source_unlocked")
    return problems


# --- allocation ----------------------------------------------------------------------


def seed(protocol_bytes: bytes) -> int:
    return int(hashlib.sha256(protocol_bytes).hexdigest()[:16], 16)


def _shuffle(items: Sequence[Any], rng: random.Random) -> list[Any]:
    """Fisher-Yates on rng.random() only, stable across Python versions."""
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        j = int(rng.random() * (i + 1))
        out[i], out[j] = out[j], out[i]
    return out


def _block_pair(p: Protocol, rng: random.Random) -> list[str]:
    order = ("A", "B") if rng.random() < 0.5 else ("B", "A")
    labels: list[str] = []
    for arm in order:
        labels += ["washout"] * p.washout_days + [arm] * (p.block_days - p.washout_days)
    return labels


def _alternating_period(p: Protocol, rng: random.Random) -> list[str]:
    lo, hi = p.a_days_per_week
    k = lo + min(int(rng.random() * (hi - lo + 1)), hi - lo)
    return _shuffle(["A"] * k + ["B"] * (p.block_days - k), rng)


def _period_days(p: Protocol) -> int:
    return 2 * p.block_days if p.design == "paired_blocks" else p.block_days


def _dates(start: str, n: int) -> list[str]:
    first = date.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(n)]


def allocate(p: Protocol, protocol_bytes: bytes, *, extra_pairs: int = 0) -> list[tuple[str, str]]:
    """[(date, 'A'|'B'|'washout')] from start_date, seeded by the protocol bytes.

    protocol_bytes must be canonical_json(p.to_dict()). extra_pairs appends
    pairs after the preregistered ones without changing them.
    """
    _fail(protocol_bytes != _canonical(p), "protocol_bytes must be the canonical bytes of the protocol")
    _fail(not _is_int(extra_pairs) or extra_pairs < 0, "extra_pairs must be an int >= 0")
    rng = random.Random(seed(protocol_bytes))
    make = _block_pair if p.design == "paired_blocks" else _alternating_period
    labels: list[str] = []
    for _ in range(p.pairs + extra_pairs):
        labels += make(p, rng)
    return list(zip(_dates(p.start_date, len(labels)), labels))


# --- exclusions ---------------------------------------------------------------------


def _value(daily: Mapping[str, Mapping[str, float | None]], day: str, key: str) -> float | None:
    return (daily.get(day) or {}).get(key)


def _rule_hits(rule: Mapping[str, Any], day: str, daily: Mapping, flags: Mapping[str, Sequence[str]]) -> bool:
    kind = rule["kind"]
    if kind == "checkin_flag":
        return rule["flag"] in (flags.get(day) or ())
    v = _value(daily, day, rule["key"])
    if kind == "missing":
        return v is None
    return v is not None and _OPS[rule["op"]](float(v), float(rule["value"]))


def _spread(days: set[str], after: int) -> set[str]:
    out = set(days)
    for d in days:
        first = date.fromisoformat(d)
        out.update((first + timedelta(days=i)).isoformat() for i in range(1, after + 1))
    return out


def excluded_days(
    p: Protocol,
    daily: Mapping[str, Mapping[str, float | None]],
    checkin_flags: Mapping[str, Sequence[str]],
) -> set[str]:
    """Dates hit by any preregistered exclusion rule, plus their days_after."""
    days = set(daily) | set(checkin_flags)
    out: set[str] = set()
    for rule in p.exclusions:
        hits = {d for d in days if _rule_hits(rule, d, daily, checkin_flags)}
        out |= _spread(hits, rule.get("days_after", 0))
    return out


# --- analysis -----------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    pair_diffs: tuple[float, ...]
    d_bar: float
    se: float
    p_value: float
    mu1: float
    sd1: float
    p_benefit: float
    p_harm: float
    decision: str
    carryover_slope: float | None
    n_excluded: int


_Day = tuple[int, str, float]  # (allocation index, label, value)


def _check_allocation(p: Protocol, allocation: Sequence[tuple[str, str]], extended: bool) -> None:
    pairs = p.pairs + (EXTEND_PAIRS if extended else 0)
    expected = pairs * _period_days(p)
    _fail(len(allocation) != expected, f"allocation has {len(allocation)} days, expected {expected}")
    _fail(allocation[0][0] != p.start_date, "allocation must start at start_date")
    _fail(any(label not in LABELS for _, label in allocation), f"labels must be in {LABELS}")


def _usable(label: str, day: str, data: Mapping[str, float | None], excluded: set[str]) -> bool:
    return label != "washout" and day not in excluded and data.get(day) is not None


def _pair_days(p: Protocol, allocation: Sequence[tuple[str, str]], data: Mapping, excluded: set[str]) -> list[list[_Day]]:
    """Usable days grouped by pair; pairs missing an arm are dropped."""
    period = _period_days(p)
    groups: dict[int, list[_Day]] = {}
    for idx, (day, label) in enumerate(allocation):
        if _usable(label, day, data, excluded):
            groups.setdefault(idx // period, []).append((idx, label, float(data[day])))
    complete = (g for _, g in sorted(groups.items()))
    return [g for g in complete if {lab for _, lab, _ in g} == {"A", "B"}]


def pending_days(p: Protocol, allocation: Sequence[tuple[str, str]], stored: set[str], extended: bool) -> list[str]:
    """Arm days of the pairs a new analysis adds that have no stored daily row yet.

    Extended: every day of the extension pairs. Otherwise: the days of the last pair,
    so a run on the morning after the allocation does not miss a day still being ingested.
    """
    period = _period_days(p)
    first_pair = p.pairs if extended else p.pairs - 1
    return [day for idx, (day, label) in enumerate(allocation)
            if idx // period >= first_pair and label != "washout" and day not in stored]


def _arm_means(days: Sequence[_Day]) -> dict[str, float]:
    return {arm: stats.mean([v for _, lab, v in days if lab == arm]) for arm in ("A", "B")}


def _pair_diff(days: Sequence[_Day]) -> float:
    m = _arm_means(days)
    return m["A"] - m["B"]


def _residuals(days: Sequence[_Day]) -> list[tuple[int, float]]:
    m = _arm_means(days)
    return [(idx, v - m[lab]) for idx, lab, v in days]


def _within_rho(res: Sequence[Sequence[tuple[int, float]]], ss: float) -> float:
    num = math.fsum(a * b for rs in res for (i, a), (j, b) in zip(rs, rs[1:]) if j == i + 1)
    return min(max(num / ss, 0.0), RHO_MAX)


def _model_se2(pairs: Sequence[Sequence[_Day]]) -> float:
    """Squared SE of d_bar from pooled within-arm variance, AR(1)-inflated."""
    res = [_residuals(days) for days in pairs]
    dof = sum(len(rs) for rs in res) - 2 * len(pairs)
    ss = math.fsum(r * r for rs in res for _, r in rs)
    if dof < 1 or ss <= 0.0:
        return 0.0
    rho = _within_rho(res, ss)
    inflation = (1.0 + rho) / (1.0 - rho)
    counts = [[lab for _, lab, _ in days] for days in pairs]
    k = math.fsum(1.0 / c.count("A") + 1.0 / c.count("B") for c in counts)
    m = len(pairs)
    return ss / dof * inflation * k / (m * m)


def _se(diffs: Sequence[float], pairs: Sequence[Sequence[_Day]]) -> float:
    """s^2 = max(sd(D)^2/m, model SE^2) * (m-1)/(m-3)."""
    m = len(diffs)
    s2 = max(stats.sd(diffs) ** 2 / m, _model_se2(pairs)) * (m - 1) / (m - 3)
    _fail(s2 <= 0.0, "pair differences have zero variance")
    return math.sqrt(s2)


def _rng_for(p: Protocol) -> random.Random:
    return random.Random(seed(_canonical(p)) + 1)


def _flip_draws(diffs: Sequence[float], rng: random.Random, n: int):
    for _ in range(n):
        yield math.fsum(d if rng.random() < 0.5 else -d for d in diffs)


def _relabel_draws(pairs: Sequence[Sequence[_Day]], rng: random.Random, n: int):
    for _ in range(n):
        diffs = []
        for days in pairs:
            labels = _shuffle([lab for _, lab, _ in days], rng)
            diffs.append(_pair_diff([(i, lab, v) for (i, _, v), lab in zip(days, labels)]))
        yield stats.mean(diffs)


def _p_value(p: Protocol, diffs: Sequence[float], pairs: Sequence[Sequence[_Day]], mc_draws: int) -> float:
    if p.design == "daily_alternating":
        return stats.mc_pvalue(stats.mean(diffs), _relabel_draws(pairs, _rng_for(p), mc_draws))
    if len(diffs) <= SIGN_FLIP_EXACT_MAX:
        return stats.sign_flip_pvalue(diffs)
    return stats.mc_pvalue(math.fsum(diffs), _flip_draws(diffs, _rng_for(p), mc_draws))


def _slope(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3:
        return None
    try:
        return stats.ols(xs, ys)[0]
    except ValueError:
        return None


def _carryover(p: Protocol, allocation: Sequence[tuple[str, str]], pairs: Sequence[Sequence[_Day]]) -> float | None:
    """Paired blocks: slope of the arm-signed residual on day-in-block (> 0 when
    the effect still builds up after washout). Daily alternating: slope of the
    residual on 'yesterday was A' among days whose yesterday was A or B."""
    points: list[tuple[float, float]] = []
    for days in pairs:
        labels = {idx: lab for idx, lab, _ in days}
        for idx, r in _residuals(days):
            points += _carry_point(p, allocation, idx, labels[idx], r)
    return _slope([x for x, _ in points], [y for _, y in points])


def _carry_point(p: Protocol, allocation: Sequence[tuple[str, str]], idx: int, label: str, r: float) -> list[tuple[float, float]]:
    if p.design == "paired_blocks":
        sign = 1.0 if label == "A" else -1.0
        return [(float(idx % p.block_days), sign * r)]
    prev = allocation[idx - 1][1] if idx > 0 else "washout"
    return [] if prev == "washout" else [(1.0 if prev == "A" else 0.0, r)]


def _decide(pb: float, ph: float, extended: bool) -> str:
    if pb >= ADOPT_BENEFIT and ph < ADOPT_HARM_BELOW:
        return "adopt"
    if pb < REJECT_BENEFIT_BELOW:
        return "reject"
    return "inconclusive" if extended else "extend"


def analyze(
    p: Protocol,
    allocation: Sequence[tuple[str, str]],
    data: Mapping[str, float | None],
    excluded: set[str],
    prior: Prior,
    *,
    extended: bool = False,
    mc_draws: int = 20000,
    direction: int = 1,
) -> Result:
    """Randomization test plus normal-normal update of the belief prior.

    Pass ``starting_prior(...)`` as ``prior`` so the update starts from the
    belief posterior (literature plus earlier personal results), not the
    literature prior alone.
    ``extended`` means the allocation carries the 2 extension pairs; the
    decision is then never 'extend' again. ``direction`` is the claim's
    direction (+1 higher is better, -1 lower is better).
    """
    _fail(direction not in (1, -1), "direction must be +1 or -1")
    _fail(not _is_int(mc_draws) or mc_draws < 1, "mc_draws must be an int >= 1")
    _check_allocation(p, allocation, extended)
    pairs = _pair_days(p, allocation, data, excluded)
    _fail(len(pairs) < MIN_ANALYSIS_PAIRS, f"need >= {MIN_ANALYSIS_PAIRS} usable pairs, got {len(pairs)}")
    diffs = [_pair_diff(days) for days in pairs]
    d_bar = stats.mean(diffs)
    se = _se(diffs, pairs)
    mu1, sd1 = stats.normal_update(prior.mu0, prior.sd0, d_bar, se)
    pb = _p_benefit(direction * mu1, sd1, p.swc)
    ph = _p_harm(direction * mu1, sd1, p.swc_harm)
    n_excluded = sum(1 for day, label in allocation if label != "washout" and day in excluded)
    return Result(
        tuple(diffs), d_bar, se, _p_value(p, diffs, pairs, mc_draws), mu1, sd1, pb, ph,
        _decide(pb, ph, extended), _carryover(p, allocation, pairs), n_excluded,
    )


# --- ledger ---------------------------------------------------------------------------


def write_protocol(ledger: Ledger, p: Protocol, *, author: str = "hippocampus") -> str:
    """Write experiments/<id>/protocol.json once; return sha256 of the canonical bytes."""
    rel = f"experiments/{p.id}/protocol.json"
    _fail(ledger.exists(rel), f"{rel} exists; a protocol is never overwritten")
    ledger.write_json(rel, p.to_dict(), author=author)
    return hashlib.sha256(_canonical(p)).hexdigest()


def _own_result(ev: Mapping[str, Any], p: Protocol) -> bool:
    return ev.get("kind") == "personal_result" and ev.get("belief_id") == p.belief_id and ev.get("experiment") == p.id


def starting_prior(events: Sequence[Mapping[str, Any]], p: Protocol, as_of: str) -> Prior:
    """The belief posterior at as_of as the prior for analyze.

    This experiment's own personal_result is left out, so a re-analysis never
    counts the same data twice.
    """
    folded = beliefs.fold([ev for ev in events if not _own_result(ev, p)], as_of)
    belief = folded.get(p.belief_id)
    _fail(belief is None or belief.mu is None or belief.prior is None, f"belief {p.belief_id!r} has no prior")
    base = belief.prior
    return Prior(belief.mu, belief.sd, base.sources, base.certainty, base.rubric_version)


def record_result(ledger: Ledger, p: Protocol, result: Result, at: str, *, author: str = "pineal") -> dict | None:
    """Append a final result to the belief ledger as personal_result and rebuild the views.

    Returns the event, or None for 'extend' or when this experiment already has one.
    """
    if result.decision not in FINAL_DECISIONS:
        return None
    if any(_own_result(ev, p) for ev in ledger.read_jsonl(beliefs.EVENTS_PATH)):
        return None
    event = beliefs.make_event("personal_result", p.belief_id, at, est=result.d_bar, se=result.se, experiment=p.id)
    ledger.append_jsonl(beliefs.EVENTS_PATH, event, author=author)
    beliefs.rebuild_views(ledger, at, author=author)
    return event

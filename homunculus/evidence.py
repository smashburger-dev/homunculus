"""Paper extraction schema and the ``thal-1`` certainty rubric.

Thalamus fills ``PAPER_SCHEMA``: every field is ``{"value": ..., "quote": str|null}``.
Groups (``population``, ``outcome``, ``effect``) hold fields, and
``reported_means`` is a list of fields whose value is ``{mean, n, decimals, items}``.
Field paths use ``/`` between parts, as in ``population/sex`` or ``reported_means/0``.

Code computes every number here. ``verify_quotes`` finds fields whose quote is
not in the abstract, ``void_fields`` nulls them, and ``score`` turns what is
left into a certainty ``c`` in 0..4 and a gate. Pure: no IO, no network.

Effect conventions: ``MD`` is in outcome units, ``SMD`` in SD units (``sd_pop``
converts it to outcome units for a prior), ``RR`` is compared on the log scale,
so ``swc_paper_units`` for an RR paper is a log risk ratio.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Sequence

from .beliefs import Prior

RUBRIC_VERSION: str = "thal-1"
DESIGNS = (
    "meta_rct", "rct_parallel", "rct_crossover", "cohort_prospective", "case_control",
    "cross_sectional", "mechanistic_human", "animal", "in_vitro", "review_opinion",
)
SEXES = ("male", "female", "mixed")
TRAINING_STATUSES = ("untrained", "recreational", "trained", "elite")
EFFECT_TYPES = ("MD", "SMD", "RR")
GATES = ("actionable", "n_of_1_only", "watchlist", "discard")

BASE_CERTAINTY: dict[str, float] = {
    "meta_rct": 4.0, "rct_parallel": 4.0, "rct_crossover": 4.0,
    "cohort_prospective": 2.0, "case_control": 2.0,
    "cross_sectional": 1.0, "mechanistic_human": 1.0,
    "animal": 0.5, "in_vitro": 0.25, "review_opinion": 0.0,
}
UPGRADABLE = frozenset({"cohort_prospective", "case_control", "cross_sectional"})
MAX_CERTAINTY = 4.0
CONCERN_CAP = 1.0
POPULATION_GATE = 0.75
ACTIONABLE_MIN = 2.0
SEED_MIN = 1.0
IMPLAUSIBLE_SMD = 1.0
CI_Z_WIDTH = 3.92  # 2 * 1.96
TAU_HET_FRACTION = 0.5
SHRINK = {4: 1.0, 3: 0.8, 2: 0.5, 1: 0.25}
INFLATE = {4: 1.0, 3: 1.5, 2: 2.5, 1: 4.0}

_RETRACTION_KINDS = frozenset({"retraction", "withdrawal", "removal"})
_CONCERN_KINDS = frozenset({"expression_of_concern", "partial_retraction"})
# Identifiers and peer-review status come from the candidate and Crossref, so a
# missing quote does not void them. A wrong quote still does.
_QUOTE_EXEMPT = frozenset({"doi", "pmid", "peer_reviewed"})
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_SLUG_MAX = 100


# --- schema -------------------------------------------------------------------


def _nullable(kind: str, **extra: Any) -> dict:
    return {"type": [kind, "null"], **extra}


def _enum(values: Sequence[str]) -> dict:
    return {"type": ["string", "null"], "enum": [*values, None]}


def _field(value_schema: Mapping[str, Any]) -> dict:
    return {
        "type": "object",
        "properties": {
            "value": dict(value_schema),
            "quote": {"type": ["string", "null"], "description": "verbatim span from the abstract"},
        },
        "required": ["value", "quote"],
        "additionalProperties": False,
    }


def _group(fields: Mapping[str, Mapping[str, Any]]) -> dict:
    return {
        "type": "object",
        "properties": {k: _field(v) for k, v in fields.items()},
        "required": list(fields),
        "additionalProperties": False,
    }


_MEAN_ENTRY = {
    "type": ["object", "null"],
    "properties": {
        "mean": {"type": "number"},
        "n": {"type": "integer", "minimum": 1},
        "decimals": {"type": "integer", "minimum": 0},
        "items": {"type": "integer", "minimum": 1},
    },
    "required": ["mean", "n", "decimals", "items"],
    "additionalProperties": False,
}

_PAPER_PROPERTIES: dict[str, dict] = {
    "doi": _field(_nullable("string")),
    "pmid": _field(_nullable("string")),
    "design": _field(_enum(DESIGNS)),
    "n_total": _field(_nullable("integer", minimum=1)),
    "n_per_arm": _field({"type": ["array", "null"], "items": {"type": "integer", "minimum": 0}}),
    "population": _group({
        "sex": _enum(SEXES),
        "age_mean": _nullable("number", minimum=0),
        "training_status": _enum(TRAINING_STATUSES),
        "bmi_mean": _nullable("number", minimum=0),
        "condition": _nullable("string"),
    }),
    "outcome": _group({
        "name": _nullable("string"),
        "unit": _nullable("string"),
        "is_surrogate": _nullable("boolean"),
    }),
    "effect": _group({
        "type": _enum(EFFECT_TYPES),
        "est": _nullable("number"),
        "ci_lo": _nullable("number"),
        "ci_hi": _nullable("number"),
        "sd_pop": _nullable("number", minimum=0),
    }),
    "registration_id": _field(_nullable("string")),
    "preregistered": _field(_nullable("boolean")),
    "attrition_pct": _field(_nullable("number", minimum=0, maximum=100)),
    "blinded_outcome": _field(_nullable("boolean")),
    "outcome_switched": _field(_nullable("boolean")),
    "funding_industry_product": _field(_nullable("boolean")),
    "i2": _field(_nullable("number", minimum=0, maximum=100)),
    "k_studies": _field(_nullable("integer", minimum=1)),
    "peer_reviewed": _field(_nullable("boolean")),
    "dose_response": _field(_nullable("boolean")),
    "reported_means": {"type": "array", "items": _field(_MEAN_ENTRY)},
}

PAPER_SCHEMA: dict = {
    "type": "object",
    "description": (
        f"Thalamus extraction, rubric {RUBRIC_VERSION}. Every value needs a verbatim quote "
        "from the abstract, or null for both when the abstract does not report it."
    ),
    "properties": _PAPER_PROPERTIES,
    "required": list(_PAPER_PROPERTIES),
    "additionalProperties": False,
}
PAPER_FIELDS: tuple[str, ...] = tuple(_PAPER_PROPERTIES)


# --- dataclasses --------------------------------------------------------------


@dataclass(frozen=True)
class OwnerProfile:
    sex: str | None
    age: int | None
    training_status: str | None
    conditions: tuple[str, ...]
    swc: Mapping[str, float]


@dataclass(frozen=True)
class Score:
    certainty: float
    base: float
    adjustments: tuple[tuple[str, float], ...]
    vetoed: bool
    flags: tuple[str, ...]
    gate: str
    rubric_version: str


# --- field access -------------------------------------------------------------


def _is_field(node: Any) -> bool:
    return isinstance(node, Mapping) and "value" in node


def _value(node: Any) -> Any:
    return node.get("value") if _is_field(node) else node


def _child(node: Any, key: str) -> Any:
    if isinstance(node, Mapping):
        return node.get(key)
    if isinstance(node, list) and key.isdigit() and int(key) < len(node):
        return node[int(key)]
    return None


def _get(paper: Mapping, path: str) -> Any:
    node: Any = paper
    for part in path.split("/"):
        node = _child(node, part)
        if node is None:
            return None
    return _value(node)


def _walk(node: Any, path: str) -> Iterator[tuple[str, Any]]:
    """Yield (path, field-or-raw-value) for every leaf field below node."""
    if _is_field(node) or not isinstance(node, (Mapping, list)):
        yield path, node
    elif isinstance(node, Mapping):
        for key in sorted(node):
            yield from _walk(node[key], f"{path}/{key}")
    else:
        for i, item in enumerate(node):
            yield from _walk(item, f"{path}/{i}")


def _fields(paper: Mapping) -> Iterator[tuple[str, Any]]:
    for name in PAPER_FIELDS:
        if name in paper:
            yield from _walk(paper[name], name)


def _squash(text: str) -> str:
    return " ".join(text.split())


# --- quotes -------------------------------------------------------------------


def _quote_fails(path: str, node: Any, haystack: str) -> bool:
    quote = node.get("quote") if _is_field(node) else None
    if quote is None:
        return _value(node) is not None and path not in _QUOTE_EXEMPT
    if not isinstance(quote, str):
        return True
    needle = _squash(quote)
    return not needle or needle not in haystack


def verify_quotes(paper: Mapping, abstract_text: str) -> list[str]:
    """Field paths whose quote is not a whitespace-normalized substring of the abstract.

    A field with a value but no quote fails too, except doi, pmid and
    peer_reviewed, which code takes from the candidate and Crossref. Callers
    treat the returned fields as missing, see ``void_fields``.
    """
    haystack = _squash(abstract_text)
    return [path for path, node in _fields(paper) if _quote_fails(path, node, haystack)]


def _void(node: Any, parts: Sequence[str]) -> None:
    for part in parts[:-1]:
        node = _child(node, part)
        if node is None:
            return
    last = parts[-1]
    target = _child(node, last)
    if _is_field(target):
        target["value"] = None
    elif isinstance(node, dict) and last in node:
        node[last] = None


def void_fields(paper: Mapping, paths: Sequence[str]) -> dict:
    """Deep copy of paper with the value of each listed field set to None; quotes stay."""
    out = copy.deepcopy(dict(paper))
    for path in paths:
        _void(out, path.split("/"))
    return out


# --- population ---------------------------------------------------------------


def _norm(value: Any) -> str | None:
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def _sex_match(paper_sex: Any, owner_sex: str | None) -> bool:
    sex, own = _norm(paper_sex), _norm(owner_sex)
    return own is not None and sex in (own, "mixed")


def _age_match(age_mean: Any, owner_age: int | None) -> bool:
    if owner_age is None or not isinstance(age_mean, (int, float)) or isinstance(age_mean, bool):
        return False
    return abs(float(age_mean) - owner_age) <= 10.0


def _training_match(status: Any, owner_status: str | None) -> bool:
    own = _norm(owner_status)
    return own is not None and _norm(status) == own


def _condition_match(condition: Any, owner_conditions: Sequence[str]) -> bool:
    cond = _norm(condition)
    owned = {_norm(c) for c in owner_conditions} - {None}
    if cond == "healthy":
        return not owned
    return cond is not None and cond in owned


def population_match(population: Mapping, owner: OwnerProfile) -> float:
    """Share of the owner's profile the paper's population covers, 0..1.

    Weights: sex .25 (a mixed sample covers either sex), mean age within 10
    years .25, training status .3, condition .2 ('healthy' matches an owner
    without conditions). Unknown values earn nothing.
    """
    pop = population or {}
    score = 0.0
    score += 0.25 if _sex_match(_get(pop, "sex"), owner.sex) else 0.0
    score += 0.25 if _age_match(_get(pop, "age_mean"), owner.age) else 0.0
    score += 0.3 if _training_match(_get(pop, "training_status"), owner.training_status) else 0.0
    score += 0.2 if _condition_match(_get(pop, "condition"), owner.conditions) else 0.0
    return round(score, 10)


# --- GRIM ---------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _grim_args_ok(mean: Any, n: Any, decimals: Any, items: Any) -> bool:
    if not all(_is_int(v) for v in (n, decimals, items)):
        return False
    return n >= 1 and items >= 1 and decimals >= 0 and _number(mean) is not None


def grim_ok(mean: float, n: int, decimals: int, items: int = 1) -> bool:
    """GRIM: can a mean of n integer responses over `items` items round to `mean`?"""
    if not _grim_args_ok(mean, n, decimals, items):
        raise ValueError("grim_ok needs a finite mean, integer n >= 1, items >= 1 and decimals >= 0")
    total = n * items
    tolerance = 0.5 * 10.0 ** (-decimals) + 1e-9
    center = round(mean * total)
    return any(abs(t / total - mean) <= tolerance for t in (center - 1, center, center + 1))


def _grim_entry_fails(entry: Any) -> bool:
    if not isinstance(entry, Mapping):
        return False
    try:
        return not grim_ok(float(entry["mean"]), entry["n"], entry["decimals"], entry.get("items", 1))
    except (KeyError, TypeError, ValueError):
        return False


def _grim_fails(paper: Mapping) -> bool:
    means = paper.get("reported_means") or []
    return any(_grim_entry_fails(_value(m)) for m in means) if isinstance(means, list) else False


# --- retraction veto ----------------------------------------------------------


def _update_kind(entry: Any) -> str:
    kind = entry.get("type") if isinstance(entry, Mapping) else None
    return str(kind or "").strip().lower().replace("-", "_").replace(" ", "_")


def _update_status(update_to: Sequence[Mapping]) -> tuple[bool, tuple[str, ...]]:
    kinds = {_update_kind(e) for e in update_to or ()}
    if kinds & _RETRACTION_KINDS:
        return True, ("retracted",)
    if kinds & _CONCERN_KINDS:
        return False, ("expression_of_concern",)
    return False, ()


# --- rubric domains -----------------------------------------------------------
# Each check returns (adjustment, flag or None). Each domain returns
# (name, adjustment, flags).

Check = Callable[[Mapping], tuple[float, str | None]]
Domain = tuple[str, float, tuple[str, ...]]


def _attrition(paper: Mapping) -> tuple[float, str | None]:
    pct = _get(paper, "attrition_pct")
    if not isinstance(pct, (int, float)):
        return -0.25, "attrition_unknown"
    return (-0.5, "high_attrition") if pct > 20 else (0.0, None)


def _blinding(paper: Mapping) -> tuple[float, str | None]:
    blinded = _get(paper, "blinded_outcome")
    if blinded is None:
        return -0.25, "blinding_unknown"
    return (0.0, None) if blinded is True else (-0.5, "unblinded_outcome")


def _switching(paper: Mapping) -> tuple[float, str | None]:
    return (-1.0, "outcome_switched") if _get(paper, "outcome_switched") is True else (0.0, None)


def _registration(paper: Mapping) -> tuple[float, str | None]:
    registered = bool(_get(paper, "registration_id")) or _get(paper, "preregistered") is True
    return (0.0, None) if registered else (-0.5, "unregistered")


def _preprint(paper: Mapping) -> tuple[float, str | None]:
    return (0.0, None) if _get(paper, "peer_reviewed") is True else (-0.5, "preprint")


def _funding(paper: Mapping) -> tuple[float, str | None]:
    linked = _get(paper, "funding_industry_product") is True
    return (-0.5, "product_linked_funding") if linked else (0.0, None)


def _implausible(paper: Mapping) -> tuple[float, str | None]:
    est = _get(paper, "effect/est")
    smd = _get(paper, "effect/type") == "SMD" and isinstance(est, (int, float))
    return (-1.0, "implausible_smd") if smd and abs(est) > IMPLAUSIBLE_SMD else (0.0, None)


def _grim(paper: Mapping) -> tuple[float, str | None]:
    return (-1.0, "grim_fail") if _grim_fails(paper) else (0.0, None)


_RCT_BIAS = (_attrition, _blinding, _switching, _registration)
_BIAS_CHECKS: dict[str, tuple[Check, ...]] = {
    "rct_parallel": _RCT_BIAS,
    "rct_crossover": _RCT_BIAS,
    "meta_rct": (_switching, _registration),
    "cohort_prospective": (_attrition, _switching),
    "case_control": (_attrition, _switching),
}
_TRUST_CHECKS: tuple[Check, ...] = (_preprint, _funding, _implausible, _grim)


def _capped(name: str, checks: Sequence[Check], paper: Mapping, floor: float) -> Domain:
    hits = [check(paper) for check in checks]
    total = max(floor, sum(v for v, _ in hits))
    return name, total, tuple(f for _, f in hits if f)


def _bias(paper: Mapping, design: str) -> Domain:
    return _capped("bias", _BIAS_CHECKS.get(design, ()), paper, -2.0)


def _trust(paper: Mapping) -> Domain:
    return _capped("trust", _TRUST_CHECKS, paper, -2.0)


def _number(value: Any) -> float | None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return float(value) if ok else None


def _interval(paper: Mapping) -> tuple[float, float, float] | None:
    """(est, lo, hi) on the comparison scale (log for RR), or None."""
    vals = [_number(_get(paper, f"effect/{k}")) for k in ("est", "ci_lo", "ci_hi")]
    if None in vals:
        return None
    est, lo, hi = vals
    if _get(paper, "effect/type") == "RR":
        if min(est, lo, hi) <= 0:
            return None
        est, lo, hi = math.log(est), math.log(lo), math.log(hi)
    return (est, lo, hi) if lo <= est <= hi and lo < hi else None


def _ci_penalty(lo: float, hi: float, swc: float) -> float:
    if lo <= 0.0 <= hi:
        return -2.0 if lo <= -swc and hi >= swc else -1.0
    return -0.5 if min(abs(lo), abs(hi)) < swc else 0.0


def _imprecision(paper: Mapping, swc: float | None) -> Domain:
    ci = _interval(paper)
    if ci is None:
        return "imprecision", -1.0, ("no_ci",)
    _, lo, hi = ci
    if swc is None or not swc > 0:
        return "imprecision", (-1.0 if lo <= 0.0 <= hi else -0.5), ("swc_unknown",)
    return "imprecision", _ci_penalty(lo, hi, swc), ()


def _inconsistency(paper: Mapping, design: str) -> Domain:
    if design != "meta_rct":
        return "inconsistency", 0.0, ()
    i2 = _number(_get(paper, "i2"))
    if i2 is None:
        return "inconsistency", -0.5, ("i2_unknown",)
    if i2 > 75:
        return "inconsistency", -1.0, ("high_heterogeneity",)
    return ("inconsistency", -0.5, ("moderate_heterogeneity",)) if i2 > 50 else ("inconsistency", 0.0, ())


def _population(pop: float) -> Domain:
    if pop >= POPULATION_GATE:
        return "population", 0.0, ()
    if pop >= 0.5:
        return "population", -0.5, ("population_partial",)
    return "population", -1.0, ("population_mismatch",)


def _indirectness(paper: Mapping) -> Domain:
    surrogate = _get(paper, "outcome/is_surrogate") is True
    return ("indirectness", -0.5, ("surrogate_outcome",)) if surrogate else ("indirectness", 0.0, ())


def _standardized(paper: Mapping, est: float) -> float | None:
    kind = _get(paper, "effect/type")
    if kind == "SMD":
        return est
    sd_pop = _smd_scale(paper)
    return est / sd_pop if kind == "MD" and sd_pop else None


def _large_effect(paper: Mapping) -> bool:
    est = _number(_get(paper, "effect/est"))
    if est is None:
        return False
    if _get(paper, "effect/type") == "RR":
        return est >= 2.0 or 0.0 < est <= 0.5
    z = _standardized(paper, est)
    return z is not None and abs(z) >= 0.8


def _upgrades(paper: Mapping, design: str, bias: float) -> list[Domain]:
    """Non-randomized studies without serious bias gain for large effects and dose-response."""
    if design not in UPGRADABLE or bias <= -1.0:
        return []
    large = 1.0 if _large_effect(paper) else 0.0
    dose = 1.0 if _get(paper, "dose_response") is True else 0.0
    return [("large_effect", large, ()), ("dose_response", dose, ())]


def _replication(replications: int) -> Domain:
    return "replication", min(1.0, 0.5 * max(0, int(replications))), ()


def _adjustments(
    paper: Mapping, design: str, pop: float, swc: float | None, replications: int
) -> tuple[tuple[tuple[str, float], ...], tuple[str, ...]]:
    bias = _bias(paper, design)
    domains = [
        bias, _imprecision(paper, swc), _inconsistency(paper, design), _population(pop),
        _indirectness(paper), _trust(paper), *_upgrades(paper, design, bias[1]), _replication(replications),
    ]
    adjustments = tuple((name, value) for name, value, _ in domains if value != 0.0)
    flags = tuple(flag for _, _, fs in domains for flag in fs)
    return adjustments, flags


def _gate(certainty: float, design: str, independent_rcts: int, pop: float) -> str:
    if certainty <= 0.0:
        return "discard"
    if certainty < SEED_MIN:
        return "watchlist"
    backed = design == "meta_rct" or independent_rcts >= 2
    if certainty >= ACTIONABLE_MIN and backed and pop >= POPULATION_GATE:
        return "actionable"
    return "n_of_1_only"


def score(
    paper: Mapping,
    owner: OwnerProfile,
    *,
    update_to: Sequence[Mapping],
    swc_paper_units: float | None,
    replications: int = 0,
    independent_rcts: int = 0,
) -> Score:
    """thal-1 certainty and gate. ``update_to`` comes from Crossref (code), never from the paper.

    A retraction vetoes (certainty 0, gate 'discard'); an expression of concern
    caps certainty at 1. Actionable needs c >= 2, a meta-analysis of RCTs or at
    least 2 independent RCTs, and a population match >= 0.75.
    """
    vetoed, update_flags = _update_status(update_to)
    design = _get(paper, "design")
    base = BASE_CERTAINTY.get(design, 0.0) if isinstance(design, str) else 0.0
    if vetoed:
        return Score(0.0, base, (), True, update_flags, "discard", RUBRIC_VERSION)
    if design not in BASE_CERTAINTY:
        return Score(0.0, 0.0, (), False, ("design_missing", *update_flags), "discard", RUBRIC_VERSION)
    pop = population_match(paper.get("population") or {}, owner)
    adjustments, flags = _adjustments(paper, design, pop, swc_paper_units, replications)
    certainty = min(MAX_CERTAINTY, max(0.0, base + sum(v for _, v in adjustments)))
    if update_flags:
        certainty = min(certainty, CONCERN_CAP)
    gate = _gate(certainty, design, independent_rcts, pop)
    return Score(certainty, base, adjustments, False, (*update_flags, *flags), gate, RUBRIC_VERSION)


# --- dual extraction ----------------------------------------------------------


def _same_number(x: float, y: float, tol: float) -> bool:
    if _is_int(x) and _is_int(y):
        return x == y
    return abs(x - y) <= tol * max(1.0, abs(x), abs(y))


def _same_mapping(x: Mapping, y: Mapping, tol: float) -> bool:
    return set(x) == set(y) and all(_same(x[k], y[k], tol) for k in x)


def _same_list(x: list, y: list, tol: float) -> bool:
    return len(x) == len(y) and all(_same(a, b, tol) for a, b in zip(x, y))


def _kind(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return "atom"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "mapping"
    return "list" if isinstance(value, list) else "atom"


_SAME: dict[str, Callable[[Any, Any, float], bool]] = {
    "number": _same_number,
    "string": lambda x, y, _t: _squash(x).casefold() == _squash(y).casefold(),
    "mapping": _same_mapping,
    "list": _same_list,
    "atom": lambda x, y, _t: x is y or x == y,
}


def _same(x: Any, y: Any, tol: float) -> bool:
    kind = _kind(x)
    if kind != _kind(y):
        return False
    return _SAME[kind](x, y, tol)


def compare_extractions(a: Mapping, b: Mapping, tol: float = 0.01) -> list[str]:
    """Field paths whose values disagree; numbers within tol (relative, absolute below 1) agree.

    Integers compare exactly, strings ignoring case and whitespace runs. Quotes
    are not compared.
    """
    va = {path: _value(node) for path, node in _fields(a)}
    vb = {path: _value(node) for path, node in _fields(b)}
    paths = sorted(set(va) | set(vb))
    return [p for p in paths if not _same(va.get(p), vb.get(p), tol)]


# --- belief prior -------------------------------------------------------------


def _smd_scale(paper: Mapping) -> float | None:
    sd_pop = _number(_get(paper, "effect/sd_pop"))
    return sd_pop if sd_pop is not None and sd_pop > 0 else None


def _effect_raw(paper: Mapping) -> tuple[float, float] | None:
    """(delta, SE) in outcome units (log RR for RR), or None when underspecified."""
    kind = _get(paper, "effect/type")
    ci = _interval(paper) if kind in EFFECT_TYPES else None
    scale = _smd_scale(paper) if kind == "SMD" else 1.0
    if ci is None or scale is None:
        return None
    est, lo, hi = ci
    return est * scale, (hi - lo) / CI_Z_WIDTH * scale


def to_prior(paper: Mapping, s: Score, belief_id: str) -> Prior | None:
    """Literature prior for the owner's effect, or None when the paper cannot seed one.

    mu0 = shrink(c) * delta, sd0^2 = (k(c) * SE)^2 + tau_het^2 with SE from the
    95% CI and tau_het = 0.5 * |delta|. c is floored to a whole level, so a
    fractional certainty gets the more conservative constants. belief_id is
    validated only; the source key is ``dedupe_key(paper)``.
    """
    if not isinstance(belief_id, str) or not belief_id:
        raise ValueError("belief_id must be a non-empty string")
    if s.vetoed or s.gate not in ("actionable", "n_of_1_only") or s.certainty < SEED_MIN:
        return None
    effect = _effect_raw(paper)
    if effect is None:
        return None
    delta, se = effect
    level = min(4, int(math.floor(s.certainty)))
    tau = TAU_HET_FRACTION * abs(delta)
    sd0 = math.sqrt((INFLATE[level] * se) ** 2 + tau * tau)
    return Prior(SHRINK[level] * delta, sd0, (dedupe_key(paper),), s.certainty, s.rubric_version)


# --- identifiers --------------------------------------------------------------


def _clean_doi(doi: str) -> str:
    return _DOI_PREFIX.sub("", doi.strip()).strip().lower()


def _text(value: Any) -> str | None:
    if _is_int(value):
        return str(value)
    return value.strip() if isinstance(value, str) and value.strip() else None


def dedupe_key(paper: Mapping) -> str:
    """'reg:<id>' > 'doi:<lower>' > 'pmid:<id>'; ValueError when none is known."""
    reg = _text(_get(paper, "registration_id"))
    if reg:
        return "reg:" + "".join(reg.split()).upper()
    doi = _text(_get(paper, "doi"))
    if doi and _clean_doi(doi):
        return "doi:" + _clean_doi(doi)
    pmid = _text(_get(paper, "pmid"))
    if pmid:
        return "pmid:" + re.sub(r"^pmid:\s*", "", pmid, flags=re.IGNORECASE)
    raise ValueError("paper has no registration_id, doi or pmid")


def slug(doi_or_pmid: str) -> str:
    """Filesystem-safe name: '10.1000/X' -> '10-1000-x'. Long names keep a hash suffix."""
    text = _clean_doi(str(doi_or_pmid))
    name = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not name:
        raise ValueError("slug needs a doi or pmid")
    if len(name) > _SLUG_MAX:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:11]
        name = name[: _SLUG_MAX - 12].rstrip("-") + "-" + digest
    return name

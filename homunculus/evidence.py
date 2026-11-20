"""Paper extraction schema, the thal-2 certainty rubric and the promise axis.

Thalamus fills ``PAPER_SCHEMA``: every field is ``{"value": ..., "quote": str|null}``.
Groups (``population``, ``outcome``, ``effect``) hold fields, and
``reported_means`` is a list of fields whose value is ``{mean, n, decimals, items}``.
Field paths use ``/`` between parts, as in ``population/sex`` or ``reported_means/0``.

Code computes every number here. ``verify_quotes`` finds fields whose quote is
not in the abstract, ``void_fields`` nulls them, and ``score`` turns what is
left into a certainty ``c`` in 0..4 and a gate (docs/EVIDENCE_WEIGHTS.md).
``appraise`` adds the second axis from docs/NOVELTY_LANE.md: ``promise`` in
0..4 (mechanistic coherence, human support per causal link, translatable dose,
expected benefit), a methodological quality gate that caps both axes, and the
status ``frontier`` for low-certainty, high-promise papers with clean methods.
Pure: no IO, no network.

Effect conventions: ``MD`` is in outcome units, ``SMD`` in SD units (``sd_pop``
converts it to outcome units for a prior), ``RR`` is compared on the log scale,
so ``swc_paper_units`` for an RR paper is a log risk ratio.

The thal-2 fields (``THAL2_FIELDS``) are optional in the schema, so a thal-1
``paper.json`` still validates; a missing field reads as null.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Sequence

from .beliefs import Prior

# --- versioned constants (DC values: docs/EVIDENCE_WEIGHTS.md, reviewed commits only) ---

# The thal-2 rules below are live. tests/test_cli.py pins the stored label.
RUBRIC_VERSION: str = "thal-2"
DESIGNS = (
    "meta_rct", "rct_parallel", "rct_crossover", "cohort_prospective", "case_control",
    "cross_sectional", "mechanistic_human", "animal", "in_vitro", "review_opinion",
)
SEXES = ("male", "female", "mixed")
TRAINING_STATUSES = ("untrained", "recreational", "trained", "elite")
TRAINING_ORDER = TRAINING_STATUSES
EFFECT_TYPES = ("MD", "SMD", "RR")
GATES = ("actionable", "n_of_1_only", "watchlist", "discard")
STATUSES = (*GATES, "frontier")

BASE_CERTAINTY: dict[str, float] = {
    "meta_rct": 4.0, "rct_parallel": 4.0, "rct_crossover": 4.0,
    "cohort_prospective": 2.0, "case_control": 2.0,
    "cross_sectional": 1.0, "mechanistic_human": 1.0,
    "animal": 0.5, "in_vitro": 0.25, "review_opinion": 0.0,
}
UPGRADABLE = frozenset({"cohort_prospective", "case_control", "cross_sectional"})
MECHANISTIC_ONLY = frozenset({"animal", "in_vitro"})
MAX_CERTAINTY = 4.0
CAPS: dict[str, float] = {
    "concern": 1.0, "includes_retracted": 1.0, "tiny_n": 1.0, "mechanistic_human": 1.5,
    "preprint": 3.0, "product_funding": 3.0, "upgraded_observational": 3.0,
}
CONCERN_CAP = CAPS["concern"]
TINY_N = 10
OIS_CONSTANT = 31.4  # total n = 4 (z_.975 + z_.80)^2 / d^2 = 31.36 / d^2
OIS_DEFAULT_D = 0.5
OIS_MIN_D = 0.2
CROSSOVER_RHO = 0.5
POPULATION_WEIGHTS: dict[str, float] = {"sex": 0.30, "age": 0.15, "training": 0.35, "condition": 0.20}
POPULATION_GATE = 0.75
ACTIONABLE_MIN = 2.0
SEED_MIN = 1.0
IMPLAUSIBLE_SMD = 1.0
UPGRADE_CAP = 2.0
REPLICATION_STEP, REPLICATION_CAP = 0.5, 1.0
CI_Z_WIDTH = 3.92  # 2 * 1.96
TAU_HET_FRACTION = 0.5
SHRINK = {4: 1.0, 3: 0.8, 2: 0.5, 1: 0.25}
INFLATE = {4: 1.0, 3: 1.5, 2: 2.5, 1: 4.0}
SHRINK_Z = ((1.96, 0.25), (3.0, 0.5), (4.0, 0.75), (math.inf, 1.0))  # z below bound -> factor
SHRINK_N = ((20, 0.3), (50, 0.5), (100, 0.75), (math.inf, 1.0))  # n below bound -> factor
SHRINK_N_UNKNOWN = 0.3

# Promise axis and frontier lane (docs/NOVELTY_LANE.md); every value is DC.
PROMISE_WEIGHTS: dict[str, float] = {
    "coherence": 0.30, "human_links": 0.25, "conserved": 0.10, "dose": 0.15, "benefit": 0.20,
}
MAX_PROMISE = 4.0
BENEFIT_FULL_SWC = 2.0  # an effect of 2 SWC earns the full benefit component
BENEFIT_FULL_D = 0.5  # fallback without SWC: a standardized effect of 0.5 earns it
QUALITY_GATE = 0.5  # share of passed quality checks below which methods count as weak
QUALITY_MIN_N = 20  # human studies; MECHANISTIC_ONLY designs use TINY_N
WEAK_CERTAINTY_CAP = 1.0
WEAK_PROMISE_CAP = 1.0
FRONTIER_MIN_PROMISE = 2.5
FRONTIER_MAX_CERTAINTY = ACTIONABLE_MIN
# Body-surface-area Km factors (FDA 2005, Estimating the maximum safe starting dose, table 1).
KM_FACTORS: dict[str, float] = {
    "mouse": 3.0, "hamster": 5.0, "rat": 6.0, "guinea_pig": 8.0, "rabbit": 12.0,
    "monkey": 12.0, "dog": 20.0, "minipig": 35.0, "human": 37.0,
}
EXPERIMENTAL = frozenset({"rct_parallel", "rct_crossover", "mechanistic_human", "animal", "in_vitro"})
RANDOMIZED_DESIGNS = frozenset({"meta_rct", "rct_parallel", "rct_crossover"})

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


def _group(fields: Mapping[str, Mapping[str, Any]], optional: Mapping[str, Mapping[str, Any]] | None = None) -> dict:
    extra = optional or {}
    return {
        "type": "object",
        "properties": {k: _field(v) for k, v in {**fields, **extra}.items()},
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
    }, {"is_acute": _nullable("boolean")}),
    "effect": _group({
        "type": _enum(EFFECT_TYPES),
        "est": _nullable("number"),
        "ci_lo": _nullable("number"),
        "ci_hi": _nullable("number"),
        "sd_pop": _nullable("number", minimum=0),
    }, {"pi_lo": _nullable("number"), "pi_hi": _nullable("number")}),
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
_OPTIONAL_PROPERTIES: dict[str, dict] = {
    "duration_weeks": _field(_nullable("number", minimum=0)),
    "funnel_asymmetry": _field(_nullable("boolean")),
    "median_study_n": _field(_nullable("number", minimum=1)),
    "randomized": _field(_nullable("boolean")),
}
THAL2_FIELDS: tuple[str, ...] = (
    "duration_weeks", "outcome/is_acute", "effect/pi_lo", "effect/pi_hi", "funnel_asymmetry",
    "median_study_n", "randomized",
)

PAPER_SCHEMA: dict = {
    "type": "object",
    "description": (
        f"Thalamus extraction, rubric {RUBRIC_VERSION}. Every value needs a verbatim quote "
        "from the abstract, or null for both when the abstract does not report it."
    ),
    "properties": {**_PAPER_PROPERTIES, **_OPTIONAL_PROPERTIES},
    "required": list(_PAPER_PROPERTIES),
    "additionalProperties": False,
}
PAPER_FIELDS: tuple[str, ...] = tuple(PAPER_SCHEMA["properties"])


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


def _training_credit(status: Any, owner_status: str | None) -> float:
    """1 at ordinal distance 0 on TRAINING_ORDER, 0.5 at distance 1, else 0."""
    got, own = _norm(status), _norm(owner_status)
    if got not in TRAINING_ORDER or own not in TRAINING_ORDER:
        return 0.0
    distance = abs(TRAINING_ORDER.index(got) - TRAINING_ORDER.index(own))
    return {0: 1.0, 1: 0.5}.get(distance, 0.0)


def _condition_match(condition: Any, owner_conditions: Sequence[str]) -> bool:
    cond = _norm(condition)
    owned = {_norm(c) for c in owner_conditions} - {None}
    if cond == "healthy":
        return not owned
    return cond is not None and cond in owned


def population_match(population: Mapping, owner: OwnerProfile) -> float:
    """Share of the owner's profile the paper's population covers, 0..1.

    pop = .30 sex (a mixed sample covers either sex) + .15 mean age within 10
    years + .35 training (1 exact, 0.5 one step away on TRAINING_ORDER) + .20
    condition ('healthy' matches an owner without conditions). Unknown values
    earn nothing.
    """
    pop = population or {}
    w = POPULATION_WEIGHTS
    score = w["sex"] if _sex_match(_get(pop, "sex"), owner.sex) else 0.0
    score += w["age"] if _age_match(_get(pop, "age_mean"), owner.age) else 0.0
    score += w["training"] * _training_credit(_get(pop, "training_status"), owner.training_status)
    score += w["condition"] if _condition_match(_get(pop, "condition"), owner.conditions) else 0.0
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


# --- numbers and intervals ----------------------------------------------------


def _number(value: Any) -> float | None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return float(value) if ok else None


def _smd_scale(paper: Mapping) -> float | None:
    sd_pop = _number(_get(paper, "effect/sd_pop"))
    return sd_pop if sd_pop is not None and sd_pop > 0 else None


def swc_paper_units(paper: Mapping, owner: OwnerProfile) -> float | None:
    """owner.swc (raw outcome units) in the paper's effect units, the ``swc_paper_units`` of score and promise.

    MD: the SWC as is. SMD: SWC / sd_pop, or None without a positive sd_pop.
    RR or an unknown type: None, since a raw-unit SWC has no log-risk-ratio equivalent.
    """
    name = _get(paper, "outcome/name")
    swc = _number(owner.swc.get(name)) if isinstance(name, str) else None
    kind = _get(paper, "effect/type")
    if swc is None or kind not in ("MD", "SMD"):
        return None
    if kind == "MD":
        return swc
    sd_pop = _smd_scale(paper)
    return swc / sd_pop if sd_pop is not None else None


def _to_scale(paper: Mapping, values: Sequence[float | None]) -> list[float] | None:
    """Values on the comparison scale (log for RR), or None when any is missing or invalid."""
    if None in values:
        return None
    if _get(paper, "effect/type") != "RR":
        return [float(v) for v in values]
    return [math.log(v) for v in values] if min(values) > 0 else None


def _interval(paper: Mapping) -> tuple[float, float, float] | None:
    """(est, lo, hi) on the comparison scale (log for RR), or None."""
    vals = _to_scale(paper, [_number(_get(paper, f"effect/{k}")) for k in ("est", "ci_lo", "ci_hi")])
    if vals is None:
        return None
    est, lo, hi = vals
    return (est, lo, hi) if lo <= est <= hi and lo < hi else None


def _prediction_interval(paper: Mapping) -> tuple[float, float] | None:
    vals = _to_scale(paper, [_number(_get(paper, f"effect/{k}")) for k in ("pi_lo", "pi_hi")])
    return (vals[0], vals[1]) if vals is not None and vals[0] < vals[1] else None


def _crosses_null(lo: float, hi: float) -> bool:
    return lo <= 0.0 <= hi


def _standardized(paper: Mapping, est: float) -> float | None:
    kind = _get(paper, "effect/type")
    if kind == "SMD":
        return est
    sd_pop = _smd_scale(paper)
    return est / sd_pop if kind == "MD" and sd_pop else None


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


def _registered(paper: Mapping) -> bool:
    return bool(_get(paper, "registration_id")) or _get(paper, "preregistered") is True


def _registration(paper: Mapping) -> tuple[float, str | None]:
    return (0.0, None) if _registered(paper) else (-0.5, "unregistered")


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


def _i2(paper: Mapping) -> tuple[float, str | None]:
    i2 = _number(_get(paper, "i2"))
    if i2 is None:
        return -0.5, "i2_unknown"
    if i2 > 75:
        return -1.0, "high_heterogeneity"
    return (-0.5, "moderate_heterogeneity") if i2 > 50 else (0.0, None)


def _few_studies(paper: Mapping) -> tuple[float, str | None]:
    k = _get(paper, "k_studies")
    return (-0.5, "few_studies") if _is_int(k) and k < 3 else (0.0, None)


def _pi_null(paper: Mapping) -> tuple[float, str | None]:
    pi, ci = _prediction_interval(paper), _interval(paper)
    if pi is None or ci is None:
        return 0.0, None
    widened = _crosses_null(*pi) and not _crosses_null(ci[1], ci[2])
    return (-0.5, "pi_crosses_null") if widened else (0.0, None)


def _surrogate(paper: Mapping) -> tuple[float, str | None]:
    return (-0.5, "surrogate_outcome") if _get(paper, "outcome/is_surrogate") is True else (0.0, None)


def _short_duration(paper: Mapping) -> tuple[float, str | None]:
    weeks = _number(_get(paper, "duration_weeks"))
    short = _get(paper, "outcome/is_acute") is False and weeks is not None and weeks < 4
    return (-0.5, "short_duration") if short else (0.0, None)


def _funnel(paper: Mapping) -> tuple[float, str | None]:
    k = _get(paper, "k_studies")
    asymmetric = _get(paper, "funnel_asymmetry") is True and _is_int(k) and k >= 10
    return (-1.0, "funnel_asymmetry") if asymmetric else (0.0, None)


def _small_studies(paper: Mapping) -> tuple[float, str | None]:
    k, median = _get(paper, "k_studies"), _number(_get(paper, "median_study_n"))
    small = _is_int(k) and k < 10 and median is not None and median < 20
    return (-0.5, "small_studies") if small else (0.0, None)


def _population_check(pop: float) -> Check:
    def check(_paper: Mapping) -> tuple[float, str | None]:
        if pop >= POPULATION_GATE:
            return 0.0, None
        return (-0.5, "population_partial") if pop >= 0.5 else (-1.0, "population_mismatch")
    return check


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


def _ci_penalty(lo: float, hi: float, swc: float) -> tuple[float, str | None]:
    if _crosses_null(lo, hi):
        return (-2.0, "ci_crosses_both") if lo <= -swc and hi >= swc else (-1.0, "ci_crosses_null")
    return (-0.5, "ci_near_swc") if min(abs(lo), abs(hi)) < swc else (0.0, None)


def _ci_check(swc: float | None) -> Check:
    def check(paper: Mapping) -> tuple[float, str | None]:
        ci = _interval(paper)
        if ci is None:
            return -1.0, "no_ci"
        _, lo, hi = ci
        if swc is None or not swc > 0:
            return (-1.0 if _crosses_null(lo, hi) else -0.5), "swc_unknown"
        return _ci_penalty(lo, hi, swc)
    return check


def _ois_d(paper: Mapping) -> float:
    est = _number(_get(paper, "effect/est"))
    d = _standardized(paper, est) if est is not None else None
    return max(abs(d) if d is not None else OIS_DEFAULT_D, OIS_MIN_D)


def optimal_information_size(paper: Mapping, design: str | None = None) -> float | None:
    """Total n an SMD or MD study needs (GRADE OIS); a crossover needs (1 - rho) / 2 of it.

    d is |SMD|, |MD| / sd_pop, or OIS_DEFAULT_D, floored at OIS_MIN_D. None for RR.
    """
    if _get(paper, "effect/type") not in ("SMD", "MD"):
        return None
    size = float(math.ceil(OIS_CONSTANT / _ois_d(paper) ** 2))
    return size * (1.0 - CROSSOVER_RHO) / 2.0 if design == "rct_crossover" else size


def _ois_check(design: str) -> Check:
    def check(paper: Mapping) -> tuple[float, str | None]:
        need = optimal_information_size(paper, design)
        if need is None:
            return 0.0, None
        n = _get(paper, "n_total")
        if not _is_int(n):
            return -1.0, "n_unknown"
        return (-1.0, "below_ois") if n < need else (0.0, None)
    return check


def _imprecision(paper: Mapping, design: str, swc: float | None) -> Domain:
    return _capped("imprecision", (_ci_check(swc), _ois_check(design)), paper, -2.0)


def _meta_domain(name: str, checks: Sequence[Check], paper: Mapping, design: str, floor: float) -> Domain:
    return _capped(name, checks if design == "meta_rct" else (), paper, floor)


def _inconsistency(paper: Mapping, design: str) -> Domain:
    return _meta_domain("inconsistency", (_i2, _few_studies, _pi_null), paper, design, -1.5)


def _publication_bias(paper: Mapping, design: str) -> Domain:
    return _meta_domain("publication_bias", (_funnel, _small_studies), paper, design, -1.0)


def _indirectness(paper: Mapping, pop: float) -> Domain:
    return _capped("indirectness", (_population_check(pop), _surrogate, _short_duration), paper, -2.0)


def _large_effect(paper: Mapping) -> float:
    """+2 for RR >= 5 or <= 0.2, +1 for RR >= 2 or <= 0.5 or a standardized |d| >= 0.8."""
    est = _number(_get(paper, "effect/est"))
    if est is None:
        return 0.0
    if _get(paper, "effect/type") == "RR":
        if est >= 5.0 or 0.0 < est <= 0.2:
            return 2.0
        return 1.0 if est >= 2.0 or 0.0 < est <= 0.5 else 0.0
    z = _standardized(paper, est)
    return 1.0 if z is not None and abs(z) >= 0.8 else 0.0


def _upgrades(paper: Mapping, design: str, bias: float, imprecision: float) -> list[Domain]:
    """Non-randomized studies without serious bias or any imprecision gain at most +2 in total."""
    if design not in UPGRADABLE or bias <= -1.0 or imprecision != 0.0:
        return []
    large = min(UPGRADE_CAP, _large_effect(paper))
    dose = min(1.0 if _get(paper, "dose_response") is True else 0.0, UPGRADE_CAP - large)
    return [("large_effect", large, ()), ("dose_response", dose, ())]


def _replication(replications: int) -> Domain:
    return "replication", min(REPLICATION_CAP, REPLICATION_STEP * max(0, int(replications))), ()


def _domains(paper: Mapping, design: str, pop: float, swc: float | None, replications: int) -> list[Domain]:
    """Domains in reporting order: D1 to D6, upgrades, replication."""
    bias, imprecision = _bias(paper, design), _imprecision(paper, design, swc)
    return [
        bias, imprecision, _inconsistency(paper, design), _indirectness(paper, pop),
        _publication_bias(paper, design), _trust(paper),
        *_upgrades(paper, design, bias[1], imprecision[1]), _replication(replications),
    ]


# --- caps, clamp and gate -----------------------------------------------------


def _caps(paper: Mapping, design: str, concern: bool, includes_retracted: bool,
          upgraded: bool) -> list[tuple[str, float]]:
    n = _get(paper, "n_total")
    active = {
        "concern": concern,
        "includes_retracted": includes_retracted is True,
        "tiny_n": _is_int(n) and n < TINY_N,
        "mechanistic_human": design == "mechanistic_human",
        "preprint": _get(paper, "peer_reviewed") is not True,
        "product_funding": _get(paper, "funding_industry_product") is True,
        "upgraded_observational": upgraded,
    }
    return [(name, cap) for name, cap in CAPS.items() if active[name]]


def _clamp(raw: float, caps: Sequence[tuple[str, float]]) -> tuple[float, tuple[str, ...]]:
    """min(cap, max(0, raw)); the smallest cap (first in CAPS order on ties) flags when it binds."""
    value = min(MAX_CERTAINTY, max(0.0, raw))
    if not caps:
        return value, ()
    name, cap = min(caps, key=lambda item: item[1])
    return min(cap, value), ((f"cap:{name}",) if cap < raw else ())


def _backed(paper: Mapping, design: str, independent_rcts: int) -> bool:
    k = _get(paper, "k_studies")
    return (design == "meta_rct" and _is_int(k) and k >= 2) or independent_rcts >= 2


def _actionable(paper: Mapping, design: str, certainty: float, independent_rcts: int, pop: float,
                concern: bool) -> bool:
    return all((
        certainty >= ACTIONABLE_MIN, _backed(paper, design, independent_rcts), pop >= POPULATION_GATE,
        _get(paper, "peer_reviewed") is True, not concern, design != "mechanistic_human",
    ))


def _gate(paper: Mapping, design: str, certainty: float, independent_rcts: int, pop: float, concern: bool) -> str:
    if certainty <= 0.0:
        return "discard"
    if design in MECHANISTIC_ONLY or certainty < SEED_MIN:
        return "watchlist"
    if _actionable(paper, design, certainty, independent_rcts, pop, concern):
        return "actionable"
    return "n_of_1_only"


@dataclass(frozen=True)
class _Inputs:
    owner: OwnerProfile
    update_flags: tuple[str, ...]
    swc: float | None
    replications: int
    independent_rcts: int
    includes_retracted: bool


def _graded(paper: Mapping, design: str, base: float, inp: _Inputs) -> Score:
    pop = population_match(paper.get("population") or {}, inp.owner)
    domains = _domains(paper, design, pop, inp.swc, inp.replications)
    upgraded = any(name in ("large_effect", "dose_response") and v > 0 for name, v, _ in domains)
    concern = bool(inp.update_flags)
    caps = _caps(paper, design, concern, inp.includes_retracted, upgraded)
    certainty, cap_flags = _clamp(base + sum(v for _, v, _ in domains), caps)
    adjustments = tuple((name, value) for name, value, _ in domains if value != 0.0)
    flags = (*inp.update_flags, *(f for _, _, fs in domains for f in fs), *cap_flags)
    gate = _gate(paper, design, certainty, inp.independent_rcts, pop, concern)
    return Score(certainty, base, adjustments, False, flags, gate, RUBRIC_VERSION)


def score(
    paper: Mapping,
    owner: OwnerProfile,
    *,
    update_to: Sequence[Mapping],
    swc_paper_units: float | None,
    replications: int = 0,
    independent_rcts: int = 0,
    includes_retracted: bool = False,
) -> Score:
    """thal-2 certainty and gate (docs/EVIDENCE_WEIGHTS.md).

    ``update_to`` comes from Crossref and ``includes_retracted`` from the
    scouts (code), never from the paper. A retraction vetoes. Animal and in
    vitro studies keep their base certainty and stay on the watchlist. For
    every other design six domains, upgrades and replication sum onto the
    base, and the result is clamped once by the smallest applicable cap.
    """
    vetoed, update_flags = _update_status(update_to)
    design = _get(paper, "design")
    base = BASE_CERTAINTY.get(design, 0.0) if isinstance(design, str) else 0.0
    if vetoed:
        return Score(0.0, base, (), True, update_flags, "discard", RUBRIC_VERSION)
    if design not in BASE_CERTAINTY or design == "review_opinion":
        missing = ("design_missing",) if design not in BASE_CERTAINTY else ()
        return Score(0.0, base, (), False, (*missing, *update_flags), "discard", RUBRIC_VERSION)
    if design in MECHANISTIC_ONLY:
        return Score(base, base, (), False, (*update_flags, "mechanistic_only"), "watchlist", RUBRIC_VERSION)
    inputs = _Inputs(owner, update_flags, swc_paper_units, replications, independent_rcts, includes_retracted)
    return _graded(paper, design, base, inputs)


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


def _effect_raw(paper: Mapping) -> tuple[float, float, float, float] | None:
    """(delta, SE, z, scale): delta and SE in outcome units (log RR for RR), z on the comparison scale."""
    kind = _get(paper, "effect/type")
    ci = _interval(paper) if kind in EFFECT_TYPES else None
    scale = _smd_scale(paper) if kind == "SMD" else 1.0
    if ci is None or scale is None:
        return None
    est, lo, hi = ci
    se = (hi - lo) / CI_Z_WIDTH
    return est * scale, se * scale, abs(est) / se, scale


def _band(table: Sequence[tuple[float, float]], x: float) -> float:
    return next(factor for bound, factor in table if x < bound)


def _shrink_n(paper: Mapping) -> float:
    n = _get(paper, "n_total")
    return _band(SHRINK_N, n) if _is_int(n) else SHRINK_N_UNKNOWN


def _tau_het(paper: Mapping, delta: float, scale: float) -> float:
    tau = TAU_HET_FRACTION * abs(delta)
    pi = _prediction_interval(paper)
    return max(tau, (pi[1] - pi[0]) / CI_Z_WIDTH * scale) if pi is not None else tau


def _seeds(paper: Mapping, s: Score, certainty: float) -> bool:
    if s.vetoed or s.gate not in ("actionable", "n_of_1_only") or certainty < SEED_MIN:
        return False
    return _get(paper, "design") not in MECHANISTIC_ONLY


def to_prior(paper: Mapping, s: Score, belief_id: str) -> Prior | None:
    """Literature prior for the owner's effect, or None when the paper cannot seed one.

    mu0 = s * delta with s = min(SHRINK[level], SHRINK_Z(z), SHRINK_N(n)), and
    sd0^2 = (INFLATE[level] * SE)^2 + tau_het^2, where SE comes from the 95% CI
    and tau_het = max(0.5 |delta|, prediction-interval SD). level is c floored
    to a whole number; weak methods (``quality``) cap c at WEAK_CERTAINTY_CAP
    first. belief_id is validated only; the source key is ``dedupe_key(paper)``.
    """
    if not isinstance(belief_id, str) or not belief_id:
        raise ValueError("belief_id must be a non-empty string")
    certainty = min(s.certainty, WEAK_CERTAINTY_CAP) if quality(paper).weak else s.certainty
    effect = _effect_raw(paper) if _seeds(paper, s, certainty) else None
    if effect is None:
        return None
    delta, se, z, scale = effect
    level = min(4, int(math.floor(certainty)))
    shrink = min(SHRINK[level], _band(SHRINK_Z, z), _shrink_n(paper))
    tau = _tau_het(paper, delta, scale)
    sd0 = math.sqrt((INFLATE[level] * se) ** 2 + tau * tau)
    return Prior(shrink * delta, sd0, (dedupe_key(paper),), certainty, s.rubric_version)


# --- promise axis and frontier lane -------------------------------------------


@dataclass(frozen=True)
class Quality:
    score: float  # share of applicable checks passed, 0..1
    checks: tuple[tuple[str, bool], ...]
    weak: bool


@dataclass(frozen=True)
class PromiseInput:
    """Inputs beyond the extraction, filled by code from a deep dive or the lexicon.

    ``human_link_share`` is the share of causal-chain links with human support
    (``deepdive`` coverage). ``plausible``: every link has some support.
    ``coherent``: no link is contested. Doses are mg/kg/day in ``species``.
    ``direction`` is the linked claim's direction (+1 higher is better, -1
    lower is better); without it the effect earns no benefit.
    """
    plausible: bool | None = None
    coherent: bool | None = None
    conserved_pathway: bool | None = None
    human_link_share: float | None = None
    dose_mg_per_kg: float | None = None
    species: str | None = None
    human_max_mg_per_kg: float | None = None
    swc_paper_units: float | None = None
    direction: int | None = None


@dataclass(frozen=True)
class Promise:
    value: float  # 0..MAX_PROMISE
    components: tuple[tuple[str, float], ...]  # each 0..1, in PROMISE_WEIGHTS order
    capped: bool
    quality: Quality


@dataclass(frozen=True)
class Appraisal:
    certainty: float  # Score.certainty, capped at WEAK_CERTAINTY_CAP when methods are weak
    promise: float
    status: str  # one of STATUSES
    frontier_blockers: tuple[str, ...]  # unmet frontier entry rules; empty means frontier
    flags: tuple[str, ...]


def human_equivalent_dose(dose_mg_per_kg: float, species: str) -> float:
    """HED in mg/kg = animal dose * Km(species) / Km(human), the body-surface-area conversion."""
    dose = _number(dose_mg_per_kg)
    km = KM_FACTORS.get(_norm(species) or "")
    if dose is None or dose < 0 or km is None:
        raise ValueError("human_equivalent_dose needs a dose >= 0 and a species in KM_FACTORS")
    return dose * km / KM_FACTORS["human"]


def _n_adequate(paper: Mapping, design: str) -> bool:
    n = _get(paper, "n_total")
    return _is_int(n) and n >= (TINY_N if design in MECHANISTIC_ONLY else QUALITY_MIN_N)


def _quality_checks(paper: Mapping, design: str) -> tuple[tuple[str, bool], ...]:
    checks = [("preregistered", _registered(paper)), ("sample_size", _n_adequate(paper, design))]
    if design in EXPERIMENTAL:
        randomized = design in RANDOMIZED_DESIGNS or _get(paper, "randomized") is True
        checks = [("randomized", randomized), ("blinded", _get(paper, "blinded_outcome") is True), *checks]
    return tuple(checks)


def _red_flags(paper: Mapping) -> bool:
    return _get(paper, "outcome_switched") is True or _grim_fails(paper)


def quality(paper: Mapping) -> Quality:
    """Methodological quality gate.

    Checks: randomization and blinding (experimental designs only),
    preregistration, sample size. Unknown counts as failed. Methods are weak
    below QUALITY_GATE, on outcome switching or on a GRIM failure.
    """
    design = _get(paper, "design")
    if design not in BASE_CERTAINTY or design == "review_opinion":
        return Quality(0.0, (), True)
    checks = _quality_checks(paper, design)
    share = round(sum(ok for _, ok in checks) / len(checks), 10)
    return Quality(share, checks, share < QUALITY_GATE or _red_flags(paper))


def _coherence(paper: Mapping, inp: PromiseInput) -> float:
    """Bradford Hill plausibility, coherence and biological gradient, one third each."""
    hits = (inp.plausible is True, inp.coherent is True, _get(paper, "dose_response") is True)
    return sum(hits) / 3.0


def _share(value: float | None) -> float:
    x = _number(value)
    return min(1.0, max(0.0, x)) if x is not None else 0.0


def _dose_translatable(paper: Mapping, inp: PromiseInput) -> bool:
    design = _get(paper, "design")
    species = inp.species or (None if design in MECHANISTIC_ONLY else "human")
    limit = _number(inp.human_max_mg_per_kg)
    if species is None or limit is None or inp.dose_mg_per_kg is None:
        return False
    try:
        return human_equivalent_dose(inp.dose_mg_per_kg, species) <= limit
    except ValueError:
        return False


def _benefit(paper: Mapping, swc: float | None, direction: int | None) -> float:
    """Signed effect / (2 SWC); without a SWC, signed standardized effect / 0.5; clamped to 0..1.

    The sign is ``direction * effect``, so a harmful effect or an unknown direction earns 0.
    """
    ci = _interval(paper)
    if ci is None or direction not in (1, -1):
        return 0.0
    if swc is not None and swc > 0:
        return min(1.0, max(0.0, direction * ci[0]) / (BENEFIT_FULL_SWC * swc))
    d = _standardized(paper, ci[0])
    return min(1.0, max(0.0, direction * d) / BENEFIT_FULL_D) if d is not None else 0.0


def promise(paper: Mapping, inputs: PromiseInput | None = None) -> Promise:
    """Promise 0..4: how plausible and valuable the effect is if true. Weak methods cap it at 1."""
    inp = inputs or PromiseInput()
    parts = {
        "coherence": _coherence(paper, inp),
        "human_links": _share(inp.human_link_share),
        "conserved": 1.0 if inp.conserved_pathway is True else 0.0,
        "dose": 1.0 if _dose_translatable(paper, inp) else 0.0,
        "benefit": _benefit(paper, inp.swc_paper_units, inp.direction),
    }
    raw = round(MAX_PROMISE * sum(PROMISE_WEIGHTS[k] * v for k, v in parts.items()), 10)
    q = quality(paper)
    value = min(raw, WEAK_PROMISE_CAP) if q.weak else raw
    components = tuple((k, round(v, 10)) for k, v in parts.items())
    return Promise(value, components, value < raw, q)


def frontier_blockers(s: Score, p: Promise, certainty: float) -> tuple[str, ...]:
    """Unmet frontier entry rules, in this order; a paper enters status 'frontier' when none remain.

    vetoed: retracted. concern: expression of concern or a pooled retracted
    study. discarded: gate 'discard'. certain_enough: c >= 2 belongs to the
    ordinary lane. low_promise: promise < 2.5. weak_methods: the quality gate failed.
    """
    rules = (
        ("vetoed", s.vetoed),
        ("concern", "expression_of_concern" in s.flags or "cap:includes_retracted" in s.flags),
        ("discarded", s.gate == "discard"),
        ("certain_enough", certainty >= FRONTIER_MAX_CERTAINTY),
        ("low_promise", p.value < FRONTIER_MIN_PROMISE),
        ("weak_methods", p.quality.weak),
    )
    return tuple(name for name, unmet in rules if unmet)


def _capped_gate(gate: str, certainty: float) -> str:
    """The gate after the weak-methods cap: 'actionable' needs ACTIONABLE_MIN, seeding needs SEED_MIN."""
    if gate == "actionable" and certainty < ACTIONABLE_MIN:
        gate = "n_of_1_only"
    if gate == "n_of_1_only" and certainty < SEED_MIN:
        gate = "watchlist"
    return gate


def appraise(paper: Mapping, s: Score, inputs: PromiseInput | None = None) -> Appraisal:
    """Both axes and the status. Certainty decides plan strength; promise opens the frontier lane.

    Weak methods cap the certainty, and the status follows the capped value.
    """
    p = promise(paper, inputs)
    weak = p.quality.weak
    certainty = min(s.certainty, WEAK_CERTAINTY_CAP) if weak else s.certainty
    value = 0.0 if s.vetoed else p.value
    blockers = frontier_blockers(s, p, certainty)
    flags = (*(("quality_weak",) if weak else ()), *(("promise_capped",) if p.capped else ()))
    status = "frontier" if not blockers else _capped_gate(s.gate, certainty)
    return Appraisal(certainty, value, status, blockers, flags)


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

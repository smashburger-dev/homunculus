"""Deep dive for frontier papers: a claim as a chain of mechanistic links, checked link by link.

Thalamus fills ``DEEPDIVE_SCHEMA`` (task ``deep_dive``, agents/thalamus.md):
the claim (intervention, category, outcome, tracker key) and an ordered chain
of links from the intervention to the outcome. Each link cites papers with a
design, a relation (supports or contradicts) and a verbatim quote from the
source text. Code does the rest:

* ``validate`` checks the schema and that the chain is contiguous.
* ``void_unquoted`` drops evidence whose quote is not in the source text.
* ``assess_link`` gives each link a certainty: the strongest supporting
  paper (its thal-2 score when known, else ``UNSCORED_MAX``, which is 0: a
  citation code has not resolved and scored counts nothing), merged with the
  lexicon's prior verdict. A link whose strongest contradiction is at least
  as strong is contested (certainty 0). A supporting item is human support
  only when it is scored, reaches ``LINK_MIN`` and has a human primary
  design (not animal, in vitro or review; the ledger design when ``designs``
  gives one).
* ``decide`` ignores the paper under review as evidence for its own chain.
* ``chain_support`` is the weakest link plus coverage, the share of links
  with human support. The chain holds at weakest >= ``LINK_MIN``, coverage
  >= ``COVERAGE_MIN`` and no contested link.
* ``decide`` promotes the claim to ``n_of_1_candidate`` only when the chain
  holds, the intervention is low risk (an allowlisted category, nothing
  forbidden, a supplement passing ``guardrails.supplement_check``) and the
  outcome is a tracker key. Everything else is ``watch``. Weak or unsupported
  links become open questions whose keywords feed the scouts.

Values marked DC are design choices (docs/NOVELTY_LANE.md), not sourced numbers.
"""

from __future__ import annotations

import copy
from typing import Any, Collection, Mapping, Sequence

from . import agents, evidence, guardrails, lexicon, metrics
from .guardrails import Rules
from .store import Ledger

DIVES_DIR = "evidence/deepdives"
CODE_AUTHOR = "pineal"
CATEGORIES = ("behavioral", "food", "timing", "training", "supplement", "drug", "device", "procedure", "other")
ALLOWED_CATEGORIES = frozenset({"behavioral", "food", "timing", "training", "supplement"})
LINK_KINDS = ("mechanism", "biomarker", "dose", "outcome")
RELATIONS = ("supports", "contradicts")
DECISIONS = ("n_of_1_candidate", "watch")
MAX_LINKS = 8
LINK_MIN = 0.5  # DC: the weakest link needs at least animal-level support
COVERAGE_MIN = 2.0 / 3.0  # DC: two of three links shown in humans
UNSCORED_MAX = 0.0  # a cited paper without a ledger score is self-attested and counts nothing
NOT_HUMAN_PRIMARY = evidence.MECHANISTIC_ONLY | {"review_opinion"}
# DC: desirable direction of a tracker key (+1 higher is better), for the promise benefit; unlisted keys earn none
TRACKER_DIRECTION: dict[str, int] = {
    "hrv_sdnn_ln": 1, "hrv_rmssd_ln": 1, "vo2max": 1, "spo2_min": 1,
    "rhr": -1, "bp_sys": -1, "bp_dia": -1, "af_burden": -1,
}


class DiveError(ValueError):
    """The Thalamus output is not a valid deep dive."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


# --- schema -------------------------------------------------------------------

_STR: dict = {"type": "string", "minLength": 1}
_NULL_STR: dict = {"type": ["string", "null"]}


def _obj(props: Mapping[str, Any]) -> dict:
    return {"type": "object", "properties": dict(props), "required": list(props), "additionalProperties": False}


def _enum(values: Sequence[str]) -> dict:
    return {"type": "string", "enum": list(values)}


_EVIDENCE = _obj({
    "paper": _STR, "design": _enum(evidence.DESIGNS), "relation": _enum(RELATIONS), "quote": _STR,
})
_LINK = _obj({
    "id": _STR, "src": _STR, "src_type": _enum(lexicon.NODE_TYPES), "dst": _STR,
    "dst_type": _enum(lexicon.NODE_TYPES), "kind": _enum(LINK_KINDS),
    "evidence": {"type": "array", "items": _EVIDENCE},
    "open_question": _NULL_STR, "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
})
_SUPPLEMENT = {
    "type": ["object", "null"],
    "properties": {"name": _STR, "dose": {"type": "number"}, "unit": _STR, "tested_by": _NULL_STR},
    "required": ["name", "dose", "unit", "tested_by"],
    "additionalProperties": False,
}
_CLAIM = _obj({
    "intervention": _STR, "category": _enum(CATEGORIES), "outcome": _STR, "tracker_key": _NULL_STR,
    "supplement": _SUPPLEMENT,
})
DEEPDIVE_SCHEMA: dict = _obj({
    "claim": _CLAIM,
    "links": {"type": "array", "items": _LINK, "minItems": 1, "maxItems": MAX_LINKS},
    "conserved_pathway": _obj({"value": {"type": ["boolean", "null"]}, "quote": _NULL_STR}),
})


# --- validation ---------------------------------------------------------------


def _ends(link: Mapping[str, Any]) -> tuple[str, str]:
    return lexicon.node_id(link["src_type"], link["src"]), lexicon.node_id(link["dst_type"], link["dst"])


def _joint_errors(links: Sequence[Mapping[str, Any]]) -> list[str]:
    ends = [_ends(link) for link in links]
    return [f"links/{i + 1}: src does not continue links/{i}" for i in range(len(ends) - 1)
            if ends[i][1] != ends[i + 1][0]]


def _claim_errors(claim: Mapping[str, Any], links: Sequence[Mapping[str, Any]]) -> list[str]:
    errors = []
    if lexicon.normalize(links[0]["src"]) != lexicon.normalize(claim["intervention"]):
        errors.append("links/0: src must be the claim's intervention")
    if lexicon.normalize(links[-1]["dst"]) != lexicon.normalize(claim["outcome"]):
        errors.append(f"links/{len(links) - 1}: dst must be the claim's outcome")
    return errors


def _chain_errors(dive: Mapping[str, Any]) -> list[str]:
    links = dive["links"]
    ids = [link["id"] for link in links]
    errors = ["link ids must be unique"] if len(set(ids)) != len(ids) else []
    try:
        errors += _joint_errors(links)
    except ValueError as exc:
        return [*errors, f"node label: {exc}"]
    return errors + _claim_errors(dive["claim"], links)


def validate(dive: Any) -> list[str]:
    """Problems with a Thalamus deep dive: schema errors first, then chain errors."""
    if not isinstance(dive, Mapping):
        return ["deep dive must be an object"]
    errors = agents.validate_json(dive, DEEPDIVE_SCHEMA)
    return errors or _chain_errors(dive)


def _quoted(quote: Any, haystack: str) -> bool:
    needle = " ".join(quote.split()) if isinstance(quote, str) else ""
    return bool(needle) and needle in haystack


def void_unquoted(dive: Mapping[str, Any], source_text: str) -> tuple[dict, list[str]]:
    """Copy of dive without evidence whose quote is not in source_text, and the dropped paths."""
    haystack = " ".join(source_text.split())
    out = copy.deepcopy(dict(dive))
    dropped = []
    for i, link in enumerate(out["links"]):
        kept = [e for e in link["evidence"] if _quoted(e["quote"], haystack)]
        dropped += [f"links/{i}/evidence/{j}" for j, e in enumerate(link["evidence"]) if e not in kept]
        link["evidence"] = kept
    if out["conserved_pathway"]["value"] is not None and not _quoted(out["conserved_pathway"]["quote"], haystack):
        out["conserved_pathway"] = {"value": None, "quote": out["conserved_pathway"]["quote"]}
        dropped.append("conserved_pathway")
    return out, dropped


# --- link and chain support ---------------------------------------------------


def _item_certainty(paper: str, design: str, known: Mapping[str, float]) -> float:
    if paper in known:
        return float(known[paper])
    return min(evidence.BASE_CERTAINTY[design], UNSCORED_MAX)


def _scored_item(item: Mapping[str, Any], known: Mapping[str, float], designs: Mapping[str, str]) -> dict:
    """The ledger design of a scored paper, when given, overrides the design Thalamus extracted."""
    design = designs.get(item["paper"]) or item["design"]
    certainty = _item_certainty(item["paper"], design, known)
    human = item["paper"] in known and certainty >= LINK_MIN and design not in NOT_HUMAN_PRIMARY
    return {"paper": item["paper"], "design": design, "relation": item["relation"],
            "certainty": certainty, "human": human}


def _scored(items: Sequence[Mapping[str, Any]], known: Mapping[str, float], retracted: Collection[str],
            designs: Mapping[str, str]) -> list[dict]:
    return [_scored_item(e, known, designs) for e in items if e["paper"] not in retracted]


def _strongest(items: Sequence[Mapping[str, Any]], relation: str, prior: float) -> float:
    return max([prior, *(e["certainty"] for e in items if e["relation"] == relation)])


def assess_link(link: Mapping[str, Any], known: Mapping[str, float] | None = None,
                prior: lexicon.LinkVerdict | None = None, retracted: Collection[str] = (), *,
                designs: Mapping[str, str] | None = None) -> dict:
    """Certainty, human support and contest state of one link, merged with the lexicon prior.

    designs maps dedupe keys of scored ledger papers to their ledger design; it
    decides human support where given, so a scored animal paper that Thalamus
    labels rct does not count as human.
    """
    items = _scored(link["evidence"], known or {}, retracted, designs or {})
    base = prior or lexicon.LinkVerdict(0.0, 0.0, False, "open", ())
    support = _strongest(items, "supports", base.support)
    contra = _strongest(items, "contradicts", base.contra)
    human = base.human or any(e["human"] for e in items if e["relation"] == "supports")
    contested = contra > 0.0 and contra >= support
    src, dst = _ends(link)
    return {"id": link["id"], "src": src, "dst": dst, "src_label": link["src"], "dst_label": link["dst"],
            "kind": link["kind"], "support": support, "contra": contra,
            "certainty": 0.0 if contested else support, "human": human, "contested": contested,
            "prior": prior.verdict if prior else None, "evidence": items}


def chain_support(assessed: Sequence[Mapping[str, Any]]) -> dict:
    """Weakest-link certainty, human coverage, their product, and whether the chain holds."""
    if not assessed:
        return {"weakest": 0.0, "coverage": 0.0, "support": 0.0, "holds": False}
    weakest = min(link["certainty"] for link in assessed)
    coverage = sum(1 for link in assessed if link["human"]) / len(assessed)
    contested = any(link["contested"] for link in assessed)
    holds = weakest >= LINK_MIN and coverage >= COVERAGE_MIN and not contested
    return {"weakest": weakest, "coverage": round(coverage, 10), "support": round(weakest * coverage, 10),
            "holds": holds}


# --- risk, measurability, decision --------------------------------------------


def _supplement_problems(claim: Mapping[str, Any], rules: Rules) -> list[str]:
    supp = claim.get("supplement")
    if not isinstance(supp, Mapping):
        return ["supplement:missing"]
    problems = guardrails.supplement_check(str(supp["name"]), supp["dose"], str(supp["unit"]), supp.get("tested_by"),
                                           rules)
    return [f"supplement:{p}" for p in problems]


def risk_problems(claim: Mapping[str, Any], rules: Rules) -> list[str]:
    """Why the intervention is not low risk; empty when an N-of-1 test is allowed."""
    category = claim.get("category")
    if category not in ALLOWED_CATEGORIES:
        return [f"category:{category}"]
    hit = guardrails.forbidden_match(str(claim.get("intervention", "")), rules)
    problems = [f"forbidden:{hit}"] if hit else []
    if category == "supplement":
        problems += _supplement_problems(claim, rules)
    return problems


def measurable(claim: Mapping[str, Any], tracker_keys: Collection[str] = metrics.DAILY_KEYS) -> bool:
    return claim.get("tracker_key") in tracker_keys


def _reasons(assessed: Sequence[Mapping[str, Any]], chain: Mapping[str, Any], risk: Sequence[str],
             is_measurable: bool) -> list[str]:
    reasons = [f"contested:{a['id']}" for a in assessed if a["contested"]]
    reasons += [f"weak_link:{a['id']}" for a in assessed if a["certainty"] < LINK_MIN and not a["contested"]]
    reasons += ["coverage_low"] if chain["coverage"] < COVERAGE_MIN else []
    return [*reasons, *risk, *([] if is_measurable else ["not_measurable"])]


def _needs_question(a: Mapping[str, Any]) -> bool:
    return a["contested"] or not a["human"] or a["certainty"] < LINK_MIN


def open_questions(links: Sequence[Mapping[str, Any]], assessed: Sequence[Mapping[str, Any]]) -> list[dict]:
    """One question per link without settled human support; keywords default to the link's labels."""
    out = []
    for link, a in zip(links, assessed):
        if not _needs_question(a):
            continue
        words = [lexicon.normalize(w) for w in link["keywords"] if lexicon.normalize(w)]
        words = words or [lexicon.normalize(link["src"]), lexicon.normalize(link["dst"])]
        text = link["open_question"] or f"Is {link['src']} -> {link['dst']} shown in humans?"
        out.append({"link": a["id"], "src": a["src"], "dst": a["dst"], "text": text,
                    "keywords": list(dict.fromkeys(words))})
    return out


def belief_id_for(paper_key: str) -> str:
    """Deterministic belief id of a frontier claim, so experiments can cite it."""
    return "fr-" + evidence.slug(paper_key)


def record_rel(paper_key: str) -> str:
    return f"{DIVES_DIR}/{evidence.slug(paper_key)}.json"


def decide(
    dive: Any,
    *,
    source_text: str,
    rules: Rules,
    paper_key: str,
    paper_ref: str,
    at: str,
    known: Mapping[str, float] | None = None,
    view: Mapping[str, Any] | None = None,
    tracker_keys: Collection[str] = metrics.DAILY_KEYS,
    designs: Mapping[str, str] | None = None,
) -> dict:
    """The deep-dive record: per-link certainty, chain support, decision and open questions.

    known maps dedupe keys of scored ledger papers to their certainty and
    designs (optional) to their ledger design; view is the lexicon view
    (prior verdicts, retracted papers). Raises DiveError for an invalid dive,
    so the caller can re-ask Thalamus once.
    """
    problems = validate(dive)
    if problems:
        raise DiveError(problems)
    clean, dropped = void_unquoted(dive, source_text)
    ignored = {*((view or {}).get("retracted") or ()), paper_key}  # retracted papers and the paper itself
    assessed = [assess_link(link, known, lexicon.lookup(view, *_ends(link)), ignored, designs=designs)
                for link in clean["links"]]
    chain = chain_support(assessed)
    risk = risk_problems(clean["claim"], rules)
    reasons = _reasons(assessed, chain, risk, measurable(clean["claim"], tracker_keys))
    questions = open_questions(clean["links"], assessed)
    return {
        "schema": 1, "paper": paper_ref, "paper_key": paper_key, "belief_id": belief_id_for(paper_key),
        "at": at, "claim": clean["claim"], "conserved_pathway": clean["conserved_pathway"]["value"],
        "links": assessed, "dropped_evidence": dropped, "chain": chain, "risk": risk,
        "decision": "n_of_1_candidate" if not reasons else "watch", "reasons": reasons,
        "open_questions": questions, "keywords": sorted({w for q in questions for w in q["keywords"]}),
    }


def promise_inputs(record: Mapping[str, Any], base: evidence.PromiseInput | None = None) -> evidence.PromiseInput:
    """Promise inputs from a deep dive: plausible, coherent, conserved pathway, human link share, direction."""
    links = record.get("links") or ()
    chain = record.get("chain") or {}
    plausible = bool(links) and all(link["support"] > 0 for link in links)
    coherent = bool(links) and not any(link["contested"] for link in links)
    fields = dict((base or evidence.PromiseInput()).__dict__)
    fields.update(plausible=plausible, coherent=coherent, conserved_pathway=record.get("conserved_pathway"),
                  human_link_share=chain.get("coverage"))
    if fields["direction"] is None:
        fields["direction"] = TRACKER_DIRECTION.get(str((record.get("claim") or {}).get("tracker_key")))
    return evidence.PromiseInput(**fields)


def request_meta(paper_meta: Mapping[str, Any], view: Mapping[str, Any] | None) -> dict:
    """The ``paper_meta`` context for a Thalamus deep-dive request: task, paper and prior link verdicts."""
    title = str(paper_meta.get("title") or "")
    return {"task": "deep_dive", "paper": dict(paper_meta), "known_links": lexicon.related_links(view, title)}


# --- lexicon events and ledger ------------------------------------------------


def _node_events(record: Mapping[str, Any], at: str) -> list[dict]:
    nodes: dict[str, dict] = {}
    for link in record["links"]:
        for end, label in ((link["src"], link["src_label"]), (link["dst"], link["dst_label"])):
            nodes.setdefault(end, lexicon.make_node(end.split(":", 1)[0], label, at))
    return list(nodes.values())


def _edge_events(record: Mapping[str, Any], at: str, dive: str) -> list[dict]:
    out = []
    for link in record["links"]:
        out += [lexicon.make_edge(e["relation"], link["src"], link["dst"], e["paper"], at,
                                  certainty=e["certainty"], human=e["human"], dive=dive) for e in link["evidence"]]
        if link["kind"] == "mechanism":
            out.append(lexicon.make_edge("mechanism_of", link["src"], link["dst"], record["paper_key"], at, dive=dive))
    return out


def dive_events(record: Mapping[str, Any], at: str) -> list[dict]:
    """Lexicon events for a deep-dive record: nodes, one edge per verified citation, open questions."""
    dive = record_rel(record["paper_key"])
    questions = [lexicon.make_question(q["src"], q["dst"], q["text"], q["keywords"], at, dive=dive)
                 for q in record["open_questions"]]
    return [*_node_events(record, at), *_edge_events(record, at, dive), *questions]


def store(ledger: Ledger, record: Mapping[str, Any], *, author: str = CODE_AUTHOR) -> str:
    """Write the record, append its lexicon events and rebuild the lexicon view. Returns the record path."""
    rel = record_rel(record["paper_key"])
    ledger.write_json(rel, dict(record), author=author)
    lexicon.append(ledger, dive_events(record, record["at"]), author=author)
    lexicon.rebuild_view(ledger, author=author)
    return rel

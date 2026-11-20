"""Knowledge lexicon: what earlier papers and deep dives found, as a graph in the private ledger.

``evidence/lexicon/events.jsonl`` is append-only. Events (all carry
``schema``, ``kind``, ``at``):

* ``node``: ``id`` (``<type>:<slug>``), ``type`` in NODE_TYPES, ``label``.
* ``edge``: ``rel`` in RELATIONS, ``src``, ``dst``, ``paper`` (a
  ``evidence.dedupe_key``), optional ``certainty`` (0..4), ``human`` and
  ``dive`` (the deep-dive record that wrote it). ``supports``,
  ``contradicts`` and ``mechanism_of`` join two node ids; ``replicates`` and
  ``retracts`` join two paper nodes (``paper:<key>``): src replicates or
  retracts dst.
  An edge counts as human support only with certainty >= ``HUMAN_MIN``, so
  edges stored before deepdive tightened its human flag stay harmless.
* ``question``: an open question about the link ``src`` -> ``dst`` with
  ``text`` and ``keywords``.

``fold`` rebuilds the view from scratch in a fixed order, so the same events
always give the same view. A retraction removes every edge the retracted
paper wrote, whenever it arrives. A question stays open until its link has a
human-supported verdict. ``lookup`` gives a later deep dive the prior verdict
for a link, and ``export_keywords`` turns open questions into scout keywords
for Retina, Spindle and Papilla. Pure except the three ledger helpers at the end.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .store import Ledger, canonical_json

LEXICON_DIR = "evidence/lexicon"
EVENTS_PATH = f"{LEXICON_DIR}/events.jsonl"
VIEW_PATH = f"{LEXICON_DIR}/view.json"
SCHEMA = 1
CODE_AUTHOR = "pineal"
NODE_TYPES = ("concept", "mechanism", "intervention", "outcome")
LINK_RELATIONS = ("supports", "contradicts", "mechanism_of")
PAPER_RELATIONS = ("replicates", "retracts")
RELATIONS = (*LINK_RELATIONS, *PAPER_RELATIONS)
EVENT_KINDS = ("node", "edge", "question")
VERDICTS = ("supported", "contested", "open")
SCOUTS = ("retina", "spindle", "papilla")
PAPER_PREFIX = "paper:"
MAX_CERTAINTY = 4.0
HUMAN_MIN = 0.5  # DC: same as deepdive.LINK_MIN; a human flag below it (or unscored) counts as no human support

_NODE_RE = re.compile(r"^(concept|mechanism|intervention|outcome):[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class LinkVerdict:
    support: float  # strongest supporting certainty, 0..4
    contra: float  # strongest contradicting certainty, 0..4
    human: bool  # some supporting paper studied humans
    verdict: str  # one of VERDICTS
    papers: tuple[str, ...]


# --- identifiers --------------------------------------------------------------


def normalize(text: str) -> str:
    """Casefold, strip diacritics, keep letters and digits separated by single spaces."""
    bare = "".join(ch for ch in unicodedata.normalize("NFKD", str(text).casefold()) if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", bare).split())


def node_id(node_type: str, label: str) -> str:
    """'<type>:<slug>', e.g. ('mechanism', 'AMPK activation') -> 'mechanism:ampk-activation'."""
    if node_type not in NODE_TYPES:
        raise ValueError(f"node type must be one of {NODE_TYPES}")
    slug = normalize(label).replace(" ", "-")
    if not slug:
        raise ValueError("node label has no letters or digits")
    return f"{node_type}:{slug}"


def paper_node(key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("paper key must be a non-empty string")
    return PAPER_PREFIX + key.strip()


def link_key(src: str, dst: str) -> str:
    return f"{src}|{dst}"


# --- events -------------------------------------------------------------------


def _fail(cond: bool, message: str) -> None:
    if cond:
        raise ValueError(message)


def _is_node(value: Any) -> bool:
    return isinstance(value, str) and bool(_NODE_RE.match(value))


def _is_paper(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PAPER_PREFIX) and len(value) > len(PAPER_PREFIX)


def _check_certainty(certainty: Any) -> None:
    if certainty is None:
        return
    ok = isinstance(certainty, (int, float)) and not isinstance(certainty, bool) and math.isfinite(certainty)
    _fail(not ok or not 0.0 <= certainty <= MAX_CERTAINTY, "certainty must be None or in 0..4")


def _check_ends(rel: str, src: Any, dst: Any) -> None:
    check = _is_paper if rel in PAPER_RELATIONS else _is_node
    _fail(not (check(src) and check(dst)), f"{rel} needs two {'paper' if rel in PAPER_RELATIONS else 'node'} ids")


def _check_edge(ev: Mapping[str, Any]) -> None:
    rel = ev.get("rel")
    _fail(rel not in RELATIONS, f"rel must be one of {RELATIONS}")
    _check_ends(rel, ev.get("src"), ev.get("dst"))
    _fail(not isinstance(ev.get("paper"), str) or not ev["paper"].strip(), "edge needs a paper key")
    _check_certainty(ev.get("certainty"))
    _fail(not isinstance(ev.get("human", False), bool), "human must be a boolean")


def _check_node(ev: Mapping[str, Any]) -> None:
    _fail(ev.get("type") not in NODE_TYPES, f"node type must be one of {NODE_TYPES}")
    _fail(not isinstance(ev.get("label"), str) or ev.get("id") != node_id(ev["type"], ev["label"]),
          "node id must equal node_id(type, label)")


def _check_question(ev: Mapping[str, Any]) -> None:
    _fail(not (_is_node(ev.get("src")) and _is_node(ev.get("dst"))), "question needs two node ids")
    _fail(not isinstance(ev.get("text"), str) or not ev["text"].strip(), "question needs text")
    words = ev.get("keywords")
    _fail(not isinstance(words, list) or not all(isinstance(w, str) for w in words), "keywords must be strings")


_CHECKS = {"node": _check_node, "edge": _check_edge, "question": _check_question}


def check_event(ev: Mapping[str, Any]) -> None:
    """Raise ValueError unless ev is a valid lexicon event."""
    _fail(not isinstance(ev, Mapping) or ev.get("kind") not in EVENT_KINDS, "unknown lexicon event kind")
    _fail(ev.get("schema") != SCHEMA or not isinstance(ev.get("at"), str), "event needs schema 1 and at")
    _CHECKS[ev["kind"]](ev)


def _event(kind: str, at: str, **data: Any) -> dict:
    ev = {"schema": SCHEMA, "kind": kind, "at": at, **data}
    check_event(ev)
    return ev


def make_node(node_type: str, label: str, at: str) -> dict:
    return _event("node", at, id=node_id(node_type, label), type=node_type, label=label)


def make_edge(rel: str, src: str, dst: str, paper: str, at: str, *, certainty: float | None = None,
              human: bool = False, dive: str | None = None) -> dict:
    return _event("edge", at, rel=rel, src=src, dst=dst, paper=paper, certainty=certainty, human=human, dive=dive)


def make_question(src: str, dst: str, text: str, keywords: Sequence[str], at: str, *, dive: str | None = None) -> dict:
    words = [normalize(w) for w in keywords if normalize(w)]
    return _event("question", at, src=src, dst=dst, text=text, keywords=list(dict.fromkeys(words)), dive=dive)


# --- fold ---------------------------------------------------------------------


def _ordered(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    out = list(events)
    for ev in out:
        check_event(ev)
    return sorted(out, key=lambda ev: (ev["at"], canonical_json(dict(ev))))


def _retracted(events: Sequence[Mapping[str, Any]]) -> set[str]:
    return {ev["dst"][len(PAPER_PREFIX):] for ev in events if ev["kind"] == "edge" and ev["rel"] == "retracts"}


def _nodes(events: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    return {ev["id"]: {"type": ev["type"], "label": ev["label"]} for ev in events if ev["kind"] == "node"}


def _live_edges(events: Sequence[Mapping[str, Any]], retracted: set[str]) -> list[Mapping[str, Any]]:
    """Edges not written by or about a retracted paper; the last event per (rel, src, dst, paper) wins."""
    latest: dict[tuple, Mapping[str, Any]] = {}
    for ev in events:
        if ev["kind"] != "edge" or ev["rel"] == "retracts" or ev["paper"] in retracted:
            continue
        if ev["rel"] == "replicates" and {ev["src"], ev["dst"]} & {PAPER_PREFIX + k for k in retracted}:
            continue
        latest[(ev["rel"], ev["src"], ev["dst"], ev["paper"])] = ev
    return list(latest.values())


def _verdict(supports: Sequence[Mapping], contradicts: Sequence[Mapping]) -> tuple[float, float, str]:
    support = max((e["certainty"] or 0.0 for e in supports), default=0.0)
    contra = max((e["certainty"] or 0.0 for e in contradicts), default=0.0)
    if contradicts and contra >= support:
        return support, contra, "contested"
    return support, contra, "supported" if supports else "open"


def _human(edge: Mapping[str, Any]) -> bool:
    """The stored human flag, honoured only for a scored edge at or above HUMAN_MIN, as deepdive sets it."""
    certainty = edge.get("certainty")
    return bool(edge.get("human")) and certainty is not None and certainty >= HUMAN_MIN


def _link(src: str, dst: str, edges: Sequence[Mapping[str, Any]]) -> dict:
    by_rel = {rel: [{"paper": e["paper"], "certainty": e.get("certainty"), "human": _human(e)}
                    for e in edges if e["rel"] == rel] for rel in LINK_RELATIONS}
    support, contra, verdict = _verdict(by_rel["supports"], by_rel["contradicts"])
    human = any(e["human"] for e in by_rel["supports"])
    return {"src": src, "dst": dst, **by_rel, "support": support, "contra": contra, "human": human,
            "verdict": verdict}


def _links(edges: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for e in edges:
        if e["rel"] in LINK_RELATIONS:
            groups.setdefault(link_key(e["src"], e["dst"]), []).append(e)
    return {key: _link(*key.split("|"), group) for key, group in sorted(groups.items())}


def _replications(edges: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for e in edges:
        if e["rel"] == "replicates":
            out.setdefault(e["dst"][len(PAPER_PREFIX):], set()).add(e["src"][len(PAPER_PREFIX):])
    return {k: sorted(v) for k, v in sorted(out.items())}


def _settled(link: Mapping[str, Any] | None) -> bool:
    return link is not None and link["verdict"] == "supported" and link["human"]


def _questions(events: Sequence[Mapping[str, Any]], links: Mapping[str, dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for ev in events:
        if ev["kind"] == "question" and not _settled(links.get(link_key(ev["src"], ev["dst"]))):
            q = {"src": ev["src"], "dst": ev["dst"], "text": ev["text"], "keywords": list(ev["keywords"])}
            seen.setdefault((q["src"], q["dst"], normalize(q["text"])), q)
    return list(seen.values())


def fold(events: Iterable[Mapping[str, Any]]) -> dict:
    """The lexicon view: nodes, link verdicts, replications, retracted papers and open questions."""
    ordered = _ordered(events)
    retracted = _retracted(ordered)
    edges = _live_edges(ordered, retracted)
    links = _links(edges)
    return {
        "schema": SCHEMA, "nodes": _nodes(ordered), "links": links, "replications": _replications(edges),
        "retracted": sorted(retracted), "questions": _questions(ordered, links),
    }


# --- queries ------------------------------------------------------------------


def lookup(view: Mapping[str, Any] | None, src: str, dst: str) -> LinkVerdict | None:
    """Prior verdict for the link src -> dst, or None when the lexicon has not seen it."""
    link = ((view or {}).get("links") or {}).get(link_key(src, dst))
    if not isinstance(link, Mapping):
        return None
    papers = sorted({e["paper"] for rel in ("supports", "contradicts") for e in link.get(rel, ())})
    return LinkVerdict(float(link["support"]), float(link["contra"]), bool(link["human"]), str(link["verdict"]),
                       tuple(papers))


def replications(view: Mapping[str, Any] | None, paper_key: str) -> int:
    """Independent papers that replicate paper_key, for ``evidence.score(replications=...)``."""
    return len(((view or {}).get("replications") or {}).get(paper_key, ()))


def _named_nodes(view: Mapping[str, Any], text: str) -> set[str]:
    hay = f" {normalize(text)} "
    labels = {nid: normalize(n["label"]) for nid, n in (view.get("nodes") or {}).items()}
    return {nid for nid, label in labels.items() if label and f" {label} " in hay}


def related_links(view: Mapping[str, Any] | None, text: str) -> list[dict]:
    """Links touching a node whose label occurs as a phrase in text, for a deep-dive prompt."""
    hits = _named_nodes(view or {}, text)
    links = ((view or {}).get("links") or {}).values()
    return [{k: link[k] for k in ("src", "dst", "verdict", "support", "contra", "human")}
            for link in links if link["src"] in hits or link["dst"] in hits]


def open_keywords(view: Mapping[str, Any] | None) -> list[str]:
    """Sorted unique keywords of every open question."""
    return sorted({w for q in (view or {}).get("questions") or () for w in q["keywords"]})


def _query(words: Sequence[str]) -> str | None:
    return " AND ".join(f'"{w}"' for w in words) if words else None


def export_keywords(view: Mapping[str, Any] | None, scouts: Sequence[str] = SCOUTS) -> dict[str, dict]:
    """Per scout: open-question keywords as ``terms`` and one AND query per question."""
    queries = [q for q in (_query(x["keywords"]) for x in (view or {}).get("questions") or ()) if q]
    section = {"terms": open_keywords(view), "queries": list(dict.fromkeys(queries))}
    return {scout: {"terms": list(section["terms"]), "queries": list(section["queries"])} for scout in scouts}


def _merged_terms(terms: Any, extra: Sequence[str]) -> Any:
    if isinstance(terms, Mapping):
        return {**{w: 1.0 for w in extra}, **dict(terms)}
    return list(dict.fromkeys([*(terms or ()), *extra]))


def merge_keywords(config: Mapping[str, Any], export: Mapping[str, Mapping[str, Sequence[str]]]) -> dict:
    """config/keywords.json with the exported terms and queries added; existing entries win."""
    out = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in config.items()}
    for scout, extra in export.items():
        section = dict(out.get(scout) or {})
        section["terms"] = _merged_terms(section.get("terms"), extra.get("terms", ()))
        section["queries"] = list(dict.fromkeys([*(section.get("queries") or ()), *extra.get("queries", ())]))
        out[scout] = section
    return out


# --- ledger -------------------------------------------------------------------


def append(ledger: Ledger, events: Iterable[Mapping[str, Any]], *, author: str = CODE_AUTHOR) -> int:
    """Validate and append events to evidence/lexicon/events.jsonl; returns how many."""
    batch = list(events)
    for ev in batch:
        check_event(ev)
    for ev in batch:
        ledger.append_jsonl(EVENTS_PATH, ev, author=author)
    return len(batch)


def load_view(ledger: Ledger) -> dict:
    return fold(ledger.read_jsonl(EVENTS_PATH))


def rebuild_view(ledger: Ledger, *, author: str = CODE_AUTHOR) -> str:
    """Fold the events and write evidence/lexicon/view.json; returns its path."""
    ledger.write_json(VIEW_PATH, load_view(ledger), author=author)
    return VIEW_PATH

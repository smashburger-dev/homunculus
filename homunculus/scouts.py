"""Retina, Spindle and Papilla: one code path with three keyword sets.

Code does the fetching, deduplication, retraction veto and ranking. Fetched
free text (abstracts) goes only to ``quarantine/<candidate-id>.txt``; the
candidate records written to ``evidence/candidates/<date>.jsonl`` hold
validated identifiers, a sanitized title and enums. Haiku sees the abstract
only inside an escaped ``<data>`` block and may answer only TRIAGE_SCHEMA.

Every request goes through ``Http``: an https-only host allowlist (redirects
included), a per-host rate limit, a file cache and an injectable ``opener`` so
tests never touch the network. ``offline=True`` serves the cache only.

``keywords`` is either the scout's own section of ``config/keywords.json`` or
the whole file keyed by scout name. A section may hold::

    {"queries": ["sleep AND exercise"],     # PubMed and Europe PMC queries
     "terms": ["sleep", "hrv"] | {"sleep": 2.0},  # bioRxiv filter and ranking
     "servers": ["biorxiv", "medrxiv"],     # preprint servers (default both)
     "pub_type_weights": {"meta-analysis": 3.0}}  # overrides the defaults
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import agents
from .evidence import DESIGNS
from .store import Ledger

log = logging.getLogger(__name__)

ALLOWED_HOSTS: frozenset[str] = frozenset({
    "eutils.ncbi.nlm.nih.gov", "www.ebi.ac.uk", "api.biorxiv.org", "api.crossref.org",
    "api.openalex.org", "api.semanticscholar.org", "wger.de", "api.ods.od.nih.gov",
    # full-text resolver (homunculus.fulltext): open-access sources, archives, publisher TDM APIs
    "api.unpaywall.org", "api.core.ac.uk", "core.ac.uk", "arxiv.org", "www.biorxiv.org", "www.medrxiv.org",
    "api.fatcat.wiki", "web.archive.org", "api.elsevier.com", "api.springernature.com", "api.wiley.com",
})

DEFAULT_RATE_PER_S = 1.0
DEFAULT_TTL_S = 12 * 3600.0  # a daily scout run never reuses yesterday's search
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TIMEOUT_S = 30
USER_AGENT = "homunculus-scout/0.1"
LOOKBACK_DAYS = 7
RETMAX = 100
BIORXIV_MAX_PAGES = 5
BIORXIV_SERVERS = ("biorxiv", "medrxiv")
QUARANTINE_MAX_CHARS = 20_000
TITLE_MAX_CHARS = 300
TRIAGE_MAX_TOKENS = 1024
TRIAGE_EFFORT = "low"
PREPRINT_PENALTY = 0.5
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agents"

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
BIORXIV = "https://api.biorxiv.org/details/"
CROSSREF = "https://api.crossref.org/works"

DEFAULT_PUB_TYPE_WEIGHTS: dict[str, float] = {
    "meta analysis": 3.0, "systematic review": 3.0, "randomized controlled trial": 2.0,
    "clinical trial": 1.0, "review": 0.5,
}
_RETRACTION_TYPES = frozenset({"retraction", "withdrawal", "removal"})
_RETRACTED_PUB_TYPES = frozenset({"retracted publication", "retraction of publication", "retraction notice"})

_DOI_RE = re.compile(r"^10\.\d{4,9}/[^\s]{1,200}$")
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_PMID_RE = re.compile(r"^\d{1,9}$")
_REG_RE = re.compile(r"\b(NCT\d{8}|ISRCTN\d{8}|DRKS\d{8}|ACTRN\d{14}|ChiCTR[-\dA-Z]{6,20})\b")
_PUB_TYPE_RE = re.compile(r"^[A-Za-z0-9 ,'()/-]{1,60}$")
_PRODUCT_RE = re.compile(r"^[^\x00-\x1f<>{}\[\]`$]{1,80}$")
_CAND_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


class HostNotAllowed(PermissionError):
    """URL is not https or its host is not in ALLOWED_HOSTS."""


class CacheMiss(LookupError):
    """Offline mode and the response is not cached."""


# --------------------------------------------------------------------------
# HTTP client

Opener = Callable[[urllib.request.Request], bytes]


def _check_url(url: str) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.port not in (None, 443):
        raise HostNotAllowed(f"refusing URL {url!r}")
    if (parts.hostname or "") not in ALLOWED_HOSTS:
        raise HostNotAllowed(f"host not allowed: {parts.hostname!r}")
    return parts


def _public_url(url: str, params: Mapping[str, str] | None) -> tuple[str, str]:
    """(host, canonical URL with sorted query). Secrets are never part of it."""
    parts = _check_url(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    pairs += [(str(k), str(v)) for k, v in (params or {}).items()]
    query = urllib.parse.urlencode(sorted(pairs))
    return parts.hostname or "", urllib.parse.urlunsplit(("https", parts.hostname, parts.path, query, ""))


def _with_query(url: str, extra: Mapping[str, str]) -> str:
    if not extra:
        return url
    sep = "&" if urllib.parse.urlsplit(url).query else "?"
    return url + sep + urllib.parse.urlencode(sorted(extra.items()))


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(req: urllib.request.Request) -> bytes:
    _check_url(req.full_url)
    opener = urllib.request.build_opener(_AllowlistRedirect())
    with opener.open(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 (allowlisted https)
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError(f"response from {req.host} exceeds {MAX_RESPONSE_BYTES} bytes")
    return raw


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Http:
    """Allowlisted, rate-limited, cached GET client.

    ``keys`` maps host -> API key, sent as ``api_key`` (header ``x-api-key`` for
    Semantic Scholar) and never written to the cache. ``mailto`` goes to the
    APIs that ask for it (Crossref, OpenAlex polite pool; NCBI ``email``).
    ``clock``/``sleep`` drive the rate limit and cache age; tests inject fakes.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        mailto: str | None,
        keys: Mapping[str, str],
        rate_per_s: Mapping[str, float],
        opener: Opener | None = None,
        offline: bool = False,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        ttl_s: float = DEFAULT_TTL_S,
    ) -> None:
        if any(float(r) <= 0 for r in rate_per_s.values()):
            raise ValueError("rate_per_s values must be > 0")
        self.cache_dir = Path(cache_dir)
        self.offline = offline
        self._mailto = mailto
        self._keys = dict(keys)
        self._rates = {h: float(r) for h, r in rate_per_s.items()}
        self._opener = opener or _default_opener
        self._clock, self._sleep, self._ttl = clock, sleep, float(ttl_s)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        return json.loads(self.get_text(url, params))

    def get_text(self, url: str, params: Mapping[str, str] | None = None) -> str:
        host, public = _public_url(url, params)
        path = self.cache_dir / host / (hashlib.sha256(public.encode("utf-8")).hexdigest() + ".json")
        cached = self._read_cache(path)
        if cached is not None and (self.offline or self._clock() - cached["fetched_at"] < self._ttl):
            return cached["body"]
        if self.offline:
            raise CacheMiss(public)
        body = self._fetch(host, public)
        entry = {"url": public, "fetched_at": self._clock(), "body": body}
        _atomic_write(path, json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        return body

    def _read_cache(self, path: Path) -> dict | None:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        ok = isinstance(entry, dict) and isinstance(entry.get("body"), str)
        return entry if ok and isinstance(entry.get("fetched_at"), (int, float)) else None

    def _extras(self, host: str) -> tuple[dict[str, str], dict[str, str]]:
        """(query params, headers) added on the wire only."""
        params: dict[str, str] = {}
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain"}
        if self._mailto and host in ("api.crossref.org", "api.openalex.org"):
            params["mailto"] = self._mailto
        if self._mailto and host == "eutils.ncbi.nlm.nih.gov":
            params.update(email=self._mailto, tool="homunculus")
        key = self._keys.get(host)
        if key and host == "api.semanticscholar.org":
            headers["x-api-key"] = key
        elif key:
            params["api_key"] = key
        return params, headers

    def _wait(self, host: str) -> None:
        gap = 1.0 / self._rates.get(host, DEFAULT_RATE_PER_S)
        with self._lock:
            now = self._clock()
            last = self._last.get(host)
            delay = 0.0 if last is None else max(0.0, last + gap - now)
            if delay > 0:
                self._sleep(delay)
            self._last[host] = now + delay

    def _fetch(self, host: str, public: str) -> str:
        params, headers = self._extras(host)
        self._wait(host)
        req = urllib.request.Request(_with_query(public, params), headers=headers, method="GET")
        return self._opener(req).decode("utf-8")


# --------------------------------------------------------------------------
# Candidates


@dataclass(frozen=True)
class Candidate:
    id: str
    scout: str
    doi: str | None
    pmid: str | None
    registration_id: str | None
    title: str
    year: int | None
    pub_types: tuple[str, ...]
    source_api: str
    preprint: bool
    query: str
    found_at: str
    quarantine_path: str | None
    triage: Mapping[str, Any] | None
    score: float


Hit = tuple[Candidate, "str | None"]  # candidate and its abstract, when the search returned one


def _as_map(value: Any) -> Mapping:
    """Malformed API shapes read as empty instead of raising deep in parsing."""
    return value if isinstance(value, Mapping) else {}


def _get_map(http: Http, url: str, params: Mapping[str, str] | None = None) -> Mapping:
    return _as_map(http.get_json(url, params))


def _clean_doi(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    doi = _DOI_PREFIX_RE.sub("", value.strip()).strip().lower()
    return doi if _DOI_RE.match(doi) else None


def _clean_pmid(value: Any) -> str | None:
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return text if _PMID_RE.match(text) else None


def _clean_title(value: Any) -> str:
    text = _CONTROL_RE.sub(" ", value if isinstance(value, str) else "")
    text = re.sub(r"<[^>]{0,40}>", "", text)  # PubMed/Europe PMC inline markup
    return " ".join(text.split())[:TITLE_MAX_CHARS]


def _clean_pub_types(values: Iterable[Any]) -> tuple[str, ...]:
    kept = (" ".join(str(v).split()) for v in values if isinstance(v, str))
    return tuple(sorted({v for v in kept if _PUB_TYPE_RE.match(v)}))[:10]


def _year(value: Any) -> int | None:
    match = re.match(r"\s*(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def normalize_title(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "").casefold()
    plain = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", plain).split())


def _keys(doi: str | None, pmid: str | None, reg: str | None, title: str) -> list[str]:
    keys = [f"doi:{doi}"] if doi else []
    keys += [f"pmid:{pmid}"] if pmid else []
    keys += [f"reg:{reg.upper()}"] if reg else []
    return keys or [f"title:{normalize_title(title)}"]


def _cand_keys(c: Candidate) -> list[str]:
    return _keys(c.doi, c.pmid, c.registration_id, c.title)


def _make(*, doi: Any, pmid: Any, title: Any, year: Any, pub_types: Iterable[Any], source_api: str,
          preprint: bool, query: str, registration_id: str | None = None) -> Candidate:
    """Candidate from raw API fields. scout and found_at are filled by run_scout."""
    clean_doi, clean_pmid, clean_title = _clean_doi(doi), _clean_pmid(pmid), _clean_title(title)
    key = _keys(clean_doi, clean_pmid, registration_id, clean_title)[0]
    return Candidate(
        id="c-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12], scout="",
        doi=clean_doi, pmid=clean_pmid, registration_id=registration_id, title=clean_title,
        year=_year(year), pub_types=_clean_pub_types(pub_types), source_api=source_api,
        preprint=preprint, query=query, found_at="", quarantine_path=None, triage=None, score=0.0,
    )


def _registration(text: str | None) -> str | None:
    match = _REG_RE.search(text or "")
    return match.group(1).upper() if match else None


# --------------------------------------------------------------------------
# PubMed


def search_pubmed(http: Http, term: str, reldays: int = 7) -> list[Candidate]:
    """E-utilities esearch + esummary; 'Retracted Publication'[pt] is excluded in the query."""
    return [c for c, _ in _pubmed_hits(http, term, reldays)]


def _pubmed_hits(http: Http, term: str, reldays: int) -> list[Hit]:
    params = {
        "db": "pubmed", "term": f"({term}) NOT Retracted Publication[pt]", "reldate": str(int(reldays)),
        "datetype": "edat", "retmode": "json", "retmax": str(RETMAX),
    }
    found = _get_map(http, EUTILS + "esearch.fcgi", params)
    ids = [i for i in _as_map(found.get("esearchresult")).get("idlist") or [] if _clean_pmid(i)]
    if not ids:
        return []
    summary = _get_map(http, EUTILS + "esummary.fcgi", {"db": "pubmed", "id": ",".join(ids), "retmode": "json"})
    result = _as_map(summary.get("result"))
    docs = (result.get(uid) for uid in result.get("uids") or ids)
    return [(_pubmed_candidate(d, term), None) for d in docs if _pubmed_usable(d)]


def _pubmed_usable(doc: Any) -> bool:
    if not isinstance(doc, Mapping) or not _clean_pmid(doc.get("uid")):
        return False
    types = {str(t).casefold() for t in doc.get("pubtype") or ()}
    return not types & _RETRACTED_PUB_TYPES


def _pubmed_doi(doc: Mapping) -> str | None:
    for entry in doc.get("articleids") or ():
        if isinstance(entry, Mapping) and entry.get("idtype") == "doi":
            return entry.get("value")
    return None


def _pubmed_candidate(doc: Mapping, term: str) -> Candidate:
    types = list(doc.get("pubtype") or ())
    return _make(
        doi=_pubmed_doi(doc), pmid=doc.get("uid"), title=doc.get("title"), year=doc.get("pubdate"),
        pub_types=types, source_api="pubmed", preprint="Preprint" in types, query=term,
    )


def _pubmed_abstract(http: Http, pmid: str) -> str:
    params = {"db": "pubmed", "id": pmid, "rettype": "abstract", "retmode": "text"}
    return http.get_text(EUTILS + "efetch.fcgi", params)


# --------------------------------------------------------------------------
# Europe PMC


def search_europepmc(http: Http, query: str, since: str) -> list[Candidate]:
    """Europe PMC REST search (core results) for first publication on or after ``since``."""
    return [c for c, _ in _europepmc_hits(http, query, since)]


def _europepmc_hits(http: Http, query: str, since: str) -> list[Hit]:
    params = {
        "query": f"({query}) AND FIRST_PDATE:[{since} TO 9999-12-31]",
        "format": "json", "resultType": "core", "pageSize": str(RETMAX),
    }
    rows = _as_map(_get_map(http, EUROPEPMC, params).get("resultList")).get("result") or []
    return [_europepmc_hit(r, query) for r in rows if _europepmc_usable(r)]


def _europepmc_types(row: Mapping) -> list[str]:
    types = _as_map(row.get("pubTypeList")).get("pubType") or []
    return [types] if isinstance(types, str) else list(types)


def _europepmc_usable(row: Any) -> bool:
    if not isinstance(row, Mapping):
        return False
    types = {str(t).casefold() for t in _europepmc_types(row)}
    return not types & _RETRACTED_PUB_TYPES


def _europepmc_hit(row: Mapping, query: str) -> Hit:
    abstract = row.get("abstractText") if isinstance(row.get("abstractText"), str) else None
    cand = _make(
        doi=row.get("doi"), pmid=row.get("pmid"), title=row.get("title"), year=row.get("pubYear"),
        pub_types=_europepmc_types(row), source_api="europepmc", preprint=row.get("source") == "PPR",
        query=query, registration_id=_registration(abstract),
    )
    return cand, abstract


# --------------------------------------------------------------------------
# bioRxiv / medRxiv


def search_biorxiv(http: Http, server: str, since: str, until: str, terms: Sequence[str]) -> list[Candidate]:
    """bioRxiv/medRxiv details API for [since, until], kept when a term occurs in title, abstract or category."""
    return dedupe([c for c, _ in _biorxiv_hits(http, server, since, until, terms)])


def _biorxiv_pages(http: Http, server: str, since: str, until: str) -> list[Mapping]:
    rows: list[Mapping] = []
    cursor = 0
    for _ in range(BIORXIV_MAX_PAGES):
        data = _get_map(http, f"{BIORXIV}{server}/{since}/{until}/{cursor}/json")
        page = [r for r in data.get("collection") or [] if isinstance(r, Mapping)]
        rows += page
        meta = _as_map((data.get("messages") or [{}])[0])
        cursor += len(page)
        if not page or cursor >= int(meta.get("total", 0) or 0):
            break
    return rows


def _matched_term(row: Mapping, terms: Sequence[str]) -> str | None:
    hay = " " + normalize_title(" ".join(str(row.get(k) or "") for k in ("title", "abstract", "category"))) + " "
    return next((t for t in terms if f" {t} " in hay), None)


def _biorxiv_hits(http: Http, server: str, since: str, until: str, terms: Sequence[str]) -> list[Hit]:
    if server not in BIORXIV_SERVERS:
        raise ValueError(f"unknown preprint server {server!r}")
    terms = [normalize_title(str(t)) for t in terms if normalize_title(str(t))]
    if not terms:
        return []
    hits: list[Hit] = []
    for row in _biorxiv_pages(http, server, since, until):
        term = _matched_term(row, terms)
        if term is not None:
            hits.append(_biorxiv_hit(row, server, term))
    return hits


def _biorxiv_hit(row: Mapping, server: str, term: str) -> Hit:
    abstract = row.get("abstract") if isinstance(row.get("abstract"), str) else None
    cand = _make(
        doi=row.get("doi"), pmid=None, title=row.get("title"), year=row.get("date"), pub_types=["preprint"],
        source_api=server, preprint=True, query=term, registration_id=_registration(abstract),
    )
    return cand, abstract


# --------------------------------------------------------------------------
# Crossref


def _crossref_get(http: Http, doi: str) -> Mapping | None:
    try:
        data = _get_map(http, f"{CROSSREF}/{urllib.parse.quote(doi, safe='/')}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    message = data.get("message")
    return message if isinstance(message, Mapping) else None


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    return _clean_title(value) or None


def _authors(message: Mapping) -> list[str]:
    names = []
    for a in message.get("author") or ():
        if isinstance(a, Mapping):
            name = " ".join(str(a.get(k) or "") for k in ("given", "family")).strip() or str(a.get("name") or "")
            names.append(_clean_title(name))
    return [n for n in names if n]


def _date(entry: Mapping) -> str | None:
    parts = (_as_map(entry.get("updated")).get("date-parts") or [[]])[0]
    if not parts or not all(isinstance(p, int) for p in parts):
        return None
    return "-".join(f"{p:02d}" for p in parts)


def _update(entry: Any, source_default: str) -> dict | None:
    if not isinstance(entry, Mapping) or not entry.get("type"):
        return None
    return {
        "type": str(entry["type"]).strip().lower(), "source": str(entry.get("source") or source_default),
        "doi": _clean_doi(entry.get("DOI")), "date": _date(entry),
    }


def _updated_by(message: Mapping) -> list[dict]:
    found = (_update(e, "crossref") for e in message.get("updated-by") or ())
    return [u for u in found if u is not None]


def resolve_doi(http: Http, doi: str) -> dict | None:
    """Crossref work: doi, title, authors, journal, year, update_to (entries updating this work)."""
    clean = _clean_doi(doi)
    message = _crossref_get(http, clean) if clean else None
    if message is None:
        return None
    issued = (_as_map(message.get("issued")).get("date-parts") or [[None]])[0]
    return {
        "doi": clean, "title": _first(message.get("title")), "authors": _authors(message),
        "journal": _first(message.get("container-title")), "year": _year(issued[0] if issued else None),
        "update_to": _updated_by(message),
    }


def _notices(http: Http, doi: str) -> list[dict]:
    """Notices whose update-to points at doi (Crossref filter 'updates', UNVERIFIED for every notice type)."""
    data = _get_map(http, CROSSREF, {"filter": f"updates:{doi}", "rows": "20"})
    items = [i for i in _as_map(data.get("message")).get("items") or [] if isinstance(i, Mapping)]
    found = []
    for item in items:
        for entry in item.get("update-to") or ():
            update = _update(entry, "crossref")
            if update and update["doi"] == doi:
                found.append({**update, "doi": _clean_doi(item.get("DOI"))})
    return found


def crossref_updates(http: Http, doi: str) -> list[dict]:
    """Updates of doi: {type, source, doi (of the notice), date}; source may be 'retraction-watch'."""
    clean = _clean_doi(doi)
    if clean is None:
        return []
    work = resolve_doi(http, clean)
    entries = list(work["update_to"]) if work else []
    entries += _notices(http, clean)
    unique = {json.dumps(e, sort_keys=True): e for e in entries}
    return [unique[k] for k in sorted(unique)]


def _retracted(updates: Sequence[Mapping]) -> bool:
    return any(str(u.get("type", "")).replace("-", "_") in _RETRACTION_TYPES for u in updates)


# --------------------------------------------------------------------------
# Matching, dedupe, ranking


def title_match(a: str, b: str) -> float:
    """difflib ratio of normalized titles; 0.0 when either is empty."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def dedupe(cands: Sequence[Candidate]) -> list[Candidate]:
    """First occurrence wins; a later candidate sharing its doi, pmid or registration id is dropped."""
    seen: set[str] = set()
    out = []
    for cand in cands:
        keys = _cand_keys(cand)
        if seen.isdisjoint(keys):
            out.append(cand)
        seen.update(keys)
    return out


def _section(keywords: Mapping[str, Any], scout: str | None) -> Mapping[str, Any]:
    inner = keywords.get(scout) if scout else None
    return inner if isinstance(inner, Mapping) else keywords


def has_keywords(keywords: Any, scout: str) -> bool:
    """True when the scout's section holds a query or a ranking term; else a run would find nothing."""
    if not isinstance(keywords, Mapping):
        return False
    section = _section(keywords, scout)
    return bool(section.get("queries") or section.get("terms"))


def _term_weights(section: Mapping[str, Any]) -> dict[str, float]:
    terms = section.get("terms") or {}
    if isinstance(terms, Mapping):
        return {normalize_title(str(k)): float(v) for k, v in terms.items() if normalize_title(str(k))}
    return {normalize_title(str(t)): 1.0 for t in terms if normalize_title(str(t))}


def _type_weights(section: Mapping[str, Any]) -> dict[str, float]:
    weights = dict(DEFAULT_PUB_TYPE_WEIGHTS)
    weights.update({normalize_title(str(k)): float(v) for k, v in (section.get("pub_type_weights") or {}).items()})
    return weights


def _score(c: Candidate, terms: Mapping[str, float], types: Mapping[str, float]) -> float:
    title = f" {normalize_title(c.title)} "
    total = sum(w for t, w in terms.items() if f" {t} " in title)
    total += max((types.get(normalize_title(p), 0.0) for p in c.pub_types), default=0.0)
    if c.triage is not None:
        total += float(c.triage.get("relevance", 0))
    return round(total - (PREPRINT_PENALTY if c.preprint else 0.0), 6)


def rank(cands: Sequence[Candidate], keywords: Mapping[str, Any]) -> list[Candidate]:
    """Keyword hits in the title, best pub-type weight, triage relevance, minus a preprint penalty.

    Ties break on newer year, then id, so the order is deterministic.
    """
    scout = cands[0].scout if cands else None
    section = _section(keywords, scout)
    terms, types = _term_weights(section), _type_weights(section)
    scored = [dataclasses.replace(c, score=_score(c, terms, types)) for c in cands]
    return sorted(scored, key=lambda c: (-c.score, -(c.year or 0), c.id))


# --------------------------------------------------------------------------
# Quarantine and the scout run


def quarantine_text(ledger: Ledger, cand_id: str, text: str, *, author: str) -> str:
    """Write fetched text to quarantine/<cand_id>.txt; returns the ledger-relative path."""
    if not _CAND_ID_RE.match(cand_id):
        raise ValueError(f"invalid candidate id {cand_id!r}")
    rel = f"quarantine/{cand_id}.txt"
    body = text.replace("\r\n", "\n")[:QUARANTINE_MAX_CHARS]
    ledger.write_text(rel, body.rstrip("\n") + "\n", author=author)
    return rel


_TRANSPORT_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, CacheMiss)


def _safe(label: str, fetch: Callable[[], list[Hit]]) -> list[Hit]:
    """A failing source is skipped; HostNotAllowed and other bugs still raise."""
    try:
        return fetch()
    except _TRANSPORT_ERRORS as exc:
        log.warning("scout source %s failed: %s", label, type(exc).__name__)
        return []


def _gather(http: Http, section: Mapping[str, Any], now: datetime) -> list[Hit]:
    since = (now.date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    until = now.date().isoformat()
    terms = list(_term_weights(section)) if section.get("terms") else []
    hits: list[Hit] = []
    for q in section.get("queries") or ():
        hits += _safe("pubmed", lambda q=q: _pubmed_hits(http, str(q), LOOKBACK_DAYS))
        hits += _safe("europepmc", lambda q=q: _europepmc_hits(http, str(q), since))
    for server in section.get("servers", BIORXIV_SERVERS):
        hits += _safe(str(server), lambda s=server: _biorxiv_hits(http, str(s), since, until, terms))
    return hits


def _history(ledger: Ledger) -> set[str]:
    seen: set[str] = set()
    for rel in ledger.glob("evidence/candidates/*.jsonl"):
        for rec in ledger.read_jsonl(rel):
            seen.update(_keys(rec.get("doi"), rec.get("pmid"), rec.get("registration_id"), rec.get("title") or ""))
    return seen


def _fresh(hits: Sequence[Hit], history: set[str]) -> tuple[list[Candidate], dict[str, str]]:
    texts: dict[str, str] = {}
    for cand, text in hits:
        if text and text.strip():
            texts.setdefault(cand.id, text)
    unique = dedupe([c for c, _ in hits])
    return [c for c in unique if history.isdisjoint(_cand_keys(c))], texts


def _abstract(http: Http, cand: Candidate, text: str | None) -> str | None:
    if text or cand.source_api != "pubmed" or not cand.pmid:
        return text
    try:
        return _pubmed_abstract(http, cand.pmid) or None
    except _TRANSPORT_ERRORS:
        return None


def _vetoed(http: Http, cand: Candidate) -> bool:
    """Retraction veto. A failed Crossref lookup vetoes too (fail closed)."""
    if cand.doi is None:
        return False
    try:
        return _retracted(crossref_updates(http, cand.doi))
    except _TRANSPORT_ERRORS:
        return True


def _admit(cand: Candidate, text: str | None, http: Http, ledger: Ledger) -> Candidate | None:
    if _vetoed(http, cand):
        return None
    text = _abstract(http, cand, text)
    if not text:
        return cand
    reg = cand.registration_id or _registration(text)
    path = quarantine_text(ledger, cand.id, f"{cand.title}\n\n{text}", author=cand.scout)
    return dataclasses.replace(cand, registration_id=reg, quarantine_path=path)


def _record(cand: Candidate) -> dict:
    rec = dataclasses.asdict(cand)
    rec["pub_types"] = list(cand.pub_types)
    rec["triage"] = dict(cand.triage) if cand.triage is not None else None
    return rec


def run_scout(scout: str, keywords: Mapping[str, Any], http: Http, ledger: Ledger, now: datetime,
              cap: int = 30) -> list[Candidate]:
    """Fetch, dedupe (batch and candidate history), rank, veto retractions, quarantine, append the day's jsonl."""
    if scout not in agents.SCOUTS:
        raise ValueError(f"unknown scout {scout!r}")
    section = _section(keywords, scout)
    stamp = now.isoformat(timespec="seconds")
    hits = [(dataclasses.replace(c, scout=scout, found_at=stamp), t) for c, t in _gather(http, section, now)]
    fresh, texts = _fresh(hits, _history(ledger))
    rel = f"evidence/candidates/{now.date().isoformat()}.jsonl"
    out: list[Candidate] = []
    for cand in rank(fresh, section):
        if len(out) >= cap:
            break
        kept = _admit(cand, texts.get(cand.id), http, ledger)
        if kept is not None:
            ledger.append_jsonl(rel, _record(kept), author=scout)
            out.append(kept)
    return out


# --------------------------------------------------------------------------
# Triage (Haiku via Batch)

POPULATIONS = (
    "healthy_trained", "healthy_recreational", "healthy_untrained", "healthy_mixed",
    "older_adults", "youth", "clinical", "animal", "cells", "not_stated",
)
EXERCISE_PATTERNS = (
    "squat", "hinge", "lunge", "push_horizontal", "push_vertical", "pull_horizontal", "pull_vertical",
    "carry", "rotation", "anti_rotation", "locomotion", "jump", "cyclical", "mobility", "other",
)
EXERCISE_EQUIPMENT = (
    "bodyweight", "barbell", "dumbbell", "kettlebell", "machine", "cable", "band", "bike", "rower",
    "treadmill", "other",
)
CONTRACTIONS = ("concentric", "eccentric", "isometric", "mixed")
ENERGY_SYSTEMS = ("aerobic", "glycolytic", "phosphagen", "mixed")
JOINT_LOADS = ("low", "moderate", "high")


def _nullable_enum(values: Sequence[str]) -> dict:
    return {"type": ["string", "null"], "enum": [*values, None]}


_EXERCISE_SCHEMA: dict = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["pattern", "equipment", "contraction", "energy_system", "joint_load"],
    "properties": {
        "pattern": {"type": "string", "enum": list(EXERCISE_PATTERNS)},
        "equipment": _nullable_enum(EXERCISE_EQUIPMENT),
        "contraction": _nullable_enum(CONTRACTIONS),
        "energy_system": _nullable_enum(ENERGY_SYSTEMS),
        "joint_load": _nullable_enum(JOINT_LOADS),
    },
}

TRIAGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relevance", "design", "population", "is_human", "exercise"],
    "properties": {
        "relevance": {"type": "integer", "minimum": 0, "maximum": 3},
        "design": {"type": "string", "enum": list(DESIGNS)},
        "population": {"type": "string", "enum": list(POPULATIONS)},
        "is_human": {"type": "boolean"},
        "exercise": _EXERCISE_SCHEMA,
        # Papilla only: names for the forbidden-list check in code.
        "products": {"type": "array", "maxItems": 20, "items": {"type": "string", "minLength": 1, "maxLength": 80}},
    },
}


def _bot(scout: str) -> agents.BotSpec:
    """Just enough of a BotSpec for render_prompt, which reads prompt_file only."""
    return agents.BotSpec(
        name=scout, anatomy="", role="", tier="small", provider="anthropic", model="", alt_provider=None,
        alt_model=None, effort=TRIAGE_EFFORT, user_facing=False, cadence="", prompt_file=f"{scout}.md",
        writes=(), max_tokens=TRIAGE_MAX_TOKENS, daily_usd_cap=0.0,
    )


def _prompt_candidate(cand: Candidate) -> dict:
    """Identifiers and enums only; the title travels in the quarantined data block."""
    return {
        "id": cand.id, "doi": cand.doi, "pmid": cand.pmid, "year": cand.year,
        "pub_types": list(cand.pub_types), "source_api": cand.source_api, "preprint": cand.preprint,
    }


def _triage_request(cand: Candidate, abstract: str, keywords: Mapping[str, Any]) -> agents.LLMRequest:
    context = {"keywords": dict(_section(keywords, cand.scout)), "candidate": _prompt_candidate(cand)}
    system, user = agents.render_prompt(_bot(cand.scout), PROMPTS_DIR, context, {"abstract": abstract})
    return agents.LLMRequest(
        bot=cand.scout, system=system, messages=({"role": "user", "content": user},),
        output_schema=TRIAGE_SCHEMA, max_tokens=TRIAGE_MAX_TOKENS, effort=TRIAGE_EFFORT,
    )


def triage_requests(cands: Sequence[Candidate], ledger: Ledger,
                    keywords: Mapping[str, Any]) -> list[tuple[str, agents.LLMRequest]]:
    """Batch items (custom_id = candidate id). Candidates without quarantined text are skipped."""
    items = []
    for cand in cands:
        if cand.scout not in agents.SCOUTS:
            raise ValueError(f"unknown scout {cand.scout!r}")
        if cand.quarantine_path and ledger.exists(cand.quarantine_path):
            items.append((cand.id, _triage_request(cand, ledger.read_text(cand.quarantine_path), keywords)))
    return items


def _valid_triage(scout: str, result: Any) -> dict | None:
    if not isinstance(result, Mapping):
        return None
    try:
        plain = json.loads(json.dumps(result, allow_nan=False))
    except (TypeError, ValueError):
        return None
    if agents.validate_json(plain, TRIAGE_SCHEMA):
        return None
    if scout != "spindle":
        plain["exercise"] = None  # exercise records belong to Spindle
    products = plain.pop("products", [])
    if scout == "papilla" and not all(_PRODUCT_RE.match(p) for p in products):
        return None
    if scout == "papilla":
        plain["products"] = products
    return plain


def apply_triage(cands: Sequence[Candidate], results: Mapping[str, Mapping]) -> list[Candidate]:
    """Attach schema-valid triage results by candidate id; invalid or missing results leave triage as is."""
    out = []
    for cand in cands:
        triage = _valid_triage(cand.scout, results.get(cand.id))
        out.append(cand if triage is None else dataclasses.replace(cand, triage=triage))
    return out

"""Legal full-text resolver (docs/NOVELTY_LANE.md, section 4).

For a DOI, PMID or arXiv id it tries the sources in ``SOURCES`` in order and
stops at the first one that yields a PDF or full text:

    unpaywall, europepmc, openalex, semanticscholar, core, preprint,
    ia_scholar, wayback, then the owner's own access: elsevier,
    springer_nature, wiley, proxy.

Every request goes through ``FulltextHttp``, a ``scouts.Http`` with the same
host allowlist, rate limit, JSON cache and injectable opener, plus raw byte
downloads. A discovered PDF URL on a host outside the allowlist is never
fetched directly; it is looked up in the Wayback Machine instead.

The owner's keys and proxy session live in a JSON file outside any git tree
(``default_access_path``, see docs/TRACKER_CONTRACT.md "Tokens"). This module
never logs in and never enters credentials: it only sends what the owner put
there, on the wire, never into URLs, the cache or the ledger. It never uses
shadow libraries.

Output goes to quarantine only: ``quarantine/<id>.fulltext.pdf`` or
``.fulltext.txt`` plus a ``quarantine/<id>.fulltext.json`` record naming the
source, URL, license and every attempt. JATS XML and plain text become text
(``text_extraction="done"``). PDFs are stored as they are with
``text_extraction="pending"``: correct PDF text extraction needs font
encodings and CMaps, which the standard library does not cover.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import scouts
from .scouts import CacheMiss, HostNotAllowed
from .store import Ledger

log = logging.getLogger(__name__)

UNPAYWALL_HOST = "api.unpaywall.org"
CORE_HOST = "api.core.ac.uk"
ELSEVIER_HOST = "api.elsevier.com"
SPRINGER_HOST = "api.springernature.com"
WILEY_HOST = "api.wiley.com"

UNPAYWALL = f"https://{UNPAYWALL_HOST}/v2/"
EPMC_REST = "https://www.ebi.ac.uk/europepmc/webservices/rest/"
OPENALEX_WORKS = "https://api.openalex.org/works/"
S2_PAPER = "https://api.semanticscholar.org/graph/v1/paper/"
CORE_SEARCH = f"https://{CORE_HOST}/v3/search/works"
ARXIV_PDF = "https://arxiv.org/pdf/"
FATCAT_LOOKUP = "https://api.fatcat.wiki/v0/release/lookup"  # catalog behind scholar.archive.org
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK = "https://web.archive.org/web/"
ELSEVIER_ARTICLE = f"https://{ELSEVIER_HOST}/content/article/doi/"
SPRINGER_JATS = f"https://{SPRINGER_HOST}/openaccess/jats"  # UNVERIFIED: TDM scope of this endpoint
WILEY_TDM = f"https://{WILEY_HOST}/onlinelibrary/tdm/v1/articles/"

# Header that carries the owner's key per host; Springer Nature takes the
# ``api_key`` query parameter that scouts.Http already adds on the wire.
KEY_HEADERS: dict[str, tuple[str, str]] = {
    CORE_HOST: ("Authorization", "Bearer "),
    ELSEVIER_HOST: ("X-ELS-APIKey", ""),
    WILEY_HOST: ("Wiley-TDM-Client-Token", ""),
}
SECRET_HEADERS = ("Authorization", "X-ELS-APIKey", "Wiley-TDM-Client-Token", "Cookie")
SECRET_NAMES: dict[str, str] = {
    "core": CORE_HOST, "elsevier": ELSEVIER_HOST, "springer_nature": SPRINGER_HOST, "wiley": WILEY_HOST,
}
ACCESS_FILE = "fulltext.json"
DEFAULT_SECRETS_DIR = "~/.local/state/homunculus"

BYTES_ACCEPT = "application/pdf, application/xml, text/xml, text/plain;q=0.9"
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
FULLTEXT_MAX_CHARS = 400_000
MIN_TEXT_CHARS = 200
WAYBACK_MAX_URLS = 5
URL_MAX_CHARS = 2000

_ARXIV_RE = re.compile(r"^(?:arxiv:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[a-z]{2})?/\d{7}(?:v\d+)?)$", re.I)
_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$")
_PMID_PREFIX_RE = re.compile(r"^pmid:\s*", re.I)
_PMCID_RE = re.compile(r"^PMC\d{1,10}$")
_LICENSE_RE = re.compile(r"^[A-Za-z0-9 .:/_+-]{1,80}$")
_URL_RE = re.compile(r"^https?://[^\s<>\"'\\]{1,%d}$" % URL_MAX_CHARS)
_TS_RE = re.compile(r"^\d{4,14}$")
_WAYBACK_RE = re.compile(r"^https?://web\.archive\.org/web/(\d{4,14})/(\S+)$")
_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]+")

_ERRORS = scouts._TRANSPORT_ERRORS


# --------------------------------------------------------------------------
# Identifiers


@dataclass(frozen=True)
class Ids:
    doi: str | None = None
    pmid: str | None = None
    arxiv: str | None = None
    pmcid: str | None = None


def _clean_arxiv(value: str) -> str | None:
    match = _ARXIV_RE.match(value.strip())
    return match.group(1).lower() if match else None


def _clean_pmcid(value: Any) -> str | None:
    text = str(value).strip().upper() if isinstance(value, str) else ""
    return text if _PMCID_RE.match(text) else None


def parse_id(text: str) -> Ids:
    """DOI (any doi.org form), PMID (optionally 'pmid:'), or arXiv id (optionally 'arXiv:')."""
    raw = str(text).strip()
    doi = scouts._clean_doi(raw)
    if doi:
        match = _ARXIV_DOI_RE.match(doi)
        return Ids(doi=doi, arxiv=_clean_arxiv(match.group(1)) if match else None)
    pmid = scouts._clean_pmid(_PMID_PREFIX_RE.sub("", raw))
    if pmid:
        return Ids(pmid=pmid)
    arxiv = _clean_arxiv(raw)
    if arxiv:
        return Ids(arxiv=arxiv)
    raise ValueError(f"not a DOI, PMID or arXiv id: {text!r}")


def fulltext_id(ids: Ids) -> str:
    """Same id scouts gives a candidate with this DOI (or PMID when there is no DOI)."""
    key = f"doi:{ids.doi}" if ids.doi else f"pmid:{ids.pmid}" if ids.pmid else f"arxiv:{ids.arxiv}"
    return "c-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Owner access (secrets outside any git tree)


@dataclass(frozen=True)
class Proxy:
    """Institutional proxy session the owner set up; ``url_template`` holds ``{doi}``."""

    url_template: str
    cookie: str = field(repr=False)

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url_template.replace("{doi}", "x")).hostname or ""


@dataclass(frozen=True)
class OwnerAccess:
    keys: Mapping[str, str] = field(default_factory=dict, repr=False)  # host -> key
    proxy: Proxy | None = None


def default_access_path(env: Mapping[str, str]) -> Path:
    """$HOMUNCULUS_SECRETS/fulltext.json, else ~/.local/state/homunculus/fulltext.json."""
    return Path(env.get("HOMUNCULUS_SECRETS") or DEFAULT_SECRETS_DIR).expanduser() / ACCESS_FILE


def _in_git_tree(path: Path) -> bool:
    return any((p / ".git").exists() for p in path.resolve().parents)


def _proxy(raw: Any) -> Proxy | None:
    if raw is None:
        return None
    spec = scouts._as_map(raw)
    template, cookie = spec.get("url_template"), spec.get("cookie")
    if not isinstance(template, str) or not isinstance(cookie, str) or "{doi}" not in template or not cookie:
        raise ValueError("proxy needs a url_template containing {doi} and a cookie")
    parts = urllib.parse.urlsplit(template.replace("{doi}", "x"))
    if parts.scheme != "https" or not parts.hostname or parts.username:
        raise ValueError("proxy url_template must be a plain https URL")
    return Proxy(template, cookie)


def load_access(path: Path) -> OwnerAccess:
    """Owner keys and proxy session. A missing file means no owner access.

    File shape: {"core": key, "elsevier": key, "springer_nature": key,
    "wiley": token, "proxy": {"url_template": "https://.../{doi}", "cookie": "..."}}.
    """
    path = Path(path).expanduser()
    if _in_git_tree(path):
        raise ValueError(f"refusing secrets inside a git tree: {path}")
    if not path.is_file():
        return OwnerAccess()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path} must hold a JSON object")
    keys = {host: raw[name] for name, host in SECRET_NAMES.items() if isinstance(raw.get(name), str) and raw[name]}
    return OwnerAccess(keys, _proxy(raw.get("proxy")))


# --------------------------------------------------------------------------
# HTTP


def _under(host: str, suffix: str | None) -> bool:
    return bool(suffix) and (host == suffix or host.endswith("." + suffix))


def _check(url: str, suffix: str | None) -> str:
    """Host of an allowed https URL; the owner's proxy host and its subdomains count as allowed."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.username or parts.port not in (None, 443):
        raise HostNotAllowed(f"refusing URL {url!r}")
    if host not in scouts.ALLOWED_HOSTS and not _under(host, suffix):
        raise HostNotAllowed(f"host not allowed: {host!r}")
    return host


def _strip_secrets(req: urllib.request.Request, new_host: str, old_host: str, suffix: str | None) -> None:
    """Credentials never follow a redirect to another host (the proxy cookie may stay inside the proxy)."""
    if new_host == old_host:
        return
    for name in SECRET_HEADERS:
        if name == "Cookie" and _under(new_host, suffix):
            continue
        req.remove_header(name.capitalize())


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, suffix: str | None) -> None:
        super().__init__()
        self._suffix = suffix

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        new_host = _check(newurl, self._suffix)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            _strip_secrets(new, new_host, urllib.parse.urlsplit(req.full_url).hostname or "", self._suffix)
        return new


def _opener_for(suffix: str | None) -> scouts.Opener:
    def open_url(req: urllib.request.Request) -> bytes:
        _check(req.full_url, suffix)
        opener = urllib.request.build_opener(_Redirect(suffix))
        with opener.open(req, timeout=scouts.TIMEOUT_S) as resp:  # noqa: S310 (allowlisted https)
            raw = resp.read(MAX_DOWNLOAD_BYTES + 1)
        if len(raw) > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"response from {req.host} exceeds {MAX_DOWNLOAD_BYTES} bytes")
        return raw

    return open_url


class FulltextHttp(scouts.Http):
    """scouts.Http plus raw downloads, owner keys as headers and the owner's proxy session.

    JSON lookups keep the scouts cache. Downloads are not cached: the stored
    copy in quarantine is the cache. ``offline=True`` refuses downloads.
    """

    def __init__(self, cache_dir: Path, *, access: OwnerAccess | None = None,
                 keys: Mapping[str, str] | None = None, rate_per_s: Mapping[str, float] | None = None,
                 opener: scouts.Opener | None = None, **kw: Any) -> None:
        access = access or OwnerAccess()
        self.proxy = access.proxy
        self._suffix = access.proxy.host if access.proxy else None
        super().__init__(cache_dir, keys={**(keys or {}), **access.keys}, rate_per_s=rate_per_s or {},
                         opener=opener or _opener_for(self._suffix), **kw)

    @property
    def mailto(self) -> str | None:
        return self._mailto

    def has_key(self, host: str) -> bool:
        return bool(self._keys.get(host))

    def allows(self, url: str) -> bool:
        try:
            _check(url, self._suffix)
        except (HostNotAllowed, ValueError):  # ValueError: malformed port in a discovered URL
            return False
        return True

    def _extras(self, host: str) -> tuple[dict[str, str], dict[str, str]]:
        params, headers = super()._extras(host)
        if host == UNPAYWALL_HOST and self._mailto:
            params["email"] = self._mailto
        if host in KEY_HEADERS and "api_key" in params:
            name, prefix = KEY_HEADERS[host]
            headers[name] = prefix + params.pop("api_key")
        if self.proxy is not None and _under(host, self._suffix):
            headers["Cookie"] = self.proxy.cookie
        return params, headers

    def get_bytes(self, url: str) -> bytes:
        host = _check(url, self._suffix)
        if self.offline:
            raise CacheMiss(url)
        params, headers = self._extras(host)
        headers["Accept"] = BYTES_ACCEPT
        self._wait(host)
        return self._opener(urllib.request.Request(scouts._with_query(url, params), headers=headers, method="GET"))


# --------------------------------------------------------------------------
# Sources


@dataclass(frozen=True)
class Location:
    url: str
    license: str | None = None


Finder = Callable[[FulltextHttp, Ids, Sequence[str]], "list[Location]"]


@dataclass(frozen=True)
class Source:
    name: str
    access: str  # "open" or "owner"
    hosts: tuple[str, ...]
    find: Finder


def _q(value: str, safe: str = "/") -> str:
    return urllib.parse.quote(value, safe=safe)


def _clean_license(value: Any) -> str | None:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if _LICENSE_RE.match(text) else None


def _clean_url(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and _URL_RE.match(value.strip()) else None


def _locations(pairs: Iterable[tuple[Any, Any]]) -> list[Location]:
    """Valid, unique URLs in order, each with its cleaned license."""
    out: dict[str, Location] = {}
    for url, lic in pairs:
        clean = _clean_url(url)
        if clean and clean not in out:
            out[clean] = Location(clean, _clean_license(lic))
    return list(out.values())


def _epmc_record(http: FulltextHttp, ids: Ids) -> Mapping:
    """Europe PMC core record for the DOI or PMID; {} when none matches."""
    query = f'DOI:"{ids.doi}"' if ids.doi else f"EXT_ID:{ids.pmid} AND SRC:MED" if ids.pmid else None
    if query is None:
        return {}
    params = {"query": query, "format": "json", "resultType": "core", "pageSize": "1"}
    rows = scouts._as_map(scouts._get_map(http, scouts.EUROPEPMC, params).get("resultList")).get("result") or []
    rec = scouts._as_map(rows[0]) if rows else {}
    if ids.doi and scouts._clean_doi(rec.get("doi")) != ids.doi:
        return {}
    return rec


def complete_ids(http: FulltextHttp, ids: Ids) -> Ids:
    """Fill DOI, PMID and PMCID from Europe PMC; a failed lookup keeps ids as they are."""
    if ids.doi is None and ids.pmid is None:
        return ids
    try:
        rec = _epmc_record(http, ids)
    except _ERRORS:
        return ids
    return Ids(doi=ids.doi or scouts._clean_doi(rec.get("doi")), pmid=ids.pmid or scouts._clean_pmid(rec.get("pmid")),
               arxiv=ids.arxiv, pmcid=ids.pmcid or _clean_pmcid(rec.get("pmcid")))


def _unpaywall(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    if not ids.doi or not http.mailto:
        return []  # Unpaywall requires a contact email
    data = scouts._get_map(http, UNPAYWALL + _q(ids.doi))
    rows = [scouts._as_map(data.get("best_oa_location"))]
    rows += [scouts._as_map(r) for r in data.get("oa_locations") or ()]
    return _locations((r.get("url_for_pdf"), r.get("license")) for r in rows)


def _europepmc(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    if not ids.pmcid:
        return []
    lic = _epmc_record(http, ids).get("license")
    return [Location(f"{EPMC_REST}{ids.pmcid}/fullTextXML", _clean_license(lic))]


def _openalex(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    key = f"doi:{ids.doi}" if ids.doi else f"pmid:{ids.pmid}" if ids.pmid else None
    if key is None:
        return []
    best = scouts._as_map(scouts._get_map(http, OPENALEX_WORKS + _q(key, "/:")).get("best_oa_location"))
    return _locations([(best.get("pdf_url"), best.get("license"))])


def _s2_key(ids: Ids) -> str | None:
    if ids.doi:
        return f"DOI:{ids.doi}"
    if ids.pmid:
        return f"PMID:{ids.pmid}"
    return f"ARXIV:{ids.arxiv}" if ids.arxiv else None


def _semantic_scholar(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    key = _s2_key(ids)
    if key is None:
        return []
    data = scouts._get_map(http, S2_PAPER + _q(key, "/:"), {"fields": "openAccessPdf"})
    pdf = scouts._as_map(data.get("openAccessPdf"))
    return _locations([(pdf.get("url"), pdf.get("license"))])


def _core(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    if not ids.doi or not http.has_key(CORE_HOST):
        return []  # CORE v3 requires an API key
    data = scouts._get_map(http, CORE_SEARCH, {"q": f'doi:"{ids.doi}"', "limit": "3"})
    rows = [scouts._as_map(r) for r in data.get("results") or ()]
    return _locations((r.get("downloadUrl"), None) for r in rows)


def _rxiv(http: FulltextHttp, server: str, doi: str) -> list[Location]:
    rows = [scouts._as_map(r) for r in scouts._get_map(http, f"{scouts.BIORXIV}{server}/{doi}").get("collection") or ()]
    version = str(rows[-1].get("version") or "") if rows else ""
    if not version.isdigit():
        return []
    return [Location(f"https://www.{server}.org/content/{doi}v{version}.full.pdf", _clean_license(rows[-1].get("license")))]


def _preprints(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    if ids.arxiv:
        return [Location(ARXIV_PDF + ids.arxiv)]
    if not ids.doi or not ids.doi.startswith("10.1101/"):
        return []
    return [loc for server in scouts.BIORXIV_SERVERS for loc in _rxiv(http, server, ids.doi)]


def _raw_wayback(url: Any) -> Any:
    """Wayback replay URL -> its id_ form, which serves the archived bytes without the replay frame."""
    match = _WAYBACK_RE.match(url) if isinstance(url, str) else None
    return f"{WAYBACK}{match.group(1)}id_/{match.group(2)}" if match else url


def _file_urls(entry: Any) -> list[Any]:
    entry = scouts._as_map(entry)
    if entry.get("mimetype") != "application/pdf":
        return []
    return [scouts._as_map(u).get("url") for u in entry.get("urls") or ()]


def _ia_scholar(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    """Internet Archive Scholar's catalog (fatcat): archived PDF files of the release."""
    params = {"doi": ids.doi} if ids.doi else {"pmid": ids.pmid} if ids.pmid else None
    if params is None:
        return []
    data = scouts._get_map(http, FATCAT_LOOKUP, {**params, "expand": "files"})
    return _locations((_raw_wayback(u), None) for f in data.get("files") or () for u in _file_urls(f))


def _snapshot(http: FulltextHttp, url: str) -> list[Location]:
    """Latest archived PDF capture of url from the CDX API; [] when there is none or the lookup fails."""
    params = {"url": url, "output": "json", "fl": "timestamp,original", "filter": "mimetype:application/pdf",
              "limit": "-1"}
    try:
        rows = http.get_json(WAYBACK_CDX, params)
    except _ERRORS:
        return []
    row = rows[-1] if isinstance(rows, list) and len(rows) > 1 else None
    if not isinstance(row, list) or len(row) != 2 or not _TS_RE.match(str(row[0])) or not _clean_url(row[1]):
        return []
    return [Location(f"{WAYBACK}{row[0]}id_/{row[1]}")]


def _wayback(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    urls = list(dict.fromkeys(u for u in offsite if _clean_url(u)))[:WAYBACK_MAX_URLS]
    return [loc for url in urls for loc in _snapshot(http, url)]


def _elsevier(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    if not ids.doi or not http.has_key(ELSEVIER_HOST):
        return []
    return [Location(f"{ELSEVIER_ARTICLE}{_q(ids.doi)}?httpAccept=text%2Fplain")]


def _springer(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    if not ids.doi or not http.has_key(SPRINGER_HOST):
        return []
    return [Location(f"{SPRINGER_JATS}?{urllib.parse.urlencode({'q': f'doi:{ids.doi}'})}")]


def _wiley(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    if not ids.doi or not http.has_key(WILEY_HOST):
        return []
    return [Location(WILEY_TDM + _q(ids.doi, ""))]


def _proxied(http: FulltextHttp, ids: Ids, offsite: Sequence[str]) -> list[Location]:
    if not ids.doi or http.proxy is None:
        return []
    return [Location(http.proxy.url_template.replace("{doi}", _q(ids.doi)))]


SOURCES: tuple[Source, ...] = (
    Source("unpaywall", "open", (UNPAYWALL_HOST,), _unpaywall),
    Source("europepmc", "open", ("www.ebi.ac.uk",), _europepmc),
    Source("openalex", "open", ("api.openalex.org",), _openalex),
    Source("semanticscholar", "open", ("api.semanticscholar.org",), _semantic_scholar),
    Source("core", "open", (CORE_HOST, "core.ac.uk"), _core),
    Source("preprint", "open", ("arxiv.org", "api.biorxiv.org", "www.biorxiv.org", "www.medrxiv.org"), _preprints),
    Source("ia_scholar", "open", ("api.fatcat.wiki", "web.archive.org"), _ia_scholar),
    Source("wayback", "open", ("web.archive.org",), _wayback),
    Source("elsevier", "owner", (ELSEVIER_HOST,), _elsevier),
    Source("springer_nature", "owner", (SPRINGER_HOST,), _springer),
    Source("wiley", "owner", (WILEY_HOST,), _wiley),
    Source("proxy", "owner", (), _proxied),  # the owner's own proxy host, from the secrets file
)
SOURCE_HOSTS: frozenset[str] = frozenset(h for s in SOURCES for h in s.hosts)


# --------------------------------------------------------------------------
# Content


@dataclass(frozen=True)
class Fetched:
    media: str  # "pdf" or "text"
    data: bytes
    text: str | None


def _classify(data: bytes) -> str | None:
    head = data[:1024].lstrip().lower()
    if head.startswith(b"%pdf-"):
        return "pdf"
    if not head or head[:1] in (b"{", b"[") or head.startswith(b"<!doctype html") or b"<html" in head:
        return None  # empty, an API error, or a landing page
    return "xml" if head.startswith(b"<") else "text"


def _xml_text(data: bytes) -> str | None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    return " ".join(" ".join(root.itertext()).split())


def _plain(text: str | None) -> str | None:
    clean = _TEXT_CONTROL_RE.sub(" ", (text or "").replace("\r\n", "\n")).strip()[:FULLTEXT_MAX_CHARS]
    return clean if len(clean) >= MIN_TEXT_CHARS else None


def _content(data: bytes) -> Fetched | None:
    media = _classify(data)
    if media == "pdf":
        return Fetched("pdf", data, None)
    if media is None:
        return None
    text = _plain(_xml_text(data) if media == "xml" else data.decode("utf-8", "replace"))
    return Fetched("text", text.encode("utf-8"), text) if text else None


def _download(http: FulltextHttp, url: str) -> Fetched | None:
    try:
        data = http.get_bytes(url)
    except (*_ERRORS, HostNotAllowed) as exc:  # a redirect off the allowlist is a miss, not a bug
        log.warning("full-text download from %s failed: %s", urllib.parse.urlsplit(url).hostname, type(exc).__name__)
        return None
    return _content(data)


# --------------------------------------------------------------------------
# Resolver


@dataclass(frozen=True)
class FulltextResult:
    id: str
    doi: str | None
    pmid: str | None
    arxiv: str | None
    pmcid: str | None
    source: str | None  # name of the source that succeeded, None when all failed
    access: str | None  # "open" or "owner"
    url: str | None
    license: str | None
    media: str | None  # "pdf" or "text"
    path: str | None  # quarantine/<id>.fulltext.pdf or .txt
    text_extraction: str | None  # "done" or "pending"
    sha256: str | None
    size: int | None
    fetched_at: str
    attempts: tuple[Mapping[str, Any], ...]
    record_path: str


def _attempt(source: str, url: str | None, outcome: str) -> dict:
    return {"source": source, "url": url, "outcome": outcome}


def _try_source(http: FulltextHttp, source: Source, ids: Ids, offsite: list[str],
                attempts: list[dict]) -> tuple[Location, Fetched] | None:
    """First downloadable location of source. URLs off the allowlist are queued for the Wayback lookup."""
    try:
        found = source.find(http, ids, tuple(offsite))
    except _ERRORS as exc:
        log.warning("full-text source %s failed: %s", source.name, type(exc).__name__)
        attempts.append(_attempt(source.name, None, "error"))
        return None
    if not found:
        attempts.append(_attempt(source.name, None, "none"))
    for loc in found:
        if not http.allows(loc.url):
            offsite.append(loc.url)
            attempts.append(_attempt(source.name, loc.url, "offsite"))
            continue
        got = _download(http, loc.url)
        attempts.append(_attempt(source.name, loc.url, "ok" if got else "failed"))
        if got:
            return loc, got
    return None


def _write_bytes(ledger: Ledger, rel: str, data: bytes, author: str) -> None:
    """Ledger has text writes only; this keeps its write rules and pending set for a binary file."""
    clean, path = ledger._writable(rel, author)
    scouts._atomic_write(path, data)
    ledger._mark(clean)


def _store(ledger: Ledger, cid: str, got: Fetched, author: str) -> tuple[str, str]:
    """(quarantine path, text_extraction)."""
    if got.media == "pdf":
        rel = f"quarantine/{cid}.fulltext.pdf"
        _write_bytes(ledger, rel, got.data, author)
        return rel, "pending"
    rel = f"quarantine/{cid}.fulltext.txt"
    ledger.write_text(rel, (got.text or "") + "\n", author=author)
    return rel, "done"


def _first_hit(http: FulltextHttp, ids: Ids, offsite: list[str], attempts: list[dict],
               sources: Sequence[Source]) -> tuple[Source, Location, Fetched] | None:
    for source in sources:
        hit = _try_source(http, source, ids, offsite, attempts)
        if hit is not None:
            return source, hit[0], hit[1]
    return None


def _result(cid: str, ids: Ids, hit: tuple[Source, Location, Fetched] | None, stored: tuple[str, str] | None,
            now: datetime, attempts: list[dict]) -> FulltextResult:
    source, loc, got = hit if hit else (None, None, None)
    path, extraction = stored if stored else (None, None)
    return FulltextResult(
        id=cid, doi=ids.doi, pmid=ids.pmid, arxiv=ids.arxiv, pmcid=ids.pmcid,
        source=source.name if source else None, access=source.access if source else None,
        url=loc.url if loc else None, license=loc.license if loc else None, media=got.media if got else None,
        path=path, text_extraction=extraction, sha256=hashlib.sha256(got.data).hexdigest() if got else None,
        size=len(got.data) if got else None, fetched_at=now.isoformat(timespec="seconds"),
        attempts=tuple(attempts), record_path=f"quarantine/{cid}.fulltext.json",
    )


def resolve_fulltext(http: FulltextHttp, ledger: Ledger, ident: str | Ids, *, author: str, now: datetime,
                     known_urls: Sequence[str] = (), cand_id: str | None = None,
                     sources: Sequence[Source] = SOURCES) -> FulltextResult:
    """Try sources in order; store the first full text in quarantine and record the outcome there.

    ``known_urls`` are PDF URLs the caller already knows (conference PDFs);
    they are looked up in the Wayback Machine. ``author`` must be allowed to
    write ``quarantine/*`` (Pineal, the code author, is).
    """
    ids = complete_ids(http, parse_id(ident) if isinstance(ident, str) else ident)
    cid = cand_id or fulltext_id(ids)
    if not scouts._CAND_ID_RE.match(cid):
        raise ValueError(f"invalid candidate id {cid!r}")
    attempts: list[dict] = []
    hit = _first_hit(http, ids, [u for u in known_urls if _clean_url(u)], attempts, sources)
    stored = _store(ledger, cid, hit[2], author) if hit else None
    result = _result(cid, ids, hit, stored, now, attempts)
    record = asdict(result)
    record["attempts"] = list(result.attempts)
    ledger.write_json(result.record_path, record, author=author)
    return result


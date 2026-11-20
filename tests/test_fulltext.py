"""Tests for homunculus.fulltext with a fake transport and tiny synthetic replies.

No network, no sleeping. Every DOI, PMID, key, cookie and text below is synthetic.
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from homunculus import fulltext as F
from homunculus import scouts as S
from homunculus.store import Ledger

NOW = datetime(2030, 1, 15, 3, 0, tzinfo=timezone.utc)
DOI = "10.9999/syn.0001"
PDF = b"%PDF-1.4\n% synthetic pdf\n"
TEXT = "Synthetic full text. " * 20
JATS = f"<article><front><title>Synthetic</title></front><body><p>{TEXT}</p></body></article>"
OFFSITE_PDF = "https://repo.example.org/syn.pdf"
OWNER_KEY = "SYNTHETIC-OWNER-KEY"
COOKIE = "ezproxy=SYNTHETIC-SESSION"
SHADOW_MARKERS = ("sci-hub", "scihub", "libgen", "library.lol", "z-lib", "zlibrary", "1lib", "annas-archive",
                  "booksc", "b-ok", "pdfdrive", "scidb", "bookfi", "nexus")

Handler = Callable[[dict[str, str], urllib.request.Request], Any]


class FakeWeb:
    """Opener keyed by (host, path). A missing route is a 404."""

    def __init__(self, routes: dict[tuple[str, str], Handler]) -> None:
        self.routes = routes
        self.calls: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request) -> bytes:
        self.calls.append(req)
        parts = urllib.parse.urlsplit(req.full_url)
        handler = self.routes.get((parts.hostname, parts.path))
        if handler is None:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
        reply = handler(dict(urllib.parse.parse_qsl(parts.query)), req)
        if isinstance(reply, bytes):
            return reply
        return reply.encode("utf-8") if isinstance(reply, str) else json.dumps(reply).encode("utf-8")

    def hosts(self) -> list[str]:
        return [urllib.parse.urlsplit(r.full_url).hostname or "" for r in self.calls]


def _reply(value: Any) -> Handler:
    return lambda q, req: value


def _epmc(rec: dict) -> Handler:
    return _reply({"resultList": {"result": [rec]}})


EPMC_SEARCH = ("www.ebi.ac.uk", "/europepmc/webservices/rest/search")
NO_EPMC = {EPMC_SEARCH: _reply({"resultList": {"result": []}})}


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.ledger = Ledger(self.tmp / "ledger", {})
        logging.disable(logging.WARNING)  # failing sources log on purpose

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)
        self._td.cleanup()

    def http(self, web: FakeWeb, **kw: Any) -> F.FulltextHttp:
        return F.FulltextHttp(self.tmp / "cache", mailto=kw.pop("mailto", "owner@example.invalid"), opener=web,
                              clock=lambda: 1000.0, sleep=lambda s: None, **kw)

    def resolve(self, http: F.FulltextHttp, ident: str = DOI, **kw: Any) -> F.FulltextResult:
        return F.resolve_fulltext(http, self.ledger, ident, author="pineal", now=NOW, **kw)

    def ledger_files(self) -> list[str]:
        root = self.ledger.root
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# --------------------------------------------------------------------------


class ParseIdTest(unittest.TestCase):
    def test_doi_pmid_arxiv(self) -> None:
        self.assertEqual(F.parse_id("https://doi.org/10.9999/SYN.0001"), F.Ids(doi=DOI))
        self.assertEqual(F.parse_id("PMID: 900001"), F.Ids(pmid="900001"))
        self.assertEqual(F.parse_id("arXiv:2101.00001v2"), F.Ids(arxiv="2101.00001v2"))
        self.assertEqual(F.parse_id("10.48550/arXiv.2101.00001").arxiv, "2101.00001")
        with self.assertRaises(ValueError):
            F.parse_id("not an id")

    def test_id_matches_scout_candidate_id(self) -> None:
        cand = S._make(doi=DOI, pmid="900001", title="t", year=2030, pub_types=[], source_api="pubmed",
                       preprint=False, query="q")
        self.assertEqual(F.fulltext_id(F.Ids(doi=DOI, pmid="900001")), cand.id)


class SourceListTest(unittest.TestCase):
    def test_order_is_the_documented_one(self) -> None:
        self.assertEqual([s.name for s in F.SOURCES], [
            "unpaywall", "europepmc", "openalex", "semanticscholar", "core", "preprint", "ia_scholar", "wayback",
            "elsevier", "springer_nature", "wiley", "proxy",
        ])
        self.assertEqual({s.access for s in F.SOURCES[-4:]}, {"owner"})
        self.assertEqual({s.access for s in F.SOURCES[:-4]}, {"open"})

    def test_no_shadow_library_hosts(self) -> None:
        source_text = Path(F.__file__).read_text(encoding="utf-8").lower()
        for marker in SHADOW_MARKERS:
            with self.subTest(marker=marker):
                self.assertFalse([h for h in F.SOURCE_HOSTS | S.ALLOWED_HOSTS if marker in h])
                self.assertNotIn(marker, source_text)

    def test_every_source_host_is_allowlisted(self) -> None:
        self.assertLessEqual(F.SOURCE_HOSTS, S.ALLOWED_HOSTS)


class FallbackOrderTest(TmpCase):
    def routes(self) -> dict:
        return {
            EPMC_SEARCH: _epmc({"doi": DOI, "pmid": "900001"}),  # no PMCID: Europe PMC has nothing
            ("api.unpaywall.org", f"/v2/{DOI}"): _reply({"best_oa_location": {"url_for_pdf": OFFSITE_PDF}}),
            ("api.openalex.org", f"/works/doi:{DOI}"): _reply({"best_oa_location": None}),
            ("api.core.ac.uk", "/v3/search/works"): _reply({"results": []}),
            ("web.archive.org", "/cdx/search/cdx"): lambda q, r: (
                [["timestamp", "original"], ["20200101000000", q["url"]]] if q["url"] == OFFSITE_PDF else []),
            ("web.archive.org", f"/web/20200101000000id_/{OFFSITE_PDF}"): _reply(PDF),
        }

    def test_falls_through_to_wayback_in_order(self) -> None:
        web = FakeWeb(self.routes())
        http = self.http(web, access=F.OwnerAccess({"api.core.ac.uk": OWNER_KEY, "api.elsevier.com": OWNER_KEY}))
        result = self.resolve(http)
        self.assertEqual(result.source, "wayback")
        self.assertEqual(result.access, "open")
        self.assertEqual(result.url, f"https://web.archive.org/web/20200101000000id_/{OFFSITE_PDF}")
        self.assertEqual([(a["source"], a["outcome"]) for a in result.attempts], [
            ("unpaywall", "offsite"), ("europepmc", "none"), ("openalex", "none"), ("semanticscholar", "error"),
            ("core", "none"), ("preprint", "none"), ("ia_scholar", "error"), ("wayback", "ok"),
        ])
        hosts = web.hosts()
        self.assertNotIn("repo.example.org", hosts)  # off the allowlist: only via Wayback
        self.assertNotIn("api.elsevier.com", hosts)  # stops at the first success
        self.assertEqual(hosts[:3], ["www.ebi.ac.uk", "api.unpaywall.org", "api.openalex.org"])

    def test_first_source_wins_and_records_license(self) -> None:
        url = "https://arxiv.org/pdf/2101.00001"
        web = FakeWeb({**NO_EPMC,
                       ("api.unpaywall.org", f"/v2/{DOI}"): _reply(
                           {"best_oa_location": {"url_for_pdf": url, "license": "cc-by"}}),
                       ("arxiv.org", "/pdf/2101.00001"): _reply(PDF)})
        result = self.resolve(self.http(web))
        self.assertEqual((result.source, result.license, result.media), ("unpaywall", "cc-by", "pdf"))
        self.assertEqual(web.hosts(), ["www.ebi.ac.uk", "api.unpaywall.org", "arxiv.org"])

    def test_unpaywall_needs_a_contact_email_and_sends_it_on_the_wire_only(self) -> None:
        web = FakeWeb(dict(NO_EPMC))
        self.resolve(self.http(web, mailto=None))
        self.assertNotIn("api.unpaywall.org", web.hosts())
        web = FakeWeb(dict(NO_EPMC))
        self.resolve(self.http(web))
        unpaywall = [r for r in web.calls if r.host == "api.unpaywall.org"]
        self.assertIn("email=owner%40example.invalid", unpaywall[0].full_url)
        cached = "".join(p.read_text() for p in (self.tmp / "cache").rglob("*.json"))
        self.assertNotIn("owner@example.invalid", cached)

    def test_europepmc_xml_becomes_text(self) -> None:
        web = FakeWeb({EPMC_SEARCH: _epmc({"doi": DOI, "pmcid": "PMC123", "license": "cc by"}),
                       ("www.ebi.ac.uk", "/europepmc/webservices/rest/PMC123/fullTextXML"): _reply(JATS)})
        result = self.resolve(self.http(web, mailto=None), "pmid:900001")
        self.assertEqual((result.source, result.license, result.text_extraction), ("europepmc", "cc by", "done"))
        self.assertEqual((result.doi, result.pmcid), (DOI, "PMC123"))
        text = self.ledger.read_text(result.path)
        self.assertTrue(text.startswith("Synthetic Synthetic full text."))
        self.assertNotIn("<p>", text)

    def test_landing_pages_and_short_bodies_are_misses(self) -> None:
        for body in (b"<!DOCTYPE html><html>paywall</html>", b"too short", b'{"error": "x"}'):
            with self.subTest(body=body):
                self.assertIsNone(F._content(body))
        self.assertEqual(F._content(PDF).media, "pdf")

    def test_all_sources_failing_still_records_the_attempts(self) -> None:
        result = self.resolve(self.http(FakeWeb(dict(NO_EPMC))))
        self.assertIsNone(result.source)
        self.assertIsNone(result.path)
        record = self.ledger.read_json(result.record_path)
        self.assertEqual([a["source"] for a in record["attempts"]], [s.name for s in F.SOURCES])
        self.assertEqual(self.ledger_files(), [result.record_path])


class OwnerAccessTest(TmpCase):
    def test_elsevier_key_goes_in_a_header_never_into_url_cache_or_ledger(self) -> None:
        path = ("api.elsevier.com", f"/content/article/doi/{DOI}")
        web = FakeWeb({**NO_EPMC, path: lambda q, r: TEXT if q.get("httpAccept") == "text/plain" else ""})
        http = self.http(web, access=F.OwnerAccess({"api.elsevier.com": OWNER_KEY}))
        result = self.resolve(http)
        self.assertEqual((result.source, result.access, result.text_extraction), ("elsevier", "owner", "done"))
        req = next(r for r in web.calls if r.host == "api.elsevier.com")
        self.assertEqual(req.get_header("X-els-apikey"), OWNER_KEY)
        self.assertNotIn(OWNER_KEY, req.full_url)
        stored = [p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()]
        self.assertFalse([b for b in stored if OWNER_KEY.encode() in b])

    def test_wiley_token_and_proxy_cookie(self) -> None:
        wiley = ("api.wiley.com", "/onlinelibrary/tdm/v1/articles/" + urllib.parse.quote(DOI, safe=""))
        web = FakeWeb({**NO_EPMC, wiley: _reply(PDF)})
        result = self.resolve(self.http(web, access=F.OwnerAccess({"api.wiley.com": OWNER_KEY})))
        self.assertEqual((result.source, result.text_extraction), ("wiley", "pending"))
        self.assertEqual(web.calls[-1].get_header("Wiley-tdm-client-token"), OWNER_KEY)

        proxy = F.Proxy("https://proxy.example.edu/login?url=https://doi.org/{doi}", COOKIE)
        web = FakeWeb({**NO_EPMC, ("proxy.example.edu", "/login"): lambda q, r: PDF if q["url"].endswith(DOI) else b""})
        result = F.resolve_fulltext(self.http(web, access=F.OwnerAccess(proxy=proxy)), self.ledger, DOI,
                                    author="pineal", now=NOW, cand_id="c-proxy")
        self.assertEqual(result.source, "proxy")
        self.assertEqual(web.calls[-1].get_header("Cookie"), COOKIE)
        self.assertNotIn(COOKIE, self.ledger.read_text(result.record_path))

    def test_without_owner_access_owner_sources_make_no_requests(self) -> None:
        web = FakeWeb(dict(NO_EPMC))
        self.resolve(self.http(web))
        owner_hosts = {"api.elsevier.com", "api.springernature.com", "api.wiley.com", "api.core.ac.uk"}
        self.assertFalse(owner_hosts & set(web.hosts()))

    def test_redirect_to_another_host_drops_credentials(self) -> None:
        handler = F._Redirect("proxy.example.edu")
        req = urllib.request.Request("https://api.elsevier.com/x", headers={"X-ELS-APIKey": OWNER_KEY,
                                                                           "Cookie": COOKIE})
        new = handler.redirect_request(req, None, 302, "Found", {}, "https://arxiv.org/pdf/1")
        self.assertIsNone(new.get_header("X-els-apikey"))
        self.assertIsNone(new.get_header("Cookie"))
        inside = F._Redirect("proxy.example.edu").redirect_request(
            urllib.request.Request("https://proxy.example.edu/a", headers={"Cookie": COOKIE}),
            None, 302, "Found", {}, "https://www-journal-org.proxy.example.edu/b")
        self.assertEqual(inside.get_header("Cookie"), COOKIE)
        with self.assertRaises(S.HostNotAllowed):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/x")

    def test_load_access_reads_owner_file_and_refuses_git_trees(self) -> None:
        secrets = self.tmp / "secrets" / "fulltext.json"
        self.assertEqual(F.load_access(secrets), F.OwnerAccess())
        secrets.parent.mkdir()
        secrets.write_text(json.dumps({"elsevier": OWNER_KEY, "wiley": "", "proxy": {
            "url_template": "https://proxy.example.edu/login?url=https://doi.org/{doi}", "cookie": COOKIE}}))
        access = F.load_access(secrets)
        self.assertEqual(dict(access.keys), {"api.elsevier.com": OWNER_KEY})
        self.assertEqual(access.proxy.host, "proxy.example.edu")
        self.assertNotIn(OWNER_KEY, repr(access))
        self.assertNotIn(COOKIE, repr(access))
        self.assertEqual(F.default_access_path({"HOMUNCULUS_SECRETS": str(secrets.parent)}), secrets)

        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaises(ValueError):
            F.load_access(repo / "fulltext.json")
        secrets.write_text(json.dumps({"proxy": {"url_template": "http://proxy.example.edu/{doi}", "cookie": "c"}}))
        with self.assertRaises(ValueError):
            F.load_access(secrets)


class QuarantineOnlyTest(TmpCase):
    def test_output_lands_in_quarantine_only(self) -> None:
        web = FakeWeb({**NO_EPMC, ("api.unpaywall.org", f"/v2/{DOI}"): _reply(
            {"best_oa_location": {"url_for_pdf": "https://arxiv.org/pdf/2101.00001"}}),
            ("arxiv.org", "/pdf/2101.00001"): _reply(PDF)})
        result = self.resolve(self.http(web))
        cid = F.fulltext_id(F.Ids(doi=DOI))
        self.assertEqual(result.path, f"quarantine/{cid}.fulltext.pdf")
        self.assertEqual(self.ledger_files(), [f"quarantine/{cid}.fulltext.json", f"quarantine/{cid}.fulltext.pdf"])
        self.assertEqual(self.ledger.pending(), self.ledger_files())
        self.assertEqual((self.ledger.root / result.path).read_bytes(), PDF)
        record = self.ledger.read_json(result.record_path)
        self.assertEqual((record["source"], record["text_extraction"], record["size"]), ("unpaywall", "pending", len(PDF)))

    def test_a_scout_author_cannot_write_full_text(self) -> None:
        ledger = Ledger(self.tmp / "l2", {"retina": ["evidence/candidates/*.jsonl", "evidence/cache/*"]})
        web = FakeWeb({**NO_EPMC, ("arxiv.org", "/pdf/2101.00001"): _reply(PDF)})
        with self.assertRaises(PermissionError):
            F.resolve_fulltext(self.http(web), ledger, "arXiv:2101.00001", author="retina", now=NOW)

    def test_offline_never_downloads(self) -> None:
        web = FakeWeb({("arxiv.org", "/pdf/2101.00001"): _reply(PDF)})
        result = self.resolve(self.http(web, offline=True), "arXiv:2101.00001")
        self.assertIsNone(result.source)
        self.assertEqual(web.calls, [])

    def test_refuses_hosts_off_the_allowlist(self) -> None:
        http = self.http(FakeWeb({}))
        for url in (OFFSITE_PDF, "http://arxiv.org/pdf/1", "https://u@arxiv.org/pdf/1"):
            with self.subTest(url=url), self.assertRaises(S.HostNotAllowed):
                http.get_bytes(url)


if __name__ == "__main__":
    unittest.main()

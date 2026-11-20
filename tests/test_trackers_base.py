"""Tests for homunculus.trackers.base and registry. Synthetic values only; no network beyond loopback."""

from __future__ import annotations

import json
import math
import os
import random
import shutil
import socket
import stat
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from homunculus.trackers import base, registry
from homunculus.trackers.base import (
    AuthRequired,
    FileTokenStore,
    HostNotAllowed,
    HttpClient,
    KeychainTokenStore,
    OAuthConfig,
    PollResult,
    RateLimited,
    SchemaError,
    Token,
    TokenManager,
    TrackerBatch,
    TrackerConfig,
    UpstreamError,
)
from tests.tracker_fakes import FakeClock, FakeTransport, MemoryTokenStore, json_response

HOST = "api.example.test"
URL = f"https://{HOST}/v1/data"
TOKEN_URL = f"https://{HOST}/oauth/token"
FAKE_ACCESS = "fake-access-0001"
FAKE_REFRESH = "fake-refresh-0001"
UTC = timezone.utc
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def _client(transport: FakeTransport, clock: FakeClock | None = None, **kw) -> HttpClient:
    clock = clock or FakeClock()
    return HttpClient(transport, allow_hosts=frozenset({HOST}), user_agent="homunculus-test", clock=clock,
                      sleep=clock.sleep, rng=random.Random(7), **kw)


def _ok(obj: object = None) -> base.Response:
    return json_response(json.dumps({"ok": True} if obj is None else obj))


def _oauth(**kw) -> OAuthConfig:
    params = dict(authorize_url=f"https://{HOST}/oauth/authorize", token_url=TOKEN_URL, client_id="client-1",
                  client_secret="fake-secret", scopes=("read:a", "read:b"))
    params.update(kw)
    return OAuthConfig(**params)


def _token(expires_at: float, refresh: str | None = FAKE_REFRESH) -> Token:
    return Token(FAKE_ACCESS, refresh, expires_at, "read:a", obtained_at=0.0)


def _form(req: base.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.body.decode("ascii")).items()}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HttpClientTests(unittest.TestCase):
    def test_refuses_host_outside_allow_list_without_io(self) -> None:
        transport = FakeTransport({})
        client = _client(transport)
        for url in ("https://evil.example.test/x", f"http://{HOST}/plain"):
            with self.subTest(url=url), self.assertRaises(HostNotAllowed):
                client.request("GET", url)
        self.assertEqual(transport.calls, [])
        self.assertEqual(client.requests_made, 0)

    def test_retry_after_seconds_then_success(self) -> None:
        clock = FakeClock()
        transport = FakeTransport({("GET", f"{HOST}/v1/data"): [
            json_response("{}", 429, {"Retry-After": "7"}), _ok()]})
        self.assertEqual(_client(transport, clock).get_json(URL), {"ok": True})
        self.assertEqual(clock.sleeps, [7.0])

    def test_long_retry_after_raises_without_sleeping(self) -> None:
        clock = FakeClock()
        transport = FakeTransport({("GET", f"{HOST}/v1/data"): [json_response("{}", 429, {"Retry-After": "900"})]})
        with self.assertRaises(RateLimited) as ctx:
            _client(transport, clock).request("GET", URL)
        self.assertEqual(ctx.exception.retry_after_s, 900.0)
        self.assertEqual(clock.sleeps, [])

    def test_retry_after_http_date_and_reset_header(self) -> None:
        clock = FakeClock(datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC).timestamp())
        date = "Fri, 20 Nov 2026 10:00:12 GMT"
        transport = FakeTransport({("GET", f"{HOST}/v1/data"): [
            json_response("{}", 503, {"Retry-After": date}),
            json_response("{}", 429, {"X-RateLimit-Reset": "3"}), _ok()]})
        _client(transport, clock).request("GET", URL)
        self.assertEqual(clock.sleeps, [12.0, 3.0])

    def test_5xx_exhausts_retries_with_seeded_jitter(self) -> None:
        clock = FakeClock()
        transport = FakeTransport({("GET", f"{HOST}/v1/data"): [json_response("{}", 503)] * 4})
        with self.assertRaises(UpstreamError):
            _client(transport, clock, max_retries=3).request("GET", URL)
        rng = random.Random(7)
        want = [min(60.0, 2.0 ** a) * (0.5 + rng.random()) for a in range(3)]
        self.assertEqual(clock.sleeps, want)
        self.assertEqual(len(transport.calls), 4)

    def test_request_budget_is_hard(self) -> None:
        transport = FakeTransport({("GET", f"{HOST}/v1/data"): [_ok()] * 4})
        client = _client(transport, max_requests=3)
        for _ in range(3):
            client.request("GET", URL)
        with self.assertRaises(RateLimited) as ctx:
            client.request("GET", URL)
        self.assertIsNone(ctx.exception.retry_after_s)
        self.assertEqual(len(transport.calls), 3)

    def test_401_is_returned_not_retried(self) -> None:
        transport = FakeTransport({("GET", f"{HOST}/v1/data"): [json_response("{}", 401), json_response("{}", 403)]})
        client = _client(transport)
        self.assertEqual(client.request("GET", URL).status, 401)
        with self.assertRaises(AuthRequired):
            client.get_json(URL)
        self.assertEqual(len(transport.calls), 2)

    def test_get_json_rejects_non_json_and_other_errors(self) -> None:
        transport = FakeTransport({("GET", f"{HOST}/v1/data"): [json_response("<html>"), json_response("{}", 404)]})
        client = _client(transport)
        with self.assertRaises(SchemaError):
            client.get_json(URL)
        with self.assertRaises(UpstreamError):
            client.get_json(URL)

    def test_params_form_and_user_agent(self) -> None:
        transport = FakeTransport({("POST", f"{HOST}/v1/data"): [_ok()]})
        _client(transport).request("POST", URL, params={"start": "2026-11-20T00:00:00+01:00"}, form={"a": "b c"})
        req = transport.calls[0]
        self.assertEqual(parse_qs(urlsplit(req.url).query)["start"], ["2026-11-20T00:00:00+01:00"])
        self.assertEqual(_form(req), {"a": "b c"})
        self.assertEqual(req.headers["User-Agent"], "homunculus-test")
        self.assertEqual(req.headers["Content-Type"], "application/x-www-form-urlencoded")

    def test_path_template_hides_ids(self) -> None:
        self.assertEqual(base.path_template("/v2/activity/12345/details"), "/v2/activity/{id}/details")
        self.assertEqual(base.path_template("/v3/exercises"), "/v3/exercises")


class PkceAndUrlTests(unittest.TestCase):
    def test_rfc7636_vector(self) -> None:
        self.assertEqual(base.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_pkce_pair_and_state(self) -> None:
        verifier, challenge = base.pkce_pair(random.Random(3))
        self.assertEqual(len(verifier), 64)
        self.assertTrue(set(verifier) <= set(base._PKCE_ALPHABET))
        self.assertEqual(challenge, base.pkce_challenge(verifier))
        state = base.new_state(8)
        self.assertEqual(len(state), 8)
        self.assertTrue(state.isalnum())

    def test_authorize_url(self) -> None:
        cfg = _oauth(scope_sep=",", extra_authorize={"access_type": "offline"})
        url = base.build_authorize_url(cfg, "abcdefgh", "chal")
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.assertEqual(q, {"response_type": "code", "client_id": "client-1", "redirect_uri": cfg.redirect_uri,
                             "scope": "read:a,read:b", "state": "abcdefgh", "code_challenge": "chal",
                             "code_challenge_method": "S256", "access_type": "offline"})
        self.assertNotIn("code_challenge", base.build_authorize_url(cfg, "s", None))
        self.assertNotIn("fake-secret", url + repr(cfg))

    def test_parse_pasted_redirect(self) -> None:
        url = "http://127.0.0.1:8765/callback?code=c-1&state=s-1"
        self.assertEqual(base.parse_pasted_redirect(url, "s-1"), "c-1")
        for bad, state in ((url, "s-2"), ("http://127.0.0.1:8765/callback?error=access_denied", "s-1"),
                           ("http://127.0.0.1:8765/callback?state=s-1", "s-1")):
            with self.subTest(bad=bad), self.assertRaises(AuthRequired):
                base.parse_pasted_redirect(bad, state)


class LoopbackTests(unittest.TestCase):
    def _run(self, query: str, state: str = "s-1") -> tuple[str | Exception, str]:
        port = _free_port()
        cfg = _oauth(redirect_uri=f"http://127.0.0.1:{port}/callback")
        page: dict = {}

        def get() -> None:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?{query}", timeout=5) as resp:
                page["text"] = resp.read().decode("utf-8")

        client = threading.Thread(target=get, daemon=True)
        try:
            result: str | Exception = base.wait_for_code(cfg, state, timeout_s=10, on_ready=client.start)
        except AuthRequired as err:
            result = err
        client.join(timeout=5)
        return result, page.get("text", "")

    def test_right_state_returns_code(self) -> None:
        code, text = self._run("code=c-123&state=s-1")
        self.assertEqual(code, "c-123")
        self.assertEqual(text, base.DONE_PAGE)

    def test_wrong_state_and_error_raise(self) -> None:
        for query in ("code=c-123&state=other", "error=access_denied&state=s-1"):
            with self.subTest(query=query):
                result, _ = self._run(query)
                self.assertIsInstance(result, AuthRequired)

    def test_binds_loopback_only(self) -> None:
        cfg = _oauth(redirect_uri=f"http://127.0.0.1:{_free_port()}/callback")
        with base.loopback_server(cfg, "s", {}) as server:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            base.loopback_server(_oauth(redirect_uri="https://example.test/cb"), "s", {})


class TokenFlowTests(unittest.TestCase):
    def test_exchange_code_body_auth_with_pkce(self) -> None:
        clock = FakeClock()
        body = {"access_token": FAKE_ACCESS, "refresh_token": FAKE_REFRESH, "expires_in": 3600, "scope": "read:a",
                "x_user_id": 42}
        transport = FakeTransport({("POST", f"{HOST}/oauth/token"): [_ok(body)]})
        token = base.exchange_code(_client(transport, clock), _oauth(), "code-1", "verifier-1", clock.now)
        form = _form(transport.calls[0])
        self.assertEqual(form["code_verifier"], "verifier-1")
        self.assertEqual(form["client_secret"], "fake-secret")
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(token.expires_at, clock.now + 3600)
        self.assertEqual(dict(token.extra), {"x_user_id": "42"})
        self.assertNotIn(FAKE_ACCESS, repr(token))

    def test_exchange_code_basic_auth_and_no_expiry(self) -> None:
        transport = FakeTransport({("POST", f"{HOST}/oauth/token"): [_ok({"access_token": FAKE_ACCESS})]})
        cfg = _oauth(client_auth="basic", use_pkce=False, extra_token={"action": "requesttoken"})
        token = base.exchange_code(_client(transport), cfg, "code-1", None, 0.0)
        req = transport.calls[0]
        self.assertTrue(req.headers["Authorization"].startswith("Basic "))
        self.assertNotIn("client_secret", _form(req))
        self.assertEqual(_form(req)["action"], "requesttoken")
        self.assertTrue(math.isinf(token.expires_at))

    def test_token_errors(self) -> None:
        transport = FakeTransport({("POST", f"{HOST}/oauth/token"): [
            json_response('{"error": "invalid_grant"}', 400), _ok({"no": "token"})]})
        client = _client(transport)
        with self.assertRaises(AuthRequired) as ctx:
            base.exchange_code(client, _oauth(), "c", None, 0.0)
        self.assertEqual(str(ctx.exception), "invalid_grant")
        with self.assertRaises(SchemaError):
            base.exchange_code(client, _oauth(), "c", None, 0.0)


class TokenManagerTests(unittest.TestCase):
    def _manager(self, transport: FakeTransport, store: MemoryTokenStore, clock: FakeClock,
                 lock_dir: Path | None = None) -> TokenManager:
        return TokenManager("whoop", store, _client(transport, clock), _oauth(), clock=clock, lock_dir=lock_dir)

    def test_fresh_token_needs_no_call(self) -> None:
        clock = FakeClock()
        store = MemoryTokenStore({"whoop": _token(clock.now + 3600)})
        transport = FakeTransport({})
        self.assertEqual(self._manager(transport, store, clock).access_token(), FAKE_ACCESS)
        self.assertEqual(transport.calls, [])

    def test_missing_token_requires_auth(self) -> None:
        with self.assertRaises(AuthRequired):
            self._manager(FakeTransport({}), MemoryTokenStore(), FakeClock()).access_token()

    def test_refresh_near_expiry_keeps_refresh_token_and_saves_first(self) -> None:
        clock = FakeClock()
        store = MemoryTokenStore({"whoop": _token(clock.now + 60)})
        transport = FakeTransport({("POST", f"{HOST}/oauth/token"): [
            _ok({"access_token": "fake-access-0002", "expires_in": 3600})]})
        got = self._manager(transport, store, clock).access_token()
        self.assertEqual(got, "fake-access-0002")
        self.assertEqual(store.saves[-1][1].access_token, got)
        self.assertEqual(store.tokens["whoop"].refresh_token, FAKE_REFRESH)
        self.assertEqual(_form(transport.calls[0])["refresh_token"], FAKE_REFRESH)

    def test_rotating_refresh_token_is_saved(self) -> None:
        clock = FakeClock()
        store = MemoryTokenStore({"whoop": _token(clock.now - 1)})
        transport = FakeTransport({("POST", f"{HOST}/oauth/token"): [
            _ok({"access_token": "fake-access-0002", "refresh_token": "fake-refresh-0002", "expires_in": 3600})]})
        with tempfile.TemporaryDirectory() as tmp:
            self._manager(transport, store, clock, lock_dir=Path(tmp) / "locks").access_token()
            self.assertTrue((Path(tmp) / "locks" / "whoop.lock").exists())
        self.assertEqual(store.tokens["whoop"].refresh_token, "fake-refresh-0002")

    def test_invalid_grant_deletes_nothing(self) -> None:
        clock = FakeClock()
        old = _token(clock.now - 1)
        store = MemoryTokenStore({"whoop": old})
        transport = FakeTransport({("POST", f"{HOST}/oauth/token"): [json_response('{"error":"invalid_grant"}', 400)]})
        with self.assertRaises(AuthRequired):
            self._manager(transport, store, clock).access_token()
        self.assertEqual(store.tokens["whoop"], old)
        self.assertEqual(store.saves, [])

    def test_no_refresh_token_requires_auth(self) -> None:
        clock = FakeClock()
        store = MemoryTokenStore({"polar": _token(clock.now - 1, refresh=None)})
        manager = TokenManager("polar", store, _client(FakeTransport({}), clock), _oauth(), clock=clock)
        with self.assertRaises(AuthRequired):
            manager.access_token()

    def test_two_threads_refresh_once(self) -> None:
        clock = FakeClock()
        store = MemoryTokenStore({"whoop": _token(clock.now + 10)})
        transport = FakeTransport({("POST", f"{HOST}/oauth/token"): [
            _ok({"access_token": "fake-access-0002", "refresh_token": "fake-refresh-0002", "expires_in": 3600})]})
        manager = self._manager(transport, store, clock)
        start, got = threading.Barrier(2), []

        def worker() -> None:
            start.wait()
            got.append(manager.access_token())

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(got, ["fake-access-0002"] * 2)
        self.assertEqual(len(transport.calls), 1)

    def test_force_refresh(self) -> None:
        clock = FakeClock()
        store = MemoryTokenStore({"whoop": _token(clock.now + 3600)})
        transport = FakeTransport({("POST", f"{HOST}/oauth/token"): [
            _ok({"access_token": "fake-access-0002", "expires_in": 3600})]})
        self.assertEqual(self._manager(transport, store, clock).force_refresh(), "fake-access-0002")


class TokenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def test_refuses_root_inside_git_tree(self) -> None:
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        with self.assertRaises(ValueError):
            FileTokenStore(self.tmp / "repo" / "secrets")

    def test_round_trip_modes_and_delete(self) -> None:
        store = FileTokenStore(self.tmp / "secrets")
        self.assertIsNone(store.load("garmin"))
        token = Token(FAKE_ACCESS, None, math.inf, "", extra={"userid": "1"})
        store.save("garmin", token)
        path = self.tmp / "secrets" / "garmin.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.tmp / "secrets").stat().st_mode), 0o700)
        self.assertEqual(store.load("garmin"), token)
        self.assertEqual([p.name for p in (self.tmp / "secrets").iterdir()], ["garmin.json"])
        store.delete("garmin")
        store.delete("garmin")
        self.assertIsNone(store.load("garmin"))
        with self.assertRaises(ValueError):
            store.load("../escape")

    def test_default_root_from_env(self) -> None:
        with mock.patch.dict(os.environ, {base.SECRETS_ENV: str(self.tmp / "s")}):
            self.assertEqual(FileTokenStore().root, self.tmp / "s")
            self.assertIsInstance(base.default_token_store("linux", lambda _: None), FileTokenStore)
        self.assertIsInstance(base.default_token_store("darwin", lambda _: "/usr/bin/security"), KeychainTokenStore)

    def test_keychain_store_commands(self) -> None:
        calls = []
        stored = base.token_to_json(_token(5.0))

        def runner(args, **kw):
            calls.append(args)
            if args[1] == "find-generic-password":
                return SimpleNamespace(returncode=0 if len(calls) > 1 else 44, stdout=stored + "\n")
            return SimpleNamespace(returncode=0, stdout="")

        store = KeychainTokenStore(runner=runner)
        self.assertIsNone(store.load("whoop"))
        self.assertEqual(store.load("whoop"), _token(5.0))
        store.save("whoop", _token(5.0))
        store.delete("whoop")
        self.assertEqual(calls[2][:7], ["security", "add-generic-password", "-U", "-s", "homunculus.trackers",
                                        "-a", "whoop"])
        self.assertEqual(calls[3][1], "delete-generic-password")


class CursorTests(unittest.TestCase):
    def test_first_run_uses_overlap(self) -> None:
        plan = base.plan_window(None, now=NOW, overlap_days=3)
        self.assertTrue(plan.regular)
        self.assertEqual(plan.chunks, ((NOW - timedelta(days=3), NOW),))
        cursor = base.cursor_after(plan, 1, None, last_id="a-1")
        self.assertEqual(cursor, {"watermark": "2026-11-20T10:00:00+00:00", "last_id": "a-1"})

    def test_watermark_window(self) -> None:
        cursor = {"watermark": "2026-11-19T10:00:00+00:00"}
        plan = base.plan_window(cursor, now=NOW, overlap_days=3)
        self.assertEqual(plan.window(1), ("2026-11-16T10:00:00+00:00", "2026-11-20T10:00:00+00:00"))
        self.assertEqual(base.cursor_after(plan, 0, cursor), cursor)

    def test_backfill_chunks_then_continuation(self) -> None:
        plan = base.plan_window(None, now=NOW, overlap_days=3, backfill_days=60, chunk_days=28)
        starts = [NOW - timedelta(days=d) for d in (3, 31, 59, 60)]
        self.assertEqual([c[0] for c in plan.chunks], starts)
        self.assertEqual([c[1] for c in plan.chunks[1:]], starts[:3])
        cursor = base.cursor_after(plan, 2)
        self.assertEqual(cursor["backfill_before"], base.iso(starts[1]))
        later = NOW + timedelta(hours=1)
        cont = base.plan_window(cursor, now=later, overlap_days=3, chunk_days=28)
        self.assertFalse(cont.regular)
        self.assertEqual(cont.chunks, ((starts[2], starts[1]), (starts[3], starts[2])))
        done = base.cursor_after(cont, 2, cursor)
        self.assertEqual(done, {"watermark": cursor["watermark"]})

    def test_max_history_caps_backfill(self) -> None:
        plan = base.plan_window(None, now=NOW, overlap_days=3, backfill_days=90, chunk_days=14, max_history_days=28)
        self.assertEqual(plan.chunks[-1][0], NOW - timedelta(days=28))

    def test_cursor_size_and_shape(self) -> None:
        self.assertEqual(base.decode_cursor(base.encode_cursor({"b": 1, "a": "x"})), {"a": "x", "b": 1})
        self.assertIsNone(base.decode_cursor(""))
        with self.assertRaises(ValueError):
            base.encode_cursor({"x": "y" * 5000})
        with self.assertRaises(ValueError):
            PollResult(TrackerBatch(), ("a", "b"), {"x": "y" * 5000}, True, 0)
        with self.assertRaises(ValueError):
            base.iso(datetime(2026, 1, 1))

    def test_strip_vendor_raw(self) -> None:
        raw = {"activityId": 1, "ownerFullName": "Synthetic Owner", "startLatitude": 1.5, "lon": 2.0,
               "locationName": "Somewhere", "deviceSerial": "S-1", "summary": {"distance": 5000.0}}
        self.assertEqual(json.loads(base.strip_vendor_raw(raw)), {"activityId": 1, "summary": {"distance": 5000.0}})


class RegistryTests(unittest.TestCase):
    CFG = TrackerConfig(enabled=("garmin",), per_source={"garmin": {"poll_minutes": 60}}, tz="Europe/Berlin")

    def test_unknown_and_blocked(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            registry.build("nope", env={}, cfg=self.CFG)
        self.assertIn("garmin", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            registry.build("oura", env={}, cfg=self.CFG)
        self.assertIn("§4(d)", str(ctx.exception))

    def test_build_passes_options(self) -> None:
        seen = {}
        with mock.patch.dict(registry.REGISTRY, {"garmin": lambda **kw: seen.update(kw) or "source"}):
            got = registry.build("garmin", env={"K": "v"}, cfg=self.CFG, token_store=MemoryTokenStore())
        self.assertEqual(got, "source")
        self.assertEqual(seen["options"], {"poll_minutes": 60})
        self.assertEqual(seen["tz"], "Europe/Berlin")

    def test_missing_adapter_module(self) -> None:
        with self.assertRaises(ValueError):
            registry._lazy("no_such_adapter", "X")()

    def test_enabled(self) -> None:
        self.assertEqual(registry.enabled(self.CFG), ["garmin"])
        with self.assertRaises(ValueError):
            registry.enabled(TrackerConfig(("fitbit",), {}, "UTC"))


if __name__ == "__main__":
    unittest.main()

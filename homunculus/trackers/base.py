"""Shared plumbing for direct tracker pollers (see docs/TRACKER_CONTRACT.md).

Holds the types every adapter uses, an HTTP client with host allow-list,
retry and request budget, the token stores, OAuth2 authorization-code helpers
(PKCE, loopback redirect, refresh with rotation) and cursor windows.

Network access goes only through an injectable ``Transport``. Tokens never
enter a git work tree: they live in the macOS Keychain or in 0600 files under
``$HOMUNCULUS_SECRETS`` (default ``~/.local/state/homunculus/trackers``).
Nothing here logs a token, a query string, a header or a body.
"""

from __future__ import annotations

import base64
import email.utils
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
import random
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from homunculus.ingest import strip_workout
from homunculus.store import (
    MAX_CURSOR_BYTES,
    DailyValue,
    HealthEvent,
    Sample,
    SleepNight,
    SleepSegment,
    Workout,
    canonical_json,
)

logger = logging.getLogger("homunculus.trackers")

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_WAIT_S = 300.0
REFRESH_MARGIN_S = 120.0
SECRETS_ENV = "HOMUNCULUS_SECRETS"
DEFAULT_SECRETS = "~/.local/state/homunculus/trackers"
DONE_PAGE = "Fertig, du kannst das Fenster schliessen."
FAILED_PAGE = "Anmeldung fehlgeschlagen. Du kannst das Fenster schliessen."

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_PKCE_ALPHABET = string.ascii_letters + string.digits + "-._~"
_STATE_ALPHABET = string.ascii_letters + string.digits
_ID_SEGMENT_RE = re.compile(r"^(\d[\d.:-]*|[0-9a-fA-F-]{16,})$")  # numbers, dates, uuids


# --------------------------------------------------------------------------
# Errors


class TrackerError(Exception):
    """Base class; ``kind`` is the TrackerState.last_error value."""

    kind = "network"


class AuthRequired(TrackerError):
    """No token, invalid_grant, key expired, or 401 after one refresh."""

    kind = "auth_required"


class RateLimited(TrackerError):
    """429 retries exhausted, a wait above MAX_WAIT_S, or the request budget is spent."""

    kind = "rate_limited"

    def __init__(self, retry_after_s: float | None, detail: str = "rate_limited") -> None:
        super().__init__(detail)
        self.retry_after_s = retry_after_s


class UpstreamError(TrackerError):
    """5xx after retries, or a network failure."""

    kind = "http_5xx"


class NetworkError(UpstreamError):
    """Socket error or timeout before any HTTP status."""

    kind = "network"


class SchemaError(TrackerError):
    """A response failed validation; the poll result is discarded."""

    kind = "schema"


class HostNotAllowed(TrackerError):
    """A URL outside the adapter's allow-list; raised before any I/O."""

    kind = "network"


# --------------------------------------------------------------------------
# HTTP


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None
    timeout_s: float = 30.0


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]  # keys lower-cased
    body: bytes


Transport = Callable[[Request], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _lower_headers(items: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in items}


def urllib_transport(req: Request) -> Response:
    """https only, no redirects; non-2xx comes back as a Response; socket errors raise UpstreamError."""
    if urlsplit(req.url).scheme != "https":
        raise HostNotAllowed("only https URLs are allowed")
    raw = urllib.request.Request(req.url, data=req.body, headers=dict(req.headers), method=req.method)
    try:
        with _OPENER.open(raw, timeout=req.timeout_s) as resp:
            return Response(resp.status, _lower_headers(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as err:
        return Response(err.code, _lower_headers(err.headers.items()), err.read())
    except (urllib.error.URLError, OSError) as err:
        raise NetworkError(f"network: {type(err).__name__}") from None


def path_template(path: str) -> str:
    """Path with id-like segments replaced, safe to log."""
    parts = ("{id}" if _ID_SEGMENT_RE.match(p) else p for p in path.split("/"))
    return "/".join(parts)


def _retry_after(headers: Mapping[str, str], now: float) -> float | None:
    """Seconds from Retry-After (seconds or HTTP date), else a reset header in seconds."""
    text = headers.get("retry-after")
    if text:
        seconds = _seconds(text)
        if seconds is not None:
            return seconds
        try:
            return max(0.0, email.utils.parsedate_to_datetime(text).timestamp() - now)
        except (TypeError, ValueError):
            pass
    for name in ("x-ratelimit-reset", "ratelimit-reset"):
        seconds = _seconds(headers.get(name) or "")
        if seconds is not None:
            return seconds
    return None


def _seconds(text: str) -> float | None:
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _encode_body(form: Mapping[str, str] | None, json_body: Any) -> tuple[bytes | None, dict[str, str]]:
    if form is not None:
        return urlencode(dict(form)).encode("ascii"), {"Content-Type": "application/x-www-form-urlencoded"}
    if json_body is not None:
        return json.dumps(json_body, allow_nan=False).encode("utf-8"), {"Content-Type": "application/json"}
    return None, {}


def _with_query(url: str, params: Mapping[str, str] | None) -> str:
    if not params:
        return url
    return f"{url}{'&' if urlsplit(url).query else '?'}{urlencode(dict(params))}"


class HttpClient:
    """Allow-listed HTTP with retry on 429/5xx and a hard per-run request budget."""

    def __init__(self, transport: Transport, *, allow_hosts: frozenset[str], user_agent: str,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None, max_retries: int = 4, max_requests: int = 200) -> None:
        self._transport = transport
        self._hosts = frozenset(allow_hosts)
        self._ua = user_agent
        self._clock, self._sleep = clock, sleep
        self._rng = rng or random.Random()
        self.max_retries, self.max_requests = max_retries, max_requests
        self.requests_made = 0
        self._count_lock = threading.Lock()

    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in self._hosts:
            raise HostNotAllowed(f"host not allowed: {parts.hostname!r}")

    def _spend(self) -> None:
        with self._count_lock:
            if self.requests_made >= self.max_requests:
                raise RateLimited(None, "request budget spent")
            self.requests_made += 1

    def _send(self, req: Request) -> Response:
        self._spend()
        started = self._clock()
        resp = self._transport(req)
        parts = urlsplit(req.url)
        logger.info("%s %s%s -> %d in %.2fs", req.method, parts.hostname, path_template(parts.path),
                    resp.status, self._clock() - started)
        return resp

    def _wait_s(self, resp: Response, attempt: int) -> float:
        hinted = _retry_after(resp.headers, self._clock())
        if hinted is not None:
            return hinted
        return min(60.0, 2.0 ** attempt) * (0.5 + self._rng.random())

    def _exhausted(self, resp: Response) -> TrackerError:
        if resp.status == 429:
            return RateLimited(_retry_after(resp.headers, self._clock()))
        return UpstreamError(f"http_{resp.status}")

    def request(self, method: str, url: str, *, params: Mapping[str, str] | None = None,
                headers: Mapping[str, str] | None = None, form: Mapping[str, str] | None = None,
                json_body: Any = None) -> Response:
        full = _with_query(url, params)
        self._check_url(full)
        body, extra = _encode_body(form, json_body)
        req = Request(method, full, {"User-Agent": self._ua, **extra, **dict(headers or {})}, body)
        for attempt in range(self.max_retries + 1):
            resp = self._send(req)
            if resp.status not in RETRY_STATUSES:
                return resp
            if attempt == self.max_retries:
                raise self._exhausted(resp)
            wait = self._wait_s(resp, attempt)
            if wait > MAX_WAIT_S:
                raise RateLimited(wait, "wait above limit")
            self._sleep(wait)
        raise AssertionError("unreachable")

    def get_json(self, url: str, *, params: Mapping[str, str] | None = None,
                 headers: Mapping[str, str] | None = None) -> Any:
        """GET and decode JSON; 401/403 raise AuthRequired, other non-2xx UpstreamError."""
        resp = self.request("GET", url, params=params, headers=headers)
        if resp.status in (401, 403):
            raise AuthRequired(f"http_{resp.status}")
        if not 200 <= resp.status < 300:
            raise UpstreamError(f"http_{resp.status}")
        return decode_json(resp.body)


def decode_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SchemaError("response is not JSON") from None


# --------------------------------------------------------------------------
# Tokens


@dataclass(frozen=True)
class Token:
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds; inf when the vendor gives none
    scope: str
    token_type: str = "Bearer"
    refresh_expires_at: float | None = None
    obtained_at: float = 0.0
    extra: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # never print a secret
        return f"Token(expires_at={self.expires_at!r}, scope={self.scope!r})"


def token_to_json(token: Token) -> str:
    obj = asdict(token)
    obj["expires_at"] = None if math.isinf(token.expires_at) else token.expires_at
    obj["extra"] = dict(token.extra)
    return canonical_json(obj).decode("utf-8")


def token_from_json(text: str) -> Token:
    obj = json.loads(text)
    expires = obj.get("expires_at")
    obj["expires_at"] = math.inf if expires is None else float(expires)
    obj["extra"] = dict(obj.get("extra") or {})
    return Token(**obj)


class TokenStore(Protocol):
    def load(self, name: str) -> Token | None: ...
    def save(self, name: str, token: Token) -> None: ...
    def delete(self, name: str) -> None: ...


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid tracker name: {name!r}")
    return name


def default_secrets_root(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(SECRETS_ENV) or DEFAULT_SECRETS).expanduser()


def inside_git_tree(path: Path) -> bool:
    """True if path or any parent holds a .git entry."""
    resolved = path.expanduser().resolve()
    return any((p / ".git").exists() for p in (resolved, *resolved.parents))


class FileTokenStore:
    """One 0600 JSON file per tracker in a 0700 directory outside every git work tree."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (Path(root) if root is not None else default_secrets_root()).expanduser()
        if inside_git_tree(self.root):
            raise ValueError("token store root must not lie inside a git work tree")

    def _path(self, name: str) -> Path:
        return self.root / f"{_check_name(name)}.json"

    def load(self, name: str) -> Token | None:
        path = self._path(name)
        return token_from_json(path.read_text(encoding="utf-8")) if path.is_file() else None

    def save(self, name: str, token: Token) -> None:
        path = self._path(name)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(token_to_json(token).encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)


_KEYCHAIN_NOT_FOUND = 44  # exit status of 'security' for a missing item


class KeychainTokenStore:
    """macOS Keychain generic passwords, service ``homunculus.trackers``, account = tracker name."""

    def __init__(self, service: str = "homunculus.trackers", runner: Callable[..., Any] = subprocess.run) -> None:
        self.service = service
        self._run = runner

    def _security(self, *args: str) -> Any:
        return self._run(["security", *args], capture_output=True, text=True, check=False)

    def load(self, name: str) -> Token | None:
        proc = self._security("find-generic-password", "-s", self.service, "-a", _check_name(name), "-w")
        if proc.returncode == _KEYCHAIN_NOT_FOUND:
            return None
        if proc.returncode != 0:
            raise TrackerError("keychain read failed")
        return token_from_json(proc.stdout.strip())

    def save(self, name: str, token: Token) -> None:
        proc = self._security("add-generic-password", "-U", "-s", self.service, "-a", _check_name(name),
                              "-w", token_to_json(token))
        if proc.returncode != 0:
            raise TrackerError("keychain write failed")

    def delete(self, name: str) -> None:
        proc = self._security("delete-generic-password", "-s", self.service, "-a", _check_name(name))
        if proc.returncode not in (0, _KEYCHAIN_NOT_FOUND):
            raise TrackerError("keychain delete failed")


def default_token_store(platform: str = sys.platform,
                        which: Callable[[str], str | None] = shutil.which) -> TokenStore:
    """Keychain on darwin when 'security' exists, else FileTokenStore."""
    if platform == "darwin" and which("security"):
        return KeychainTokenStore()
    return FileTokenStore()


# --------------------------------------------------------------------------
# OAuth2 authorization code


@dataclass(frozen=True)
class OAuthConfig:
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str | None
    scopes: tuple[str, ...]
    scope_sep: str = " "
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    use_pkce: bool = True
    client_auth: str = "body"  # 'body' | 'basic'
    state_len: int = 32
    extra_authorize: Mapping[str, str] = field(default_factory=dict)
    extra_token: Mapping[str, str] = field(default_factory=dict)
    token_parser: Callable[[Any, float], Token] | None = None

    def __repr__(self) -> str:  # never print the client secret
        return f"OAuthConfig(token_url={self.token_url!r}, scopes={self.scopes!r})"


def pkce_challenge(verifier: str) -> str:
    """S256: base64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def pkce_pair(rng: random.Random | None = None) -> tuple[str, str]:
    """(verifier, challenge); the verifier is 64 chars from [A-Za-z0-9-._~]."""
    pick = rng or secrets.SystemRandom()
    verifier = "".join(pick.choice(_PKCE_ALPHABET) for _ in range(64))
    return verifier, pkce_challenge(verifier)


def new_state(length: int) -> str:
    return "".join(secrets.choice(_STATE_ALPHABET) for _ in range(length))


def build_authorize_url(cfg: OAuthConfig, state: str, challenge: str | None) -> str:
    params = {"response_type": "code", "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri,
              "scope": cfg.scope_sep.join(cfg.scopes), "state": state}
    if challenge is not None:
        params.update(code_challenge=challenge, code_challenge_method="S256")
    params.update(cfg.extra_authorize)
    return _with_query(cfg.authorize_url, params)


def _code_from_query(query: str, state: str) -> str:
    """The code of a redirect query; AuthRequired on an error, a state mismatch or no code."""
    params = {k: v[0] for k, v in parse_qs(query).items()}
    if "error" in params:
        raise AuthRequired(params["error"])
    if not hmac.compare_digest(params.get("state", "").encode(), state.encode()):
        raise AuthRequired("state_mismatch")
    if not params.get("code"):
        raise AuthRequired("no_code")
    return params["code"]


def parse_pasted_redirect(text: str, state: str) -> str:
    """Manual mode: the owner pastes the full redirect URL; returns the code."""
    return _code_from_query(urlsplit(text.strip()).query, state)


def _redirect_target(cfg: OAuthConfig) -> tuple[int, str]:
    parts = urlsplit(cfg.redirect_uri)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost") or not parts.port:
        raise ValueError("redirect_uri must be http://127.0.0.1:<port>/<path>")
    return parts.port, parts.path or "/"


def _callback_handler(path: str, state: str, outcome: dict) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            parts = urlsplit(self.path)
            if parts.path != path:
                self._reply(404, "Nicht gefunden.")
                return
            try:
                outcome["code"] = _code_from_query(parts.query, state)
            except AuthRequired as err:
                outcome["error"] = err
            self._reply(200, FAILED_PAGE if "error" in outcome else DONE_PAGE)

        def _reply(self, status: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  the URL carries the code
            return

    return Handler


def loopback_server(cfg: OAuthConfig, state: str, outcome: dict) -> HTTPServer:
    """HTTP server on 127.0.0.1 and the redirect port only."""
    port, path = _redirect_target(cfg)
    return HTTPServer(("127.0.0.1", port), _callback_handler(path, state, outcome))


def wait_for_code(cfg: OAuthConfig, state: str, *, timeout_s: float = 300.0,
                  on_ready: Callable[[], None] = lambda: None) -> str:
    """Serve one request on the redirect path and return its code; AuthRequired on error or timeout."""
    outcome: dict = {}
    deadline = time.monotonic() + timeout_s
    with loopback_server(cfg, state, outcome) as server:
        on_ready()
        while not outcome and time.monotonic() < deadline:
            server.timeout = max(0.05, min(1.0, deadline - time.monotonic()))
            server.handle_request()
    if "error" in outcome:
        raise outcome["error"]
    if "code" not in outcome:
        raise AuthRequired("timeout")
    return outcome["code"]


_TOKEN_KEYS = frozenset({"access_token", "refresh_token", "expires_in", "scope", "token_type",
                         "refresh_token_expires_in", "id_token"})


def _token_extra(obj: Mapping[str, Any]) -> dict[str, str]:
    """Scalar non-standard fields (Polar x_user_id, Withings userid) as strings."""
    scalars = {k: v for k, v in obj.items() if k not in _TOKEN_KEYS and not isinstance(v, bool)}
    return {k: str(v) for k, v in scalars.items() if isinstance(v, (str, int))}


def _deadline(now: float, value: Any) -> float | None:
    seconds = _seconds(str(value)) if value is not None else None
    return None if seconds is None else now + seconds


def parse_token_response(obj: Any, now: float) -> Token:
    """Standard RFC 6749 token JSON; without expires_in the token never expires."""
    if not isinstance(obj, Mapping) or not isinstance(obj.get("access_token"), str):
        raise SchemaError("token response without access_token")
    expires_at = _deadline(now, obj.get("expires_in"))
    return Token(
        access_token=obj["access_token"], refresh_token=obj.get("refresh_token") or None,
        expires_at=math.inf if expires_at is None else expires_at, scope=str(obj.get("scope") or ""),
        token_type=str(obj.get("token_type") or "Bearer"),
        refresh_expires_at=_deadline(now, obj.get("refresh_token_expires_in")), obtained_at=now,
        extra=_token_extra(obj),
    )


def _client_auth(cfg: OAuthConfig, form: dict[str, str]) -> dict[str, str]:
    """Adds client credentials to the form (body) or returns a Basic header."""
    if cfg.client_auth == "basic":
        pair = f"{cfg.client_id}:{cfg.client_secret or ''}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
    form["client_id"] = cfg.client_id
    if cfg.client_secret:
        form["client_secret"] = cfg.client_secret
    return {}


def _token_error(resp: Response) -> TrackerError:
    if resp.status >= 500:
        return UpstreamError(f"http_{resp.status}")
    try:
        error = json.loads(resp.body.decode("utf-8")).get("error")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        error = None
    return AuthRequired(error if isinstance(error, str) and error else f"http_{resp.status}")


def _token_call(http: HttpClient, cfg: OAuthConfig, form: dict[str, str], now: float) -> Token:
    form.update(cfg.extra_token)
    headers = {"Accept": "application/json", **_client_auth(cfg, form)}
    resp = http.request("POST", cfg.token_url, form=form, headers=headers)
    if not 200 <= resp.status < 300:
        raise _token_error(resp)
    parser = cfg.token_parser or parse_token_response
    return parser(decode_json(resp.body), now)


def exchange_code(http: HttpClient, cfg: OAuthConfig, code: str, verifier: str | None, now: float) -> Token:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": cfg.redirect_uri}
    if verifier is not None:
        form["code_verifier"] = verifier
    return _token_call(http, cfg, form, now)


def refresh_token(http: HttpClient, cfg: OAuthConfig, token: Token, now: float) -> Token:
    """New token; keeps the old refresh token and extras when the response omits them (Google)."""
    if not token.refresh_token:
        raise AuthRequired("no_refresh_token")
    new = _token_call(http, cfg, {"grant_type": "refresh_token", "refresh_token": token.refresh_token}, now)
    return replace(
        new, refresh_token=new.refresh_token or token.refresh_token,
        refresh_expires_at=new.refresh_expires_at if new.refresh_token else token.refresh_expires_at,
        extra={**token.extra, **new.extra},
    )


class TokenManager:
    """Hands out a valid access token; refreshes under a lock and saves before returning.

    Rotating refresh tokens (WHOOP, Withings) make the order matter: load,
    refresh, save, then use. ``lock_dir`` adds an fcntl lock across processes;
    without it only threads of this process are serialized.
    """

    def __init__(self, name: str, store: TokenStore, http: HttpClient, cfg: OAuthConfig,
                 clock: Callable[[], float] = time.time, lock_dir: Path | None = None) -> None:
        self.name, self._store, self._http, self._cfg = _check_name(name), store, http, cfg
        self._clock, self._lock_dir = clock, lock_dir
        self._thread_lock = threading.Lock()

    def _load(self) -> Token:
        token = self._store.load(self.name)
        if token is None:
            raise AuthRequired("no_token")
        return token

    def _fresh(self, token: Token) -> bool:
        return token.expires_at - self._clock() >= REFRESH_MARGIN_S

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            if self._lock_dir is None:
                yield
                return
            self._lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(self._lock_dir / f"{self.name}.lock", "w") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def _refresh(self, token: Token) -> str:
        new = refresh_token(self._http, self._cfg, token, self._clock())
        self._store.save(self.name, new)
        return new.access_token

    def access_token(self) -> str:
        token = self._load()
        if self._fresh(token):
            return token.access_token
        with self._locked():
            token = self._load()  # another thread or process may have refreshed meanwhile
            return token.access_token if self._fresh(token) else self._refresh(token)

    def force_refresh(self) -> str:
        """Refresh regardless of expiry; used once after a 401."""
        with self._locked():
            return self._refresh(self._load())


# --------------------------------------------------------------------------
# Adapter types


@dataclass(frozen=True)
class TrackerBatch:
    samples: tuple[Sample, ...] = ()
    sleep_segments: tuple[SleepSegment, ...] = ()
    sleep_nights: tuple[SleepNight, ...] = ()
    workouts: tuple[Workout, ...] = ()
    events: tuple[HealthEvent, ...] = ()
    daily_vendor_scores: tuple[DailyValue, ...] = ()


@dataclass(frozen=True)
class PollResult:
    batch: TrackerBatch
    window: tuple[str, str]  # (since, until) the batch replaces, ISO-8601 with offset
    new_cursor: Mapping[str, Any]
    complete: bool
    requests_made: int
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        encode_cursor(self.new_cursor)


class AuthUI(Protocol):
    def show_url(self, url: str) -> None: ...
    def ask(self, prompt: str) -> str: ...
    def info(self, text: str) -> None: ...


@dataclass(frozen=True)
class AuthStatus:
    ok: bool
    expires_at: float | None
    refresh_expires_at: float | None
    detail: str  # no secrets


class TrackerSource(Protocol):
    name: ClassVar[str]
    hosts: ClassVar[frozenset[str]]
    auth_kind: ClassVar[str]  # 'oauth2' | 'api_key'
    overlap_days: ClassVar[int]
    backfill_chunk_days: ClassVar[int]
    max_history_days: ClassVar[int | None]
    vendor_keys: ClassVar[frozenset[str]]

    def authorize(self, ui: AuthUI, *, manual: bool = False) -> AuthStatus: ...
    def auth_status(self) -> AuthStatus: ...
    def revoke(self) -> None: ...
    def poll(self, since_cursor: Mapping[str, Any] | None, *, now: datetime, tz: str,
             backfill_days: int = 0) -> PollResult: ...


@dataclass(frozen=True)
class TrackerConfig:
    """Non-secret tracker settings from the ledger's config/trackers.json."""

    enabled: tuple[str, ...]
    per_source: Mapping[str, Mapping[str, Any]]
    tz: str


_OWNER_KEYS = ("owner", "email", "username", "displayname", "fullname", "firstname", "lastname",
               "profileimage")


def _drop_owner(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _drop_owner(v) for k, v in value.items()
                if not any(s in str(k).lower() for s in _OWNER_KEYS)}
    if isinstance(value, list):
        return [_drop_owner(v) for v in value]
    return value


def strip_vendor_raw(obj: Any) -> str:
    """canonical_json of a vendor summary without GPS, locations, devices, serials or owner names."""
    return canonical_json(strip_workout(_drop_owner(obj))).decode("utf-8")


# --------------------------------------------------------------------------
# Cursors and windows
#
# A cursor is a small JSON object stored in TrackerState.cursor_json. The
# keys used here are 'watermark' (end of the last complete regular window),
# 'backfill_before' (oldest start backfilled so far) and 'backfill_target'
# (oldest start still wanted). Adapters may add their own keys.


def iso(dt: datetime) -> str:
    """ISO-8601 with offset, whole seconds; refuses naive datetimes."""
    if dt.tzinfo is None:
        raise ValueError("datetime must carry an offset")
    return dt.isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without offset: {text!r}")
    return dt


def encode_cursor(cursor: Mapping[str, Any]) -> str:
    """canonical_json text; ValueError above MAX_CURSOR_BYTES."""
    data = canonical_json(dict(cursor))
    if len(data) > MAX_CURSOR_BYTES:
        raise ValueError(f"cursor exceeds {MAX_CURSOR_BYTES} bytes")
    return data.decode("utf-8")


def decode_cursor(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("cursor must be a JSON object")
    return obj or None


@dataclass(frozen=True)
class WindowPlan:
    """Request ranges for one poll, newest first, contiguous from since to until."""

    chunks: tuple[tuple[datetime, datetime], ...]
    regular: bool  # True when the first chunk is the trailing overlap window ending now
    backfill_target: datetime | None

    @property
    def until(self) -> datetime:
        return self.chunks[0][1]

    def window(self, done: int) -> tuple[str, str]:
        """(since, until) covered by the first ``done`` chunks, for PollResult.window."""
        if not 1 <= done <= len(self.chunks):
            raise ValueError(f"done must lie in 1..{len(self.chunks)}")
        return iso(self.chunks[done - 1][0]), iso(self.until)


def _target(cursor: Mapping[str, Any], now: datetime, backfill_days: int,
            max_history_days: int | None) -> datetime | None:
    wanted = [parse_iso(cursor["backfill_target"])] if cursor.get("backfill_target") else []
    if backfill_days > 0:
        wanted.append(now - timedelta(days=backfill_days))
    if not wanted:
        return None
    oldest = min(wanted)
    floor = now - timedelta(days=max_history_days) if max_history_days else oldest
    return max(oldest, floor)


def _chunks_down(top: datetime, target: datetime | None, chunk_days: int) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    step = timedelta(days=chunk_days)
    while target is not None and top > target:
        bottom = max(target, top - step)
        out.append((bottom, top))
        top = bottom
    return out


def plan_window(cursor: Mapping[str, Any] | None, *, now: datetime, overlap_days: int,
                backfill_days: int = 0, chunk_days: int = 28,
                max_history_days: int | None = None) -> WindowPlan:
    """Plan the ranges of one poll.

    Regular run: [watermark - overlap, now], or [now - overlap, now] without a
    cursor, followed by backfill chunks below it when a backfill is wanted.
    Continuation run (an earlier backfill stopped at 'backfill_before'): only
    the chunks below that point, so the window stays contiguous and nothing
    between it and the regular window is deleted.
    """
    cursor = cursor or {}
    target = _target(cursor, now, backfill_days, max_history_days)
    edge = parse_iso(cursor["backfill_before"]) if cursor.get("backfill_before") else None
    if edge is not None and target is not None and target < edge:
        return WindowPlan(tuple(_chunks_down(edge, target, chunk_days)), False, target)
    mark = parse_iso(cursor["watermark"]) if cursor.get("watermark") else now
    since = min(mark, now) - timedelta(days=overlap_days)
    return WindowPlan(((since, now), *_chunks_down(since, target, chunk_days)), True, target)


def cursor_after(plan: WindowPlan, done: int, cursor: Mapping[str, Any] | None = None,
                 **extra: Any) -> dict[str, Any]:
    """Cursor after the first ``done`` chunks succeeded; adapter keys go in ``extra``."""
    out = {k: v for k, v in dict(cursor or {}).items() if k not in ("backfill_before", "backfill_target")}
    if done == 0:
        return {**dict(cursor or {}), **extra}
    if plan.regular:
        out["watermark"] = iso(plan.until)
    lowest = plan.chunks[done - 1][0]
    if plan.backfill_target is not None and lowest > plan.backfill_target:
        out["backfill_before"] = iso(lowest)
        out["backfill_target"] = iso(plan.backfill_target)
    out.update(extra)
    return out

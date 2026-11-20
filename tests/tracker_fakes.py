"""Test doubles for tracker adapters: no network, no sleeping, no real tokens."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from homunculus.trackers.base import Request, Response, Token


class FakeTransport:
    """Serves canned responses per (method, 'host/path') in order; query params are recorded, not matched."""

    def __init__(self, routes: Mapping[tuple[str, str], Sequence[Response]]) -> None:
        self._routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[Request] = []
        self._lock = threading.Lock()

    def __call__(self, req: Request) -> Response:
        parts = urlsplit(req.url)
        key = (req.method, f"{parts.hostname}{parts.path}")
        with self._lock:
            self.calls.append(req)
            queue = self._routes.get(key)
            if not queue:
                raise AssertionError(f"unexpected request {key}")
            return queue.pop(0)


def json_response(body: bytes | str, status: int = 200, headers: Mapping[str, str] | None = None) -> Response:
    data = body.encode("utf-8") if isinstance(body, str) else body
    return Response(status, {k.lower(): v for k, v in (headers or {}).items()}, data)


class FakeClock:
    """.now is epoch seconds; sleep records waits and advances time."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class MemoryTokenStore:
    def __init__(self, tokens: Mapping[str, Token] | None = None) -> None:
        self.tokens = dict(tokens or {})
        self.saves: list[tuple[str, Token]] = []

    def load(self, name: str) -> Token | None:
        return self.tokens.get(name)

    def save(self, name: str, token: Token) -> None:
        self.saves.append((name, token))
        self.tokens[name] = token

    def delete(self, name: str) -> None:
        self.tokens.pop(name, None)

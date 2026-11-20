"""Tracker names to adapter factories, plus the vendors homunculus refuses to poll.

Adapter modules are imported lazily, so a missing or broken adapter only fails
when that tracker is built. Every adapter class takes keyword arguments
``env, options, tz, transport, token_store, clock``; ``options`` is its block
from config/trackers.json and ``env`` holds its secrets.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Mapping
from typing import Any

from homunculus.trackers.base import (
    TokenStore,
    TrackerConfig,
    TrackerSource,
    Transport,
    default_token_store,
    urllib_transport,
)


def _lazy(module: str, cls: str) -> Callable[..., TrackerSource]:
    path = f"homunculus.trackers.{module}"

    def factory(**kwargs: Any) -> TrackerSource:
        try:
            mod = importlib.import_module(path)
        except ModuleNotFoundError as err:
            if err.name != path:
                raise
            raise ValueError(f"tracker adapter {path} is not installed yet") from None
        return getattr(mod, cls)(**kwargs)

    factory.__qualname__ = f"{module}.{cls}"
    return factory


REGISTRY: Mapping[str, Callable[..., TrackerSource]] = {
    "garmin": _lazy("garmin", "GarminSource"),
    "whoop": _lazy("whoop", "WhoopSource"),
    "google": _lazy("google", "GoogleHealthSource"),
    "withings": _lazy("withings", "WithingsSource"),
    "polar": _lazy("polar", "PolarSource"),
}
BLOCKED: Mapping[str, str] = {
    "oura": "Oura API Agreement §4(d): API data may not be AI input. Use Oura -> Apple Health -> HAE.",
    "fitbit": "Legacy Fitbit Web API is shut down. Use google.",
}


def check_name(name: str) -> str:
    """name if it is a registered tracker; ValueError with the reason otherwise."""
    if name in BLOCKED:
        raise ValueError(f"tracker {name!r} is blocked: {BLOCKED[name]}")
    if name not in REGISTRY:
        raise ValueError(f"unknown tracker {name!r}; known: {sorted(REGISTRY)}, blocked: {sorted(BLOCKED)}")
    return name


def build(name: str, *, env: Mapping[str, str], cfg: TrackerConfig, transport: Transport = urllib_transport,
          token_store: TokenStore | None = None, clock: Callable[[], float] = time.time) -> TrackerSource:
    """Construct the adapter for name; ValueError for unknown, blocked or not yet installed trackers."""
    factory = REGISTRY[check_name(name)]
    return factory(env=env, options=dict(cfg.per_source.get(name) or {}), tz=cfg.tz, transport=transport,
                   token_store=token_store or default_token_store(), clock=clock)


def enabled(cfg: TrackerConfig) -> list[str]:
    """The enabled trackers in config order; ValueError for an unknown or blocked name."""
    return [check_name(n) for n in cfg.enabled]

"""Roster, prompts, message envelopes, LLM providers and the budget guard.

Every LLM bot turn goes through ``call``: the budget decides whether it may
run, the provider from ``roster.json`` answers, the answer is priced and
appended to ``ops/budget.jsonl``, and structured output is validated against
the request's schema. Providers talk HTTPS through ``urllib``; tests inject an
``opener`` and never touch the network.

Prompt files (``agents/<bot>.md``) hold the system text, then a line
``<!-- user -->``, then the user template. Both parts are ``string.Template``
text filled from code-computed context (write ``$$`` for a literal dollar).
``agents/README.md``, when present, is prepended verbatim to every system
prompt so the cached prefix stays identical across bots' turns. Untrusted text
never passes through the template: it is escaped and appended as
``<data source="k">...</data>`` blocks after the rendered user text.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import string
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .store import Ledger

# --------------------------------------------------------------------------
# Roster

KNOWN_BOTS: tuple[str, ...] = (
    "cerebellum", "hippocampus", "amygdala", "thalamus", "motor_cortex", "insula",
    "sartorius", "vagus", "retina", "spindle", "papilla", "brainstem", "pineal",
)
PROVIDERS = ("anthropic", "gemini", "devin", "none")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
COACHES = frozenset({"sartorius", "vagus"})
SCOUTS = frozenset({"retina", "spindle", "papilla"})
# Bots that still run once the weekly cap is spent: the weekly review.
CORE_BOTS = frozenset({"hippocampus", "amygdala", "cerebellum"})
# A fallback model must sit in the bot's own tier. Unknown models have no tier,
# so they never qualify as a fallback (fail closed).
MODEL_TIERS: dict[str, str] = {
    "claude-fable-5-2": "frontier-top",
    "claude-opus-5-5": "frontier",
    "claude-sonnet-5-5": "mid",
    "claude-haiku-5-5": "small",
}

_PROMPT_FILE_RE = re.compile(r"^[a-z][a-z0-9_-]*\.md$")
_ENTRY_KEYS = frozenset({
    "name", "anatomy", "role", "tier", "provider", "model", "alt_provider", "alt_model",
    "effort", "user_facing", "cadence", "prompt_file", "writes", "max_tokens", "daily_usd_cap",
})
_OPTIONAL_KEYS = frozenset({"alt_provider", "alt_model"})


@dataclass(frozen=True)
class BotSpec:
    name: str
    anatomy: str
    role: str
    tier: str
    provider: str
    model: str
    alt_provider: str | None
    alt_model: str | None
    effort: str
    user_facing: bool
    cadence: str
    prompt_file: str
    writes: tuple[str, ...]
    max_tokens: int
    daily_usd_cap: float


def _entry_errors(entry: Mapping[str, Any]) -> list[str]:
    keys = {k for k in entry if not str(k).startswith("_")}
    missing = sorted(_ENTRY_KEYS - _OPTIONAL_KEYS - keys)
    unknown = sorted(keys - _ENTRY_KEYS)
    errors = [f"missing key {k!r}" for k in missing] + [f"unknown key {k!r}" for k in unknown]
    if entry.get("name") not in KNOWN_BOTS:
        errors.append(f"unknown bot {entry.get('name')!r}")
    if entry.get("provider") not in PROVIDERS or entry.get("alt_provider") not in (None, *PROVIDERS):
        errors.append("invalid provider")
    return errors


def _llm_errors(bot: BotSpec) -> list[str]:
    """Checks that only apply to bots backed by an LLM."""
    if bot.provider == "none":
        return [] if bot.alt_provider is None else ["code bot with alt_provider"]
    errors = []
    if bot.effort not in EFFORTS:
        errors.append(f"invalid effort {bot.effort!r}")
    if not _PROMPT_FILE_RE.match(bot.prompt_file):
        errors.append(f"invalid prompt_file {bot.prompt_file!r}")
    if bot.max_tokens <= 0 or bot.daily_usd_cap < 0:
        errors.append("max_tokens must be > 0 and daily_usd_cap >= 0")
    if MODEL_TIERS.get(bot.model, bot.tier) != bot.tier:
        errors.append(f"model {bot.model!r} is not in tier {bot.tier!r}")
    return errors


def _bot_from_entry(entry: Mapping[str, Any]) -> BotSpec:
    errors = _entry_errors(entry)
    if errors:
        raise ValueError(f"roster entry {entry.get('name')!r}: " + "; ".join(errors))
    writes = entry["writes"]
    if isinstance(writes, str) or not all(isinstance(w, str) for w in writes):
        raise ValueError(f"roster entry {entry['name']!r}: writes must be a list of globs")
    bot = BotSpec(
        name=entry["name"], anatomy=str(entry["anatomy"]), role=str(entry["role"]),
        tier=str(entry["tier"]), provider=entry["provider"], model=str(entry["model"]),
        alt_provider=entry.get("alt_provider"), alt_model=entry.get("alt_model"),
        effort=str(entry["effort"]), user_facing=bool(entry["user_facing"]),
        cadence=str(entry["cadence"]), prompt_file=str(entry["prompt_file"]),
        writes=tuple(writes), max_tokens=int(entry["max_tokens"]),
        daily_usd_cap=float(entry["daily_usd_cap"]),
    )
    errors = _llm_errors(bot)
    if errors:
        raise ValueError(f"roster entry {bot.name!r}: " + "; ".join(errors))
    return bot


def load_roster(data: Mapping[str, Any]) -> dict[str, BotSpec]:
    """Parse config/roster.json ({"bots": [...]}); every name must be a known bot, once."""
    entries = data.get("bots")
    if not isinstance(entries, list):
        raise ValueError("roster needs a 'bots' list")
    roster: dict[str, BotSpec] = {}
    for entry in entries:
        bot = _bot_from_entry(entry)
        if bot.name in roster:
            raise ValueError(f"duplicate bot {bot.name!r}")
        roster[bot.name] = bot
    return roster


def write_rules(roster: Mapping[str, BotSpec]) -> dict[str, list[str]]:
    """Author -> fnmatch globs, the input for store.Ledger."""
    return {name: list(bot.writes) for name, bot in roster.items()}


# --------------------------------------------------------------------------
# Prompt rendering

USER_MARKER = "<!-- user -->"
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _context_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)


def escape_data(text: str) -> str:
    """Escape untrusted text so it cannot open or close a tag."""
    return text.replace("&", "&amp;").replace("<", "&lt;")


def _data_block(source: str, text: str) -> str:
    if not _SOURCE_RE.match(source):
        raise ValueError(f"invalid data source name: {source!r}")
    return f'<data source="{source}">\n{escape_data(text)}\n</data>'


def render_prompt(
    bot: BotSpec,
    prompts_dir: Path,
    context: Mapping[str, Any],
    untrusted: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """(system, user). Missing template keys raise KeyError instead of rendering a gap."""
    prompts_dir = Path(prompts_dir)
    raw = (prompts_dir / bot.prompt_file).read_text(encoding="utf-8")
    system_tpl, _, user_tpl = raw.partition(USER_MARKER)
    values = {k: _context_text(v) for k, v in context.items()}
    system = string.Template(system_tpl.strip()).substitute(values)
    shared = prompts_dir / "README.md"
    if shared.is_file():
        system = shared.read_text(encoding="utf-8").strip() + "\n\n" + system
    parts = [string.Template(user_tpl.strip()).substitute(values)]
    parts += [_data_block(k, v) for k, v in sorted((untrusted or {}).items())]
    return system, "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# Requests, responses, errors


@dataclass(frozen=True)
class LLMRequest:
    bot: str
    system: str
    messages: tuple[Mapping[str, Any], ...]
    output_schema: Mapping[str, Any] | None
    max_tokens: int
    effort: str
    cache_system: bool = True


@dataclass(frozen=True)
class LLMResponse:
    text: str
    structured: Mapping[str, Any] | None
    model: str
    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_write: int
    usd: float
    stop_reason: str


class ProviderError(Exception):
    """The provider answered with something unusable, or the request is invalid."""


class ProviderUnavailable(ProviderError):
    """Transient or missing provider: network, 429, 5xx, not configured."""


class SchemaViolation(ProviderError):
    """Structured output is missing, not JSON, or fails the output schema."""


class Provider(Protocol):
    name: str

    def complete(self, req: LLMRequest, model: str) -> LLMResponse: ...

    def submit_batch(self, items: Sequence[tuple[str, LLMRequest]], model: str) -> str: ...

    def batch_results(self, batch_id: str) -> dict[str, LLMResponse] | None: ...


# --------------------------------------------------------------------------
# Pricing

_MTOK = 1_000_000


def price(
    model: str,
    usage: Mapping[str, int],
    prices: Mapping[str, Mapping[str, float]],
    batch: bool = False,
) -> float:
    """USD for one call. prices[model] = {input, output, cache_read?, cache_write?} per million tokens.

    usage uses Anthropic's keys: input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens. Thinking bills as output.
    """
    if model not in prices:
        raise ValueError(f"no price configured for model {model!r}")
    p = prices[model]
    rate_in = float(p["input"])
    cost = (
        usage.get("input_tokens", 0) * rate_in
        + usage.get("output_tokens", 0) * float(p["output"])
        + usage.get("cache_read_input_tokens", 0) * float(p.get("cache_read", rate_in * 0.1))
        + usage.get("cache_creation_input_tokens", 0) * float(p.get("cache_write", rate_in * 1.25))
    ) / _MTOK
    return round(cost * (0.5 if batch else 1.0), 6)


def _require_price(model: str, prices: Mapping[str, Any]) -> None:
    if model not in prices:
        raise ProviderError(f"no price configured for model {model!r}")


# --------------------------------------------------------------------------
# HTTP

Opener = Callable[[urllib.request.Request], bytes]
_RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


def _default_opener(req: urllib.request.Request) -> bytes:
    if not req.full_url.startswith("https://"):
        raise ProviderError("refusing a non-https provider URL")
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310 (https checked)
        return resp.read()


def _send(opener: Opener, req: urllib.request.Request) -> Any:
    """Run the request and decode JSON; map transport failures to provider errors."""
    try:
        raw = opener(req)
    except urllib.error.HTTPError as exc:
        cls = ProviderUnavailable if exc.code in _RETRYABLE else ProviderError
        raise cls(f"HTTP {exc.code} from {req.host}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ProviderUnavailable(f"cannot reach {req.host}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ProviderError(f"non-JSON reply from {req.host}") from exc


def _request(url: str, headers: Mapping[str, str], body: Any = None) -> urllib.request.Request:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    method = "GET" if body is None else "POST"
    return urllib.request.Request(url, data=data, headers=dict(headers), method=method)


def _parse_structured(text: str) -> Mapping[str, Any]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise SchemaViolation("structured output is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SchemaViolation("structured output is not a JSON object")
    return value


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


# --------------------------------------------------------------------------
# Anthropic

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2026-10-21"
_UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "pattern",
})


def _wire_map(value: Mapping[str, Any]) -> dict[str, Any]:
    return {k: wire_schema(v) for k, v in value.items()}


def _wire_list(value: Sequence[Any]) -> list[Any]:
    return [wire_schema(v) for v in value]


_SCHEMA_CHILDREN: dict[str, Callable[[Any], Any]] = {
    "properties": _wire_map, "$defs": _wire_map, "definitions": _wire_map,
    "anyOf": _wire_list, "allOf": _wire_list, "items": lambda v: wire_schema(v),
}


def wire_schema(schema: Any) -> Any:
    """Schema for output_config.format: drop constraints the API rejects, close every object.

    validate_json still enforces the dropped constraints after the reply.
    """
    if not isinstance(schema, Mapping):
        return schema
    out = {
        key: _SCHEMA_CHILDREN.get(key, lambda v: v)(value)
        for key, value in schema.items()
        if key not in _UNSUPPORTED_SCHEMA_KEYS
    }
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
    return out


def anthropic_body(req: LLMRequest, model: str) -> dict[str, Any]:
    """Messages API body. No thinking param (adaptive is the default), no forced tool_choice."""
    system: Any = req.system
    if req.cache_system:
        system = [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}]
    output_config: dict[str, Any] = {"effort": req.effort}
    if req.output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": wire_schema(req.output_schema)}
    return {
        "model": model,
        "max_tokens": req.max_tokens,
        "system": system,
        "messages": [dict(m) for m in req.messages],
        "output_config": output_config,
    }


def _check_anthropic_stop(data: Mapping[str, Any]) -> str:
    stop = data.get("stop_reason") or ""
    if stop == "refusal":
        category = (data.get("stop_details") or {}).get("category")
        raise ProviderError(f"model refused (category {category})")
    if stop == "max_tokens":
        raise ProviderError("reply truncated at max_tokens")
    return stop


def _anthropic_response(
    data: Mapping[str, Any], model: str, prices: Mapping[str, Any], structured: bool, batch: bool,
) -> LLMResponse:
    stop = _check_anthropic_stop(data)
    text = _message_text(data.get("content") or [])
    usage = data.get("usage") or {}
    return LLMResponse(
        text=text,
        structured=_parse_structured(text) if structured else None,
        model=str(data.get("model") or model),
        tokens_in=int(usage.get("input_tokens", 0)),
        tokens_out=int(usage.get("output_tokens", 0)),
        cache_read=int(usage.get("cache_read_input_tokens", 0)),
        cache_write=int(usage.get("cache_creation_input_tokens", 0)),
        usd=price(model, usage, prices, batch=batch),
        stop_reason=stop,
    )


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self, api_key: str, prices: Mapping[str, Mapping[str, float]], opener: Callable | None = None,
    ) -> None:
        if not api_key:
            raise ProviderUnavailable("no Anthropic API key")
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        self._prices = prices
        self._open = opener or _default_opener

    def complete(self, req: LLMRequest, model: str) -> LLMResponse:
        _require_price(model, self._prices)
        data = _send(self._open, _request(ANTHROPIC_URL, self._headers, anthropic_body(req, model)))
        return _anthropic_response(data, model, self._prices, req.output_schema is not None, False)

    def submit_batch(self, items: Sequence[tuple[str, LLMRequest]], model: str) -> str:
        _require_price(model, self._prices)
        requests = [{"custom_id": cid, "params": anthropic_body(r, model)} for cid, r in items]
        data = _send(self._open, _request(ANTHROPIC_URL + "/batches", self._headers, {"requests": requests}))
        return str(data["id"])

    def batch_results(self, batch_id: str) -> dict[str, LLMResponse] | None:
        """None while processing; afterwards only the succeeded, usable items.

        Stateless, so a later process can collect a batch an earlier one submitted.
        structured is the parsed JSON object, or None; the caller validates it.
        """
        info = _send(self._open, _request(f"{ANTHROPIC_URL}/batches/{batch_id}", self._headers))
        if info.get("processing_status") != "ended":
            return None
        raw = self._fetch_text(str(info["results_url"]))
        return _batch_lines(raw, self._prices)

    def _fetch_text(self, url: str) -> str:
        req = _request(url, self._headers)
        try:
            return self._open(req).decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(f"HTTP {exc.code} fetching batch results") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise ProviderUnavailable("cannot fetch batch results") from exc


def _maybe_structured(text: str) -> Mapping[str, Any] | None:
    try:
        return _parse_structured(text)
    except SchemaViolation:
        return None


def _batch_item(line: Mapping[str, Any], prices: Mapping[str, Any]) -> LLMResponse | None:
    result = line.get("result") or {}
    if result.get("type") != "succeeded":
        return None
    message = result.get("message") or {}
    model = str(message.get("model") or "")
    _require_price(model, prices)  # unpriced spend must not vanish from the budget
    try:
        resp = _anthropic_response(message, model, prices, False, True)
    except ProviderError:
        return None
    return dataclasses.replace(resp, structured=_maybe_structured(resp.text))


def _batch_lines(raw: str, prices: Mapping[str, Any]) -> dict[str, LLMResponse]:
    out: dict[str, LLMResponse] = {}
    for text in raw.splitlines():
        if not text.strip():
            continue
        line = json.loads(text)
        resp = _batch_item(line, prices)
        if resp is not None:
            out[str(line["custom_id"])] = resp
    return out


# --------------------------------------------------------------------------
# Gemini (request and reply shape for Gemini 4 UNVERIFIED)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_GEMINI_REFUSALS = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION", "SPII"})


def gemini_body(req: LLMRequest) -> dict[str, Any]:
    """generateContent body. Effort has no verified Gemini equivalent and is not sent."""
    contents = [
        {"role": "model" if m.get("role") == "assistant" else "user",
         "parts": [{"text": _message_text(m.get("content", ""))}]}
        for m in req.messages
    ]
    config: dict[str, Any] = {"maxOutputTokens": req.max_tokens}
    if req.output_schema is not None:
        config["responseMimeType"] = "application/json"
        config["responseJsonSchema"] = dict(req.output_schema)  # field name UNVERIFIED for Gemini 4
    return {
        "systemInstruction": {"parts": [{"text": req.system}]},
        "contents": contents,
        "generationConfig": config,
    }


def _gemini_first(data: Mapping[str, Any]) -> Mapping[str, Any]:
    block = (data.get("promptFeedback") or {}).get("blockReason")
    candidates = data.get("candidates") or []
    if block or not candidates:
        raise ProviderError(f"prompt blocked ({block})")
    return candidates[0]


def _gemini_candidate(data: Mapping[str, Any]) -> tuple[str, str]:
    first = _gemini_first(data)
    finish = str(first.get("finishReason") or "")
    if finish in _GEMINI_REFUSALS:
        raise ProviderError(f"model refused ({finish})")
    if finish == "MAX_TOKENS":
        raise ProviderError("reply truncated at max tokens")
    parts = (first.get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought")), finish


class GeminiProvider:
    name = "gemini"

    def __init__(
        self, api_key: str, prices: Mapping[str, Mapping[str, float]], opener: Callable | None = None,
    ) -> None:
        if not api_key:
            raise ProviderUnavailable("no Gemini API key")
        self._headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
        self._prices = prices
        self._open = opener or _default_opener

    def complete(self, req: LLMRequest, model: str) -> LLMResponse:
        _require_price(model, self._prices)
        url = GEMINI_URL.format(model=model)
        data = _send(self._open, _request(url, self._headers, gemini_body(req)))
        text, finish = _gemini_candidate(data)
        meta = data.get("usageMetadata") or {}
        cached = int(meta.get("cachedContentTokenCount", 0))
        tokens_in = int(meta.get("promptTokenCount", 0)) - cached
        tokens_out = int(meta.get("candidatesTokenCount", 0)) + int(meta.get("thoughtsTokenCount", 0))
        usage = {"input_tokens": tokens_in, "output_tokens": tokens_out, "cache_read_input_tokens": cached}
        return LLMResponse(
            text=text,
            structured=_parse_structured(text) if req.output_schema is not None else None,
            model=model, tokens_in=tokens_in, tokens_out=tokens_out, cache_read=cached,
            cache_write=0, usd=price(model, usage, self._prices), stop_reason=finish,
        )

    def submit_batch(self, items: Sequence[tuple[str, LLMRequest]], model: str) -> str:
        raise ProviderUnavailable("Gemini batches are not wired up")

    def batch_results(self, batch_id: str) -> dict[str, LLMResponse] | None:
        raise ProviderUnavailable("Gemini batches are not wired up")


# --------------------------------------------------------------------------
# Devin (optional; off unless configured)
#
# Devin sessions cannot pin a model, bill in ACUs and run on Cognition's
# machines (docs/ARCHITECTURE.md, "Devin reality check"). The adapter therefore
# runs only bots listed in its config, reports usd 0 (ACU cost is not priced
# here), and should only get bots whose context holds no health data.

DEVIN_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "base_url": "https://api.devin.ai",
    # UNVERIFIED: check docs.devin.ai
    "create_path": "/v1/sessions",
    # UNVERIFIED: check docs.devin.ai
    "status_path": "/v1/session/{session_id}",
    "api_key_env": "DEVIN_API_KEY",
    "poll_interval_s": 30.0,
    "max_polls": 120,
    "max_acu_limit": None,
    "bots": [],
}
# UNVERIFIED: check docs.devin.ai (status_enum values)
_DEVIN_DONE = frozenset({"finished", "blocked"})
_DEVIN_FAILED = frozenset({"expired", "stopped", "failed", "error"})


def devin_prompt(req: LLMRequest) -> str:
    """One self-contained session prompt from a rendered bot turn."""
    turns = "\n\n".join(_message_text(m.get("content", "")) for m in req.messages)
    schema = json.dumps(req.output_schema or {"type": "object"}, sort_keys=True, ensure_ascii=False)
    return (
        f"{req.system}\n\n{turns}\n\n"
        "Do not open pull requests, change repositories or browse the web. "
        "When done, set the session's structured output to one JSON object that "
        f"matches this JSON Schema:\n{schema}"
    )


def _devin_structured(value: Any) -> Mapping[str, Any] | None:
    if value is None or isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        return _parse_structured(value)
    raise SchemaViolation("Devin structured output is not a JSON object")


class DevinProvider:
    """Runs one bot turn as a Devin session and polls for its structured output."""

    name = "devin"

    def __init__(
        self,
        api_key: str,
        *,
        bots: Sequence[str],
        base_url: str = DEVIN_DEFAULTS["base_url"],
        create_path: str = DEVIN_DEFAULTS["create_path"],
        status_path: str = DEVIN_DEFAULTS["status_path"],
        poll_interval_s: float = DEVIN_DEFAULTS["poll_interval_s"],
        max_polls: int = DEVIN_DEFAULTS["max_polls"],
        max_acu_limit: int | None = None,
        opener: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ProviderUnavailable("no Devin API key")
        self._headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
        self._bots = frozenset(bots)
        self._base = base_url.rstrip("/")
        self._create_path = create_path
        self._status_path = status_path
        self._interval = float(poll_interval_s)
        self._max_polls = int(max_polls)
        self._acu = max_acu_limit
        self._open = opener or _default_opener
        self._sleep = sleep

    def complete(self, req: LLMRequest, model: str) -> LLMResponse:
        if req.bot not in self._bots:
            raise ProviderError(f"bot {req.bot!r} is not enabled for Devin")
        session_id = self._create(req)
        status, structured = self._poll(session_id)
        return LLMResponse(
            text=json.dumps(structured, sort_keys=True, ensure_ascii=False),
            structured=structured, model=f"devin:{model}", tokens_in=0, tokens_out=0,
            cache_read=0, cache_write=0, usd=0.0, stop_reason=status,
        )

    def _create(self, req: LLMRequest) -> str:
        body: dict[str, Any] = {
            "prompt": devin_prompt(req),
            "title": f"homunculus {req.bot}",
            "tags": ["homunculus", req.bot],
            "idempotent": True,
        }
        if self._acu is not None:
            body["max_acu_limit"] = int(self._acu)
        data = _send(self._open, _request(self._base + self._create_path, self._headers, body))
        session_id = data.get("session_id")
        if not session_id:
            raise ProviderError("Devin returned no session id")
        return str(session_id)

    def _poll(self, session_id: str) -> tuple[str, Mapping[str, Any]]:
        url = self._base + self._status_path.format(session_id=session_id)
        for attempt in range(self._max_polls):
            if attempt:
                self._sleep(self._interval)
            data = _send(self._open, _request(url, self._headers))
            status = str(data.get("status_enum") or data.get("status") or "")
            if status in _DEVIN_FAILED:
                raise ProviderError(f"Devin session ended as {status!r}")
            structured = _devin_structured(data.get("structured_output"))
            if status in _DEVIN_DONE and structured is not None:
                return status, structured
            if status in _DEVIN_DONE:
                raise SchemaViolation(f"Devin session {status!r} without structured output")
        raise ProviderUnavailable("Devin session did not finish in time")

    def submit_batch(self, items: Sequence[tuple[str, LLMRequest]], model: str) -> str:
        raise ProviderUnavailable("Devin has no batch mode")

    def batch_results(self, batch_id: str) -> dict[str, LLMResponse] | None:
        raise ProviderUnavailable("Devin has no batch mode")


def devin_from_config(
    config: Mapping[str, Any] | None, env: Mapping[str, str], **kwargs: Any,
) -> DevinProvider | None:
    """DevinProvider from roster.json's optional 'devin' block, or None when disabled.

    kwargs (opener, sleep) are passed through for tests.
    """
    cfg = {**DEVIN_DEFAULTS, **(config or {})}
    key = env.get(str(cfg["api_key_env"]), "")
    if not cfg["enabled"] or not key or not cfg["bots"]:
        return None
    return DevinProvider(
        key, bots=tuple(cfg["bots"]), base_url=cfg["base_url"], create_path=cfg["create_path"],
        status_path=cfg["status_path"], poll_interval_s=cfg["poll_interval_s"],
        max_polls=cfg["max_polls"], max_acu_limit=cfg["max_acu_limit"], **kwargs,
    )


# --------------------------------------------------------------------------
# Fake provider


def _minimal_object(schema: Mapping[str, Any]) -> dict:
    props = schema.get("properties") or {}
    return {k: minimal_instance(props.get(k, {})) for k in schema.get("required", ())}


def _minimal_string(schema: Mapping[str, Any]) -> str:
    return "x" * int(schema.get("minLength", 0))


def _minimal_number(schema: Mapping[str, Any]) -> int | float:
    return schema.get("minimum", 0)


def _minimal_array(schema: Mapping[str, Any]) -> list:
    return [minimal_instance(schema.get("items", {})) for _ in range(int(schema.get("minItems", 0)))]


_MINIMAL: dict[str, Callable[[Mapping[str, Any]], Any]] = {
    "object": _minimal_object,
    "array": _minimal_array,
    "string": _minimal_string,
    "integer": _minimal_number,
    "number": _minimal_number,
    "boolean": lambda _s: False,
    "null": lambda _s: None,
}


def minimal_instance(schema: Mapping[str, Any]) -> Any:
    """Smallest value valid under the validate_json subset (deterministic)."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type", "object" if "properties" in schema else "null")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    return _MINIMAL[kind](schema)


def _fake_text(value: Any) -> str:
    return "" if value is None else json.dumps(value, sort_keys=True, ensure_ascii=False)


class FakeProvider:
    """Deterministic provider for tests and --dry-run.

    fixtures[bot] may be a mapping (structured reply), a string (raw text), an
    LLMResponse, an exception instance (raised) or a callable(req) returning
    any of these. Without a fixture the reply is minimal_instance(output_schema).
    """

    name = "fake"

    def __init__(self, fixtures: Mapping[str, Any] | None = None) -> None:
        self._fixtures = dict(fixtures or {})
        self.calls: list[tuple[LLMRequest, str]] = []
        self._batches: dict[str, dict[str, LLMResponse]] = {}

    def complete(self, req: LLMRequest, model: str) -> LLMResponse:
        self.calls.append((req, model))
        fixture = self._fixtures.get(req.bot)
        if callable(fixture):
            fixture = fixture(req)
        return self._respond(fixture, req, model)

    def _respond(self, fixture: Any, req: LLMRequest, model: str) -> LLMResponse:
        if isinstance(fixture, BaseException):
            raise fixture
        if isinstance(fixture, LLMResponse):
            return fixture
        if fixture is None and req.output_schema is not None:
            fixture = minimal_instance(req.output_schema)
        text = fixture if isinstance(fixture, str) else _fake_text(fixture)
        structured = None
        if req.output_schema is not None:
            structured = fixture if isinstance(fixture, Mapping) else _parse_structured(text)
        return LLMResponse(text=text, structured=structured, model=model, tokens_in=0, tokens_out=0,
                           cache_read=0, cache_write=0, usd=0.0, stop_reason="end_turn")

    def submit_batch(self, items: Sequence[tuple[str, LLMRequest]], model: str) -> str:
        batch_id = f"fake-batch-{len(self._batches) + 1}"
        self._batches[batch_id] = {cid: self.complete(req, model) for cid, req in items}
        return batch_id

    def batch_results(self, batch_id: str) -> dict[str, LLMResponse] | None:
        if batch_id not in self._batches:
            raise ProviderError(f"unknown batch {batch_id!r}")
        return dict(self._batches[batch_id])


# --------------------------------------------------------------------------
# Budget

BUDGET_LOG = "ops/budget.jsonl"
_BUDGET_AUTHOR = "pineal"


class BudgetExceeded(Exception):
    """The budget guard refused a call; the job is deferred, never downgraded."""


def _iso_week(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


class Budget:
    def __init__(self, ledger: Ledger, config: Mapping[str, Any], now: Callable[[], datetime]) -> None:
        self._ledger = ledger
        self.cap = float(config.get("weekly_cap_usd", 40.0))
        self.reduce_at = float(config.get("reduce_at", 0.8))
        self.prices: Mapping[str, Mapping[str, float]] = config.get("prices", {})
        self._now = now

    def _entries(self) -> list[dict]:
        return self._ledger.read_jsonl(BUDGET_LOG)

    def spent_week(self) -> float:
        week = _iso_week(self._now())
        return sum(float(e.get("usd", 0)) for e in self._entries() if e.get("week") == week)

    def spent_today(self, bot: str) -> float:
        day = self._now().date().isoformat()
        rows = (e for e in self._entries() if e.get("date") == day and e.get("bot") == bot)
        return sum(float(e.get("usd", 0)) for e in rows)

    def level(self) -> str:
        """'normal' below reduce_at of the weekly cap, 'reduced' below the cap, else 'core_only'."""
        if self.cap <= 0:
            return "core_only"
        share = self.spent_week() / self.cap
        if share >= 1.0:
            return "core_only"
        return "reduced" if share >= self.reduce_at else "normal"

    def allow(self, bot: BotSpec, est_usd: float) -> bool:
        if self._ledger.halted() or bot.provider == "none":
            return False
        level = self.level()
        if level == "reduced" and bot.name in SCOUTS:
            return False
        over_cap = level == "core_only" or self.spent_week() + est_usd > self.cap
        if over_cap and bot.name not in CORE_BOTS:
            return False
        return self.spent_today(bot.name) + est_usd <= bot.daily_usd_cap

    def record(self, bot: str, resp: LLMResponse, run_id: str) -> None:
        moment = self._now()
        self._ledger.append_jsonl(BUDGET_LOG, {
            "ts": moment.isoformat(), "date": moment.date().isoformat(), "week": _iso_week(moment),
            "bot": bot, "run_id": run_id, "model": resp.model, "tokens_in": resp.tokens_in,
            "tokens_out": resp.tokens_out, "cache_read": resp.cache_read,
            "cache_write": resp.cache_write, "usd": resp.usd,
        }, author=_BUDGET_AUTHOR)


def estimate_usd(req: LLMRequest, model: str, prices: Mapping[str, Mapping[str, float]]) -> float:
    """Upper-bound guess before a call: about 4 characters per input token, full max_tokens out."""
    if model not in prices:
        return 0.0
    chars = len(req.system) + len(json.dumps([dict(m) for m in req.messages], ensure_ascii=False))
    usage = {"input_tokens": math.ceil(chars / 4), "output_tokens": req.max_tokens}
    return price(model, usage, prices)


# --------------------------------------------------------------------------
# call


def _complete(providers: Mapping[str, Provider], name: str | None, req: LLMRequest, model: str) -> LLMResponse:
    provider = providers.get(name or "")
    if provider is None:
        raise ProviderUnavailable(f"provider {name!r} is not configured")
    return provider.complete(req, model)


def _alt_allowed(bot: BotSpec) -> bool:
    return bool(bot.alt_provider and bot.alt_model) and MODEL_TIERS.get(bot.alt_model or "") == bot.tier


def _effective(bot: BotSpec, req: LLMRequest, budget: Budget) -> LLMRequest:
    """At the reduced budget level the coaches drop to effort low."""
    if bot.name in COACHES and req.effort != "low" and budget.level() == "reduced":
        return dataclasses.replace(req, effort="low")
    return req


def _check_structured(req: LLMRequest, resp: LLMResponse) -> None:
    if req.output_schema is None:
        return
    if resp.structured is None:
        raise SchemaViolation("reply has no structured output")
    errors = validate_json(resp.structured, req.output_schema)
    if errors:
        raise SchemaViolation("; ".join(errors[:10]))


def call(bot: BotSpec, req: LLMRequest, providers: Mapping[str, Provider], budget: Budget, run_id: str) -> LLMResponse:
    """One budgeted, recorded, validated bot turn.

    The alternative model runs only on ProviderUnavailable and only when it sits
    in the bot's own tier; otherwise the error propagates and the job defers.
    """
    if bot.provider == "none":
        raise ProviderError(f"{bot.name} is a code component, not an LLM bot")
    if not budget.allow(bot, estimate_usd(req, bot.model, budget.prices)):
        raise BudgetExceeded(f"budget refuses {bot.name} at level {budget.level()}")
    req = _effective(bot, req, budget)
    try:
        resp = _complete(providers, bot.provider, req, bot.model)
    except ProviderUnavailable:
        if not _alt_allowed(bot):
            raise
        resp = _complete(providers, bot.alt_provider, req, bot.alt_model or "")
    budget.record(bot.name, resp, run_id)
    _check_structured(req, resp)
    return resp


# --------------------------------------------------------------------------
# JSON Schema subset validator


def _is_integer(v: Any) -> bool:
    return (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer())


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": _is_integer,
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _json_equal(a: Any, b: Any) -> bool:
    return a == b and isinstance(a, bool) == isinstance(b, bool)


def _type_error(inst: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    kind = schema.get("type")
    if kind is None:
        return []
    kinds = kind if isinstance(kind, list) else [kind]
    if any(_TYPE_CHECKS[k](inst) for k in kinds):
        return []
    return [f"{path or '/'}: expected {'|'.join(kinds)}"]


def _enum_errors(inst: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    if "enum" not in schema or any(_json_equal(inst, e) for e in schema["enum"]):
        return []
    return [f"{path or '/'}: {inst!r} not in enum"]


def _object_errors(inst: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    if not isinstance(inst, dict):
        return []
    props = schema.get("properties") or {}
    errors = [f"{path}/{k}: required" for k in schema.get("required", ()) if k not in inst]
    extra = schema.get("additionalProperties", True)
    for key, value in inst.items():
        sub = props.get(key, extra if isinstance(extra, Mapping) else None)
        if sub is not None:
            errors += _validate(value, sub, f"{path}/{key}")
        elif extra is False:
            errors.append(f"{path}/{key}: additional property")
    return errors


def _bound_errors(size: float, schema: Mapping[str, Any], path: str, low: str, high: str) -> list[str]:
    errors = []
    if low in schema and size < schema[low]:
        errors.append(f"{path or '/'}: below {low} {schema[low]}")
    if high in schema and size > schema[high]:
        errors.append(f"{path or '/'}: above {high} {schema[high]}")
    return errors


def _array_errors(inst: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    if not isinstance(inst, list):
        return []
    errors = _bound_errors(len(inst), schema, path, "minItems", "maxItems")
    items = schema.get("items")
    if isinstance(items, Mapping):
        for i, value in enumerate(inst):
            errors += _validate(value, items, f"{path}/{i}")
    return errors


def _scalar_errors(inst: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    if isinstance(inst, str):
        return _bound_errors(len(inst), schema, path, "minLength", "maxLength")
    if _TYPE_CHECKS["number"](inst):
        return _bound_errors(inst, schema, path, "minimum", "maximum")
    return []


def _validate(inst: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    errors = _type_error(inst, schema, path)
    if errors:
        return errors
    for check in (_enum_errors, _object_errors, _array_errors, _scalar_errors):
        errors += check(inst, schema, path)
    return errors


def validate_json(instance: Any, schema: Mapping[str, Any]) -> list[str]:
    """Errors as 'json/pointer: reason'; empty means valid.

    Subset: type, enum, required, properties, items, additionalProperties,
    minimum/maximum, minLength/maxLength, minItems/maxItems.
    """
    return _validate(instance, schema, "")


# --------------------------------------------------------------------------
# Message envelopes

MESSAGE_KINDS: tuple[str, ...] = (
    "evidence.candidate", "evidence.appraisal", "data.anomaly", "checkin.received",
    "goal.update", "review.draft", "challenge", "rebuttal", "ruling", "plan.ratified",
    "plan.rollback", "ask.owner", "alert",
)
MESSAGE_SCHEMA = 1
PARTIES = frozenset(KNOWN_BOTS) | {"owner"}
SEVERITIES = frozenset({"blocker", "major", "minor"})
_MSG_ID_RE = re.compile(r"^m-[0-9a-f]{12}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_STR_FIELDS = ("thread", "run_id", "model", "body")


def message_id(sender: str, kind: str, thread: str, round_: int, data_sha: str) -> str:
    digest = hashlib.sha256(f"{sender}|{kind}|{thread}|{round_}|{data_sha}".encode("utf-8")).hexdigest()
    return "m-" + digest[:12]


def make_message(
    *,
    sender: str,
    to: Sequence[str],
    kind: str,
    thread: str,
    round_: int,
    max_rounds: int,
    refs: Sequence[str],
    data_sha: str,
    run_id: str,
    model: str,
    body: str,
    items: Sequence[Mapping] = (),
    cc: Sequence[str] = (),
    created_at: str,
) -> dict:
    """Envelope with a deterministic id; raises ValueError if it fails validate_message."""
    msg = {
        "schema": MESSAGE_SCHEMA,
        "id": message_id(sender, kind, thread, round_, data_sha),
        "from": sender, "to": list(to), "cc": list(cc), "kind": kind, "thread": thread,
        "round": round_, "max_rounds": max_rounds, "created_at": created_at, "refs": list(refs),
        "data_sha": data_sha, "run_id": run_id, "model": model, "body": body,
        "items": [dict(i) for i in items],
    }
    errors = validate_message(msg)
    if errors:
        raise ValueError("invalid message: " + "; ".join(errors))
    return msg


def _party_errors(msg: Mapping[str, Any]) -> list[str]:
    errors = [] if msg.get("from") in PARTIES else [f"unknown sender {msg.get('from')!r}"]
    for field in ("to", "cc"):
        names = msg.get(field)
        if not isinstance(names, list) or any(n not in PARTIES for n in names):
            errors.append(f"invalid {field}")
    if not msg.get("to"):
        errors.append("no recipient")
    return errors


def _round_errors(msg: Mapping[str, Any]) -> list[str]:
    rnd, top = msg.get("round"), msg.get("max_rounds")
    if not (_is_int(rnd) and _is_int(top)) or not 0 <= rnd <= top:
        return ["round must be an int in 0..max_rounds"]
    return []


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _format_errors(msg: Mapping[str, Any]) -> list[str]:
    checks = (
        (msg.get("schema") == MESSAGE_SCHEMA, "unsupported schema"),
        (bool(_MSG_ID_RE.match(str(msg.get("id", "")))), "invalid id"),
        (msg.get("kind") in MESSAGE_KINDS, f"unknown kind {msg.get('kind')!r}"),
        (bool(_SHA_RE.match(str(msg.get("data_sha", "")))), "data_sha must be a sha256 hex digest"),
        (bool(_UTC_RE.match(str(msg.get("created_at", "")))), "created_at must be UTC 'YYYY-MM-DDTHH:MM:SSZ'"),
        (all(isinstance(msg.get(f), str) for f in _STR_FIELDS), "thread, run_id, model and body must be strings"),
        (isinstance(msg.get("refs"), list) and all(isinstance(r, str) for r in msg["refs"]), "refs must be strings"),
    )
    return [reason for ok, reason in checks if not ok]


def _item_errors(msg: Mapping[str, Any]) -> list[str]:
    items = msg.get("items")
    if not isinstance(items, list) or not all(isinstance(i, Mapping) for i in items):
        return ["items must be a list of objects"]
    bad = [i for i in items if "severity" in i and i["severity"] not in SEVERITIES]
    return ["invalid item severity"] if bad else []


def validate_message(msg: Mapping[str, Any]) -> list[str]:
    """Problems with an envelope; empty means valid."""
    if not isinstance(msg, Mapping):
        return ["message is not an object"]
    return _format_errors(msg) + _party_errors(msg) + _round_errors(msg) + _item_errors(msg)


def commit_trailers(msg: Mapping[str, Any], resp: LLMResponse | None) -> dict[str, str]:
    """Git trailers for the commit that lands this message."""
    return {
        "Msg-Id": str(msg["id"]),
        "Run-Id": str(msg["run_id"]),
        "Model": resp.model if resp else str(msg["model"]),
        "Tokens-In": str(resp.tokens_in if resp else 0),
        "Tokens-Out": str(resp.tokens_out if resp else 0),
        "Usd": f"{resp.usd if resp else 0.0:.6f}",
        "Kind": str(msg["kind"]),
    }

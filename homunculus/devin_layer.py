"""Devin Bots layer: what a Devin Bot session runs, and the Automation specs that start it.

Four Devin Bots (Cerebellum, Sartorius, Vagus, Hippocampus) are the chat shell.
Every judgement call still goes through homunculus: a session runs a command
from this module, which calls the exact model from ``roster.json`` through the
providers in ``agents`` with the API key the Bot holds as a secret. Devin's own
model never grades, scores or plans. See ``devin/README.md``.

- ``run_bot``: one validated bot turn written through ``Ledger``.
- ``run_task``: one Automation's Pineal jobs, with the owner texts captured so
  the Bot can post them verbatim in its chat.
- ``reply``: an owner message from the Bot's chat, routed like ``POST /message``.
- ``ingest_stdin``: a Health Auto Export payload from stdin, a file or the text
  Devin puts into a webhook session's prompt, stored and aggregated.
- ``ingest_relay``: the optional relay route of path "a". The webhook carries
  only {object_key, sha256, bytes}; ``fetch_relay_object`` downloads the body
  from the relay (``relay/``), checks size and digest, and the object is
  deleted after ingest. The body never passes through a model prompt.
- ``export_automations``: Automation specs (RRULE, time zone, webhook, ACU
  limit, network allowlist, secret names, prompt) per path.
- ``scout_poll_script``: the stdlib script for a scout's Automation script step.

Path "a" (serverless) starts every session on a fresh Devin machine, so the
SQLite store is empty. ``restore_store`` rebuilds it from the ledger: the
parsed daily summaries in ``data/summaries/``, the daily rows in ``data/daily/``
and the check-ins in ``checkins/``. Path "b" runs on the owner's box (directly
or as a Devin Outpost) and keeps its SQLite store.

Devin field names in the specs are this repo's own format and are mapped by
hand onto the Automations form; Devin's API names for them are UNVERIFIED.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import io
import json
import os
import re
import string
import sys
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homunculus import agents, guardrails, ingest, ledger_sync, metrics, pineal, scouts, weekly
from homunculus.store import (
    CheckinItem, DailyValue, HealthEvent, Ledger, PathNotAllowed, Sample, SleepNight, SleepSegment, Store, Workout,
)

WRITER = "pineal"
DEVIN_BOTS = ("cerebellum", "sartorius", "vagus", "hippocampus")
PATHS = ("a", "b")
PROMPTS_DIR = weekly.PROMPTS_DIR
ENGINE_ROSTER = Path(__file__).resolve().parent.parent / "config" / "roster.json"
DEFAULT_TZ = "UTC"

MAX_INPUT_BYTES = 1024 * 1024  # a webhook carries at most 200 KB; full exports go through import-history
WEBHOOK_MAX_BYTES = 200 * 1024
# --file holds a webhook prompt a Bot wrote out: the body plus a little prompt text. Anything larger is not
# a faithful copy of one webhook, so it is refused before parsing.
FILE_MAX_BYTES = WEBHOOK_MAX_BYTES + 16 * 1024
MAX_JSON_STARTS = 200
SUMMARY_DIR = "data/summaries"
DAILY_DIR = "data/daily"
CHECKIN_DIR = "checkins"
REPLAY_DAYS = 120  # covers the 60-day HRV baseline behind its 7-day gap, with room
HAE_SECTIONS = frozenset({"metrics", "workouts", "heartRateNotifications", "ecg", "symptoms", "stateOfMind"})
WRAPPER_KEYS = ("payload", "body")

_ROW_TYPES: Mapping[str, type] = {
    "samples": Sample, "sleep_segments": SleepSegment, "sleep_nights": SleepNight,
    "workouts": Workout, "events": HealthEvent,
}
_ROW_WRITERS: Mapping[str, str] = {
    "samples": "upsert_samples", "sleep_segments": "upsert_sleep_segments",
    "sleep_nights": "upsert_sleep_nights", "workouts": "upsert_workouts", "events": "insert_events",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc_z(moment: datetime) -> str:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_git(ledger: Ledger) -> bool:
    return (ledger.root / ".git").is_dir()


def _commit(ledger: Ledger, author: str, message: str, trailers: Mapping[str, str]) -> str | None:
    """Commit pending ledger writes when the ledger is a git repo; None otherwise."""
    if not _is_git(ledger) or not ledger.pending():
        return None
    return ledger.commit(author=author, message=message, trailers=trailers)


def _code_commit(ledger: Ledger, kind: str, message: str, now: datetime) -> str | None:
    stamp = re.sub(r"[-:]", "", _utc_z(now))
    return _commit(ledger, WRITER, message, {"Kind": kind, "Run-Id": f"devin-{kind}-{stamp}"})


# --------------------------------------------------------------------------
# run_bot: one validated bot turn


@dataclass(frozen=True)
class BotRun:
    path: str
    output: Mapping[str, Any]
    model: str
    usd: float
    commit: str | None


def providers_from_env(
    env: Mapping[str, str], prices: Mapping[str, Mapping[str, float]], opener: Callable | None = None,
) -> dict[str, agents.Provider]:
    """API providers whose key is set. Never Devin: a nested session cannot pin the model."""
    out: dict[str, agents.Provider] = {}
    if env.get("ANTHROPIC_API_KEY"):
        out["anthropic"] = agents.AnthropicProvider(env["ANTHROPIC_API_KEY"], prices, opener)
    if env.get("GEMINI_API_KEY"):
        out["gemini"] = agents.GeminiProvider(env["GEMINI_API_KEY"], prices, opener)
    return out


def _llm_bot(roster: Mapping[str, agents.BotSpec], name: str) -> agents.BotSpec:
    spec = roster.get(name)
    if spec is None:
        raise ValueError(f"unknown bot {name!r}")
    if spec.provider in ("none", "devin"):
        raise agents.ProviderError(f"{name} has provider {spec.provider!r}; run_bot calls an API model")
    return spec


def _check_out_path(spec: agents.BotSpec, rel: str) -> None:
    """Fail before any spend when the bot may not write rel (Ledger enforces it again)."""
    if not any(fnmatchcase(rel, glob) for glob in spec.writes):
        raise PathNotAllowed(f"{spec.name!r} may not write {rel!r}")


def _bot_request(spec: agents.BotSpec, prompts_dir: Path, context: Mapping[str, Any], schema: Mapping[str, Any],
                 untrusted: Mapping[str, str] | None) -> agents.LLMRequest:
    system, user = agents.render_prompt(spec, prompts_dir, context, untrusted)
    return agents.LLMRequest(bot=spec.name, system=system, messages=({"role": "user", "content": user},),
                             output_schema=schema, max_tokens=spec.max_tokens, effort=spec.effort)


def _check_vocabulary(spec: agents.BotSpec, output: Mapping[str, Any]) -> None:
    if not spec.user_facing:
        return
    hits = guardrails.scan_text(json.dumps(output, ensure_ascii=False))
    if hits:
        raise agents.SchemaViolation(f"{spec.name} output holds banned vocabulary: {', '.join(sorted(hits))}")


def _usage_trailers(run_id: str, resp: agents.LLMResponse) -> dict[str, str]:
    return {"Run-Id": run_id, "Model": resp.model, "Tokens-In": str(resp.tokens_in),
            "Tokens-Out": str(resp.tokens_out), "Usd": f"{resp.usd:.6f}", "Kind": "run-bot"}


def run_bot(
    bot: str,
    ledger: Ledger,
    *,
    roster: Mapping[str, agents.BotSpec],
    budget: agents.Budget,
    context: Mapping[str, Any],
    schema: Mapping[str, Any],
    out_rel: str,
    run_id: str,
    providers: Mapping[str, agents.Provider] | None = None,
    env: Mapping[str, str] | None = None,
    untrusted: Mapping[str, str] | None = None,
    prompts_dir: Path = PROMPTS_DIR,
    now: Callable[[], datetime] = _utcnow,
) -> BotRun:
    """Render the bot's prompt, call its roster model, validate, write out_rel as the bot, commit.

    providers default to providers_from_env(env); agents.call enforces the
    budget and the output schema. User-facing output must pass scan_text.
    """
    spec = _llm_bot(roster, bot)
    _check_out_path(spec, out_rel)
    if providers is None:
        providers = providers_from_env(env or {}, budget.prices)
    req = _bot_request(spec, Path(prompts_dir), context, schema, untrusted)
    resp = agents.call(spec, req, providers, budget, run_id)
    output = dict(resp.structured or {})
    _check_vocabulary(spec, output)
    record = {"schema": 1, "bot": bot, "run_id": run_id, "model": resp.model,
              "created_at": _utc_z(now()), "output": output}
    ledger.write_json(out_rel, record, author=bot)
    sha = _commit(ledger, bot, f"{bot}: run-bot {run_id}", _usage_trailers(run_id, resp))
    return BotRun(out_rel, output, resp.model, resp.usd, sha)


# --------------------------------------------------------------------------
# Store persistence for path "a"


def _stem_day(rel: str) -> str:
    digits = re.sub(r"\D", "", Path(rel).stem)[:8]
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if len(digits) == 8 else ""


def _recent(ledger: Ledger, pattern: str, cutoff: str) -> list[str]:
    return [rel for rel in ledger.glob(pattern) if _stem_day(rel) >= cutoff]


def _row(cls: type, data: Any) -> Any:
    if not isinstance(data, Mapping):
        return None
    fields = dict(data)
    if cls is Workout:
        fields["hr_series"] = tuple(tuple(p) for p in fields.get("hr_series") or ())
    try:
        return cls(**fields)
    except (TypeError, ValueError):
        return None


def _rows(cls: type, items: Any) -> list:
    if not isinstance(items, list):
        return []
    rows = (_row(cls, d) for d in items)
    return [r for r in rows if r is not None]


def _replay_summary(store: Store, doc: Any) -> int:
    tables = doc.get("rows") if isinstance(doc, Mapping) else None
    if not isinstance(tables, Mapping):
        return 0
    count = 0
    for name, cls in _ROW_TYPES.items():
        rows = _rows(cls, tables.get(name))
        count += getattr(store, _ROW_WRITERS[name])(rows) if rows else 0
    return count


def _daily_row(day: str, key: str, value: Any) -> DailyValue | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return DailyValue(day, str(key), value.get("value"), str(value.get("source") or "other"),
                          int(value.get("n") or 0), str(value.get("quality")))
    except (TypeError, ValueError):
        return None


def _daily_rows(doc: Any) -> list[DailyValue]:
    """data/daily docs: cli writes {'values': ...}, pineal writes {'rows': ...}."""
    if not isinstance(doc, Mapping) or not isinstance(doc.get("date"), str):
        return []
    values = doc.get("values") or doc.get("rows") or {}
    rows = (_daily_row(doc["date"], k, v) for k, v in values.items()) if isinstance(values, Mapping) else ()
    return [r for r in rows if r is not None]


def _checkin_rows(doc: Any) -> list[CheckinItem]:
    return _rows(CheckinItem, doc.get("items")) if isinstance(doc, Mapping) else []


def restore_store(store: Store, ledger: Ledger, now: datetime) -> int:
    """Rebuild a fresh session's SQLite store from the ledger (last REPLAY_DAYS days); rows written."""
    cutoff = (now.date() - timedelta(days=REPLAY_DAYS)).isoformat()
    count = 0
    for rel in _recent(ledger, f"{SUMMARY_DIR}/*/*.json", cutoff):
        count += _replay_summary(store, ledger.read_json(rel))
    for rel in _recent(ledger, f"{DAILY_DIR}/*/*.json", cutoff):
        rows = _daily_rows(ledger.read_json(rel))
        count += store.put_daily(rows) if rows else 0
    for rel in _recent(ledger, f"{CHECKIN_DIR}/*/*.json", cutoff):
        rows = _checkin_rows(ledger.read_json(rel))
        count += store.put_checkins(rows) if rows else 0
    return count


def save_summary(ledger: Ledger, batch: ingest.ParsedBatch, now: datetime) -> str | None:
    """The parsed rows of one payload (sources normalized, GPS and device names already gone)."""
    tables = {name: [asdict(r) for r in getattr(batch, name)] for name in _ROW_TYPES}
    if not any(tables.values()):
        return None
    moment = now.astimezone(timezone.utc) if now.tzinfo else now
    rel = f"{SUMMARY_DIR}/{moment:%Y}/{moment:%Y%m%dT%H%M%SZ}.json"
    ledger.write_json(rel, {"schema": 1, "rows": tables}, author=WRITER)
    return rel


def save_checkins(store: Store, ledger: Ledger, days: Sequence[str]) -> list[str]:
    """checkins/<YYYY>/<day>.json for each day with check-ins, so the next session sees them."""
    written = []
    for day in days:
        items = store.checkins(day, (Date.fromisoformat(day) + timedelta(days=1)).isoformat())
        if items:
            rel = f"{CHECKIN_DIR}/{day[:4]}/{day}.json"
            ledger.write_json(rel, {"date": day, "items": [asdict(c) for c in items]}, author=WRITER)
            written.append(rel)
    return written


# --------------------------------------------------------------------------
# ingest_stdin


def _loads(text: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _is_payload(obj: Any) -> bool:
    root = obj.get("data", obj) if isinstance(obj, Mapping) else None
    return isinstance(root, Mapping) and bool(HAE_SECTIONS.intersection(root))


def _unwrap(obj: Any, depth: int = 0) -> Mapping[str, Any] | None:
    """The payload itself, or one nested under 'payload' or 'body' (object or JSON string)."""
    if _is_payload(obj):
        return obj
    if not isinstance(obj, Mapping) or depth >= 2:
        return None
    for key in WRAPPER_KEYS:
        inner = obj.get(key)
        found = _unwrap(_loads(inner) if isinstance(inner, str) else inner, depth + 1)
        if found is not None:
            return found
    return None


def _first_json(text: str, find: Callable[[Any], Any]) -> Any:
    """The first find(obj) that is not None over the JSON objects starting inside free text."""
    decoder = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"\{", text)][:MAX_JSON_STARTS]
    for start in starts:
        try:
            obj, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        found = find(obj)
        if found is not None:
            return found
    return None


def _embedded(text: str) -> Mapping[str, Any] | None:
    """The first payload inside free text, such as a session prompt around the webhook body."""
    return _first_json(text, _unwrap)


def extract_payload(text: str) -> Mapping[str, Any]:
    """A Health Auto Export payload from bare JSON, a wrapper object or surrounding prompt text."""
    found = _unwrap(_loads(text.strip())) or _embedded(text)
    if found is None:
        raise ValueError("no Health Auto Export payload found in the input")
    return found


def read_input(stream: TextIO | None = None, path: str | Path | None = None) -> str:
    """Text from path (refused above FILE_MAX_BYTES), else from stream (default stdin, above MAX_INPUT_BYTES)."""
    if path is not None:
        source = Path(path)
        if source.stat().st_size > FILE_MAX_BYTES:
            raise ValueError(f"{source} is larger than {FILE_MAX_BYTES} bytes; use --stdin or import-history")
        return source.read_text(encoding="utf-8")
    text = (stream or sys.stdin).read(MAX_INPUT_BYTES + 1)
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError(f"input is larger than {MAX_INPUT_BYTES} bytes; use import-history")
    return text


def ingest_stdin(
    store: Store,
    ledger: Ledger,
    cfg: Any,
    *,
    stream: TextIO | None = None,
    path: str | Path | None = None,
    source_map: Mapping[str, str] | None = None,
    now: datetime | None = None,
    api_sources: frozenset[str] = frozenset(),
) -> dict:
    """Parse a payload with ingest.parse_payload, store it, write daily rows, baselines and freshness.

    The raw text is never archived. The parsed rows go to data/summaries/ so a
    later session on a fresh machine can rebuild the store. Returns counts
    only, never values, so the session has nothing to echo into chat.
    """
    from homunculus import cli  # lazy: cli wires this module and owns the shared pipeline helpers

    moment = now or _utcnow()
    batch = ingest.parse_payload(extract_payload(read_input(stream, path)), source_map or {}, api_sources)
    restored = restore_store(store, ledger, moment)
    counts = cli.store_batch(store, batch)
    summary = save_summary(ledger, batch, moment)
    days = cli.batch_days(batch)
    cli.finalize_days(store, ledger, cfg, days, moment)
    sha = _code_commit(ledger, "devin-ingest", f"Ingest Health Auto Export summary: {len(days)} days", moment)
    return {"counts": counts, "days": days, "skipped": dict(batch.skipped), "restored": restored,
            "summary": summary, "commit": sha}


# --------------------------------------------------------------------------
# Relay route (optional, path a): the webhook carries a pointer, code fetches the body

_RELAY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELAY_TIMEOUT_S = 60


class RelayError(ValueError):
    """The relay object is missing, malformed, or does not match its pointer."""


@dataclass(frozen=True)
class RelayPointer:
    object_key: str
    sha256: str
    bytes: int


def _size_ok(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_INPUT_BYTES


def _as_pointer(obj: Any) -> RelayPointer | None:
    if not isinstance(obj, Mapping) or not _size_ok(obj.get("bytes")):
        return None
    key, digest = obj.get("object_key"), obj.get("sha256")
    if not isinstance(key, str) or not isinstance(digest, str):
        return None
    if not _RELAY_KEY_RE.match(key) or not _SHA256_RE.match(digest):
        return None
    return RelayPointer(key, digest, obj["bytes"])


def _find_pointer(obj: Any, depth: int = 0) -> RelayPointer | None:
    """The pointer itself, or one nested under 'payload' or 'body' (object or JSON string)."""
    found = _as_pointer(obj)
    if found is not None or not isinstance(obj, Mapping) or depth >= 2:
        return found
    for key in WRAPPER_KEYS:
        inner = obj.get(key)
        found = _find_pointer(_loads(inner) if isinstance(inner, str) else inner, depth + 1)
        if found is not None:
            return found
    return None


def extract_pointer(text: str) -> RelayPointer:
    """The relay pointer {object_key, sha256, bytes} from bare JSON, a wrapper or surrounding prompt text."""
    found = _find_pointer(_loads(text.strip())) or _first_json(text, _find_pointer)
    if found is None:
        raise RelayError("no relay pointer (object_key, sha256, bytes) found in the input")
    return found


def _relay_request(base_url: str, token: str, key: str, method: str) -> urllib.request.Request:
    if not base_url.startswith("https://"):
        raise RelayError(f"{RELAY_URL_NAME} must be an https URL")
    if not token:
        raise RelayError(f"{RELAY_TOKEN_NAME} is not set")
    url = f"{base_url.rstrip('/')}/objects/{key}"
    return urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {token}"})


def _relay_open(req: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(req, timeout=RELAY_TIMEOUT_S) as resp:  # noqa: S310 (https checked)
        return resp.read(MAX_INPUT_BYTES + 1)


def fetch_relay_object(pointer: RelayPointer, base_url: str, token: str,
                       opener: Callable[[urllib.request.Request], bytes] | None = None) -> bytes:
    """GET <base_url>/objects/<key> with the bearer token; the body must match the pointer's size and sha256."""
    body = (opener or _relay_open)(_relay_request(base_url, token, pointer.object_key, "GET"))
    if len(body) != pointer.bytes:
        raise RelayError(f"relay object has {len(body)} bytes, the pointer says {pointer.bytes}")
    if hashlib.sha256(body).hexdigest() != pointer.sha256:
        raise RelayError("relay object does not match the pointer's sha256")
    return body


def delete_relay_object(pointer: RelayPointer, base_url: str, token: str,
                        opener: Callable[[urllib.request.Request], bytes] | None = None) -> bool:
    """DELETE the object; False when the relay cannot be reached (its TTL removes the object anyway)."""
    try:
        (opener or _relay_open)(_relay_request(base_url, token, pointer.object_key, "DELETE"))
    except OSError:
        return False
    return True


def ingest_relay(
    store: Store,
    ledger: Ledger,
    cfg: Any,
    text: str,
    *,
    base_url: str,
    token: str,
    opener: Callable[[urllib.request.Request], bytes] | None = None,
    source_map: Mapping[str, str] | None = None,
    now: datetime | None = None,
    api_sources: frozenset[str] = frozenset(),
) -> dict:
    """Pointer from text, verified body from the relay, ingest_stdin on it, then delete the object.

    A failed download, size or digest check or ingest raises and leaves the
    object for a retry; the Worker's TTL removes it in any case.
    """
    pointer = extract_pointer(text)
    body = fetch_relay_object(pointer, base_url, token, opener)
    out = ingest_stdin(store, ledger, cfg, stream=io.StringIO(body.decode("utf-8")), source_map=source_map,
                       now=now, api_sources=api_sources)
    return {**out, "deleted": delete_relay_object(pointer, base_url, token, opener)}


# --------------------------------------------------------------------------
# push-ledger (path a): parallel sessions, derived snapshots rebuilt after a rebase


def _stem_days(paths: Sequence[str], prefix: str) -> list[str]:
    return sorted({d for d in (_stem_day(p) for p in paths if p.startswith(prefix + "/")) if d})


def _rebuild_baselines(store: Store, ledger: Ledger, cfg: Any, rel: str) -> None:
    """data/baselines/<week>.json for the week's latest day with a daily file."""
    from homunculus import cli

    week = Path(rel).stem
    days = [d for d in _stem_days(ledger.glob(f"{DAILY_DIR}/*/*.json"), DAILY_DIR) if cli._iso_week(d) == week]
    if days:
        ledger.write_json(rel, metrics.baselines(store, days[-1], cfg), author=WRITER)


def rebuild_derived(store: Store, ledger: Ledger, cfg: Any, paths: Sequence[str], now: datetime) -> str | None:
    """After a rebase kept upstream's snapshots: replay the merged ledger into the session store, rewrite
    the daily rows, baselines, freshness and check-in files the conflict touched, and commit."""
    from homunculus import cli

    restore_store(store, ledger, now)
    days = _stem_days(paths, DAILY_DIR)
    if days:
        cli.finalize_days(store, ledger, cfg, days, now)
    for rel in (p for p in paths if p.startswith("data/baselines/")):
        _rebuild_baselines(store, ledger, cfg, rel)
    if pineal.FRESHNESS_PATH in paths:
        ledger.write_json(pineal.FRESHNESS_PATH, metrics.freshness(store.latest_ts(), now, weekly.FRESHNESS_H),
                          author=WRITER)
    save_checkins(store, ledger, _stem_days(paths, CHECKIN_DIR))
    return _code_commit(ledger, "devin-ledger-merge", "Rebuild snapshots after a parallel session", now)


def push_ledger(store: Store, ledger: Ledger, cfg: Any, now: Callable[[], datetime],
                env: Mapping[str, str] | None = None) -> bool:
    """ledger_sync.push with rebuild_derived; False when the push failed."""
    git = ledger_sync.git_runner(ledger.root, env)
    return ledger_sync.push(git, lambda paths: rebuild_derived(store, ledger, cfg, paths, now()))


def _push_ledger_cmd(env: Mapping[str, str]) -> dict:
    from homunculus import cli  # lazy, as in ingest_stdin

    app = cli.App(cli.build_parser().parse_args(["pending"]), env)
    try:
        if not push_ledger(app.store, app.ledger, app.cfg, app.now):
            raise RuntimeError("ledger push failed: a conflict outside the derived snapshots, or no try succeeded")
    finally:
        app.close()
    return {"pushed": True}


def _clear_hold_cmd(env: Mapping[str, str]) -> dict:
    from homunculus import cli  # lazy, as in ingest_stdin

    app = cli.App(cli.build_parser().parse_args(["pending"]), env)
    try:
        return clear_hold(cli._pineal_context(app, pineal))
    finally:
        app.close()


def _relay_ingest_cmd(file: str, env: Mapping[str, str]) -> dict:
    from homunculus import cli  # lazy, as in ingest_stdin

    app = cli.App(cli.build_parser().parse_args(["ingest", "--file", file]), env)
    try:
        out = ingest_relay(app.store, app.ledger, app.cfg, read_input(path=file),
                           base_url=env.get(RELAY_URL_NAME, ""), token=env.get(RELAY_TOKEN_NAME, ""),
                           source_map=cli._source_map(env), now=app.now(), api_sources=cli._api_sources(env))
    finally:
        app.close()
    return {"counts": out["counts"], "days": len(out["days"]), "skipped": out["skipped"],
            "restored": out["restored"], "commit": out["commit"], "deleted": out["deleted"]}


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """``python -m homunculus.devin_layer relay-ingest --file F``, ``clear-hold`` or ``push-ledger``;
    prints counts only."""
    parser = argparse.ArgumentParser(prog="python -m homunculus.devin_layer")
    parser.add_argument("command", choices=("relay-ingest", "clear-hold", "push-ledger"))
    parser.add_argument("--file", default=None)
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    if args.command == "relay-ingest" and not args.file:
        parser.error("relay-ingest needs --file")
    commands = {"relay-ingest": lambda: _relay_ingest_cmd(args.file, env), "clear-hold": lambda: _clear_hold_cmd(env),
                "push-ledger": lambda: _push_ledger_cmd(env)}
    try:
        out = commands[args.command]()
    except Exception as exc:  # noqa: BLE001 - map every failure to an exit code, as the CLI does
        print(f"homunculus: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(out, sort_keys=True, ensure_ascii=False, default=str), flush=True)
    return 0


# --------------------------------------------------------------------------
# Automations


@dataclass(frozen=True)
class AutomationDef:
    """One Devin Automation. jobs are Pineal job names run by run_task, in order.

    when overrides the schedule as (cadence, 'HH:MM', weekdays); None takes it
    from the first job in pineal.default_jobs().
    """

    name: str
    bot: str
    trigger: str  # schedule | webhook | script
    paths: tuple[str, ...]
    jobs: tuple[str, ...]
    max_acu: int
    when: tuple[str, str, tuple[int, ...]] | None = None
    scout: str | None = None
    note: str = ""
    extra: tuple[str, ...] = ()  # commands after devin-task
    relay: bool = False  # webhook body is a relay pointer, not the payload


# The ingest-health webhook relies on the model copying the payload verbatim; nothing can check that copy.
LLM_COPY_CONFIG = "config/devin.json"
LLM_COPY_OPT_IN = "llm_copy_ingest"


def llm_copy_allowed(ledger: Ledger) -> bool:
    """True only when the owner opted in to the unreliable model-copy ingest in LLM_COPY_CONFIG."""
    cfg = ledger.read_json(LLM_COPY_CONFIG, default=None)
    return isinstance(cfg, Mapping) and cfg.get(LLM_COPY_OPT_IN) is True


_PENDING_NOTE = (
    "For each change in the approve tier that `pending` prints, put one Decision "
    "into Needs You: options approve and veto, approve marked as recommended because the ruling ratified it, "
    "body the reasons line and the path plans/pending/<id>.json. "
    "When the owner answers, run `python -m homunculus approve <id>` or `python -m homunculus veto <id>`."
)
_INGEST_NOTE = (
    "UNRELIABLE, opt-in only: ingest-relay is the supported webhook path. This session's prompt carries the "
    "webhook body, a Health Auto Export daily summary. Do not read, quote or summarize it. Write this whole "
    "prompt unchanged to $HOMUNCULUS_LEDGER/.private/inbox.txt; the ingest command finds the JSON inside it. "
    "A changed digit in that copy would pass unnoticed, so ingest refuses unless the ledger opts in with "
    f"{LLM_COPY_OPT_IN} set to true in {LLM_COPY_CONFIG}."
)
_INGEST_COMMANDS = (
    'mkdir -p "$HOMUNCULUS_LEDGER/.private"',
    'python -m homunculus ingest --llm-copy --file "$HOMUNCULUS_LEDGER/.private/inbox.txt"',
    'rm "$HOMUNCULUS_LEDGER/.private/inbox.txt"',
)
_RELAY_NOTE = (
    "This session's prompt carries a relay pointer: object_key, sha256 and bytes. It holds no health data. "
    "Write this whole prompt unchanged to $HOMUNCULUS_LEDGER/.private/inbox.txt; relay-ingest finds the "
    "pointer inside it, downloads the body, checks size and digest, ingests it and deletes the object."
)
_RELAY_COMMANDS = (
    'mkdir -p "$HOMUNCULUS_LEDGER/.private"',
    'python -m homunculus.devin_layer relay-ingest --file "$HOMUNCULUS_LEDGER/.private/inbox.txt"',
    'rm "$HOMUNCULUS_LEDGER/.private/inbox.txt"',
)
# Path a sessions run in parallel on fresh clones of one ledger. Append-only logs (ops/runs.jsonl,
# ops/budget.jsonl, alerts) get git's union merge in this clone, so a rebase keeps both sides' rows.
# push-ledger (ledger_sync.push) retries on top of whatever another session pushed first and
# rebuilds the derived snapshots (heartbeats, freshness, daily rows, baselines, check-ins) both rewrote.
LEDGER_SYNC_START = (
    'd="$(git -C "$HOMUNCULUS_LEDGER" rev-parse --absolute-git-dir)/info" && mkdir -p "$d" '
    '&& echo "*.jsonl merge=union" >> "$d/attributes"',
    'git -C "$HOMUNCULUS_LEDGER" pull --ff-only',
)
PUSH_ATTEMPTS = ledger_sync.PUSH_ATTEMPTS
LEDGER_PUSH = "python -m homunculus.devin_layer push-ledger"
_SCOUT_NOTE = (
    "The script step found new candidate ids. They only signal that the scout has work; do not open them. "
    "The command fetches, deduplicates and triages through code."
)

AUTOMATIONS: tuple[AutomationDef, ...] = (
    AutomationDef("ingest-health", "hippocampus", "webhook", ("a",), ("red_flags", "freshness"), 1,
                  note=_INGEST_NOTE),
    AutomationDef("ingest-relay", "hippocampus", "webhook", ("a",), ("red_flags", "freshness"), 1,
                  note=_RELAY_NOTE, relay=True),
    AutomationDef("pineal-daily", "hippocampus", "schedule", ("a",),
                  ("finalize_day", "red_flags", "freshness", "budget", "heartbeat", "ratify_due"), 1),
    *(AutomationDef(f"{s}-scout", "hippocampus", "script", ("a",), (s,), 1, scout=s, note=_SCOUT_NOTE)
      for s in ("retina", "spindle", "papilla")),
    AutomationDef("thalamus", "hippocampus", "schedule", ("a",), ("thalamus",), 2, when=("weekly", "04:00", (3, 4))),
    AutomationDef("saturday", "hippocampus", "schedule", ("a",), ("insula", "motor_cortex"), 2),
    AutomationDef("weekly-review", "hippocampus", "schedule", ("a", "b"), ("weekly_review",), 3),
    AutomationDef("sartorius-morning", "sartorius", "schedule", ("a", "b"), ("sartorius_morning",), 1),
    AutomationDef("vagus-morning", "vagus", "schedule", ("a", "b"), ("vagus_morning",), 1),
    AutomationDef("vagus-evening", "vagus", "schedule", ("a", "b"), ("vagus_evening",), 1),
    AutomationDef("cerebellum-sunday", "cerebellum", "schedule", ("a", "b"), (), 1,
                  when=("weekly", "21:00", (6,)), note=_PENDING_NOTE, extra=("python -m homunculus pending",)),
    AutomationDef("cerebellum-monthly", "cerebellum", "schedule", ("a", "b"), ("monthly_report",), 1),
)

# Bots behind a job whose Job.bot is None or incomplete.
JOB_BOTS: Mapping[str, tuple[str, ...]] = {"weekly_review": ("hippocampus", "amygdala", "cerebellum")}
EVIDENCE_JOBS = frozenset({"retina", "spindle", "papilla", "thalamus"})
BASE_HOSTS = ("github.com",)  # clone the engine, pull and push the ledger
PROVIDER_HOSTS: Mapping[str, str] = {"anthropic": "api.anthropic.com", "gemini": "generativelanguage.googleapis.com"}
PROVIDER_SECRETS: Mapping[str, str] = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
WEBHOOK_HEADER = "X-Webhook-Secret"
WEBHOOK_SECRET_NAME = "HOMUNCULUS_WEBHOOK_SECRET"
RELAY_URL_NAME = "HOMUNCULUS_RELAY_URL"  # https://<worker host>, no trailing path
RELAY_TOKEN_NAME = "HOMUNCULUS_RELAY_TOKEN"  # the Worker's FETCH_TOKEN
MAILTO_NAME = "HOMUNCULUS_MAILTO"  # contact address the scouts and the full-text resolver send
RELAY_HOST = "<your-relay>.workers.dev"  # placeholder: replace with the Worker's host in the Automations form
RELAY_POINTER_MAX_BYTES = 1024
DEFAULT_MAX_ITEMS = 10  # Devin's documented default per script run
MAX_ITEMS_LIMIT = 50  # Devin's documented maximum
RRULE_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_BOT_TITLES = {b: b.replace("_", " ").title() for b in DEVIN_BOTS}


def automation(name: str, path: str | None = None) -> AutomationDef:
    """The AutomationDef called name (with or without the 'homunculus-' prefix), optionally for path."""
    key = name.removeprefix("homunculus-")
    found = next((d for d in AUTOMATIONS if d.name == key), None)
    if found is None:
        raise ValueError(f"unknown automation {name!r}; known: {', '.join(d.name for d in AUTOMATIONS)}")
    if path is not None and path not in found.paths:
        raise ValueError(f"automation {found.name!r} is not part of path {path!r}")
    return found


def _job_index() -> dict[str, pineal.Job]:
    return {job.name: job for job in pineal.default_jobs()}


_FREQS: Mapping[str, str] = {"daily": "FREQ=DAILY", "monthly": "FREQ=MONTHLY;BYMONTHDAY=1"}


def _byday(weekdays: Sequence[int]) -> str:
    if not weekdays or any(d not in range(7) for d in weekdays):
        raise ValueError(f"weekly needs weekdays 0..6, got {list(weekdays)!r}")
    return ",".join(RRULE_DAYS[d] for d in weekdays)


def rrule(cadence: str, at: str, weekdays: Sequence[int] = ()) -> str:
    """iCalendar RRULE for a daily, weekly (weekdays 0 = Monday) or monthly (day 1) slot."""
    match = _HHMM_RE.match(at or "")
    if match is None:
        raise ValueError(f"expected HH:MM, got {at!r}")
    clock = f"BYHOUR={int(match.group(1))};BYMINUTE={int(match.group(2))};BYSECOND=0"
    if cadence in _FREQS:
        return f"{_FREQS[cadence]};{clock}"
    if cadence != "weekly":
        raise ValueError(f"no RRULE for cadence {cadence!r}")
    return f"FREQ=WEEKLY;BYDAY={_byday(weekdays)};{clock}"


def _weekdays(value: Any) -> tuple[int, ...]:
    days = value if isinstance(value, list) else [value]
    if not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
        raise ValueError(f"weekday must be 0..6 or a list of them, got {value!r}")
    return tuple(days)


def _base_when(defn: AutomationDef, jobs: Mapping[str, pineal.Job]) -> tuple[str, str, tuple[int, ...]]:
    if defn.when is not None:
        return defn.when
    job = jobs[defn.jobs[0]]
    return job.cadence, str(job.at), () if job.weekday is None else (job.weekday,)


def _when(defn: AutomationDef, jobs: Mapping[str, pineal.Job], overrides: Mapping[str, Any]) -> tuple:
    """Schedule with cadence.json's jobs.<first job or automation name> override of at and weekday."""
    cadence, at, days = _base_when(defn, jobs)
    override = overrides.get(defn.jobs[0] if defn.jobs else defn.name) or {}
    if not isinstance(override, Mapping):
        raise ValueError(f"cadence override for {defn.name!r} must be an object")
    days = _weekdays(override["weekday"]) if "weekday" in override else days
    return cadence, str(override.get("at", at)), days


def _zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name!r}") from None
    return name


@dataclass(frozen=True)
class _Export:
    path: str
    tz: str
    roster: Mapping[str, agents.BotSpec]
    jobs: Mapping[str, pineal.Job]
    overrides: Mapping[str, Any]
    acu: Mapping[str, Any]
    keywords: Mapping[str, Any]
    max_items: int


def _bots_of(defn: AutomationDef, jobs: Mapping[str, pineal.Job]) -> set[str]:
    bots: set[str] = set()
    for name in defn.jobs:
        bots.update(JOB_BOTS.get(name) or ((jobs[name].bot,) if jobs[name].bot else ()))
    return bots


def _providers(defn: AutomationDef, x: _Export) -> set[str]:
    specs = [x.roster[b] for b in _bots_of(defn, x.jobs) if b in x.roster]
    names = {s.provider for s in specs} | {s.alt_provider for s in specs if s.alt_provider}
    return names & set(PROVIDER_HOSTS)


def _hosts(defn: AutomationDef, x: _Export) -> list[str]:
    hosts = set(BASE_HOSTS) | {PROVIDER_HOSTS[p] for p in _providers(defn, x)}
    hosts |= {RELAY_HOST} if defn.relay else set()
    if EVIDENCE_JOBS.intersection(defn.jobs):
        hosts |= scouts.ALLOWED_HOSTS
    return sorted(hosts)


def _trigger(defn: AutomationDef, x: _Export) -> dict:
    if defn.trigger == "webhook":
        return {"type": "webhook", "secret_header": WEBHOOK_HEADER, "secret_name": WEBHOOK_SECRET_NAME,
                "max_payload_bytes": RELAY_POINTER_MAX_BYTES if defn.relay else WEBHOOK_MAX_BYTES}
    cadence, at, days = _when(defn, x.jobs, x.overrides)
    return {"type": "schedule", "rrule": rrule(cadence, at, days), "timezone": x.tz}


def _relay_lines(defn: AutomationDef) -> list[str]:
    if defn.bot in pineal.OWNER_TARGETS:
        return ["Post each entry of owner_texts in the command's JSON output to the owner, verbatim, one message "
                "each, nothing added. Red-flag texts are fixed templates: never reword or soften them."]
    return ["If owner_texts in the command's JSON output is not empty, send each entry verbatim to the Sartorius "
            "Bot to post, and put a Report into Needs You that names the job, never the values."]


def _command_lines(defn: AutomationDef, path: str) -> list[str]:
    lines = list(LEDGER_SYNC_START) if path == "a" else []
    if defn.trigger == "webhook":
        lines += list(_RELAY_COMMANDS if defn.relay else _INGEST_COMMANDS)
    if defn.jobs:
        lines.append(f"python -m homunculus devin-task {defn.name} --path {path}")
    lines += list(defn.extra)
    return lines + ([LEDGER_PUSH] if path == "a" else [])


def automation_prompt(defn: AutomationDef, path: str) -> str:
    """The text the Automation sends; the Bot's General instructions hold the standing rules."""
    title = _BOT_TITLES.get(defn.bot, defn.bot)
    parts = [f"homunculus automation {defn.name}, path {path}. You are the {title} Bot."]
    parts += [defn.note] if defn.note and defn.trigger != "schedule" else []
    commands = _command_lines(defn, path)
    if commands:
        parts.append("Run in order, in the engine checkout:\n" + "\n".join(f"    {c}" for c in commands))
    parts += [defn.note] if defn.note and defn.trigger == "schedule" else []
    parts += _relay_lines(defn) if defn.jobs else []
    parts.append("A failed command: put a Report into Needs You with the job name and the exit code only. "
                 "Never paste health values, payloads or ledger file contents into any chat.")
    return "\n\n".join(parts)


def _script(defn: AutomationDef, x: _Export) -> dict:
    return {"language": "python3", "state_file": "STATE_FILE", "max_items": x.max_items,
            "network_allowlist": [scouts.EUTILS.split("/")[2]],
            "source": scout_poll_script(str(defn.scout), x.keywords, max_items=x.max_items)}


def _secrets(defn: AutomationDef, x: _Export) -> list[str]:
    relay = [RELAY_TOKEN_NAME, RELAY_URL_NAME] if defn.relay else []
    mailto = [MAILTO_NAME] if EVIDENCE_JOBS.intersection(defn.jobs) else []  # PubMed and Crossref contact
    return sorted([PROVIDER_SECRETS[p] for p in _providers(defn, x)] + relay + mailto)


def _spec(defn: AutomationDef, x: _Export) -> dict:
    spec = {
        "schema": 1, "name": f"homunculus-{defn.name}", "bot": defn.bot, "path": x.path,
        "runs_on": "cloud" if x.path == "a" else "outpost", "trigger": _trigger(defn, x),
        "max_acu_limit": int(x.acu.get(defn.name, defn.max_acu)), "network_allowlist": _hosts(defn, x),
        "secrets": _secrets(defn, x), "prompt": automation_prompt(defn, x.path),
    }
    if defn.trigger == "script":
        spec["script"] = _script(defn, x)
    return spec


def _engine_roster() -> dict[str, agents.BotSpec]:
    return agents.load_roster(json.loads(ENGINE_ROSTER.read_text(encoding="utf-8")))


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be an object")
    return value


def _check_path(path: str) -> None:
    if path not in PATHS:
        raise ValueError(f"path must be one of {PATHS}, got {path!r}")


def export_automations(
    cadence: Mapping[str, Any] | None = None,
    *,
    path: str = "a",
    tz: str | None = None,
    roster: Mapping[str, agents.BotSpec] | None = None,
    keywords: Mapping[str, Any] | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[dict]:
    """Automation specs for path 'a' (serverless) or 'b' (own box / Outpost), sorted by name.

    cadence is config/cadence.json: optional 'tz', 'jobs' {<job or automation>:
    {'at', 'weekday'}} and 'devin' {'max_acu': {<automation>: n}}. The specs
    carry secret names only, never values.
    """
    _check_path(path)
    cadence = _mapping(cadence, "cadence")
    x = _Export(
        path=path, tz=_zone(tz or cadence.get("tz") or DEFAULT_TZ), roster=roster or _engine_roster(),
        jobs=_job_index(), overrides=_mapping(cadence.get("jobs"), "cadence.jobs"),
        acu=_mapping(_mapping(cadence.get("devin"), "cadence.devin").get("max_acu"), "cadence.devin.max_acu"),
        keywords=_mapping(keywords, "keywords"), max_items=_max_items(max_items),
    )
    return sorted((_spec(d, x) for d in AUTOMATIONS if path in d.paths), key=lambda s: s["name"])


def write_automations(out_dir: str | Path, specs: Sequence[Mapping[str, Any]]) -> list[Path]:
    """One <name>.json per spec in out_dir (created), sorted keys, trailing newline."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for spec in specs:
        target = out / f"{spec['name']}.json"
        target.write_text(json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(target)
    return written


def box_jobs(jobs: Sequence[pineal.Job], path: str = "b") -> list[pineal.Job]:
    """The Pineal jobs the box still ticks when path's Automations run the others in Devin sessions."""
    _check_path(path)
    taken = {j for d in AUTOMATIONS if path in d.paths for j in d.jobs}
    return [job for job in jobs if job.name not in taken]


# --------------------------------------------------------------------------
# Scout script step

_SCOUT_SCRIPT = string.Template('''#!/usr/bin/env python3
"""homunculus $scout poll for a Devin Automation script step.

Generated by homunculus.devin_layer.scout_poll_script; regenerate instead of editing.
Searches PubMed for the scout's queries, keeps the highest PMID it has emitted as
the cursor in the JSON file named by STATE_FILE, and prints at most MAX_ITEMS new
ids. The ids only signal work: the session reruns the scout through code.
Standard library only; the only host is eutils.ncbi.nlm.nih.gov.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

SCOUT = $scout_lit
QUERIES = json.loads($queries_lit)
MAX_ITEMS = $max_items
RELDAYS = $reldays
ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
RETMAX = 100


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_url(query):
    params = {"db": "pubmed", "term": "(" + query + ") NOT Retracted Publication[pt]",
              "reldate": str(RELDAYS), "datetype": "edat", "retmode": "json", "retmax": str(RETMAX)}
    return ESEARCH + "?" + urllib.parse.urlencode(params)


def found_ids(fetch):
    ids = set()
    for query in QUERIES:
        result = fetch(search_url(query)).get("esearchresult") or {}
        ids.update(str(i) for i in result.get("idlist") or [] if str(i).isdigit())
    return sorted(ids, key=int)


def load_cursor(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return int(json.load(fh).get("cursor", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def save_cursor(path, cursor):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"scout": SCOUT, "cursor": cursor}, fh)
    os.replace(tmp, path)


def main(fetch=fetch_json, state_path=None, out=sys.stdout):
    path = state_path or os.environ.get("STATE_FILE") or ".homunculus-" + SCOUT + "-state.json"
    cursor = load_cursor(path)
    new = [i for i in found_ids(fetch) if int(i) > cursor][:MAX_ITEMS]
    save_cursor(path, int(new[-1]) if new else cursor)
    out.write(json.dumps({"scout": SCOUT, "items": ["pmid:" + i for i in new]}) + "\\n")
    return new


if __name__ == "__main__":
    main()
''')


def _max_items(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ITEMS_LIMIT:
        raise ValueError(f"max_items must be an int in 1..{MAX_ITEMS_LIMIT}")
    return value


def _queries(keywords: Mapping[str, Any], scout: str) -> list[str]:
    section = keywords.get(scout, keywords) if isinstance(keywords.get(scout), Mapping) else keywords
    raw = section.get("queries") or []
    if not isinstance(raw, list) or not all(isinstance(q, str) for q in raw):
        raise ValueError(f"{scout}: queries must be a list of strings")
    return [q for q in raw if q.strip()]


def scout_poll_script(scout: str, keywords: Mapping[str, Any] | None = None, *,
                      max_items: int = DEFAULT_MAX_ITEMS, reldays: int = scouts.LOOKBACK_DAYS) -> str:
    """Python source for the scout's script step; keywords is its config/keywords.json section or the file."""
    if scout not in agents.SCOUTS:
        raise ValueError(f"unknown scout {scout!r}")
    queries = _queries(_mapping(keywords, "keywords"), scout)
    return _SCOUT_SCRIPT.substitute(
        scout=scout, scout_lit=repr(scout), queries_lit=repr(json.dumps(queries, ensure_ascii=False)),
        max_items=_max_items(max_items), reldays=int(reldays),
    )


# --------------------------------------------------------------------------
# run_task and reply: what an Automation or a chat message runs


class CaptureNotifier:
    """Forwards to the real notifier and keeps what was sent, so the Bot can post it."""

    def __init__(self, inner: pineal.Notifier) -> None:
        self.inner = inner
        self.sent: list[dict[str, str]] = []

    def send(self, to: str, text: str, level: str) -> None:
        self.inner.send(to, text, level)
        self.sent.append({"to": to, "text": text, "level": level})


def _local_now(ctx: pineal.Context) -> datetime:
    moment = ctx.now()
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(ZoneInfo(ctx.tz))


def _done(ledger: Ledger, job: str, period: str) -> bool:
    return any(r.get("job") == job and r.get("period") == period and r.get("status") in pineal.FINAL_STATUSES
               for r in ledger.read_jsonl(pineal.RUNS_LOG))


def _admitted(ctx: pineal.Context, job: pineal.Job) -> bool:
    """Same gate as Pineal's tick: HALT keeps essential jobs, core_only keeps essential and code jobs."""
    if ctx.ledger.halted():
        return job.essential
    return ctx.budget.level() != "core_only" or job.essential or job.bot is None


# Jobs a webhook session reruns even when their period is done: the batch it just ingested may carry
# days an earlier run this hour could not see. deliver_alerts dedupes by (rule_id, date).
WEBHOOK_RERUN = frozenset({"red_flags"})


def _run_one(ctx: pineal.Context, job: pineal.Job, local: datetime, rerun: bool = False) -> dict:
    period = pineal.period_key(job, local)
    if not rerun and _done(ctx.ledger, job.name, period):
        return {"job": job.name, "period": period, "status": "skipped", "message": "already done"}
    if not _admitted(ctx, job):
        return {"job": job.name, "period": period, "status": "skipped", "message": "halted or budget"}
    return pineal.run_job(ctx, job, period)


def _persist(ctx: pineal.Context, local: datetime, kind: str) -> str | None:
    """Path a: save today's and yesterday's check-ins and commit; a dry run writes nothing."""
    if ctx.dry_run:
        return None
    day = local.date()
    save_checkins(ctx.store, ctx.ledger, [(day - timedelta(days=1)).isoformat(), day.isoformat()])
    return _code_commit(ctx.ledger, kind, f"Devin session state: {kind}", ctx.now())


def run_task(ctx: pineal.Context, name: str, *, path: str = "a") -> dict:
    """Run one Automation's Pineal jobs once per period; owner texts come back for the Bot to post.

    A job already ok or skipped in its period is not rerun, so an Automation
    retry or a box tick that got there first does no double work. Webhook
    sessions always rerun WEBHOOK_RERUN, since their batch is new data.
    """
    _check_path(path)
    defn = automation(name, path)
    capture = CaptureNotifier(ctx.notifier)
    run_ctx = dataclasses.replace(ctx, notifier=capture)
    local = _local_now(ctx)
    if path == "a":
        restore_store(ctx.store, ctx.ledger, local)
    index = _job_index()
    webhook = defn.trigger == "webhook"
    runs = [_run_one(run_ctx, index[job], local, webhook and job in WEBHOOK_RERUN) for job in defn.jobs]
    state = _persist(ctx, local, f"devin-task-{defn.name}") if path == "a" else None
    return {"automation": defn.name, "bot": defn.bot, "path": path, "runs": runs, "state_commit": state,
            "owner_texts": [m["text"] for m in capture.sent if m["to"] == "owner"]}


def clear_hold(ctx: pineal.Context) -> dict:
    """Lift every urgent coaching hold (pineal.clear_coaching_hold) and commit; the owner's Decision only."""
    cleared = 0 if ctx.dry_run else pineal.clear_coaching_hold(ctx)
    sha = _code_commit(ctx.ledger, "devin-clear-hold", f"Coaching hold cleared: {cleared}", ctx.now())
    return {"cleared": cleared, "commit": sha}


def reply(ctx: pineal.Context, bot: str, text: str, *, path: str = "a") -> tuple[int, dict]:
    """An owner message from the Bot's chat, through pineal.handle_owner_message (112 short-circuit included)."""
    _check_path(path)
    if bot not in pineal.OWNER_TARGETS:
        return 400, {"error": f"{bot!r} does not talk to the owner"}
    local = _local_now(ctx)
    if path == "a":
        restore_store(ctx.store, ctx.ledger, local)
    status, body = pineal.handle_owner_message(ctx, bot, text)
    if path == "a":
        _persist(ctx, local, f"devin-reply-{bot}")
    return status, body


if __name__ == "__main__":
    sys.exit(main())

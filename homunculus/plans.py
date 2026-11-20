"""Versioned training and nutrition plan as typed JSON.

The plan lives in the ledger as ``plans/vNNNN.json`` plus a German
``plans/vNNNN.md`` rendering; ``plans/CURRENT`` names the active version.
LLMs never write a plan. They emit a typed diff of JSON-pointer ops, code
sorts it into a guardrail tier, ``propose`` stores it under
``plans/pending/<id>.json`` and ``ratify_due``/``approve`` turn it into a new
version. Rollback is a new version that copies old content.

``modulate_day`` is the deterministic daily adjustment the coaches read. It
may shorten, swap to a lighter planned session or rest, and never raises load.

This module does not commit; Pineal commits what it wrote (``Ledger.pending``).
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from homunculus.guardrails import HARD_RPE, Rules
from homunculus.store import Ledger, canonical_json

# --------------------------------------------------------------------------
# Schemas

_NULLABLE_NUMBER = {"type": ["number", "null"]}
_STRING_LIST = {"type": "array", "items": {"type": "string"}}

_EXERCISE_SCHEMA: dict = {
    "type": "object",
    "required": ["exercise_id", "sets", "reps", "load_kg", "rir", "novel", "probation"],
    "additionalProperties": False,
    "properties": {
        "exercise_id": {"type": "string"},
        "sets": {"type": "integer", "minimum": 0},
        "reps": {"type": "integer", "minimum": 0},
        "load_kg": {"type": ["number", "null"], "minimum": 0},
        "rir": {"type": ["number", "null"], "minimum": 0},
        "novel": {"type": "boolean"},
        "probation": {"type": "boolean"},
    },
}

_SESSION_SCHEMA: dict = {
    "type": "object",
    "required": ["id", "weekday", "kind", "duration_min", "target_rpe", "exercises"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "weekday": {"type": "integer", "minimum": 0, "maximum": 6},
        "kind": {"type": "string"},
        "duration_min": {"type": "number", "minimum": 0},
        "target_rpe": {"type": "number", "minimum": 0, "maximum": 10},
        "exercises": {"type": "array", "items": _EXERCISE_SCHEMA},
    },
}

PLAN_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "version", "parent", "effective_from", "valid_until", "author", "ratified_by",
        "data_snapshot_sha", "rationale_refs", "sessions", "nutrition", "supplements",
        "experiments", "modulation",
    ],
    "additionalProperties": False,
    "properties": {
        "version": {"type": "integer", "minimum": 1},
        "parent": {"type": ["integer", "null"], "minimum": 1},
        "effective_from": {"type": "string"},
        "valid_until": {"type": ["string", "null"]},
        "author": {"type": "string"},
        "ratified_by": {"type": "string"},
        "data_snapshot_sha": {"type": ["string", "null"]},
        "rationale_refs": _STRING_LIST,
        "sessions": {"type": "array", "items": _SESSION_SCHEMA},
        "nutrition": {
            "type": "object",
            "required": ["kcal_target", "protein_g_per_kg", "notes_de"],
            "additionalProperties": False,
            "properties": {
                "kcal_target": {"type": ["number", "null"], "minimum": 0},
                "protein_g_per_kg": {"type": ["number", "null"], "minimum": 0},
                "notes_de": {"type": "string"},
            },
        },
        "supplements": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "dose", "unit", "tested_by"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "dose": {"type": "number", "minimum": 0},
                    "unit": {"type": "string"},
                    "tested_by": {"type": ["string", "null"]},
                },
            },
        },
        "experiments": _STRING_LIST,
        "modulation": {
            "type": "object",
            "required": ["max_daily_load_cut_pct", "allow_swap", "readiness_low_action"],
            "additionalProperties": False,
            "properties": {
                "max_daily_load_cut_pct": {"type": "number", "minimum": 0, "maximum": 100},
                "allow_swap": {"type": "boolean"},
                "readiness_low_action": {"type": "string", "enum": ["shorten", "swap", "rest"]},
            },
        },
    },
}

DIFF_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "array",
    "items": {
        "type": "object",
        "required": ["op", "path", "refs"],
        "additionalProperties": False,
        "properties": {
            "op": {"type": "string", "enum": ["set", "add", "remove"]},
            "path": {"type": "string"},
            "value": {},
            "refs": _STRING_LIST,
        },
    },
}

PROPOSABLE_TIERS = ("auto", "notify", "approve")
PROTECTED_KEYS = frozenset({"version", "parent", "effective_from", "author", "ratified_by", "data_snapshot_sha"})
VETO_WINDOW = timedelta(hours=24)
VALIDITY = timedelta(days=7)
WRITER = "pineal"
PROPOSER = "cerebellum"
CURRENT_PATH = "plans/CURRENT"
_VERSION_FILE_RE = re.compile(r"^plans/v(\d+)\.json$")
_CURRENT_RE = re.compile(r"^v?(\d+)$")
_PENDING_ID_RE = re.compile(r"^p-[0-9a-f]{12}$")
_INDEX_RE = re.compile(r"^(0|[1-9][0-9]*)$")


class PlanError(ValueError):
    """Invalid plan, diff, ledger state or transition."""


# --------------------------------------------------------------------------
# Minimal JSON Schema validator (the subset used above)

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
}


def _type_ok(expected: Any, value: Any) -> bool:
    if expected is None:
        return True
    names = expected if isinstance(expected, list) else [expected]
    return any(_TYPE_CHECKS[name](value) for name in names)


def _range_errors(schema: Mapping, value: Any, path: str) -> list[str]:
    if not _TYPE_CHECKS["number"](value):
        return []
    errors = []
    if "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{path}: below minimum {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        errors.append(f"{path}: above maximum {schema['maximum']}")
    return errors


def _object_errors(schema: Mapping, value: Any, path: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    props = schema.get("properties", {})
    errors = [f"{path}/{key}: required" for key in schema.get("required", ()) if key not in value]
    for key in sorted(value):
        if key in props:
            errors.extend(_schema_errors(props[key], value[key], f"{path}/{_escape(key)}"))
        elif schema.get("additionalProperties") is False:
            errors.append(f"{path}/{_escape(key)}: unexpected key")
    return errors


def _array_errors(schema: Mapping, value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or "items" not in schema:
        return []
    errors: list[str] = []
    for i, item in enumerate(value):
        errors.extend(_schema_errors(schema["items"], item, f"{path}/{i}"))
    return errors


def _schema_errors(schema: Mapping, value: Any, path: str) -> list[str]:
    if not _type_ok(schema.get("type"), value):
        return [f"{path or '/'}: expected {schema['type']}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path or '/'}: not one of {schema['enum']}"]
    return _range_errors(schema, value, path) + _object_errors(schema, value, path) + _array_errors(schema, value, path)


def _duplicate_exercises(plan: Mapping) -> list[str]:
    """One entry per exercise and session: copies would add volume past the per-exercise load check."""
    errors = []
    for i, session in enumerate(plan.get("sessions", [])):
        eids = [e.get("exercise_id") for e in session.get("exercises", []) if isinstance(e, dict)]
        if len(eids) != len(set(eids)):
            errors.append(f"/sessions/{i}/exercises: duplicate exercise_id")
    return errors


def _valid_until_errors(plan: Mapping) -> list[str]:
    """valid_until is shown to the owner verbatim, so it must be an ISO date, not free text."""
    raw = plan.get("valid_until")
    if raw is None:
        return []
    try:
        datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return ["/valid_until: expected an ISO date"]
    return []


def _semantic_errors(plan: Mapping) -> list[str]:
    errors = [*_duplicate_exercises(plan), *_valid_until_errors(plan)]
    ids = [s.get("id") for s in plan.get("sessions", []) if isinstance(s, dict)]
    if len(ids) != len(set(ids)):
        errors.append("/sessions: duplicate session id")
    version, parent = plan.get("version"), plan.get("parent")
    if isinstance(version, int) and isinstance(parent, int) and parent >= version:
        errors.append("/parent: must be lower than version")
    return errors


def validate_plan(plan: Mapping) -> list[str]:
    """Error strings, empty when the plan matches PLAN_SCHEMA."""
    if not isinstance(plan, Mapping):
        return ["/: expected object"]
    errors = _schema_errors(PLAN_SCHEMA, dict(plan), "")
    return errors if errors else _semantic_errors(plan)


# --------------------------------------------------------------------------
# JSON pointer diffs


def _escape(token: str) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def _tokens(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise PlanError(f"invalid JSON pointer: {path!r}")
    return [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]


def _index(token: str, length: int, allow_end: bool) -> int:
    if allow_end and token == "-":
        return length
    if not _INDEX_RE.match(token):
        raise PlanError(f"invalid array index: {token!r}")
    idx = int(token)
    if idx > length or (idx == length and not allow_end):
        raise PlanError(f"array index out of range: {token}")
    return idx


def _resolve(doc: Any, tokens: Sequence[str]) -> Any:
    node = doc
    for token in tokens:
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list):
            node = node[_index(token, len(node), allow_end=False)]
        else:
            raise PlanError(f"path not found at {token!r}")
    return node


def _dict_op(parent: dict, key: str, op: str, value: Any) -> None:
    exists = key in parent
    if op == "add" and exists:
        raise PlanError(f"add on existing key {key!r}; use set")
    if op != "add" and not exists:
        raise PlanError(f"{op} on missing key {key!r}")
    if op == "remove":
        del parent[key]
    else:
        parent[key] = value


def _list_op(parent: list, token: str, op: str, value: Any) -> None:
    idx = _index(token, len(parent), allow_end=op == "add")
    if op == "add":
        parent.insert(idx, value)
    elif op == "remove":
        parent.pop(idx)
    else:
        parent[idx] = value


def _apply_op(doc: Any, op: Mapping) -> Any:
    tokens = _tokens(op["path"])
    value = copy.deepcopy(op.get("value"))
    if not tokens:
        if op["op"] != "set":
            raise PlanError("only set may target the document root")
        return value
    parent = _resolve(doc, tokens[:-1])
    if isinstance(parent, dict):
        _dict_op(parent, tokens[-1], op["op"], value)
    elif isinstance(parent, list):
        _list_op(parent, tokens[-1], op["op"], value)
    else:
        raise PlanError(f"cannot address into a scalar: {op['path']!r}")
    return doc


def _diff_errors(diff: Any) -> list[str]:
    errors = _schema_errors(DIFF_SCHEMA, diff, "")
    if errors:
        return errors
    return [f"/{i}: {op['op']} needs a value" for i, op in enumerate(diff) if op["op"] != "remove" and "value" not in op]


def _normalize_diff(diff: Sequence[Mapping]) -> list[dict]:
    ops = [dict(op) if isinstance(op, Mapping) else op for op in diff]
    errors = _diff_errors(ops)
    if errors:
        raise PlanError("invalid diff: " + "; ".join(errors))
    return copy.deepcopy(ops)


def apply_diff(plan: Mapping, diff: Sequence[Mapping]) -> dict:
    """Apply ops to a copy of plan. Raises PlanError on a bad op or an invalid result."""
    doc: Any = copy.deepcopy(dict(plan))
    for op in _normalize_diff(diff):
        doc = _apply_op(doc, op)
    errors = validate_plan(doc) if isinstance(doc, dict) else ["/: expected object"]
    if errors:
        raise PlanError("diff yields an invalid plan: " + "; ".join(errors))
    return doc


def _op(op: str, path: str, value: Any = None) -> dict:
    out: dict = {"op": op, "path": path, "refs": []}
    if op != "remove":
        out["value"] = copy.deepcopy(value)
    return out


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def _diff_dict(a: dict, b: dict, path: str) -> list[dict]:
    ops: list[dict] = []
    for key in sorted(set(a) | set(b)):
        sub = f"{path}/{_escape(key)}"
        if key not in b:
            ops.append(_op("remove", sub))
        elif key not in a:
            ops.append(_op("add", sub, b[key]))
        else:
            ops.extend(_diff(a[key], b[key], sub))
    return ops


def _diff_list(a: list, b: list, path: str) -> list[dict]:
    ops: list[dict] = []
    for i in range(min(len(a), len(b))):
        ops.extend(_diff(a[i], b[i], f"{path}/{i}"))
    ops.extend(_op("remove", f"{path}/{i}") for i in range(len(a) - 1, len(b) - 1, -1))
    ops.extend(_op("add", f"{path}/{i}", b[i]) for i in range(len(a), len(b)))
    return ops


def _diff(a: Any, b: Any, path: str) -> list[dict]:
    if isinstance(a, dict) and isinstance(b, dict):
        return _diff_dict(a, b, path)
    if isinstance(a, list) and isinstance(b, list):
        return _diff_list(a, b, path)
    return [] if _same(a, b) else [_op("set", path, b)]


def compute_diff(a: Mapping, b: Mapping) -> list[dict]:
    """Ops turning a into b, so apply_diff(a, compute_diff(a, b)) == b. Refs are empty."""
    return _diff(copy.deepcopy(dict(a)), copy.deepcopy(dict(b)), "")


def changed_paths(diff: Sequence[Mapping]) -> set[str]:
    return {str(op["path"]) for op in diff}


def _session_load(session: Mapping) -> float:
    return float(session["target_rpe"]) * float(session["duration_min"])


def weekly_load(plan: Mapping) -> float:
    """Planned sRPE load: sum of target_rpe * duration_min over all sessions."""
    return math.fsum(_session_load(s) for s in plan.get("sessions", []))


# --------------------------------------------------------------------------
# Ledger: versions


def _vpath(version: int, ext: str = "json") -> str:
    return f"plans/v{version:04d}.{ext}"


def _current_version(ledger: Ledger) -> int:
    if not ledger.exists(CURRENT_PATH):
        raise PlanError("no current plan: plans/CURRENT is missing")
    match = _CURRENT_RE.match(ledger.read_text(CURRENT_PATH).strip())
    if not match:
        raise PlanError("plans/CURRENT is malformed")
    return int(match.group(1))


def get(ledger: Ledger, version: int) -> dict:
    plan = ledger.read_json(_vpath(version))
    if plan is None:
        raise PlanError(f"plan version {version} not found")
    return plan


def current(ledger: Ledger) -> dict:
    return get(ledger, _current_version(ledger))


def _latest_version(ledger: Ledger) -> int:
    found = (_VERSION_FILE_RE.match(p) for p in ledger.glob("plans/v*.json"))
    return max((int(m.group(1)) for m in found if m), default=0)


def _write_version(ledger: Ledger, plan: Mapping) -> int:
    errors = validate_plan(plan)
    if errors:
        raise PlanError("refusing to store invalid plan: " + "; ".join(errors))
    version = int(plan["version"])
    ledger.write_json(_vpath(version), dict(plan), author=WRITER)
    ledger.write_text(_vpath(version, "md"), render_markdown_de(plan), author=WRITER)
    ledger.write_text(CURRENT_PATH, f"v{version:04d}\n", author=WRITER)
    return version


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> datetime:
    return _utc(datetime.fromisoformat(text))


def _dedupe(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _stamp(plan: Mapping, ledger: Ledger, parent: int, now: datetime, meta: Mapping[str, Any]) -> dict:
    """New version metadata on top of plan content. meta: author, ratified_by, sha, refs."""
    out = copy.deepcopy(dict(plan))
    out.update({
        "version": _latest_version(ledger) + 1,
        "parent": parent,
        "effective_from": _iso(now),
        "author": meta["author"],
        "ratified_by": meta["ratified_by"],
        "data_snapshot_sha": meta["sha"],
        "rationale_refs": _dedupe(list(meta["refs"])),
    })
    if not meta.get("keep_valid_until"):
        out["valid_until"] = _iso(_utc(now) + VALIDITY)
    return out


# --------------------------------------------------------------------------
# Ledger: pending proposals


@dataclass(frozen=True)
class Pending:
    id: str
    diff: tuple[Mapping, ...]
    tier: str
    reasons: tuple[str, ...]
    created_at: str
    veto_until: str | None
    refs: tuple[str, ...]
    ruling_ref: str


def _pending_path(pending_id: str) -> str:
    if not _PENDING_ID_RE.match(str(pending_id)):
        raise PlanError(f"invalid pending id: {pending_id!r}")
    return f"plans/pending/{pending_id}.json"


def _pending_from(raw: Mapping) -> Pending:
    return Pending(
        id=raw["id"], diff=tuple(raw["diff"]), tier=raw["tier"], reasons=tuple(raw["reasons"]),
        created_at=raw["created_at"], veto_until=raw["veto_until"], refs=tuple(raw["refs"]),
        ruling_ref=raw["ruling_ref"],
    )


def _protected(path: str) -> bool:
    tokens = _tokens(path)
    return not tokens or tokens[0] in PROTECTED_KEYS


def _pending_id(base_version: int, ops: Sequence[Mapping], tier: str, ruling_ref: str) -> str:
    key = {"base_version": base_version, "diff": list(ops), "tier": tier, "ruling_ref": ruling_ref}
    return "p-" + hashlib.sha256(canonical_json(key)).hexdigest()[:12]


def propose(
    ledger: Ledger, diff: Sequence[Mapping], verdict_tier: str, reasons: Sequence[str],
    refs: Sequence[str], ruling_ref: str, now: datetime,
) -> Pending:
    """Store a checked diff as plans/pending/<id>.json. Same inputs on the same base give the same id."""
    if verdict_tier not in PROPOSABLE_TIERS:
        raise PlanError(f"tier {verdict_tier!r} cannot be proposed")
    ops = _normalize_diff(diff)
    blocked = sorted(op["path"] for op in ops if _protected(op["path"]))
    if blocked:
        raise PlanError(f"diff touches protected metadata: {blocked}")
    base = current(ledger)
    apply_diff(base, ops)
    pid = _pending_id(base["version"], ops, verdict_tier, ruling_ref)
    existing = ledger.read_json(_pending_path(pid))
    if existing is not None:
        return _pending_from(existing)
    veto_until = _iso(_utc(now) + VETO_WINDOW) if verdict_tier == "notify" else None
    raw = {
        "schema": 1, "id": pid, "diff": ops, "tier": verdict_tier, "reasons": list(reasons),
        "created_at": _iso(now), "veto_until": veto_until, "refs": list(refs), "ruling_ref": ruling_ref,
        "base_version": base["version"], "status": "pending",
    }
    ledger.write_json(_pending_path(pid), raw, author=PROPOSER)
    return _pending_from(raw)


def _read_pending(ledger: Ledger, pending_id: str) -> dict:
    raw = ledger.read_json(_pending_path(pending_id))
    if raw is None:
        raise PlanError(f"pending {pending_id!r} not found")
    if raw.get("status") != "pending":
        raise PlanError(f"pending {pending_id!r} is {raw.get('status')!r}")
    return raw


def _set_status(ledger: Ledger, raw: Mapping, status: str, now: datetime, **extra: Any) -> None:
    updated = {**raw, "status": status, "status_at": _iso(now), **extra}
    ledger.write_json(_pending_path(raw["id"]), updated, author=WRITER)


def _open_pendings(ledger: Ledger) -> list[dict]:
    rows = (ledger.read_json(p) for p in ledger.glob("plans/pending/*.json"))
    live = [r for r in rows if isinstance(r, dict) and r.get("status") == "pending"]
    return sorted(live, key=lambda r: (r["created_at"], r["id"]))


def _apply_pending(ledger: Ledger, raw: Mapping, now: datetime, sha: str | None, ratified_by: str) -> int:
    base = current(ledger)
    if base["version"] != raw["base_version"]:
        _set_status(ledger, raw, "superseded", now)
        raise PlanError(f"pending {raw['id']} was drafted against v{raw['base_version']}, current is v{base['version']}")
    try:
        content = apply_diff(base, raw["diff"])
    except PlanError as exc:
        _set_status(ledger, raw, "failed", now, error=str(exc))
        raise
    meta = {
        "author": PROPOSER, "ratified_by": ratified_by, "sha": sha,
        "refs": [*raw["refs"], raw["ruling_ref"]],
        "keep_valid_until": "/valid_until" in changed_paths(raw["diff"]),
    }
    version = _write_version(ledger, _stamp(content, ledger, base["version"], now, meta))
    _set_status(ledger, raw, "applied", now, version=version)
    return version


def _due_ratifier(raw: Mapping, now: datetime) -> str | None:
    if raw["tier"] == "auto":
        return "auto"
    if raw["tier"] == "notify" and raw.get("veto_until") and _utc(now) >= _parse_iso(raw["veto_until"]):
        return "veto_window"
    return None


def ratify_due(ledger: Ledger, now: datetime, data_snapshot_sha: str) -> list[int]:
    """Apply auto pendings now and notify pendings after their veto window; return new versions."""
    versions = []
    for raw in _open_pendings(ledger):
        ratified_by = _due_ratifier(raw, now)
        if ratified_by is None:
            continue
        try:
            versions.append(_apply_pending(ledger, raw, now, data_snapshot_sha, ratified_by))
        except PlanError:
            continue  # status already recorded as superseded or failed
    return versions


def approve(ledger: Ledger, pending_id: str, now: datetime) -> int:
    """Owner approval: apply a pending diff of any proposable tier at once."""
    return _apply_pending(ledger, _read_pending(ledger, pending_id), now, None, "owner")


def veto(ledger: Ledger, pending_id: str, now: datetime) -> None:
    _set_status(ledger, _read_pending(ledger, pending_id), "vetoed", now)


def rollback(ledger: Ledger, to_version: int, now: datetime) -> int:
    """New version copying the content of to_version, with parent = current version."""
    base_version = _current_version(ledger)
    if to_version == base_version:
        raise PlanError(f"v{to_version} is already current")
    old = get(ledger, to_version)
    meta = {"author": "owner", "ratified_by": "owner", "sha": old.get("data_snapshot_sha"), "refs": [_vpath(to_version)]}
    return _write_version(ledger, _stamp(old, ledger, base_version, now, meta))


# --------------------------------------------------------------------------
# German rendering

WEEKDAYS_DE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_LOW_ACTION_DE = {"shorten": "kürzen", "swap": "tauschen", "rest": "Ruhetag"}


def _num(x: float | int) -> str:
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    return format(x, "g").replace(".", ",") if isinstance(x, float) else str(x)


def _or(value: Any, unit: str, missing: str = "nicht festgelegt") -> str:
    return missing if value is None else f"{_num(value)}{unit}"


def _exercise_line(ex: Mapping) -> str:
    parts = [f"{ex['sets']} × {ex['reps']}"]
    if ex.get("load_kg") is not None:
        parts.append(f"{_num(ex['load_kg'])} kg")
    if ex.get("rir") is not None:
        parts.append(f"RIR {_num(ex['rir'])}")
    if ex.get("novel"):
        parts.append("neu")
    if ex.get("probation"):
        parts.append("Probephase")
    return f"- {ex['exercise_id']}: " + ", ".join(parts)


def _session_lines(session: Mapping) -> list[str]:
    lines = [
        f"### {WEEKDAYS_DE[session['weekday']]}: {session['kind']} ({session['id']})",
        "",
        f"{_num(session['duration_min'])} min, Ziel-RPE {_num(session['target_rpe'])}",
        "",
    ]
    exercises = [_exercise_line(ex) for ex in session["exercises"]]
    return lines + (exercises or ["Keine Übungen festgelegt."]) + [""]


def _sessions_md(plan: Mapping) -> list[str]:
    ordered = sorted(enumerate(plan["sessions"]), key=lambda p: (p[1]["weekday"], p[0]))
    lines = [line for _, s in ordered for line in _session_lines(s)]
    return ["## Einheiten", ""] + (lines or ["Keine Einheiten geplant.", ""])


def _bullets(items: Sequence[str], empty: str) -> list[str]:
    return [f"- {item}" for item in items] if items else [empty]


def _supplement_line(s: Mapping) -> str:
    tested = f"geprüft: {s['tested_by']}" if s.get("tested_by") else "ohne Prüfsiegel"
    return f"{s['name']}: {_num(s['dose'])} {s['unit']} ({tested})"


def _header_md(plan: Mapping) -> list[str]:
    parent = plan.get("parent")
    sha = plan.get("data_snapshot_sha")
    return [
        f"# Trainingsplan v{plan['version']:04d}",
        "",
        f"- Gültig ab: {plan['effective_from']}",
        f"- Gültig bis: {plan.get('valid_until') or 'offen'}",
        f"- Vorgänger: {f'v{parent:04d}' if parent is not None else 'keiner'}",
        f"- Autor: {plan['author']}",
        f"- Freigegeben durch: {plan['ratified_by']}",
        f"- Datenstand: {sha[:12] if sha else 'unbekannt'}",
        f"- Wochenlast (RPE × Minuten): {_num(round(weekly_load(plan), 1))}",
        "",
    ]


def _nutrition_md(plan: Mapping) -> list[str]:
    n = plan["nutrition"]
    lines = [
        "## Ernährung", "",
        f"- Kalorienziel: {_or(n.get('kcal_target'), ' kcal')}",
        f"- Protein: {_or(n.get('protein_g_per_kg'), ' g/kg')}",
    ]
    if n.get("notes_de"):
        lines.append(f"- Hinweise: {n['notes_de']}")
    return lines + [""]


def _modulation_md(plan: Mapping) -> list[str]:
    m = plan["modulation"]
    return [
        "## Tagesanpassung", "",
        f"- Maximale Kürzung pro Tag: {_num(m['max_daily_load_cut_pct'])} %",
        f"- Tausch erlaubt: {'ja' if m['allow_swap'] else 'nein'}",
        f"- Bei niedriger Bereitschaft: {_LOW_ACTION_DE[m['readiness_low_action']]}",
        "",
    ]


def render_markdown_de(plan: Mapping) -> str:
    lines = _header_md(plan) + _sessions_md(plan) + _nutrition_md(plan)
    lines += ["## Supplemente", ""] + _bullets([_supplement_line(s) for s in plan["supplements"]], "Keine.") + [""]
    lines += ["## Experimente", ""] + _bullets(list(plan["experiments"]), "Keine.") + [""]
    lines += _modulation_md(plan)
    lines += ["## Begründung", ""] + _bullets(list(plan["rationale_refs"]), "Keine Referenzen.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Daily modulation


def _day_result(session: Mapping | None, action: str, reasons: Sequence[str]) -> dict:
    return {"session": copy.deepcopy(dict(session)) if session is not None else None,
            "action": action, "reason_codes": list(reasons)}


def _session_for(plan: Mapping, weekday: int) -> Mapping | None:
    return next((s for s in plan["sessions"] if s["weekday"] == weekday), None)


def _shorten(session: Mapping, cut_pct: float) -> dict:
    keep = 1.0 - min(max(float(cut_pct), 0.0), 100.0) / 100.0
    out = copy.deepcopy(dict(session))
    out["duration_min"] = min(session["duration_min"], math.floor(session["duration_min"] * keep + 1e-9))
    for ex in out["exercises"]:
        if ex["sets"] >= 1:
            ex["sets"] = min(ex["sets"], max(1, math.floor(ex["sets"] * keep + 1e-9)))
    return out


def _lighter_session(plan: Mapping, planned: Mapping) -> Mapping | None:
    """Lightest session with less load and no higher intensity; never a hard session."""
    limit, rpe = _session_load(planned), float(planned["target_rpe"])
    lighter = [s for s in plan["sessions"] if _session_load(s) < limit
               and float(s["target_rpe"]) <= rpe and float(s["target_rpe"]) < HARD_RPE]
    return min(lighter, key=lambda s: (float(s["target_rpe"]), _session_load(s))) if lighter else None


def _low_readiness(plan: Mapping, planned: Mapping) -> dict:
    mod = plan["modulation"]
    action, reasons = mod["readiness_low_action"], ["readiness_low"]
    if action == "swap" and not mod["allow_swap"]:
        action = "shorten"
        reasons.append("swap_not_allowed")
    if action == "swap":
        alt = _lighter_session(plan, planned)
        if alt is not None:
            return _day_result(alt, "swap", reasons)
        action = "shorten"
        reasons.append("no_lighter_session")
    if action == "shorten":
        return _day_result(_shorten(planned, mod["max_daily_load_cut_pct"]), "shorten", reasons)
    return _day_result(None, "rest", reasons)


PAUSE_RPE_CAP = HARD_RPE - 1  # highest target RPE while an alert pauses intense training


def _eased(day: dict, reason: str) -> dict:
    if day["action"] == "as_planned":
        day["action"] = "shorten"
    day["reason_codes"].append(reason)
    return day


def _cap_intensity(day: dict) -> dict:
    session = day["session"]
    if session is None or float(session["target_rpe"]) <= PAUSE_RPE_CAP:
        return day
    session["target_rpe"] = PAUSE_RPE_CAP
    return _eased(day, "pause_intense")


def _cap_return(day: dict, planned: Mapping, days_off: int, rules: Rules) -> dict:
    """After a break the day's load stays at or below return_cap_pct of the planned session."""
    session = day["session"]
    if session is None or days_off < rules.return_after_days_off:
        return day
    target, load = _session_load(planned) * rules.return_cap_pct / 100, _session_load(session)
    if load <= target:
        return day
    day["session"] = _shorten(session, (1 - target / load) * 100)
    return _eased(day, "return_after_break")


def _readiness_day(plan: Mapping, planned: Mapping, readiness_band: str | None, stale: bool) -> dict:
    if stale:
        return _day_result(planned, "as_planned", ["stale_data"])
    if readiness_band in ("normal", "high"):
        return _day_result(planned, "as_planned", [f"readiness_{readiness_band}"])
    if readiness_band != "low":
        return _day_result(planned, "as_planned", ["readiness_unknown"])
    return _low_readiness(plan, planned)


def modulate_day(
    plan: Mapping, weekday: int, readiness_band: str | None, red_flag_active: bool, stale: bool,
    *, pause_intense: bool = False, days_off: int = 0, rules: Rules | None = None,
) -> dict:
    """Deterministic session for the day. Never returns more load than planned.

    pause_intense (an alert such as rhr_resp_temp holds) caps target_rpe at PAUSE_RPE_CAP.
    days_off is the length of the training break before the day; from rules.return_after_days_off
    on, the session is cut to rules.return_cap_pct % of the planned load, also for an unchanged plan.
    """
    if not isinstance(weekday, int) or not 0 <= weekday <= 6:
        raise PlanError(f"weekday must be 0..6, got {weekday!r}")
    if red_flag_active:
        return _day_result(None, "rest", ["red_flag"])
    planned = _session_for(plan, weekday)
    if planned is None:
        return _day_result(None, "as_planned", ["no_session"])
    day = _readiness_day(plan, planned, readiness_band, stale)
    if pause_intense:
        day = _cap_intensity(day)
    return _cap_return(day, planned, days_off, rules or Rules())


# --------------------------------------------------------------------------
# Exercise library


def load_exercises(ledger: Ledger) -> dict[str, dict]:
    """config/exercises.json as {exercise_id: feature record}; accepts a mapping or a list with 'id'."""
    raw = ledger.read_json("config/exercises.json", default={})
    if isinstance(raw, dict) and all(isinstance(v, Mapping) for v in raw.values()):
        return {str(k): dict(v) for k, v in raw.items()}
    if isinstance(raw, list) and all(isinstance(r, Mapping) and "id" in r for r in raw):
        return {str(r["id"]): dict(r) for r in raw}
    raise PlanError("config/exercises.json must be an object or a list of records with 'id'")

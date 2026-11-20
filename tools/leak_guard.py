#!/usr/bin/env python3
"""Fail-closed leak guard for the public engine repo.

Scans the files git would commit and exits 1 if any of them looks like
personal health data or a secret. Personal data belongs in the private
ledger repo, never here.

Rules:
  sqlite        SQLite files (by suffix or file header)
  hae_payload   Health Auto Export JSON with more than 20 metric data points
                plus workouts in total
  gps_route     JSON with workouts[].route or a list of lat/lon points
  export_file   .csv, .gpx, .tcx, .fit, .xml and .zip exports
  data_dir      paths under raw/, health_raw/, ledger/, .private/, checkins/,
                quarantine/, site/, data/daily/ or data/summaries/
  env_file      .env and .env.* files
  api_key       sk-ant-, AIza, gho_, ghp_, ghs_, ghu_, github_pat_ key patterns
  token_json    JSON fields access_token, refresh_token, cookie(s) with a value
  large_file    files over 2 MB outside docs/
  unreadable    a staged or committed blob git could not read (fail closed)

Small files (up to 8 KiB) under tests/ that declare themselves synthetic
(marker comment "leak-guard: synthetic" or top-level JSON field
"synthetic": true) are exempt from the hae_payload, gps_route, export_file
and data_dir rules. No file is exempt from the secret, SQLite, .env or size
rules.

Modes:
  default       working-tree files git would commit (tree walk without git)
  PATH ...      the given working-tree files
  --staged      the staged blobs (git index), for a pre-commit hook
  --rev REV     every blob in the tree of REV, for a pre-push hook

Usage: python tools/leak_guard.py [--root DIR] [--staged | --rev REV] [PATH ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

MAX_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD_POINTS = 20
MARKER = "leak-guard: synthetic"
MARKER_WINDOW = 4096
SYNTHETIC_MAX_BYTES = 8 * 1024
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-journal", ".db-wal", ".db-shm")
SQLITE_HEADER = b"SQLite format 3\x00"
DATA_DIRS = frozenset(
    {"raw", "health_raw", "ledger", ".private", "checkins", "quarantine", "site"}
)
DATA_PATHS = ("data/daily", "data/summaries")
EXPORT_SUFFIXES = (".csv", ".gpx", ".tcx", ".fit", ".xml", ".zip")
TOKEN_KEYS = frozenset({"access_token", "refresh_token", "cookie", "cookies"})
LAT_KEYS = frozenset({"lat", "latitude"})
LON_KEYS = frozenset({"lon", "lng", "long", "longitude"})
EXEMPT_RULES = frozenset({"hae_payload", "gps_route", "export_file", "data_dir"})
SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules"})
KEY_PATTERNS = (
    ("anthropic", re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("google", re.compile(rb"AIza[0-9A-Za-z_\-]{35}")),
    ("github-oauth", re.compile(rb"gho_[A-Za-z0-9]{36}")),
    ("github-pat", re.compile(rb"ghp_[A-Za-z0-9]{36}")),
    ("github-server", re.compile(rb"ghs_[A-Za-z0-9]{36}")),
    ("github-user", re.compile(rb"ghu_[A-Za-z0-9]{36}")),
    ("github-fine-grained", re.compile(rb"github_pat_[A-Za-z0-9_]{40,}")),
)


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.rule}: {self.detail}"


def _git_list(root: Path, args: Sequence[str]) -> list[str] | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def _walk(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        base = Path(dirpath).relative_to(root)
        out.extend((base / name).as_posix() for name in filenames)
    return out


def candidate_files(root: Path) -> list[str]:
    """Tracked, untracked-but-not-ignored and staged files; tree walk without git."""
    listed = _git_list(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if listed is None:
        return sorted(_walk(root))
    staged = _git_list(root, ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"])
    return sorted(set(listed) | set(staged or []))


def is_synthetic(rel: str, data: bytes, doc: Any) -> bool:
    """True for small tests/ files that carry the synthetic marker or field."""
    if not rel.startswith("tests/") or len(data) > SYNTHETIC_MAX_BYTES:
        return False
    if isinstance(doc, dict) and doc.get("synthetic") is True:
        return True
    return MARKER.encode() in data[:MARKER_WINDOW]


def _section(doc: Any, key: str) -> list:
    """doc["data"][key] or doc[key] when it is a list, else []."""
    if not isinstance(doc, dict):
        return []
    inner = doc.get("data")
    if isinstance(inner, dict) and isinstance(inner.get(key), list):
        return inner[key]
    value = doc.get(key)
    return value if isinstance(value, list) else []


def _metrics(doc: Any) -> list:
    return _section(doc, "metrics")


def payload_points(doc: Any) -> int:
    """Largest data[] length among the metrics of a HAE-shaped document."""
    sizes = [
        len(m["data"])
        for m in _metrics(doc)
        if isinstance(m, dict) and isinstance(m.get("data"), list)
    ]
    return max(sizes, default=0)


def total_points(doc: Any) -> int:
    """All metric data points plus workouts of a HAE-shaped document."""
    sizes = [
        len(m["data"])
        for m in _metrics(doc)
        if isinstance(m, dict) and isinstance(m.get("data"), list)
    ]
    return sum(sizes) + len(_section(doc, "workouts"))


def _nodes(doc: Any) -> Iterable[Any]:
    """Every dict, list and scalar in a JSON document, without recursion."""
    stack = [doc]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _is_point(item: Any) -> bool:
    return isinstance(item, dict) and bool(LAT_KEYS & item.keys()) and bool(LON_KEYS & item.keys())


def _has_route(node: Any) -> bool:
    """A dict with a non-empty route list, or a list holding 2+ lat/lon points."""
    if isinstance(node, dict):
        route = node.get("route")
        return isinstance(route, list) and any(isinstance(r, dict) for r in route)
    if isinstance(node, list):
        return sum(1 for item in node if _is_point(item)) > 1
    return False


def _token_keys(node: Any) -> set[str]:
    if not isinstance(node, dict):
        return set()
    return {k for k, v in node.items() if isinstance(k, str) and k.lower() in TOKEN_KEYS and v}


def check_json(rel: str, doc: Any) -> list[Finding]:
    """GPS routes and credential fields anywhere in a parsed JSON document."""
    if doc is None:
        return []
    route = False
    tokens: set[str] = set()
    for node in _nodes(doc):
        route = route or _has_route(node)
        tokens |= _token_keys(node)
    found = [Finding(rel, "gps_route", "workout route or lat/lon point list")] if route else []
    found += [Finding(rel, "token_json", f"{key} field") for key in sorted(tokens)]
    return found


def _load_json(rel: str, data: bytes) -> Any:
    if not rel.lower().endswith(".json"):
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _data_dir(dirs: Sequence[str]) -> str | None:
    """The first ledger directory among the parent dirs, or None."""
    lowered = [d.lower() for d in dirs]
    hits = sorted(DATA_DIRS.intersection(lowered))
    if hits:
        return hits[0]
    joined = "/" + "/".join(lowered) + "/"
    return next((p for p in DATA_PATHS if f"/{p}/" in joined), None)


def check_path(rel: str) -> list[Finding]:
    """Rules that depend on the path alone."""
    parts = rel.split("/")
    name = parts[-1]
    found = []
    if name.lower().endswith(SQLITE_SUFFIXES):
        found.append(Finding(rel, "sqlite", "SQLite file suffix"))
    if name.lower().endswith(EXPORT_SUFFIXES):
        found.append(Finding(rel, "export_file", "health or GPS export format"))
    if name == ".env" or name.startswith(".env."):
        found.append(Finding(rel, "env_file", "environment file"))
    data_dir = _data_dir(parts[:-1])
    if data_dir:
        found.append(Finding(rel, "data_dir", f"inside {data_dir}/ directory"))
    return found


def check_content(rel: str, data: bytes, doc: Any) -> list[Finding]:
    """Rules that depend on file content."""
    found = []
    if data.startswith(SQLITE_HEADER) and not rel.lower().endswith(SQLITE_SUFFIXES):
        found.append(Finding(rel, "sqlite", "SQLite file header"))
    points = total_points(doc)
    if points > MAX_PAYLOAD_POINTS:
        found.append(Finding(rel, "hae_payload", f"{points} data points"))
    for label, pattern in KEY_PATTERNS:
        if pattern.search(data):
            found.append(Finding(rel, "api_key", f"{label} key pattern"))
    return found + check_json(rel, doc)


def _too_large(rel: str, size: int) -> list[Finding]:
    if size <= MAX_BYTES or rel.startswith("docs/"):
        return []
    return [Finding(rel, "large_file", f"{size} bytes > {MAX_BYTES}")]


def _exempt(finding: Finding) -> bool:
    return finding.rule in EXEMPT_RULES


def scan_bytes(rel: str, data: bytes) -> list[Finding]:
    """All findings for one file's path and content."""
    doc = _load_json(rel, data)
    found = check_path(rel) + _too_large(rel, len(data)) + check_content(rel, data, doc)
    if is_synthetic(rel, data, doc):
        found = [f for f in found if not _exempt(f)]
    return found


def scan_file(root: Path, rel: str) -> list[Finding]:
    """All findings for one file relative to root; missing files yield none."""
    path = root / rel
    if not path.is_file():
        return []
    size = path.stat().st_size
    if size > MAX_BYTES and check_path(rel):
        return check_path(rel) + _too_large(rel, size)
    return scan_bytes(rel, path.read_bytes())


def _git_blob(root: Path, spec: str) -> bytes | None:
    try:
        proc = subprocess.run(
            ["git", "cat-file", "blob", spec], cwd=root, capture_output=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def scan_git(root: Path, rev: str | None = None) -> list[Finding] | None:
    """Findings for the staged blobs (rev None) or every blob of rev; None when git fails."""
    if rev is None:
        rels = _git_list(root, ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"])
        prefix = ":"
    else:
        rels = _git_list(root, ["ls-tree", "-r", "-z", "--name-only", rev])
        prefix = f"{rev}:"
    if rels is None:
        return None
    found = []
    for rel in rels:
        data = _git_blob(root, prefix + rel)
        if data is None:
            found.append(Finding(rel, "unreadable", "git blob could not be read"))
        else:
            found += scan_bytes(rel, data)
    return found


def scan(root: Path, files: Iterable[str] | None = None) -> list[Finding]:
    """Findings for the given files, or for every candidate file under root."""
    rels = candidate_files(root) if files is None else files
    return [f for rel in rels for f in scan_file(root, Path(rel).as_posix())]


def _relative(root: Path, paths: Sequence[str]) -> list[str] | None:
    if not paths:
        return None
    return [os.path.relpath(Path(p).resolve(), root) for p in paths]


def _findings(root: Path, args: argparse.Namespace) -> list[Finding] | None:
    if args.staged or args.rev:
        return scan_git(root, args.rev)
    return scan(root, _relative(root, args.paths))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Block personal data and secrets from the public repo.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan the staged blobs (pre-commit)")
    mode.add_argument("--rev", help="scan every blob in the tree of REV (pre-push)")
    parser.add_argument("paths", nargs="*", help="files to scan instead of the git file list")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    findings = _findings(root, args)
    if findings is None:
        print("leak_guard: git could not list the files, refusing", file=sys.stderr)
        return 1
    for finding in findings:
        print(f"leak_guard: {finding}", file=sys.stderr)
    if findings:
        print(f"leak_guard: {len(findings)} finding(s), refusing", file=sys.stderr)
        return 1
    print("leak_guard: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

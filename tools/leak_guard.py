#!/usr/bin/env python3
"""Fail-closed leak guard for the public engine repo.

Scans the files git would commit and exits 1 if any of them looks like
personal health data or a secret. Personal data belongs in the private
ledger repo, never here.

Rules:
  sqlite        SQLite files (by suffix or file header)
  hae_payload   Health Auto Export JSON with data.metrics[].data[] > 20 points
  data_dir      paths under raw/, health_raw/ or ledger/ directories
  env_file      .env and .env.* files
  api_key       sk-ant-, AIza, gho_, ghp_ key patterns
  large_file    files over 2 MB outside docs/

Files under tests/ that declare themselves synthetic (marker comment
"leak-guard: synthetic" or top-level JSON field "synthetic": true) are
exempt from the hae_payload and data_dir rules. No file is exempt from the
secret, SQLite, .env or size rules.

Usage: python tools/leak_guard.py [--root DIR] [PATH ...]
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
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-journal", ".db-wal", ".db-shm")
SQLITE_HEADER = b"SQLite format 3\x00"
DATA_DIRS = frozenset({"raw", "health_raw", "ledger"})
SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules"})
KEY_PATTERNS = (
    ("anthropic", re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("google", re.compile(rb"AIza[0-9A-Za-z_\-]{35}")),
    ("github-oauth", re.compile(rb"gho_[A-Za-z0-9]{36}")),
    ("github-pat", re.compile(rb"ghp_[A-Za-z0-9]{36}")),
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
    """True for tests/ files that carry the synthetic marker or field."""
    if not rel.startswith("tests/"):
        return False
    if isinstance(doc, dict) and doc.get("synthetic") is True:
        return True
    return MARKER.encode() in data[:MARKER_WINDOW]


def _metrics(doc: Any) -> list:
    if not isinstance(doc, dict):
        return []
    inner = doc.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("metrics"), list):
        return inner["metrics"]
    metrics = doc.get("metrics")
    return metrics if isinstance(metrics, list) else []


def payload_points(doc: Any) -> int:
    """Largest data[] length among the metrics of a HAE-shaped document."""
    sizes = [
        len(m["data"])
        for m in _metrics(doc)
        if isinstance(m, dict) and isinstance(m.get("data"), list)
    ]
    return max(sizes, default=0)


def _load_json(rel: str, data: bytes) -> Any:
    if not rel.lower().endswith(".json"):
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def check_path(rel: str) -> list[Finding]:
    """Rules that depend on the path alone."""
    parts = rel.split("/")
    name = parts[-1]
    found = []
    if name.lower().endswith(SQLITE_SUFFIXES):
        found.append(Finding(rel, "sqlite", "SQLite file suffix"))
    if name == ".env" or name.startswith(".env."):
        found.append(Finding(rel, "env_file", "environment file"))
    dirs = DATA_DIRS.intersection(p.lower() for p in parts[:-1])
    if dirs:
        found.append(Finding(rel, "data_dir", f"inside {sorted(dirs)[0]}/ directory"))
    return found


def check_content(rel: str, data: bytes, doc: Any) -> list[Finding]:
    """Rules that depend on file content."""
    found = []
    if data.startswith(SQLITE_HEADER) and not rel.lower().endswith(SQLITE_SUFFIXES):
        found.append(Finding(rel, "sqlite", "SQLite file header"))
    points = payload_points(doc)
    if points > MAX_PAYLOAD_POINTS:
        found.append(Finding(rel, "hae_payload", f"metric with {points} data points"))
    for label, pattern in KEY_PATTERNS:
        if pattern.search(data):
            found.append(Finding(rel, "api_key", f"{label} key pattern"))
    return found


def _too_large(rel: str, size: int) -> list[Finding]:
    if size <= MAX_BYTES or rel.startswith("docs/"):
        return []
    return [Finding(rel, "large_file", f"{size} bytes > {MAX_BYTES}")]


def _exempt(finding: Finding) -> bool:
    return finding.rule in ("hae_payload", "data_dir")


def scan_file(root: Path, rel: str) -> list[Finding]:
    """All findings for one file relative to root; missing files yield none."""
    path = root / rel
    if not path.is_file():
        return []
    size = path.stat().st_size
    found = check_path(rel) + _too_large(rel, size)
    if found and size > MAX_BYTES:
        return found
    data = path.read_bytes()
    doc = _load_json(rel, data)
    found += check_content(rel, data, doc)
    if is_synthetic(rel, data, doc):
        found = [f for f in found if not _exempt(f)]
    return found


def scan(root: Path, files: Iterable[str] | None = None) -> list[Finding]:
    """Findings for the given files, or for every candidate file under root."""
    rels = candidate_files(root) if files is None else files
    return [f for rel in rels for f in scan_file(root, Path(rel).as_posix())]


def _relative(root: Path, paths: Sequence[str]) -> list[str] | None:
    if not paths:
        return None
    return [os.path.relpath(Path(p).resolve(), root) for p in paths]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Block personal data and secrets from the public repo.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("paths", nargs="*", help="files to scan instead of the git file list")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    findings = scan(root, _relative(root, args.paths))
    for finding in findings:
        print(f"leak_guard: {finding}", file=sys.stderr)
    if findings:
        print(f"leak_guard: {len(findings)} finding(s), refusing", file=sys.stderr)
        return 1
    print("leak_guard: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

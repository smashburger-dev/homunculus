"""Path a: push the ledger from one of several parallel Devin sessions.

Each session works on its own clone. Append-only logs (``*.jsonl``) merge by
union. Derived snapshots, which every session overwrites with its own clock
and data, conflict on a rebase; for them upstream wins, a heartbeat keeps the
later run, and the caller rebuilds the snapshots from the merged ledger. Any
other conflict aborts the rebase and fails the push, so a lost write is loud.
"""

from __future__ import annotations

import json
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

PUSH_ATTEMPTS = 5
MAX_REBASE_STEPS = 50  # commits a single rebase may stop on
UNION_ATTRIBUTE = "*.jsonl merge=union"
HEARTBEATS = "ops/heartbeats/*.json"
# Snapshots code rewrites from the store on every run; their conflicts are resolved, not reported.
DERIVED_GLOBS = (
    HEARTBEATS, "data/freshness.json", "data/daily/*/*.json", "data/baselines/*.json", "checkins/*/*.json",
)

Git = Callable[[Sequence[str]], subprocess.CompletedProcess]


def git_runner(root: str | Path, env: Mapping[str, str] | None = None) -> Git:
    def run(args: Sequence[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *args], env=None if env is None else dict(env),
                              capture_output=True, text=True, check=False)

    return run


def derived(rel: str) -> bool:
    return any(fnmatchcase(rel, g) for g in DERIVED_GLOBS)


def ensure_union_merge(git: Git) -> None:
    """The clone's .git/info/attributes merge *.jsonl by union (idempotent)."""
    info = Path(git(["rev-parse", "--absolute-git-dir"]).stdout.strip()) / "info"
    info.mkdir(parents=True, exist_ok=True)
    attrs = info / "attributes"
    lines = attrs.read_text(encoding="utf-8").splitlines() if attrs.is_file() else []
    if UNION_ATTRIBUTE not in lines:
        attrs.write_text("\n".join([*lines, UNION_ATTRIBUTE]) + "\n", encoding="utf-8")


def _ended(git: Git, stage: int, rel: str) -> str:
    proc = git(["show", f":{stage}:{rel}"])
    try:
        doc: Any = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except ValueError:
        doc = {}
    return str(doc.get("ended", "")) if isinstance(doc, Mapping) else ""


def _resolve(git: Git, rel: str) -> bool:
    """Upstream's version (stage 2 during a rebase); a heartbeat keeps the later run's."""
    side = "--ours"
    if fnmatchcase(rel, HEARTBEATS) and _ended(git, 3, rel) > _ended(git, 2, rel):
        side = "--theirs"
    return git(["checkout", side, "--", rel]).returncode == 0 and git(["add", "--", rel]).returncode == 0


def _conflicts(git: Git) -> list[str]:
    return git(["diff", "--name-only", "--diff-filter=U"]).stdout.split()


def _continue(git: Git) -> subprocess.CompletedProcess:
    """rebase --continue; a commit that became empty is skipped instead."""
    if git(["diff", "--cached", "--quiet"]).returncode == 0:
        return git(["rebase", "--skip"])
    return git(["-c", "core.editor=true", "rebase", "--continue"])


def rebase(git: Git) -> list[str] | None:
    """pull --rebase; resolved derived paths, or None after an abort on any other conflict."""
    resolved: list[str] = []
    proc = git(["pull", "--rebase"])
    for _ in range(MAX_REBASE_STEPS):
        if proc.returncode == 0:
            return sorted(set(resolved))
        conflicts = _conflicts(git)
        if not conflicts or not all(derived(c) and _resolve(git, c) for c in conflicts):
            git(["rebase", "--abort"])
            return None
        resolved += conflicts
        proc = _continue(git)
    git(["rebase", "--abort"])
    return None


def push(git: Git, rebuild: Callable[[list[str]], None], attempts: int = PUSH_ATTEMPTS) -> bool:
    """Push, rebasing onto what other sessions pushed; rebuild(paths) refreshes resolved snapshots
    (and commits) before the next try. False when every try failed or a conflict was not derived."""
    ensure_union_merge(git)
    for _ in range(attempts):
        if git(["push"]).returncode == 0:
            return True
        resolved = rebase(git)
        if resolved is None:
            return False
        if resolved:
            rebuild(resolved)
    return False

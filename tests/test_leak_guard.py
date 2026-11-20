"""Tests for tools/leak_guard.py. Every fixture is synthetic and built in a temp dir."""

from __future__ import annotations

import importlib.util
import io
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("leak_guard", ROOT / "tools" / "leak_guard.py")
leak_guard = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("leak_guard", leak_guard)
_SPEC.loader.exec_module(leak_guard)

RNG_SEED = 1234


def synthetic_payload(points: int, *, nested: bool = True, seed: int = RNG_SEED) -> dict:
    rng = random.Random(seed)
    data = [
        {"date": f"2000-01-01 00:{i:02d}:00 +0000", "qty": round(rng.uniform(1.0, 2.0), 3)}
        for i in range(points)
    ]
    metrics = {"metrics": [{"name": "synthetic_metric", "units": "u", "data": data}]}
    return {"data": metrics} if nested else metrics


def fake_key(prefix: str, length: int) -> str:
    rng = random.Random(RNG_SEED)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return prefix + "".join(rng.choice(alphabet) for _ in range(length))


class TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(leak_guard, "_git_list", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, rel: str, content: str | bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def rules(self) -> set[tuple[str, str]]:
        return {(f.path, f.rule) for f in leak_guard.scan(self.root)}


class CleanTreeTests(TreeCase):
    def test_clean_tree_has_no_findings(self) -> None:
        self.put("homunculus/x.py", "print('hello')\n")
        self.put("docs/notes.md", "# notes\n")
        self.put("config/roster.json", json.dumps({"bots": []}))
        self.assertEqual(leak_guard.scan(self.root), [])

    def test_small_payload_is_allowed(self) -> None:
        self.put("docs/example.json", json.dumps(synthetic_payload(20)))
        self.assertEqual(self.rules(), set())

    def test_walk_skips_git_and_cache_dirs(self) -> None:
        self.put(".git/objects/x.db", b"x")
        self.put("homunculus/__pycache__/m.db", b"x")
        self.assertEqual(self.rules(), set())


class RuleTests(TreeCase):
    def test_sqlite_by_suffix(self) -> None:
        self.put("health.db", b"")
        self.put("sub/data.sqlite3", b"")
        self.assertEqual(self.rules(), {("health.db", "sqlite"), ("sub/data.sqlite3", "sqlite")})

    def test_sqlite_by_header(self) -> None:
        conn = sqlite3.connect(self.root / "blob.bin")
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()
        self.assertEqual(self.rules(), {("blob.bin", "sqlite")})

    def test_hae_payload_nested_and_bare(self) -> None:
        self.put("export.json", json.dumps(synthetic_payload(21)))
        self.put("bare.json", json.dumps(synthetic_payload(25, nested=False)))
        self.assertEqual(self.rules(), {("export.json", "hae_payload"), ("bare.json", "hae_payload")})

    def test_data_dirs(self) -> None:
        self.put("raw/a.txt", "x")
        self.put("x/health_raw/b.txt", "x")
        self.put("ledger/c.md", "x")
        self.put("homunculus/ledger.py", "x")
        expected = {("raw/a.txt", "data_dir"), ("x/health_raw/b.txt", "data_dir"), ("ledger/c.md", "data_dir")}
        self.assertEqual(self.rules(), expected)

    def test_env_files(self) -> None:
        self.put(".env", "A=1\n")
        self.put("config/.env.local", "A=1\n")
        self.put("docs/env.md", "A=1\n")
        self.assertEqual(self.rules(), {(".env", "env_file"), ("config/.env.local", "env_file")})

    def test_api_keys(self) -> None:
        keys = [
            fake_key("sk-" + "ant-", 40),
            fake_key("AI" + "za", 35),
            fake_key("gh" + "o_", 36),
            fake_key("gh" + "p_", 36),
        ]
        for i, key in enumerate(keys):
            self.put(f"homunculus/k{i}.py", f"TOKEN = '{key}'\n")
        details = {f.detail for f in leak_guard.scan(self.root)}
        self.assertEqual(details, {"anthropic key pattern", "google key pattern",
                                   "github-oauth key pattern", "github-pat key pattern"})

    def test_short_prefix_is_not_a_key(self) -> None:
        self.put("homunculus/k.py", "PREFIX = 'sk-" + "ant-'\n")
        self.assertEqual(self.rules(), set())

    def test_large_file_outside_docs(self) -> None:
        big = b"0" * (leak_guard.MAX_BYTES + 1)
        self.put("assets/big.bin", big)
        self.put("docs/big.pdf", big)
        self.assertEqual(self.rules(), {("assets/big.bin", "large_file")})


class SyntheticAllowlistTests(TreeCase):
    def test_synthetic_field_exempts_payload_in_tests(self) -> None:
        doc = synthetic_payload(30)
        doc["synthetic"] = True
        self.put("tests/fixtures/hae.json", json.dumps(doc))
        self.assertEqual(self.rules(), set())

    def test_synthetic_marker_exempts_data_dir_in_tests(self) -> None:
        self.put("tests/fixtures/raw/sample.txt", "# leak-guard: synthetic\nvalue=1\n")
        self.assertEqual(self.rules(), set())

    def test_marker_outside_tests_is_ignored(self) -> None:
        doc = synthetic_payload(30)
        doc["synthetic"] = True
        self.put("docs/hae.json", json.dumps(doc))
        self.assertEqual(self.rules(), {("docs/hae.json", "hae_payload")})

    def test_unmarked_payload_in_tests_is_flagged(self) -> None:
        self.put("tests/fixtures/hae.json", json.dumps(synthetic_payload(30)))
        self.assertEqual(self.rules(), {("tests/fixtures/hae.json", "hae_payload")})

    def test_marker_does_not_exempt_secrets_or_sqlite(self) -> None:
        key = fake_key("gh" + "p_", 36)
        self.put("tests/fixtures/k.py", f"# leak-guard: synthetic\nK = '{key}'\n")
        self.put("tests/fixtures/x.db", b"")
        expected = {("tests/fixtures/k.py", "api_key"), ("tests/fixtures/x.db", "sqlite")}
        self.assertEqual(self.rules(), expected)


class CandidateFileTests(unittest.TestCase):
    def test_union_of_listed_and_staged(self) -> None:
        answers = {"ls-files": ["b.py", "a.py"], "diff": ["c.py", "a.py"]}
        with mock.patch.object(leak_guard, "_git_list", side_effect=lambda root, args: answers[args[0]]):
            self.assertEqual(leak_guard.candidate_files(Path(".")), ["a.py", "b.py", "c.py"])

    def test_git_list_parses_nul_output(self) -> None:
        done = subprocess.CompletedProcess(["git"], 0, stdout=b"a.py\0dir/b.py\0", stderr=b"")
        with mock.patch.object(leak_guard.subprocess, "run", return_value=done):
            self.assertEqual(leak_guard._git_list(Path("."), ["ls-files"]), ["a.py", "dir/b.py"])

    def test_git_failure_falls_back(self) -> None:
        failed = subprocess.CompletedProcess(["git"], 128, stdout=b"", stderr=b"not a repo")
        with mock.patch.object(leak_guard.subprocess, "run", return_value=failed):
            self.assertIsNone(leak_guard._git_list(Path("."), ["ls-files"]))
        with mock.patch.object(leak_guard.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(leak_guard._git_list(Path("."), ["ls-files"]))

    def test_listed_but_deleted_file_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(leak_guard.scan(Path(tmp), ["gone.db"]), [])


class MainTests(TreeCase):
    def run_main(self, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = leak_guard.main(["--root", str(self.root), *args])
        return code, err.getvalue()

    def test_exit_zero_when_clean(self) -> None:
        self.put("a.py", "x = 1\n")
        self.assertEqual(self.run_main()[0], 0)

    def test_exit_one_with_findings(self) -> None:
        self.put("health.db", b"")
        code, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("health.db: sqlite", err)

    def test_explicit_paths(self) -> None:
        self.put("health.db", b"")
        self.put("a.py", "x = 1\n")
        self.assertEqual(self.run_main(str(self.root / "a.py"))[0], 0)
        self.assertEqual(self.run_main(str(self.root / "health.db"))[0], 1)


class SelfScanTests(unittest.TestCase):
    def test_guard_and_its_tests_are_clean(self) -> None:
        files = ["tools/leak_guard.py", "tests/test_leak_guard.py", ".githooks/pre-commit", ".github/workflows/ci.yml"]
        self.assertEqual(leak_guard.scan(ROOT, files), [])


if __name__ == "__main__":
    unittest.main()

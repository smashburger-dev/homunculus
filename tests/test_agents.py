"""Tests for homunculus.agents. No network: every provider gets a fake opener."""

from __future__ import annotations

import dataclasses
import io
import json
import random
import shutil
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from homunculus import agents
from homunculus.agents import (
    AnthropicProvider,
    BotSpec,
    Budget,
    BudgetExceeded,
    DevinProvider,
    FakeProvider,
    GeminiProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    ProviderUnavailable,
    SchemaViolation,
)
from homunculus.store import Ledger, PathNotAllowed

ROOT = Path(__file__).resolve().parents[1]
RNG_SEED = 4711
PRICES = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.4, "cache_write": 5.0},
    "claude-haiku-5-5": {"input": 0.1, "output": 0.5},
    "gemini-test": {"input": 1.0, "output": 2.0},
}
SCHEMA = {
    "type": "object",
    "required": ["verdict", "score"],
    "properties": {
        "verdict": {"type": "string", "enum": ["keep", "change"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 3},
        "note": {"type": ["string", "null"], "maxLength": 20},
    },
    "additionalProperties": False,
}
NOW = datetime(2026, 11, 22, 19, 0, tzinfo=timezone.utc)  # Sunday, ISO week 2026-W47


def _roster() -> dict[str, BotSpec]:
    return agents.load_roster(json.loads((ROOT / "config" / "roster.json").read_text(encoding="utf-8")))


def _req(bot: str = "hippocampus", schema=SCHEMA, effort: str = "high") -> LLMRequest:
    return LLMRequest(bot=bot, system="sys", messages=({"role": "user", "content": "hi"},),
                      output_schema=schema, max_tokens=100, effort=effort)


def _resp(usd: float = 0.0, model: str = "claude-opus-5-5", structured=None) -> LLMResponse:
    return LLMResponse(text=json.dumps(structured), structured=structured, model=model, tokens_in=10,
                       tokens_out=5, cache_read=0, cache_write=0, usd=usd, stop_reason="end_turn")


class FakeOpener:
    """Returns queued replies (dict, str or bytes) or raises queued exceptions; records requests."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, req):
        self.requests.append(req)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, bytes):
            return reply
        return (reply if isinstance(reply, str) else json.dumps(reply)).encode("utf-8")

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].data)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(b"{}"))


def _anthropic_reply(payload, stop="end_turn", **usage) -> dict:
    return {
        "model": "claude-opus-5-5", "stop_reason": stop,
        "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": json.dumps(payload)}],
        "usage": {"input_tokens": 1000, "output_tokens": 500, **usage},
    }


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.roster = _roster()
        self.ledger = Ledger(self.tmp, agents.write_rules(self.roster))
        self.clock = [NOW]
        self.budget = Budget(self.ledger, {"weekly_cap_usd": 10.0, "reduce_at": 0.8, "prices": PRICES},
                             lambda: self.clock[0])

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def spend(self, bot: str, usd: float) -> None:
        self.budget.record(bot, _resp(usd), "r-test")


class RosterTest(unittest.TestCase):
    def test_config_roster_has_all_bots(self) -> None:
        roster = _roster()
        self.assertEqual(set(roster), set(agents.KNOWN_BOTS))
        self.assertEqual(roster["cerebellum"].model, "claude-fable-5-2")
        self.assertEqual(roster["insula"].model, "claude-sonnet-5-5")
        self.assertEqual(roster["retina"].model, "claude-haiku-5-5")
        self.assertEqual(roster["pineal"].provider, "none")
        models = {b.model for b in roster.values() if b.provider != "none"}
        self.assertLessEqual(models, set(agents.MODEL_TIERS))

    def test_rejects_unknown_duplicate_and_bad_entries(self) -> None:
        good = json.loads((ROOT / "config" / "roster.json").read_text(encoding="utf-8"))["bots"][0]
        cases = [
            {**good, "name": "frontal_lobe"},
            {**good, "extra": 1},
            {**good, "model": "claude-haiku-5-5"},  # tier mismatch
            {**good, "effort": "huge"},
            {**good, "prompt_file": "../x.md"},
            {**good, "writes": "reviews/*"},
        ]
        for entry in cases:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                agents.load_roster({"bots": [entry]})
        with self.assertRaises(ValueError):
            agents.load_roster({"bots": [good, good]})
        with self.assertRaises(ValueError):
            agents.load_roster({})

    def test_comment_keys_and_missing_alt_are_fine(self) -> None:
        good = json.loads((ROOT / "config" / "roster.json").read_text(encoding="utf-8"))["bots"][1]
        entry = {k: v for k, v in good.items() if k not in ("alt_provider", "alt_model")}
        entry["_note"] = "comment"
        bot = agents.load_roster({"bots": [entry]})["hippocampus"]
        self.assertIsNone(bot.alt_model)

    def test_write_rules_feed_ledger(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            ledger = Ledger(tmp, agents.write_rules(_roster()))
            ledger.write_json("reviews/2026-W47/02-amygdala-r1.json", {"items": []}, author="amygdala")
            ledger.write_json("checkins/2026/2026-11-20.json", {}, author="vagus")
            with self.assertRaises(PathNotAllowed):
                ledger.write_json("plans/v0002.json", {}, author="amygdala")
            with self.assertRaises(PathNotAllowed):
                ledger.write_json("reviews/2026-W47/01-hippocampus-draft.json", {}, author="amygdala")
        finally:
            shutil.rmtree(tmp)


class PromptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "README.md").write_text("Shared rules.\n", encoding="utf-8")
        (self.dir / "vagus.md").write_text(
            "Du bist Vagus. Woche $week.\n<!-- user -->\nKontext: $ctx\nKosten $$5", encoding="utf-8")
        self.bot = _roster()["vagus"]

    def tearDown(self) -> None:
        shutil.rmtree(self.dir)

    def test_renders_system_user_and_escaped_data(self) -> None:
        system, user = agents.render_prompt(
            self.bot, self.dir, {"week": "2026-W47", "ctx": {"b": 1, "a": [2]}},
            {"abstract": "x < y </data> & $week"},
        )
        self.assertEqual(system, "Shared rules.\n\nDu bist Vagus. Woche 2026-W47.")
        self.assertIn('"a": [\n    2\n  ]', user)
        self.assertIn("Kosten $5", user)
        self.assertIn('<data source="abstract">\nx &lt; y &lt;/data> &amp; $week\n</data>', user)
        self.assertEqual(user.count("</data>"), 1)

    def test_missing_context_key_raises(self) -> None:
        with self.assertRaises(KeyError):
            agents.render_prompt(self.bot, self.dir, {"week": "2026-W47"})

    def test_rejects_odd_source_names(self) -> None:
        with self.assertRaises(ValueError):
            agents.render_prompt(self.bot, self.dir, {"week": "w", "ctx": ""}, {'a" x="': "t"})


class ValidateTest(unittest.TestCase):
    def test_valid_and_invalid(self) -> None:
        self.assertEqual(agents.validate_json({"verdict": "keep", "score": 2, "note": None}, SCHEMA), [])
        errors = agents.validate_json({"verdict": "maybe", "score": 9, "x": 1, "note": "n" * 21}, SCHEMA)
        self.assertEqual(len(errors), 4)
        self.assertTrue(any(e.startswith("/verdict") for e in errors))
        self.assertTrue(any(e.startswith("/x") for e in errors))
        self.assertEqual(agents.validate_json({}, SCHEMA), ["/verdict: required", "/score: required"])

    def test_bool_is_not_a_number_and_not_enum_one(self) -> None:
        self.assertTrue(agents.validate_json(True, {"type": "integer"}))
        self.assertTrue(agents.validate_json(True, {"enum": [1]}))
        self.assertEqual(agents.validate_json(2.0, {"type": "integer"}), [])

    def test_arrays_and_nested_paths(self) -> None:
        schema = {"type": "array", "minItems": 1, "maxItems": 2,
                  "items": {"type": "object", "properties": {"cid": {"type": "string"}}, "required": ["cid"]}}
        self.assertEqual(agents.validate_json([{"cid": "c1"}], schema), [])
        self.assertEqual(agents.validate_json([{"cid": 3}], schema), ["/0/cid: expected string"])
        self.assertEqual(len(agents.validate_json([], schema)), 1)
        self.assertEqual(len(agents.validate_json([{"cid": "a"}] * 3, schema)), 1)

    def test_minimal_instance_is_valid(self) -> None:
        rng = random.Random(RNG_SEED)
        for _ in range(20):
            low = rng.randint(0, 5)
            schema = {"type": "object", "required": ["n", "s", "l"], "properties": {
                "n": {"type": "number", "minimum": low},
                "s": {"type": "string", "minLength": low},
                "l": {"type": "array", "minItems": low, "items": {"type": "boolean"}},
            }}
            self.assertEqual(agents.validate_json(agents.minimal_instance(schema), schema), [])
        self.assertEqual(agents.minimal_instance(SCHEMA), {"verdict": "keep", "score": 0})

    def test_wire_schema_drops_unsupported_and_closes_objects(self) -> None:
        wired = agents.wire_schema({"type": "object", "properties": {
            "a": {"type": "integer", "minimum": 0},
            "b": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {}}},
        }})
        self.assertEqual(wired["properties"]["a"], {"type": "integer"})
        self.assertFalse(wired["additionalProperties"])
        self.assertFalse(wired["properties"]["b"]["items"]["additionalProperties"])
        self.assertNotIn("maxItems", wired["properties"]["b"])


class PriceTest(unittest.TestCase):
    def test_price(self) -> None:
        usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 1_000_000,
                 "cache_creation_input_tokens": 100_000}
        self.assertAlmostEqual(agents.price("claude-opus-5-5", usage, PRICES), 4 + 2 + 0.4 + 0.5)
        self.assertAlmostEqual(agents.price("claude-opus-5-5", usage, PRICES, batch=True), 3.45)
        self.assertAlmostEqual(agents.price("claude-haiku-5-5", {"cache_read_input_tokens": 1_000_000}, PRICES), 0.01)
        with self.assertRaises(ValueError):
            agents.price("unknown", usage, PRICES)


class AnthropicTest(unittest.TestCase):
    def test_complete_request_and_response(self) -> None:
        opener = FakeOpener(_anthropic_reply({"verdict": "keep", "score": 1}, cache_read_input_tokens=200))
        resp = AnthropicProvider("k-test", PRICES, opener).complete(_req(), "claude-opus-5-5")
        req = opener.requests[0]
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(req.get_header("X-api-key"), "k-test")
        self.assertEqual(req.get_header("Anthropic-version"), agents.ANTHROPIC_VERSION)
        body = opener.body()
        self.assertEqual(body["output_config"]["effort"], "high")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("minimum", json.dumps(body["output_config"]["format"]["schema"]))
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        for key in ("thinking", "temperature", "tool_choice", "top_p"):
            self.assertNotIn(key, body)
        self.assertEqual(resp.structured, {"verdict": "keep", "score": 1})
        self.assertEqual((resp.tokens_in, resp.tokens_out, resp.cache_read), (1000, 500, 200))
        self.assertAlmostEqual(resp.usd, (1000 * 4 + 500 * 20 + 200 * 0.4) / 1e6)

    def test_uncached_text_reply(self) -> None:
        opener = FakeOpener(_anthropic_reply("plain"))
        resp = AnthropicProvider("k", PRICES, opener).complete(
            dataclasses.replace(_req(schema=None), cache_system=False), "claude-opus-5-5")
        self.assertEqual(opener.body()["system"], "sys")
        self.assertNotIn("format", opener.body()["output_config"])
        self.assertIsNone(resp.structured)

    def test_errors(self) -> None:
        cases = [
            (_anthropic_reply({}, stop="refusal"), ProviderError),
            (_anthropic_reply({}, stop="max_tokens"), ProviderError),
            ({"stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}], "usage": {}}, SchemaViolation),
            (_http_error(529), ProviderUnavailable),
            (_http_error(429), ProviderUnavailable),
            (urllib.error.URLError("down"), ProviderUnavailable),
            (b"<html>", ProviderError),
        ]
        for reply, exc in cases:
            with self.subTest(exc=exc), self.assertRaises(exc) as ctx:
                AnthropicProvider("k", PRICES, FakeOpener(reply)).complete(_req(), "claude-opus-5-5")
            if exc is ProviderError:
                self.assertNotIsInstance(ctx.exception, ProviderUnavailable)
        with self.assertRaises(ProviderError) as ctx:
            AnthropicProvider("k", PRICES, FakeOpener(_http_error(400))).complete(_req(), "claude-opus-5-5")
        self.assertNotIsInstance(ctx.exception, ProviderUnavailable)

    def test_unpriced_model_fails_before_any_request(self) -> None:
        opener = FakeOpener()
        with self.assertRaises(ProviderError):
            AnthropicProvider("k", PRICES, opener).complete(_req(), "claude-unknown")
        self.assertEqual(opener.requests, [])
        with self.assertRaises(ProviderUnavailable):
            AnthropicProvider("", PRICES, opener)

    def test_batch_roundtrip(self) -> None:
        lines = "\n".join(json.dumps(x) for x in [
            {"custom_id": "c1", "result": {"type": "succeeded", "message": _anthropic_reply({"verdict": "keep", "score": 0})}},
            {"custom_id": "c2", "result": {"type": "errored", "error": {"type": "invalid_request"}}},
            {"custom_id": "c3", "result": {"type": "succeeded", "message": _anthropic_reply({}, stop="refusal")}},
        ])
        opener = FakeOpener(
            {"id": "msgbatch_1", "processing_status": "in_progress"},
            {"id": "msgbatch_1", "processing_status": "in_progress"},
            {"id": "msgbatch_1", "processing_status": "ended", "results_url": "https://api.anthropic.com/r/1"},
            lines,
        )
        provider = AnthropicProvider("k", PRICES, opener)
        batch_id = provider.submit_batch([("c1", _req("retina")), ("c2", _req("retina"))], "claude-opus-5-5")
        self.assertEqual(batch_id, "msgbatch_1")
        sent = opener.body(0)["requests"]
        self.assertEqual([r["custom_id"] for r in sent], ["c1", "c2"])
        self.assertEqual(sent[0]["params"]["model"], "claude-opus-5-5")
        self.assertIsNone(provider.batch_results(batch_id))
        results = AnthropicProvider("k", PRICES, opener).batch_results(batch_id)  # fresh process
        self.assertEqual(list(results), ["c1"])
        self.assertAlmostEqual(results["c1"].usd, (1000 * 4 + 500 * 20) / 1e6 / 2)
        self.assertEqual(results["c1"].structured, {"verdict": "keep", "score": 0})
        self.assertTrue(opener.requests[2].full_url.endswith("/v1/messages/batches/msgbatch_1"))

    def test_batch_with_unpriced_model_fails_loudly(self) -> None:
        message = {**_anthropic_reply({}), "model": "claude-unknown"}
        opener = FakeOpener({"processing_status": "ended", "results_url": "https://api.anthropic.com/r/2"},
                            json.dumps({"custom_id": "c1", "result": {"type": "succeeded", "message": message}}))
        with self.assertRaises(ProviderError):
            AnthropicProvider("k", PRICES, opener).batch_results("b2")


class GeminiTest(unittest.TestCase):
    def test_complete(self) -> None:
        reply = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": "ignored", "thought": True}, {"text": '{"verdict": "change", "score": 3}'}]}}],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100, "thoughtsTokenCount": 50,
                              "cachedContentTokenCount": 200}}
        opener = FakeOpener(reply)
        req = dataclasses.replace(_req("amygdala"), messages=(
            {"role": "user", "content": [{"type": "text", "text": "a"}]}, {"role": "assistant", "content": "b"}))
        resp = GeminiProvider("g-test", PRICES, opener).complete(req, "gemini-test")
        self.assertIn("models/gemini-test:generateContent", opener.requests[0].full_url)
        self.assertEqual(opener.requests[0].get_header("X-goog-api-key"), "g-test")
        body = opener.body()
        self.assertEqual([c["role"] for c in body["contents"]], ["user", "model"])
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(resp.structured, {"verdict": "change", "score": 3})
        self.assertEqual((resp.tokens_in, resp.tokens_out, resp.cache_read), (800, 150, 200))

    def test_refusal_and_block(self) -> None:
        for reply in ({"candidates": [{"finishReason": "SAFETY"}]}, {"promptFeedback": {"blockReason": "OTHER"}}):
            with self.subTest(reply=reply), self.assertRaises(ProviderError) as ctx:
                GeminiProvider("g", PRICES, FakeOpener(reply)).complete(_req(), "gemini-test")
            self.assertNotIsInstance(ctx.exception, ProviderUnavailable)


class DevinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []

    def provider(self, *replies, **kwargs) -> tuple[DevinProvider, FakeOpener]:
        opener = FakeOpener(*replies)
        args = {"bots": ["retina"], "base_url": "https://devin.test", "poll_interval_s": 7.0, "max_polls": 3}
        args.update(kwargs)
        return DevinProvider("d-test", opener=opener, sleep=self.sleeps.append, **args), opener

    def test_session_roundtrip(self) -> None:
        provider, opener = self.provider(
            {"session_id": "devin-abc"},
            {"status_enum": "working", "structured_output": None},
            {"status_enum": "finished", "structured_output": '{"verdict": "keep", "score": 2}'},
            max_acu_limit=3,
        )
        resp = provider.complete(_req("retina"), "claude-haiku-5-5")
        self.assertEqual(resp.structured, {"verdict": "keep", "score": 2})
        self.assertEqual(resp.usd, 0.0)
        self.assertEqual(resp.model, "devin:claude-haiku-5-5")
        self.assertEqual(self.sleeps, [7.0])
        self.assertEqual(opener.requests[0].full_url, "https://devin.test/v1/sessions")
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer d-test")
        self.assertEqual(opener.requests[1].full_url, "https://devin.test/v1/session/devin-abc")
        body = opener.body(0)
        self.assertEqual(body["max_acu_limit"], 3)
        self.assertIn('"enum": ["keep", "change"]', body["prompt"])
        self.assertIn("sys", body["prompt"])

    def test_failures(self) -> None:
        cases = [
            (({"session_id": "s"}, {"status_enum": "expired"}), ProviderError),
            (({"session_id": "s"}, {"status_enum": "finished", "structured_output": None}), SchemaViolation),
            (({"session_id": "s"}, {"status_enum": "working"}, {"status_enum": "working"},
              {"status_enum": "working"}), ProviderUnavailable),
            (({},), ProviderError),
            ((_http_error(503),), ProviderUnavailable),
        ]
        for replies, exc in cases:
            with self.subTest(replies=replies), self.assertRaises(exc):
                self.provider(*replies)[0].complete(_req("retina"), "m")

    def test_bot_not_enabled(self) -> None:
        provider, opener = self.provider()
        with self.assertRaises(ProviderError):
            provider.complete(_req("hippocampus"), "m")
        self.assertEqual(opener.requests, [])
        with self.assertRaises(ProviderUnavailable):
            provider.submit_batch([], "m")

    def test_from_config(self) -> None:
        env = {"MY_DEVIN": "d-key"}
        self.assertIsNone(agents.devin_from_config(None, env))
        self.assertIsNone(agents.devin_from_config({"enabled": True, "bots": ["retina"]}, {}))
        self.assertIsNone(agents.devin_from_config({"enabled": True, "api_key_env": "MY_DEVIN"}, env))
        roster_cfg = json.loads((ROOT / "config" / "roster.json").read_text(encoding="utf-8"))["devin"]
        self.assertIsNone(agents.devin_from_config(roster_cfg, {"DEVIN_API_KEY": "x"}))
        opener = FakeOpener({"session_id": "s"}, {"status_enum": "finished", "structured_output": {"a": 1}})
        provider = agents.devin_from_config(
            {"enabled": True, "api_key_env": "MY_DEVIN", "bots": ["retina"], "base_url": "https://devin.test/",
             "poll_interval_s": 1}, env, opener=opener, sleep=self.sleeps.append)
        self.assertIsInstance(provider, DevinProvider)
        self.assertEqual(provider.complete(_req("retina", schema=None), "m").structured, {"a": 1})
        self.assertEqual(opener.requests[0].full_url, "https://devin.test/v1/sessions")


class FakeProviderTest(unittest.TestCase):
    def test_fixtures(self) -> None:
        fake = FakeProvider({
            "hippocampus": {"verdict": "change", "score": 1},
            "amygdala": lambda req: "plain text",
            "insula": ProviderUnavailable("down"),
        })
        self.assertEqual(fake.complete(_req(), "m").structured, {"verdict": "change", "score": 1})
        self.assertEqual(fake.complete(_req("amygdala", schema=None), "m").text, "plain text")
        self.assertEqual(fake.complete(_req("thalamus"), "m").structured, {"verdict": "keep", "score": 0})
        with self.assertRaises(ProviderUnavailable):
            fake.complete(_req("insula"), "m")
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(fake.calls[0][1], "m")

    def test_batch(self) -> None:
        fake = FakeProvider()
        batch_id = fake.submit_batch([("a", _req("retina")), ("b", _req("retina"))], "claude-haiku-5-5")
        self.assertEqual(sorted(fake.batch_results(batch_id)), ["a", "b"])
        with self.assertRaises(ProviderError):
            fake.batch_results("nope")


class BudgetTest(LedgerCase):
    def test_levels_follow_weekly_spend(self) -> None:
        self.assertEqual(self.budget.level(), "normal")
        self.spend("hippocampus", 7.9)
        self.assertEqual(self.budget.level(), "normal")
        self.spend("hippocampus", 0.1)
        self.assertEqual(self.budget.level(), "reduced")
        self.spend("hippocampus", 2.0)
        self.assertEqual(self.budget.level(), "core_only")
        self.clock[0] = NOW + timedelta(days=1)  # Monday, new ISO week
        self.assertEqual(self.budget.level(), "normal")

    def test_record_line(self) -> None:
        self.spend("vagus", 0.25)
        rows = self.ledger.read_jsonl(agents.BUDGET_LOG)
        self.assertEqual(rows[0]["week"], "2026-W47")
        self.assertEqual(rows[0]["date"], "2026-11-22")
        self.assertEqual((rows[0]["bot"], rows[0]["usd"], rows[0]["run_id"]), ("vagus", 0.25, "r-test"))

    def test_allow_rules(self) -> None:
        roster = self.roster
        self.assertTrue(self.budget.allow(roster["retina"], 0.01))
        self.assertFalse(self.budget.allow(roster["retina"], 1.0))  # daily cap 0.25
        self.assertFalse(self.budget.allow(roster["pineal"], 0.0))
        self.spend("hippocampus", 8.5)
        self.assertFalse(self.budget.allow(roster["retina"], 0.01))  # scouts pause at 80%
        self.assertTrue(self.budget.allow(roster["vagus"], 0.1))
        self.assertFalse(self.budget.allow(roster["vagus"], 2.0))  # crosses the weekly cap
        self.spend("motor_cortex", 1.5)
        self.assertFalse(self.budget.allow(roster["vagus"], 0.01))
        self.assertTrue(self.budget.allow(roster["amygdala"], 0.5))  # weekly review still runs
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "HALT").write_text("", encoding="utf-8")
        self.assertFalse(self.budget.allow(roster["amygdala"], 0.5))

    def test_daily_cap_resets_next_day(self) -> None:
        rng = random.Random(RNG_SEED)
        spent = [round(rng.uniform(0.3, 0.4), 3) for _ in range(4)]
        for usd in spent:
            self.spend("sartorius", usd)
        self.assertFalse(self.budget.allow(self.roster["sartorius"], 0.2))
        self.clock[0] = NOW - timedelta(days=1)
        self.assertTrue(self.budget.allow(self.roster["sartorius"], 0.2))


class CallTest(LedgerCase):
    def test_success_is_recorded(self) -> None:
        fake = FakeProvider({"hippocampus": _resp(0.5, structured={"verdict": "keep", "score": 1})})
        resp = agents.call(self.roster["hippocampus"], _req(), {"anthropic": fake}, self.budget, "r-1")
        self.assertEqual(resp.structured["score"], 1)
        self.assertEqual(fake.calls[0][1], "claude-opus-5-5")
        rows = self.ledger.read_jsonl(agents.BUDGET_LOG)
        self.assertEqual([(r["bot"], r["usd"], r["run_id"]) for r in rows], [("hippocampus", 0.5, "r-1")])

    def test_schema_violation_after_recording(self) -> None:
        fake = FakeProvider({"hippocampus": _resp(0.2, structured={"verdict": "maybe", "score": 1})})
        with self.assertRaises(SchemaViolation):
            agents.call(self.roster["hippocampus"], _req(), {"anthropic": fake}, self.budget, "r-1")
        self.assertEqual(len(self.ledger.read_jsonl(agents.BUDGET_LOG)), 1)
        fake = FakeProvider({"hippocampus": _resp(0.0, structured=None)})
        with self.assertRaises(SchemaViolation):
            agents.call(self.roster["hippocampus"], _req(), {"anthropic": fake}, self.budget, "r-2")

    def test_no_fallback_to_a_lower_tier(self) -> None:
        fake = FakeProvider({"sartorius": ProviderUnavailable("down")})
        with self.assertRaises(ProviderUnavailable):
            agents.call(self.roster["sartorius"], _req("sartorius"), {"anthropic": fake}, self.budget, "r")
        self.assertEqual([m for _, m in fake.calls], ["claude-opus-5-5"])
        self.assertEqual(self.ledger.read_jsonl(agents.BUDGET_LOG), [])

    def test_same_tier_fallback_when_primary_missing(self) -> None:
        bot = dataclasses.replace(self.roster["amygdala"], provider="gemini", model="gemini-test",
                                  alt_provider="anthropic", alt_model="claude-opus-5-5")
        fake = FakeProvider()
        resp = agents.call(bot, _req("amygdala"), {"anthropic": fake}, self.budget, "r")
        self.assertEqual(resp.model, "claude-opus-5-5")
        self.assertEqual(resp.structured, {"verdict": "keep", "score": 0})

    def test_refusal_does_not_fall_back(self) -> None:
        bot = dataclasses.replace(self.roster["amygdala"], alt_provider="anthropic", alt_model="claude-opus-5-5")
        fake = FakeProvider({"amygdala": ProviderError("refused")})
        with self.assertRaises(ProviderError):
            agents.call(bot, _req("amygdala"), {"anthropic": fake}, self.budget, "r")
        self.assertEqual(len(fake.calls), 1)

    def test_budget_and_code_bots(self) -> None:
        self.spend("hippocampus", 10.0)
        fake = FakeProvider()
        with self.assertRaises(BudgetExceeded):
            agents.call(self.roster["vagus"], _req("vagus"), {"anthropic": fake}, self.budget, "r")
        with self.assertRaises(ProviderError):
            agents.call(self.roster["pineal"], _req("pineal"), {"anthropic": fake}, self.budget, "r")
        self.assertEqual(fake.calls, [])

    def test_coaches_drop_to_low_effort_when_reduced(self) -> None:
        self.spend("hippocampus", 8.5)
        fake = FakeProvider()
        bot = dataclasses.replace(self.roster["vagus"], effort="medium")
        agents.call(bot, _req("vagus", effort="medium"), {"anthropic": fake}, self.budget, "r")
        agents.call(self.roster["insula"], _req("insula", effort="medium"), {"anthropic": fake}, self.budget, "r")
        self.assertEqual([r.effort for r, _ in fake.calls], ["low", "medium"])

    def test_devin_bot_through_call_is_validated(self) -> None:
        bot = dataclasses.replace(self.roster["retina"], provider="devin")
        opener = FakeOpener({"session_id": "s"}, {"status_enum": "finished", "structured_output": {"verdict": 1}})
        devin = DevinProvider("d", bots=["retina"], opener=opener, sleep=lambda s: None)
        with self.assertRaises(SchemaViolation):
            agents.call(bot, _req("retina"), {"devin": devin}, self.budget, "r")


class MessageTest(unittest.TestCase):
    SHA = "ab" * 32

    def make(self, **overrides) -> dict:
        args = dict(sender="amygdala", to=["hippocampus"], cc=["cerebellum"], kind="challenge",
                    thread="review/2026-W47", round_=1, max_rounds=2,
                    refs=["reviews/2026-W47/packet.json#/hrv"], data_sha=self.SHA,
                    run_id="r-2026W47-amygdala-1", model="claude-opus-5-5", body="text",
                    items=[{"cid": "c1", "severity": "blocker", "claim": "c", "ref": "x"}],
                    created_at="2026-11-22T19:05:00Z")
        args.update(overrides)
        return agents.make_message(**args)

    def test_id_is_deterministic(self) -> None:
        a = agents.message_id("amygdala", "challenge", "review/2026-W47", 1, self.SHA)
        self.assertRegex(a, r"^m-[0-9a-f]{12}$")
        self.assertEqual(a, agents.message_id("amygdala", "challenge", "review/2026-W47", 1, self.SHA))
        self.assertNotEqual(a, agents.message_id("amygdala", "challenge", "review/2026-W47", 2, self.SHA))
        self.assertEqual(self.make()["id"], a)

    def test_envelope_shape(self) -> None:
        msg = self.make()
        self.assertEqual(msg["from"], "amygdala")
        self.assertEqual(msg["schema"], 1)
        self.assertEqual(agents.validate_message(msg), [])
        self.assertEqual(self.make(), msg)

    def test_invalid_messages(self) -> None:
        cases = [
            {"kind": "gossip"}, {"sender": "frontal_lobe"}, {"to": []}, {"to": ["nobody"]},
            {"round_": 3}, {"data_sha": "short"}, {"created_at": "2026-11-22 19:05"},
            {"items": [{"severity": "huge"}]},
        ]
        for override in cases:
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.make(**override)
        self.assertTrue(agents.validate_message({"schema": 1}))
        self.assertEqual(agents.validate_message([]), ["message is not an object"])

    def test_commit_trailers(self) -> None:
        msg = self.make()
        trailers = agents.commit_trailers(msg, _resp(0.0123))
        self.assertEqual(trailers["Msg-Id"], msg["id"])
        self.assertEqual(trailers["Kind"], "challenge")
        self.assertEqual((trailers["Tokens-In"], trailers["Tokens-Out"], trailers["Usd"]), ("10", "5", "0.012300"))
        self.assertEqual(agents.commit_trailers(msg, None)["Model"], "claude-opus-5-5")
        self.assertEqual(set(trailers), {"Msg-Id", "Run-Id", "Model", "Tokens-In", "Tokens-Out", "Usd", "Kind"})


if __name__ == "__main__":
    unittest.main()

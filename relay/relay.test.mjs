// node --test relay/ (synthetic values only)
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { KEY_RE, prepare, sameSecret, strip } from "./relay.mjs";

const payload = {
  data: {
    metrics: [{ name: "step_count", units: "count", data: [{ date: "2026-11-19 12:00:00 +0000", qty: 6000 }] }],
    workouts: [{
      name: "Run", location: "Outdoor", start: "2026-11-19 07:00:00 +0000",
      route: [{ lat: 1.5, lon: 2.5 }], locationName: "Synthetic Park",
      heartRateData: [{ qty: 120 }], latitude: 1.5,
    }],
    ecg: [{ classification: "Sinus Rhythm", voltageMeasurements: [{ voltage: 0.1 }] }],
  },
};

test("strip drops routes, coordinates, place names and ECG, keeps the indoor tag", () => {
  const out = strip(payload);
  const w = out.data.workouts[0];
  assert.equal(w.location, "Outdoor");
  assert.deepEqual(w.heartRateData, [{ qty: 120 }]);
  for (const gone of ["route", "locationName", "latitude"]) assert.ok(!(gone in w), gone);
  assert.ok(!("ecg" in out.data));
  assert.equal(strip({ location: "Synthetic Street 1" }).location, undefined);
  assert.equal(out.data.metrics[0].data[0].qty, 6000);
});

test("prepare returns a pointer that matches the stored bytes", async () => {
  const { bytes, pointer } = await prepare(JSON.stringify(payload));
  assert.match(pointer.object_key, KEY_RE);
  assert.equal(pointer.bytes, bytes.length);
  assert.equal(pointer.sha256, createHash("sha256").update(bytes).digest("hex"));
  assert.deepEqual(Object.keys(pointer).sort(), ["bytes", "object_key", "sha256"]);
  assert.ok(!new TextDecoder().decode(bytes).includes("Synthetic Park"));
  await assert.rejects(prepare("not json"));
});

test("sameSecret", () => {
  assert.ok(sameSecret("synthetic-token", "synthetic-token"));
  assert.ok(!sameSecret("synthetic-tokeN", "synthetic-token"));
  assert.ok(!sameSecret("synthetic", "synthetic-token"));
  assert.ok(!sameSecret("", ""));
  assert.ok(!sameSecret(undefined, "synthetic-token"));
});

function fakeEnv() {
  const kv = new Map();
  return {
    INGEST_TOKEN: "synthetic-ingest", FETCH_TOKEN: "synthetic-fetch",
    DEVIN_WEBHOOK_URL: "https://devin.invalid/hook", DEVIN_WEBHOOK_SECRET: "synthetic-hook",
    kv,
    RELAY_KV: {
      put: async (k, v) => { kv.set(k, new Uint8Array(v)); },
      get: async (k) => (kv.has(k) ? kv.get(k).buffer : null),
      delete: async (k) => { kv.delete(k); },
    },
  };
}

test("worker: HAE post is stripped and stored, Devin gets only the pointer, fetch and delete need the token", async () => {
  const { default: worker } = await import("./worker.mjs");
  const env = fakeEnv();
  const sent = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => { sent.push({ url, init }); return new Response(null, { status: 200 }); };
  try {
    const post = (token) => worker.fetch(new Request("https://relay.invalid/hae", {
      method: "POST", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(payload),
    }), env);
    assert.equal((await post("wrong")).status, 401);
    assert.equal((await post("synthetic-ingest")).status, 202);
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.equal(sent.length, 1);
  assert.equal(sent[0].init.headers["X-Webhook-Secret"], "synthetic-hook");
  const pointer = JSON.parse(sent[0].init.body);
  assert.deepEqual(Object.keys(pointer).sort(), ["bytes", "object_key", "sha256"]);
  const url = `https://relay.invalid/objects/${pointer.object_key}`;
  const auth = { Authorization: "Bearer synthetic-fetch" };
  assert.equal((await worker.fetch(new Request(url, { headers: { Authorization: "Bearer synthetic-ingest" } }), env)).status, 401);
  const got = new Uint8Array(await (await worker.fetch(new Request(url, { headers: auth }), env)).arrayBuffer());
  assert.equal(got.length, pointer.bytes);
  assert.equal(createHash("sha256").update(got).digest("hex"), pointer.sha256);
  assert.ok(!new TextDecoder().decode(got).includes("route"));
  assert.equal((await worker.fetch(new Request(url, { method: "DELETE", headers: auth }), env)).status, 204);
  assert.equal((await worker.fetch(new Request(url, { headers: auth }), env)).status, 404);
});

test("worker: a failed Devin webhook removes the object", async () => {
  const { default: worker } = await import("./worker.mjs");
  const env = fakeEnv();
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("offline"); };
  try {
    const res = await worker.fetch(new Request("https://relay.invalid/hae", {
      method: "POST", headers: { Authorization: "Bearer synthetic-ingest" }, body: JSON.stringify(payload),
    }), env);
    assert.equal(res.status, 502);
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.equal(env.kv.size, 0);
});

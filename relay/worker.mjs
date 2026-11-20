// homunculus relay: optional Cloudflare Worker in front of the Devin ingest-relay webhook (path a).
//
//   POST   /hae           Health Auto Export, header "Authorization: Bearer <INGEST_TOKEN>"
//   GET    /objects/<key> the session's relay-ingest, "Authorization: Bearer <FETCH_TOKEN>"
//   DELETE /objects/<key> the same, after a successful ingest
//
// Bindings (wrangler.example.toml): KV namespace RELAY_KV. Secrets (`wrangler secret put`, never in git):
// INGEST_TOKEN, FETCH_TOKEN, DEVIN_WEBHOOK_URL, DEVIN_WEBHOOK_SECRET. See relay/README.md.

import { MAX_BODY_BYTES, TTL_SECONDS, KEY_RE, bearer, prepare } from "./relay.mjs";

const WEBHOOK_HEADER = "X-Webhook-Secret"; // homunculus.devin_layer.WEBHOOK_HEADER

function reply(status, obj) {
  return new Response(obj === undefined ? null : JSON.stringify(obj), {
    status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

async function readBody(request) {
  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > MAX_BODY_BYTES) return null;
  const buf = await request.arrayBuffer();
  return buf.byteLength > MAX_BODY_BYTES ? null : new TextDecoder().decode(buf);
}

async function receive(request, env) {
  if (!bearer(request, env.INGEST_TOKEN)) return reply(401, { error: "unauthorized" });
  const text = await readBody(request);
  if (text === null) return reply(413, { error: "too large" });
  let prepared;
  try {
    prepared = await prepare(text);
  } catch {
    return reply(400, { error: "not json" });
  }
  const { bytes, pointer } = prepared;
  await env.RELAY_KV.put(pointer.object_key, bytes, { expirationTtl: TTL_SECONDS });
  if (!(await notify(env, pointer))) {
    await env.RELAY_KV.delete(pointer.object_key);
    return reply(502, { error: "devin webhook failed" }); // Health Auto Export logs the export as failed
  }
  return reply(202, { stored: true });
}

// Devin gets the pointer only: object_key, sha256, bytes. No value from the body.
async function notify(env, pointer) {
  try {
    const hook = await fetch(env.DEVIN_WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", [WEBHOOK_HEADER]: env.DEVIN_WEBHOOK_SECRET },
      body: JSON.stringify(pointer),
    });
    return hook.ok;
  } catch {
    return false;
  }
}

async function serveObject(request, env, key) {
  if (!bearer(request, env.FETCH_TOKEN)) return reply(401, { error: "unauthorized" });
  if (!KEY_RE.test(key)) return reply(404, { error: "not found" });
  if (request.method === "DELETE") {
    await env.RELAY_KV.delete(key);
    return reply(204);
  }
  const body = await env.RELAY_KV.get(key, "arrayBuffer");
  if (body === null) return reply(404, { error: "not found" });
  return new Response(body, { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === "/hae" && request.method === "POST") return receive(request, env);
    const match = pathname.match(/^\/objects\/([^/]+)$/);
    if (match && (request.method === "GET" || request.method === "DELETE")) return serveObject(request, env, match[1]);
    return reply(404, { error: "not found" });
  },
};

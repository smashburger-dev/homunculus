// Pure helpers of the homunculus relay Worker (worker.mjs). No platform APIs beyond Web Crypto,
// so `node --test relay/` runs them as they are.

export const MAX_BODY_BYTES = 1024 * 1024; // homunculus.devin_layer.MAX_INPUT_BYTES
export const TTL_SECONDS = 3600; // KV minimum is 60; one hour covers a Devin session start and retries
export const KEY_RE = /^[A-Za-z0-9_-]{16,128}$/;

// Same idea as homunculus.ingest._scrub, applied before anything is stored: no GPS, routes or ECG traces.
const DROP_KEYS = new Set([
  "lat", "lon", "lng", "coordinate", "coordinates", "course", "altitude", "ecg",
  "horizontalaccuracy", "verticalaccuracy", "courseaccuracy", "speedaccuracy",
]);
const DROP_SUBSTRINGS = ["route", "latitude", "longitude", "gps", "location", "voltage", "electrocardiogram"];
const LOCATION_TAGS = new Set(["Indoor", "Outdoor", "Unknown"]); // a workout's indoor flag, not a place

function keep(key, value) {
  const k = key.toLowerCase();
  if (k === "location") return typeof value === "string" && LOCATION_TAGS.has(value);
  return !DROP_KEYS.has(k) && !DROP_SUBSTRINGS.some((s) => k.includes(s));
}

export function strip(value) {
  if (Array.isArray(value)) return value.map(strip);
  if (value === null || typeof value !== "object") return value;
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    if (keep(k, v)) out[k] = strip(v);
  }
  return out;
}

export function sameSecret(given, expected) {
  // Length-independent loop so the comparison time does not leak a matching prefix.
  if (typeof given !== "string" || typeof expected !== "string" || expected.length === 0) return false;
  let diff = given.length ^ expected.length;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i % expected.length);
  return diff === 0;
}

export function bearer(request, expected) {
  const header = request.headers.get("Authorization") || "";
  return header.startsWith("Bearer ") && sameSecret(header.slice(7), expected);
}

export function randomKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function sha256Hex(bytes) {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join("");
}

// The stripped body as bytes plus the pointer Devin gets: no value from the body reaches the webhook.
export async function prepare(text) {
  const bytes = new TextEncoder().encode(JSON.stringify(strip(JSON.parse(text))));
  return { bytes, pointer: { object_key: randomKey(), sha256: await sha256Hex(bytes), bytes: bytes.length } };
}

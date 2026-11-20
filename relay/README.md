# homunculus relay

A Cloudflare Worker that sits between Health Auto Export and the Devin `ingest-relay` webhook on path (a). Without it, the export body lands in the Devin session prompt, and ingest depends on the model copying up to 200 KB into a file. With it, the session prompt holds three fields: `object_key`, `sha256` and `bytes`. Code fetches the body and checks it.

Path (a) needs it; the `ingest-health` prompt copy is an unreliable fallback that ingest refuses without an explicit opt-in. Path (b) does not use it.

## What it does

1. `POST /hae` needs `Authorization: Bearer <INGEST_TOKEN>`. Bodies above 1 MiB get a 413.
2. It parses the JSON and drops routes, coordinates, place names and the whole `ecg` section. A workout's `location` survives only as the tag `Indoor`, `Outdoor` or `Unknown`, which ingest reads as the indoor flag. The rules mirror `homunculus.ingest._scrub`.
3. It stores the stripped body in Workers KV under a random 32-character key with a one-hour TTL.
4. It posts `{object_key, sha256, bytes}` to `DEVIN_WEBHOOK_URL` with `X-Webhook-Secret: <DEVIN_WEBHOOK_SECRET>`. If Devin does not answer 2xx, it deletes the object and returns 502.
5. `GET /objects/<key>` and `DELETE /objects/<key>` need `Authorization: Bearer <FETCH_TOKEN>`. The session's `python -m homunculus.devin_layer relay-ingest` uses them: download, check size and SHA-256, ingest, delete.

The Worker never logs bodies, and `wrangler.example.toml` turns observability off.

## Setup

```sh
cd relay
cp wrangler.example.toml wrangler.toml          # gitignored
npx wrangler kv namespace create RELAY_KV        # paste the id into wrangler.toml
npx wrangler secret put INGEST_TOKEN             # a long random string; goes into Health Auto Export
npx wrangler secret put FETCH_TOKEN              # another one; goes into Devin as HOMUNCULUS_RELAY_TOKEN
npx wrangler secret put DEVIN_WEBHOOK_URL        # the ingest-relay Automation's webhook URL
npx wrangler secret put DEVIN_WEBHOOK_SECRET     # the secret Devin generated for that webhook
npx wrangler deploy
```

`npx wrangler` downloads Wrangler from npm. The Worker itself has no dependencies.

Then:

- In Devin, create the `ingest-relay` Automation from `devin/automations/a/homunculus-ingest-relay.json`. Replace `<your-relay>.workers.dev` in its network allowlist with the Worker's host. Set the secrets `HOMUNCULUS_RELAY_URL` (`https://<host>`, no path) and `HOMUNCULUS_RELAY_TOKEN` (the `FETCH_TOKEN` value) for the Hippocampus Bot.
- Delete or pause `ingest-health`, or every export is ingested twice.
- In Health Auto Export, point the REST API automation at `https://<host>/hae` and add the header `Authorization: Bearer <INGEST_TOKEN>`. Turn off ECG and workout routes there as well ([devin/README.md](../devin/README.md#health-auto-export-settings-for-path-a)).

Never commit `wrangler.toml` with secrets, `.dev.vars`, or a token value. Rotate both tokens if one leaks: `wrangler secret put` again, then update Health Auto Export and Devin.

## Tests

`node --test relay/relay.test.mjs` runs the strip rules, the pointer and the Worker routes against a fake KV and a stubbed `fetch`. `tests/test_devin_layer.py` runs it when `node` is installed.

## Limits

- Cloudflare sees the stripped body for up to an hour. That is less than the full prompt Devin and its model see without the relay, but it is another processor.
- Workers KV is eventually consistent. A read from another location right after the write can miss the object (UNVERIFIED for the delay). If `relay-ingest` gets a 404 the run fails and the Bot files a Report. The object stays until its TTL ends, so a rerun of the Automation can still fetch it.
- Free-plan limits and prices are in [devin/README.md](../devin/README.md#two-paths).

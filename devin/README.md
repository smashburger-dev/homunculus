# homunculus on Devin Bots

This folder maps the swarm onto Devin Bots, Automations, Apps and Outposts. The facts about Devin come from [docs/DEVIN_BOTS.md](../docs/DEVIN_BOTS.md), read on 2026-11-20. The setup steps are in [docs/DEVIN_SETUP.md](../docs/DEVIN_SETUP.md). The own-box path is in [docs/RUNBOOK.md](../docs/RUNBOOK.md).

## The rule that does not change

A Devin Bot is the shell: a chat, a Needs You list, a schedule. It never makes the judgement call. Every bot turn runs as a command from `homunculus/devin_layer.py`, which calls the exact model from `config/roster.json` (Fable 5.2, Opus 5.5, Sonnet 5.5, Haiku 5.5) through the Anthropic API, with the key the Bot holds as a secret. Code computes every number, `guardrails` tiers every plan diff, and Pineal's fixed German templates carry every red flag. The Bot's own Devin model only runs the commands, posts what they print and files Needs You items.

`roster.json` keeps `"devin": {"enabled": false}`. `DevinProvider` would start a nested Devin session, which cannot pin a model, so this layer never uses it.

## Who is a Bot and who is an API call

| Role | Runs as | Where it talks |
|---|---|---|
| Cerebellum | Devin Bot | Slack. Goal talks, the Sunday Decisions, the monthly report |
| Sartorius | Devin Bot | iMessage. The morning session, post-session RPE, replies |
| Vagus | Devin Bot | Slack. Morning greeting, 21:00 check-in, replies |
| Hippocampus | Devin Bot | No owner chat. Owns ingest, the daily code jobs, the scouts, Thursday, Saturday and Sunday |
| Amygdala | API call inside the Sunday `weekly-review` session | |
| Thalamus | API call (Message Batches) inside the Thursday and Friday `thalamus` session | |
| Insula, Motor Cortex | API calls inside the Saturday `saturday` session | |
| Retina, Spindle, Papilla | Script Automations with a `STATE_FILE` cursor; a session starts only when PubMed has new ids | |
| Brainstem | Path (a): the `relay/` Worker and the `ingest-relay` webhook Automation. Path (b): `homunculus serve` on the box | |
| Pineal | Path (a): the `pineal-daily` Automation plus the state saved after each session. Path (b): `homunculus tick` on the box | |

Only one Bot can sit on an iMessage number, so Sartorius gets it: the owner writes to the fitness coach most often. Cerebellum and Vagus each get their own Slack app and handle in a private workspace.

## Two paths

Path (a), serverless. Health Auto Export posts a daily summary to the relay Worker, which calls the `ingest-relay` webhook (see below). Every session starts on a fresh Devin machine, so `devin_layer.restore_store` rebuilds the SQLite store from the ledger first: the parsed summaries in `data/summaries/`, the daily rows in `data/daily/` and the check-ins in `checkins/`. Each session ends by saving check-ins and pushing the ledger.

What path (a) costs you:

- Data is coarse and daily. Red flags run twice a day instead of hourly, and plan changes are ratified once a day.
- The daily summaries sit in the private ledger's git history.
- The `ingest-health` fallback skips the relay: the webhook body lands in the session prompt, the Bot's model reads it, and Cognition stores it with the session. Ingest only gets what the model copied into `inbox.txt`, and a copy with a changed digit would still parse. `ingest --llm-copy` refuses unless `config/devin.json` holds `{"llm_copy_ingest": true}`.
- Every webhook is one Devin session. `max_acu_limit` 1 caps what a run can spend; Devin's ACU price is on its pricing page and is not repeated here.
- The relay runs on Cloudflare's free Workers plan for one export a day. The free limits I know of are 100,000 Worker requests and 1,000 KV writes a day (UNVERIFIED; check Cloudflare's pricing page). One export costs one KV write, one read and one delete.

Sessions on path (a) run in parallel on separate clones of the ledger. Each prompt sets `*.jsonl merge=union` in the clone's `.git/info/attributes`, pulls first, and ends with `python -m homunculus.devin_layer push-ledger` (`ledger_sync.push`): up to 5 tries of `push`, with `pull --rebase` between them. Two sessions that both append to `ops/runs.jsonl` keep both rows. Snapshots that every session rewrites (`ops/heartbeats/*.json`, `data/freshness.json`, `data/daily/`, `data/baselines/`, `checkins/`) take upstream's version during the rebase, a heartbeat keeps the later run, and code then replays the merged ledger into the session store and rebuilds them, so a second ingest for the same day lands in the daily rows. Any other conflict, for example two edits of `config/goals.md`, aborts the rebase, the push step exits non-zero, and the Bot files a Report.

### The relay (the path a webhook)

`relay/` in this repo is a small Cloudflare Worker. Health Auto Export posts to it with a bearer token instead of posting to Devin. The Worker removes routes, coordinates, place names and ECG traces, stores the stripped body in Workers KV under a random key for one hour, and calls the `ingest-relay` webhook with `{object_key, sha256, bytes}` only. The session writes that pointer to `inbox.txt`. `python -m homunculus.devin_layer relay-ingest` (`devin_layer.ingest_relay`) downloads the object with `HOMUNCULUS_RELAY_TOKEN`, checks its size and SHA-256 against the pointer, ingests it and deletes it.

With the relay, the raw body never enters a model prompt, Devin's session history or the Bot's chat. A model that mistypes the pointer gets a 404 or a digest mismatch, so the run fails instead of storing wrong values. Setup is in [relay/README.md](../relay/README.md). Do not also run the unreliable `ingest-health` fallback, or every export is ingested twice.

### Health Auto Export settings for path (a)

Turn off what homunculus never uses before the first export:

- In the REST API automation, leave ECG out of the exported data types. No red flag reads ECG; Irregular Rhythm Notifications come in as heart rate notifications and stay on.
- For workouts, turn off route (GPS) data. Ingest and the relay drop routes anyway, but a route that is never exported never reaches Devin or Cloudflare.

The exact option names differ between app versions (UNVERIFIED); look for the data type list and the workout route option in the automation's settings.

Path (b), own box. A Mac mini or Raspberry Pi runs `homunculus serve` and `homunculus tick --devin-path b`. The box is also a Devin Outpost, so the Bot sessions run their commands on the box, next to the SQLite store, and raw data never leaves it. Only the owner-facing jobs and the weekly review run as Automations; the box ticks the rest (`devin_layer.box_jobs`).

| Automation | Bot | Path | Schedule (owner's zone) | Runs |
|---|---|---|---|---|
| `ingest-health` | Hippocampus | a | webhook, opt-in fallback, unreliable | `ingest --llm-copy`, then `red_flags`, `freshness` |
| `ingest-relay` | Hippocampus | a | webhook from `relay/` | `relay-ingest`, then `red_flags`, `freshness` |
| `pineal-daily` | Hippocampus | a | daily 06:30 | `finalize_day`, `red_flags`, `freshness`, `budget`, `heartbeat`, `ratify_due` |
| `retina-scout`, `spindle-scout`, `papilla-scout` | Hippocampus | a | daily 03:00, script step | the scout |
| `thalamus` | Hippocampus | a | Thu and Fri 04:00 | submit the batch Thursday, collect it Friday |
| `saturday` | Hippocampus | a | Sat 08:00 | `insula`, `motor_cortex` |
| `weekly-review` | Hippocampus | a, b | Sun 17:00 | packet, Hippocampus, Amygdala, Cerebellum's ruling |
| `sartorius-morning` | Sartorius | a, b | daily 07:00 | morning session message |
| `vagus-morning`, `vagus-evening` | Vagus | a, b | daily 07:00, 21:00 | greeting, evening check-in |
| `cerebellum-sunday` | Cerebellum | a, b | Sun 21:00 | `pending`, one Decision per approve-tier change |
| `cerebellum-monthly` | Cerebellum | a, b | 1st of the month 08:00 | monthly report |

The specs in `automations/a/` and `automations/b/` are the output of `devin_layer.export_automations` for the default config (time zone UTC, empty scout keywords). Generate your own with `python -m homunculus devin-export --path a --out <dir outside this repo>`: it reads `config/cadence.json` and `config/keywords.json` from your ledger. Scout queries can reveal what you care about, so the generated script steps stay out of this public repo.

Each spec holds the RRULE and time zone, the webhook header, the per-run ACU limit, the hosts the session may reach, the names of the secrets it needs, and the prompt the Automation sends. The field names are this repo's format. Devin's API names for them are not documented (UNVERIFIED), so you copy the values into the Automations form by hand or let the Bot set them up from chat.

## Commands a session runs

The CLI wiring in `homunculus/cli.py` exposes these `devin_layer` functions:

| Command | Function | Purpose |
|---|---|---|
| `python -m homunculus devin-task <automation> --path a\|b` | `run_task` | run the Automation's Pineal jobs once per period; prints JSON with `runs` and `owner_texts` |
| `python -m homunculus message <bot> --path a\|b --text-file F` | `reply` | an owner message from the Bot's chat; prints `reply_de` |
| `python -m homunculus ingest --stdin` or `--file F` | `ingest_stdin` | a Health Auto Export payload, bare, wrapped or inside prompt text |
| `python -m homunculus.devin_layer relay-ingest --file F` | `ingest_relay` | a relay pointer from prompt text: fetch, verify, ingest, delete; prints counts and `deleted` |
| `python -m homunculus.devin_layer clear-hold` | `clear_hold` | lift every urgent coaching hold after the owner confirms a doctor has seen them; Cerebellum runs it only after the owner approves a Needs You Decision, never on its own reading of a chat |
| `python -m homunculus run-bot <bot> --context F --schema F --out REL` | `run_bot` | one validated turn of any LLM bot, written as that bot |
| `python -m homunculus devin-export --path a\|b --out DIR` | `export_automations` | the Automation specs |
| `python -m homunculus scout-script <scout> --max-items N` | `scout_poll_script` | the script step source |
| `python -m homunculus tick --devin-path b` | `box_jobs` | the box's Pineal tick without the jobs Devin runs |

`devin-task` skips a job that already finished in its period, so an Automation retry or a box tick that got there first does no double work. `HALT` and the budget gate work as in Pineal's tick.

## Script steps

Devin documents that a script step keeps a JSON `STATE_FILE` between runs and dispatches at most 10 new items per run by default, 50 at most. The rendered script reads the state path from the `STATE_FILE` environment variable, searches PubMed for the scout's queries, emits PMIDs above its cursor (at most `max_items`, oldest first) and moves the cursor to the last one it emitted. The ids are only a trigger: the session reruns the scout through code, which fetches seven days, deduplicates against the ledger and triages with Haiku. Two things are not documented and need a check on the first run (UNVERIFIED): whether `STATE_FILE` is an environment variable or a fixed file name, and the output format Devin reads items from. The script prints one JSON line `{"scout": ..., "items": [...]}`; adapt the last `out.write` if Devin expects another format. If Devin starts one session per item rather than one per run, set `--max-items 1`.

## Privacy

- Whatever a Bot reads, Devin's cloud model reads too. Turn data-control opt-out on before the first session.
- Bots see aggregates, counts and fixed texts. `ingest` prints counts only. The Bot instructions forbid pasting values, payloads or ledger files into a chat.
- Path (a) without the relay puts the daily summary into a session prompt. With the relay, the prompt holds only the pointer, and the stripped body passes through Cloudflare KV for at most an hour. Path (b) keeps raw samples on the box.
- Secrets live in Devin's secret store per Bot, by name only. Nothing in this folder holds a value. Tracker tokens stay outside every git tree ([TRACKER_CONTRACT.md](../docs/TRACKER_CONTRACT.md)).

## Files

- `bots/<bot>.md`: paste into the Bot's General instructions.
- `automations/a/*.json`, `automations/b/*.json`: generated Automation specs.
- `blueprint.yaml`: the environment snippet (Python 3.11, both repos, secret names).
- `app/README.md`: the dashboard as a Devin App, with the Tailscale fallback.
- `../relay/`: the optional Cloudflare Worker for `ingest-relay`.

# Devin setup prompt

This is the message the owner sends to Devin to set up the swarm on Devin Max. It grew out of the owner's original request in [BRIEF.md](BRIEF.md): about 10 bots, 3 to 4 on frontier models and 4 to 5 small scrapers, each pinned to a model tier. Devin cannot pin a model per Bot or per session ([DEVIN_BOTS.md](DEVIN_BOTS.md)), so the request now asks for 4 Bots that run `homunculus` commands, and the commands call the pinned models through the Anthropic API.

Do these yourself before you send it:

1. Create the ledger on your own machine: `python3 -m homunculus init-ledger ~/homunculus-ledger-<you>`, commit the skeleton with `git -C ~/homunculus-ledger-<you> add -A && git -C ~/homunculus-ledger-<you> commit -m "Ledger skeleton"` (`init-ledger` commits nothing), then run `python3 -m homunculus --ledger ~/homunculus-ledger-<you> init-plan`. Push it to an empty private GitHub repo `homunculus-ledger-<you>`.
2. Turn on the data-control opt-out in the Devin settings. Paid plans train on customer data until you opt out.
3. Create an Anthropic API key in a workspace with a monthly spend limit. Keep it in the console until Devin asks for it by name.
4. For path (b), set up the box first with [RUNBOOK.md](RUNBOOK.md). For path (a), set up the relay Worker with [relay/README.md](../relay/README.md) once Devin has created the `ingest-relay` webhook.

Replace `<you>` with your GitHub account, then paste everything below the line.

---

Set up homunculus, my personal health coach, as four Devin Bots. Work through the steps in order and stop where a step says to ask me.

## Repositories

- `<you>/homunculus`: the engine code. Read only. Never commit or open a pull request against it from this setup or from any Bot.
- `<you>/homunculus-ledger-<you>`: my private data. Bots read and write it only through `python -m homunculus` commands. Never make it public and never turn on GitHub Pages for it.

## Rules for the whole setup

- The Bots are a shell. Every number comes from code, and every coaching text comes from a `homunculus` command that calls the model pinned in `config/roster.json`: Fable 5.2 for Cerebellum, Opus 5.5 for Hippocampus, Amygdala, Thalamus, Motor Cortex, Sartorius and Vagus, Sonnet 5.5 for Insula, Haiku 5.5 for Retina, Spindle and Papilla. A Bot never writes coaching text, a review or a score itself.
- Never paste a secret value, a health value, a payload or a ledger file into a chat, a memory file, a Report or a file in either repo. Ask me for secrets by name, and I enter them in Devin's secret store.
- Ask me before you create or change any Automation or usage cap.

## 1. Read first

Read these files in the engine repo: `README.md`, `docs/DEVIN_BOTS.md`, `docs/DEVIN_SETUP.md`, `devin/README.md`, `devin/blueprint.yaml`, the four files in `devin/bots/`, and `config/roster.json`. `docs/DEVIN_SETUP.md` is the checklist. Follow it where this message is shorter.

## 2. Ask me which path

Ask me these questions and wait for the answers:

1. Do you have an always-on Mac mini or Raspberry Pi on your tailnet that runs `homunculus serve` and `homunculus tick --devin-path b`? If yes, what is its Outpost name?
2. Do you want hourly data with hourly red flags (path b), or is one daily summary with red flags twice a day enough (path a)?
3. Path (a) only: may the daily summaries sit in the private ledger's git history, and may the stripped Health Auto Export body pass through Cloudflare Workers KV for up to an hour?
4. Path (b) only: are `HOMUNCULUS_PUSH_URL` and `HOMUNCULUS_PUSH_TOKEN` set in the box's env file? Without the URL `tick --devin-path b` exits 1 on every run, and without the token anyone who knows the URL can read the alerts.
5. What is your time zone, and when do you usually first unlock your phone in the morning? The coaches send at 07:00 by default, and iOS sends health data only after the first unlock.

Write the path letter down. Every Bot instruction and every Automation uses the same letter.

## 3. Connect GitHub and set the environment

Install the Devin GitHub app with access to exactly the two repositories above: read on the engine, read and write on the ledger. If the app cannot split the permissions, tell me and keep the engine read only by instruction. Add the environment steps from `devin/blueprint.yaml`: Python 3.11 or newer, both checkouts, `HOMUNCULUS_LEDGER`, and the smoke test `python3 -m homunculus pending`. There is no `pip install`, because the engine uses the standard library only. On path (b) the sessions run on the Outpost, which already has both checkouts.

## 4. Create four Bots

Create four private Bots under my account, so they connect to each other without a prompt. Use mode Lite and child capability Lite for all four, because the models that judge are called through the API.

| Bot | Instructions to paste into General | Chat |
|---|---|---|
| Cerebellum | `devin/bots/cerebellum.md`, the part below the line | Slack DM |
| Sartorius | `devin/bots/sartorius.md`, the part below the line | iMessage |
| Vagus | `devin/bots/vagus.md`, the part below the line | Slack DM |
| Hippocampus | `devin/bots/hippocampus.md`, the part below the line | none, Needs You only |

Replace `<you>` and `<path>` in each file. Grant Hippocampus access to Sartorius, so Hippocampus can hand red-flag texts to the Bot on iMessage. Tell me when to link my iMessage number to Sartorius and when to add the Slack apps for Cerebellum and Vagus in my private workspace.

## 5. Ask me for these secrets by name

| Secret | Bots | When |
|---|---|---|
| `ANTHROPIC_API_KEY` | all four | always |
| `HOMUNCULUS_MAILTO` | Hippocampus | always, a contact address for PubMed and Crossref |
| `HOMUNCULUS_RELAY_URL` | Hippocampus | path (a), the Worker's `https://<host>` without a path |
| `HOMUNCULUS_RELAY_TOKEN` | Hippocampus | path (a), the Worker's `FETCH_TOKEN` |
| `GEMINI_API_KEY` | Hippocampus | only once `roster.json` moves Amygdala to a paid Gemini model |

Do not ask for tracker tokens, `HEALTH_TOKEN`, `HOMUNCULUS_PUSH_URL` or `HOMUNCULUS_PUSH_TOKEN`. They stay on the box, outside Devin.

## 6. Ask me for keywords and references

The three scouts skip every run while `config/keywords.json` is empty. Ask me, one scout at a time:

- Retina (biology, physiology, neurobiology): PubMed queries, ranking terms with optional weights, and reference papers (DOIs) or authors I trust.
- Spindle (workouts and sport methods): the same, plus the sports I train.
- Papilla (nutrition and supplements): the same, plus any supplements I already take.

Also ask for my goals for the next 12 months, ranked and with dates. Then draft `config/keywords.json` in this format and show it to me, together with a draft of `config/goals.md`:

```json
{"retina": {"queries": ["heart rate variability AND sleep"], "terms": {"hrv": 2.0, "sleep": 1.0}, "pub_type_weights": {}},
 "spindle": {"queries": [], "terms": [], "pub_type_weights": {}},
 "papilla": {"queries": [], "terms": [], "pub_type_weights": {}}}
```

I commit both files to the ledger myself. Do not edit ledger files by hand.

## 7. Create the Automations

Generate the specs from my ledger, outside the engine repo, because the scout queries are personal:

```sh
cd ~/homunculus
python3 -m homunculus --ledger "$HOMUNCULUS_LEDGER" devin-export --path <path> --out ~/devin-automations
python3 -m homunculus --ledger "$HOMUNCULUS_LEDGER" scout-script retina --max-items 10
```

Run `scout-script` for `spindle` and `papilla` too. The specs take the time zone from `tz` in `config/cadence.json`, or from `--tz`, never from `profile/owner.json`, and fall back to UTC. If the `timezone` field is not my time zone, tell me before you go on. Show me the list below for my path and wait for one yes, then create each Automation in its Bot. Copy the RRULE, time zone, `max_acu_limit`, `network_allowlist`, secret names and prompt from each spec unchanged.

Path (a), 13 Automations:

- Hippocampus: `ingest-relay` (webhook), `pineal-daily`, `retina-scout`, `spindle-scout`, `papilla-scout` (script steps from `scout-script`, item cap `max_items`), `thalamus`, `saturday`, `weekly-review`.
- Sartorius: `sartorius-morning`.
- Vagus: `vagus-morning`, `vagus-evening`.
- Cerebellum: `cerebellum-sunday`, `cerebellum-monthly`.

Skip `ingest-health`. It is a locked fallback, and running it next to `ingest-relay` would ingest every export twice. Devin generates the webhook secret for `ingest-relay`. Tell me when it exists, so I can put it into the Worker as `DEVIN_WEBHOOK_SECRET`, and do not write it anywhere yourself.

Path (b), 6 Automations, each running on the Outpost:

- Hippocampus: `weekly-review`.
- Sartorius: `sartorius-morning`.
- Vagus: `vagus-morning`, `vagus-evening`.
- Cerebellum: `cerebellum-sunday`, `cerebellum-monthly`.

The box ticks every other job.

Two things are not documented, so check them on the first script run and tell me what you found: whether `STATE_FILE` is an environment variable or a fixed file name, and which output format Devin reads items from. The script prints one JSON line `{"scout": ..., "items": [...]}`.

## 8. Usage caps

Suggest these weekly caps on the Bots usage-limits page, and wait for my yes: Hippocampus 40 ACU, Sartorius 15, Vagus 20, Cerebellum 10.

## 9. Verify with synthetic data before any real data

Health Auto Export stays pointed at nothing until every check below passes.

1. In a Hippocampus session, run `python3 -m homunculus demo --days 56` and `python3 -m unittest discover -s tests`. Expect exit 0 for both, `LLM calls: 14 to FakeProvider, 0 network requests` in the demo output, and `Ran 1120 tests` with `OK`. The demo uses a fresh temporary ledger and never reads mine.
2. Run one Automation per Bot by hand with the global flag `--dry-run` before the subcommand, for example `python -m homunculus --dry-run devin-task weekly-review --path <path>`. A dry run swaps every model for `FakeProvider` and commits nothing.
   - Hippocampus, `weekly-review`: exit 0, JSON whose `runs` show the job, and no Report.
   - Sartorius, `sartorius-morning`: one iMessage with the placeholder text `x`.
   - Vagus and Cerebellum the same way in Slack.
3. Path (a): post this synthetic payload to the webhook: `{"data": {"metrics": [{"name": "step_count", "units": "count", "data": [{"date": "2026-11-19 12:00:00 +0000", "qty": 1000, "source": "Synthetic"}]}]}}`. Check that the ledger gained one file under `data/summaries/` and two under `data/daily/`, for 2026-11-19 and the following wake date 2026-11-20, then revert that commit in the ledger.
4. Path (b): give each dry run `--ledger` with a scratch copy of the ledger. A dry run still appends to `ops/runs.jsonl`, and the real job would then count as done for that period.

## 10. Report back

Send me one summary with:

- the path letter and the four Bots with their chat surfaces,
- every Automation you created, with its schedule and ACU limit,
- the secrets that are set, by name only,
- each check from step 9 with pass or fail,
- what you found about `STATE_FILE`, the script output format and the GitHub app permissions,
- anything you could not do and why.

Only after I confirm that summary do I point Health Auto Export at the relay or the box.

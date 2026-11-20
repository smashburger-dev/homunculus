# Devin setup for Devin Max

Step by step from an empty Devin account to four running Bots. The Devin facts behind it are in [DEVIN_BOTS.md](DEVIN_BOTS.md), read 2026-11-20. How the swarm maps onto Devin is in [devin/README.md](../devin/README.md). Devin UI labels below follow those docs; where a page does not name a field, the step says UNVERIFIED.

Plan for about two hours. Run the synthetic demo (step 12) before any real data arrives.

## 0. Decide the path

- Path (a), serverless: no hardware. Health Auto Export sends one daily summary to a Devin webhook. Coarse data, red flags twice a day, the daily summaries live in the private ledger's git history.
- Path (b), own box: a Mac mini or Raspberry Pi runs `homunculus serve` and Pineal, and is a Devin Outpost, so raw samples never leave it. Hourly data and red flags. Red flags reach your phone only through `HOMUNCULUS_PUSH_URL`, for example an ntfy topic, and `HOMUNCULUS_PUSH_TOKEN`, its access token, both in the box's env file. Path (b) requires both: without them Pineal writes alerts to `mail/owner/inbox/` and nowhere else, so `tick --devin-path b` still runs its jobs but exits 1 on every run, and `serve` warns at start. Set the box up first with [RUNBOOK.md](RUNBOOK.md), then come back here.

You can start on (a) and move to (b) later: the ledger format is the same. Write the letter down; every Bot instruction and Automation uses it.

## 1. Create the ledger repo

On your own machine, outside this repo:

```sh
git clone https://github.com/<you>/homunculus ~/homunculus
cd ~/homunculus
python3 -m homunculus init-ledger ~/homunculus-ledger-<you>
```

Create an empty private GitHub repo `homunculus-ledger-<you>`, add it as `origin` of the new ledger and push. Never make it public, never fork it into a public account, never turn on GitHub Pages for it.

Edit `profile/owner.json` (time zone, sleep target, training status) and `config/cadence.json`:

```json
{"schema": 1, "tz": "Europe/Berlin", "jobs": {}, "devin": {"max_acu": {}}}
```

`jobs` takes overrides such as `{"sartorius_morning": {"at": "06:45"}}`; `devin.max_acu` takes per-Automation ACU limits such as `{"weekly-review": 4}`. Commit, then write the first plan:

```sh
python3 -m homunculus --ledger ~/homunculus-ledger-<you> init-plan
```

The starter plan has no fixed sessions; the first weekly review proposes them from `config/goals.md`. `--from FILE` takes your own plan content instead, without a kcal target. `init-plan` commits on its own. Push.

## 2. Turn on the data-control opt-out

Paid plans train on customer data until Data Controls opt out. In the Devin settings, under data controls, opt out before the first session. Whatever a Bot reads, Devin's cloud model reads too.

## 3. Connect GitHub

Install the Devin GitHub app on your account with access to exactly two repos:

- `homunculus`: read. The Bots never push to the engine.
- `homunculus-ledger-<you>`: read and write. Sessions on path (a) pull and push it.

Whether the GitHub app can grant read on one repo and write on the other is not documented (UNVERIFIED). If it cannot, give both write and rely on the Bot instructions, which forbid engine commits; or keep the engine in a fork the app may only read.

## 4. Set the environment

Open the environment setup and add the steps from [devin/blueprint.yaml](../devin/blueprint.yaml): Python 3.11 or newer, the two checkouts, `HOMUNCULUS_LEDGER`, and the smoke test. There is no `pip install`: the engine is standard library only.

## 5. Create the four Bots

Create four private Bots, all by the same account, so they connect to each other without a prompt and pick up your sign-ins:

| Bot | Mode | Child capability |
|---|---|---|
| Cerebellum | Lite | Lite |
| Sartorius | Lite | Lite |
| Vagus | Lite | Lite |
| Hippocampus | Lite | Lite |

The Bot's own model only runs commands and posts their output, so the cheapest mode fits. The models that judge are pinned in `roster.json` and called through the API. Mode names are from the Bots page; which one bills least is not documented (UNVERIFIED), so check the usage page after the first week.

Grant Hippocampus access to Sartorius, so it can hand red-flag texts to the Bot on iMessage.

## 6. Paste the instructions

For each Bot, open General and paste the part below the line of its file. Replace `<you>` and `<path>`:

- [devin/bots/cerebellum.md](../devin/bots/cerebellum.md)
- [devin/bots/sartorius.md](../devin/bots/sartorius.md)
- [devin/bots/vagus.md](../devin/bots/vagus.md)
- [devin/bots/hippocampus.md](../devin/bots/hippocampus.md)

## 7. Set the secrets

Per Bot, in its connections, add secrets by name. Values come from the provider consoles; never paste one into a chat, a file or a repo.

| Secret | Cerebellum | Sartorius | Vagus | Hippocampus |
|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes | yes | yes | yes |
| `GEMINI_API_KEY` | | | | only once Amygdala moves to a paid Gemini model |
| `HOMUNCULUS_MAILTO` | | | | yes, a contact address for PubMed and Crossref |

Use an Anthropic key from a workspace with a monthly spend limit. Tracker tokens (Garmin, WHOOP and the rest) never go into Devin: they live on the box, outside every git tree ([TRACKER_CONTRACT.md](TRACKER_CONTRACT.md)). On path (b), `HOMUNCULUS_PUSH_URL` and `HOMUNCULUS_PUSH_TOKEN` stay out of Devin too: Pineal on the box sends the push, so they go into the box's env file ([RUNBOOK.md](RUNBOOK.md#2-engine-ledger-secrets)).

## 8. Link the chats

- iMessage: link your number to Sartorius. A number links to one Bot at a time, and Enterprise has no iMessage.
- Slack: in a private workspace with only you in it, add the Slack apps for Cerebellum and Vagus. Each Bot gets its own app and handle. Hippocampus gets no chat surface; its output goes to Needs You.

## 9. Create the Automations

Generate the specs from your ledger, outside this repo, because the scout queries are personal:

```sh
cd ~/homunculus
python3 -m homunculus --ledger ~/homunculus-ledger-<you> devin-export --path <path> --out ~/devin-automations
python3 -m homunculus --ledger ~/homunculus-ledger-<you> scout-script retina --max-items 10
```

`devin/automations/a/` and `devin/automations/b/` show what the output looks like for the default config. For each spec, create the Automation in its Bot (or ask the Bot in chat to set it up from the spec and approve):

- Trigger: schedule with the spec's `rrule` and `timezone`, or webhook for `ingest-relay`. Skip `ingest-health`: it is the unreliable fallback described in step 11.
- Max ACU per run: `max_acu_limit`.
- Network policy: `network_allowlist`.
- Secrets: the names in `secrets`.
- Prompt: `prompt`, unchanged.
- Script automations (path a): paste `script.source` as the script step and set the item cap to `script.max_items`. Check the first run's output format against the Automations page (UNVERIFIED, see devin/README.md).

For the webhook: Devin generates the secret. Copy it once into the relay Worker as `DEVIN_WEBHOOK_SECRET` ([relay/README.md](../relay/README.md)) and nowhere else.

## 10. Set usage caps

On the Bots usage-limits page (Max, Teams and Enterprise), set a cap per Bot. Starting points for a week: Hippocampus 40 ACU, Sartorius 15, Vagus 20, Cerebellum 10. A Bot's usage is its chat plus every session it starts, billed to you. The Anthropic side has its own cap: `config/budget.json` (`weekly_cap_usd`, default 40) and the workspace spend limit.

## 11. Point Health Auto Export at the right place

Path (a): Health Auto Export posts to the relay Worker in `relay/`, and the Worker calls the `ingest-relay` webhook with a pointer. Set up the Worker first ([relay/README.md](../relay/README.md)), then one REST automation in Health Auto Export.

- URL: `https://<your-relay>.workers.dev/hae`. Header `Authorization: Bearer <INGEST_TOKEN>`, the Worker's token.
- Data: health metrics, JSON v2, Summarize off. Select only what `metrics.py` uses: heart rate variability, resting heart rate, sleep analysis, step count, active energy, respiratory rate, blood oxygen, body mass, VO2 max, wrist temperature, blood pressure. Leave plain heart rate out; it is the largest series.
- Range: since last sync. Frequency: once a day, after you usually wake up and unlock the phone.
- iOS sends only after the first unlock, so the 07:00 messages may run on yesterday's data. If the summary usually lands later, move the coaches in `config/cadence.json`, for example `{"jobs": {"sartorius_morning": {"at": "08:00"}, "vagus_morning": {"at": "08:00"}}}`, and regenerate the specs.
- Keep the body under 1 MiB, the Worker's limit. Whether one day with Summarize off stays under it depends on the selection (UNVERIFIED); check the first payload size in the HAE log.
- Keep route and ECG export off, and send no workouts through this automation. The Worker strips routes and ECG as well, and the session prompt holds only the pointer.

With the relay, code downloads the body and checks its size and SHA-256 against the pointer, so a mistyped pointer fails the run instead of storing wrong values.

The `ingest-health` spec is a fallback without the relay, and it is unreliable: HAE posts straight to Devin, the body lands in the session prompt, and the Bot copies it into `.private/inbox.txt`. A copy with one changed number would pass, because HAE headers are static and no digest exists to check it against. `ingest --llm-copy` therefore refuses unless the ledger opts in with `{"llm_copy_ingest": true}` in `config/devin.json`. Use it only if you accept that risk, and never together with `ingest-relay`, or every export is ingested twice.

Path (b): point Health Auto Export at the box instead, as in [RUNBOOK.md](RUNBOOK.md#5-health-auto-export). No webhook Automation.

## 12. Verify with the synthetic demo first

Before any real data:

```sh
cd ~/homunculus
python3 -m homunculus demo --days 56
python3 -m unittest discover -s tests
```

`demo` runs the whole pipeline on synthetic data in a fresh temporary ledger with `FakeProvider` and never reads yours. Then, in Devin, run one Automation by hand per Bot with the global flag `--dry-run` before the subcommand, for example `python -m homunculus --dry-run devin-task weekly-review --path <path>`. A dry run swaps every model for `FakeProvider` and commits nothing.

1. Hippocampus, `weekly-review`. Expect exit 0 and JSON whose `runs` show the job, and no Report.
2. Sartorius, `sartorius-morning`. Expect one iMessage with the placeholder text `x`.
3. Vagus and Cerebellum the same way in Slack.
4. Path (a): post a synthetic payload to the webhook, for example `{"data": {"metrics": [{"name": "step_count", "units": "count", "data": [{"date": "2026-11-19 12:00:00 +0000", "qty": 1000, "source": "Synthetic"}]}]}}`. Check that the ledger gained one file under `data/summaries/` and one under `data/daily/`, then revert that commit in the ledger.

Run without `--dry-run` only after all four work. On path (b), add `--ledger` with a scratch copy of the ledger to each dry run: a dry run still appends to `ops/runs.jsonl`, and the real job would then count as done for that period.

## 13. First week

- Check the usage page daily against the caps from step 10.
- `python3 -m homunculus pending` should stay empty until the first Sunday.
- Without a first plan (step 1) the weekly review ends as `failed` with reason `no_plan` and alerts you, and Sartorius sends no sessions. `doctor` reports it as `fail plan`.
- If a Bot writes its own coaching text instead of posting command output, tighten its instructions; that is the failure to watch for.
- `config/HALT` stops everything except ingest and red flags. Tell Cerebellum "Stopp" or run `python3 -m homunculus halt`.

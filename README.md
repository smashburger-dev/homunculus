# homunculus

homunculus coaches one person. It reads their tracker data, their daily check-ins and new research papers, and keeps a training, nutrition and recovery plan up to date. The plan changes once a week, after an argued review between three model roles. On any day the coaches may only shorten or swap a session inside bounds the plan already sets.

Better numbers are a means. The goal is that the owner enjoys training and life more, so a daily 0 to 10 wellbeing item sits next to every physiological metric and every experiment.

The name comes from the cortical homunculus, the map of the body that the sensory and motor cortex keep, drawn with each body part sized by how much cortex it gets. This repo builds that map for one body. Each of its 13 roles is named after the piece of anatomy whose job matches its own.

The design brief is [docs/BRIEF.md](docs/BRIEF.md). The full design is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## The 13 roles

Eleven roles call a language model. Two are plain code. The models and effort levels come from [config/roster.json](config/roster.json).

| Role | Anatomy | Why the name fits | Job | Model (tier) | Talks to the owner |
|---|---|---|---|---|---|
| Cerebellum | Cerebellum | Compares intended with actual movement and corrects the error | Orchestrator. Holds the goals, relays them to the bots that need them, rules on the weekly review | Fable 5.2, effort high (frontier top) | Yes, goal talks in Slack |
| Hippocampus | Hippocampus | Consolidates the day's memories during sleep | Weekly reviewer. Drafts the review and a typed plan diff, answers every challenge, designs N-of-1 protocols | Opus 5.5, effort high (frontier) | No |
| Amygdala | Amygdala | Detects threats | Adversarial critic. Challenges the draft with ref-backed items and can block a diff. Re-extracts 10% of papers as an audit | Opus 5.5, effort high (frontier). A paid Gemini model is intended once its model ID is confirmed | No |
| Thalamus | Thalamus | Gates which signals reach the cortex | Extracts paper fields with verbatim quotes, which code then scores. Runs deep dives on frontier papers | Opus 5.5 via Message Batches, effort medium (frontier). Sonnet 5.5 as second extractor | No |
| Motor Cortex | Primary motor cortex | Plans and issues movement | Proposes session changes and novel exercises as feature records. The guardrail solver admits or rejects them | Opus 5.5, effort medium (frontier) | No |
| Insula | Insular cortex | Interoception, linking body signals to feelings | Explains conflicts that code found between check-ins and tracker data, drafts at most 3 owner questions a week | Sonnet 5.5, effort medium (mid). Haiku 5.5 as alternative | No |
| Sartorius | Sartorius muscle | The tailor's muscle, the longest in the body | Fitness coach. Morning session, post-session RPE, gym feel. Never adds load | Opus 5.5, effort low (frontier). Sonnet 5.5 as alternative | Yes, daily on iMessage |
| Vagus | Vagus nerve | Rest-and-digest nerve, the source of HRV | Well-being coach. Sleep, mood, stress, enjoyment. Morning greeting and 21:00 check-in | Opus 5.5, effort low (frontier). Sonnet 5.5 as alternative | Yes, daily in Slack |
| Retina | Retina | Turns light into signal | Paper scout for biology, physiology and neurobiology. Classifies candidates into enums only | Haiku 5.5 (small) | No, beyond keywords at setup |
| Spindle | Muscle spindle | Senses stretch and length change | Workout and sport-methods scout | Haiku 5.5 (small) | No |
| Papilla | Lingual papilla | Carries the taste buds | Nutrition and supplement scout | Haiku 5.5 (small) | No |
| Brainstem | Brainstem | Keeps the vital signs running | Ingest server for Health Auto Export, check-ins and owner messages | Code | No |
| Pineal | Pineal gland | Sets the circadian clock | Scheduler, single git writer, budget guard, alert delivery | Code | Fixed alert texts only |

The coaches talk to the owner personally, in German. The review roles talk to each other in a neutral tone, through immutable JSON messages in the ledger.

## Code computes, models judge

Deterministic Python computes every number. A model reads those numbers, argues about them and writes prose.

Code does this:

- Daily metrics, keyed on the local wake date: HRV (SDNN and RMSSD as separate series), resting HR, sleep, training load (sRPE and Edwards TRIMP), weight trend, readiness ([homunculus/metrics.py](homunculus/metrics.py)).
- Conflict detection between check-ins and tracker data, rules R1 to R3 ([homunculus/insula.py](homunculus/insula.py)).
- Paper certainty (0 to 4) and promise (0 to 4) from the extracted fields ([homunculus/evidence.py](homunculus/evidence.py)).
- Belief priors and updates ([homunculus/beliefs.py](homunculus/beliefs.py)), N-of-1 design, allocation and analysis ([homunculus/experiments.py](homunculus/experiments.py)).
- Plan-diff tiers (`auto`, `notify`, `approve`, `reject`), exercise admission, nutrition floors, red flags and their fixed German alert texts ([homunculus/guardrails.py](homunculus/guardrails.py)).
- Scheduling, git commits, the weekly budget and alert delivery ([homunculus/pineal.py](homunculus/pineal.py)).

Models do this:

- Triage scout candidates into enums (Haiku).
- Fill paper fields, each with a quote from the fetched text (Thalamus).
- Explain conflicts, propose sessions, draft the review, challenge it, rule on it.
- Write the coach messages from what `plans.modulate_day` allows.

Code checks every model output before it counts. Answers must match a JSON schema. A quote that is not a substring of the source text voids its field. A challenge whose `ref` does not resolve to a packet pointer or ledger file is dropped. `guardrails.scan_text` rejects diagnostic vocabulary in English and German. A plan diff takes effect only through `guardrails.check_diff`, and no prose plan is ever executed.

## The review loop

Every Sunday `weekly.build_packet` writes the week's aggregates, baselines, conflicts, actionable papers, beliefs, experiments and the current plan into one packet. Its SHA-256 is the `data_sha` every later message cites. Hippocampus drafts, Amygdala challenges, Hippocampus answers each challenge with `accept` or `rebut`, and Amygdala's second round may only touch diff paths that changed. The loop stops after 2 rounds and at most 6 messages, Cerebellum's ruling included. Cerebellum ratifies, ratifies in part, or keeps the plan, and code reruns `check_diff` on the result. An open blocker keeps the current plan and asks the owner. Questions to the owner go out as one more message, so the demo below prints `messages=7`.

## The frontier lane

Code gives every paper two scores: certainty, how sure we are the effect is real in people like the owner, and promise, how plausible and valuable it would be if true. A methods gate caps both at 1 when fewer than half of the checks pass (randomization, blinding, preregistration, sample size), so a weak RCT cannot stay actionable. A paper with certainty below 2, promise of at least 2.5 and clean methods enters status `frontier`, which needs a Thalamus deep dive that splits the claim into at most 8 mechanistic links and checks each against scored human papers. When the chain holds (weakest link at least 0.5, human coverage at least 2/3, no contested link) and the intervention is low risk and measurable with tracker data, it becomes an N-of-1 experiment candidate, otherwise it stays on watch. At most one frontier experiment runs at a time, Amygdala must challenge every frontier promotion, and the owner approves every plan change that cites a deep dive.

The details are in [docs/NOVELTY_LANE.md](docs/NOVELTY_LANE.md) and [docs/EVIDENCE_WEIGHTS.md](docs/EVIDENCE_WEIGHTS.md).

## Where it runs

Devin Bots are the always-on shell: a chat, a Needs You list and a schedule. They never make the judgement call. A Bot's session runs a `homunculus` command, and that command calls the exact model from `roster.json` through the Anthropic API with the key the Bot holds as a secret. The Bot's own Devin model only runs commands and posts what they print. The reasons and the Devin facts, read on 2026-11-20, are in [docs/DEVIN_BOTS.md](docs/DEVIN_BOTS.md).

Four Bots carry the 13 roles:

| Bot | Chat | Runs |
|---|---|---|
| Cerebellum | Slack | Goal talks, the Sunday 21:00 Decisions for approve-tier changes, the monthly report |
| Sartorius | iMessage | Morning session at 07:00, replies, session RPE |
| Vagus | Slack | Morning greeting at 07:00, check-in at 21:00, replies |
| Hippocampus | none, Needs You only | Ingest, daily code jobs, scouts, Thursday extraction, Saturday step, Sunday review |

Amygdala, Thalamus, Insula and Motor Cortex are API calls inside Hippocampus sessions. Retina, Spindle and Papilla are script Automations that keep a PubMed cursor in `STATE_FILE`, so a session starts only when new ids arrive. Only one Bot can sit on an iMessage number, so Sartorius gets it.

There are two paths. Both use the same ledger format, so you can start on (a) and move to (b).

Path (a), serverless, no hardware:

- Health Auto Export posts one daily summary to the Cloudflare Worker in [relay/](relay/). The Worker strips routes and ECG, holds the body in Workers KV for at most an hour, and calls the `ingest-relay` webhook with a pointer (`object_key`, `sha256`, `bytes`).
- 13 Automations run the jobs. [devin/automations/a/](devin/automations/a/) has 14 specs, and you skip `ingest-health`, an opt-in fallback.
- Every session rebuilds the SQLite store from the ledger, then pushes the ledger with up to 5 tries.
- Data is daily and coarse, and red flags run twice a day.

Path (b), own box (Mac mini or Raspberry Pi):

- The box runs `homunculus serve` (Brainstem) and `homunculus tick --devin-path b` (Pineal), and is a Devin Outpost, so Bot sessions run their commands on the box and raw samples never leave it.
- Health Auto Export posts hourly to `https://<box>.<tailnet>.ts.net/health` over `tailscale serve`.
- Only 6 Automations run in Devin ([devin/automations/b/](devin/automations/b/)): the three coach messages, the weekly review and Cerebellum's two jobs. The box ticks everything else.
- Red flags reach the phone only through `HOMUNCULUS_PUSH_URL` and `HOMUNCULUS_PUSH_TOKEN`, for example an ntfy topic. Without the URL `tick --devin-path b` exits 1 on every run. Without the token `doctor` warns.

The box also works with no Devin at all. Pineal then runs every job, and the dashboard and the `POST /message` route are the only surfaces.

Setup for Devin Max is in [docs/DEVIN_SETUP.md](docs/DEVIN_SETUP.md), the box in [docs/RUNBOOK.md](docs/RUNBOOK.md), and the mapping of roles onto Bots in [devin/README.md](devin/README.md). [docs/DEVIN_PROMPT.md](docs/DEVIN_PROMPT.md) is the message to paste into Devin.

## Trackers

Apple Health is the hub. The iOS app Health Auto Export pushes JSON to Brainstem on path (b) or to the relay on path (a), and [homunculus/ingest.py](homunculus/ingest.py) parses it.

Five vendors have direct pollers in [homunculus/trackers/](homunculus/trackers/): Garmin (Connect Personal API), WHOOP (Developer API v2), Google Health API v4 (Fitbit, Pixel Watch), Withings and Polar (AccessLink v3). They run only on the box, where Pineal's `tracker_poll` job runs hourly at :20. No path (a) Automation runs it. You enable pollers in the ledger's `config/trackers.json` and manage them with `homunculus tracker auth|poll|status|revoke`. Oura is refused, because its API agreement forbids API data as AI input, so Oura data comes in through Apple Health. The legacy Fitbit Web API is refused because it is shut down.

Rules for every source:

- One source per daily key, chosen by priority (watch, then garmin, then phone). Sources are never averaged.
- SDNN and RMSSD never share a baseline.
- Vendor scores (recovery, Body Battery, readiness) live under vendor-prefixed keys and feed no decision.
- Each poll replaces a trailing 3-day window. A failed poll leaves values `None`.
- Tokens live in the macOS Keychain or in 0600 files under `$HOMUNCULUS_SECRETS`, never in git.

The vendor research is in [docs/TRACKERS.md](docs/TRACKERS.md), the adapter contract in [docs/TRACKER_CONTRACT.md](docs/TRACKER_CONTRACT.md).

## Privacy

There are two repos:

- `homunculus`, this one, holds the engine code and may be public. Devin may read it.
- `homunculus-ledger-<you>` is private and holds only data: aggregates computed by code, check-ins, plans, reviews and the audit trail. Raw samples stay in SQLite under `.private/`, which git ignores. GPS routes and device names are dropped at ingest.

Models get the weekly packet or a smaller context pack, never raw samples, names, birthdates or locations.

[tools/leak_guard.py](tools/leak_guard.py) blocks SQLite files, Health Auto Export payloads, GPS routes, export files, data directories, `.env` files, API keys, OAuth tokens and files over 2 MB. CI runs it on every push. Turn on the git hooks once per clone:

```sh
git config core.hooksPath .githooks
```

`pre-commit` scans the staged blobs. `pre-push` scans every commit the remote does not have yet, so a secret added in one commit and removed in the next is still caught.

## Quick start

Python 3.11 or newer, standard library only. There is nothing to install.

Run the demo first. It runs the whole pipeline on 56 days of synthetic data for a fictional person, in a fresh temporary ledger, with `FakeProvider` for every model call:

```sh
python3 -m homunculus demo --out /tmp/hm-finish
```

Output of one run:

```
Demo ledger: /tmp/hm-finish (synthetic data, fictional person)
Dashboard:   /tmp/hm-finish/site/index.html
Data:        2026-09-21 .. 2026-11-15; samples=340, sleep_segments=275, sleep_nights=0, workouts=26, events=0, checkins=403
Conflicts:   DATA_GAP=1, R1_sustained=1, R2_single_day=3, R3_session_residual=1
Papers:      10-5555-demo-0001=actionable, 10-5555-demo-0002=n_of_1_only, 10-5555-demo-0003=discard, 10-5555-demo-0004=watchlist, 10-5555-demo-0005=n_of_1_only
Review 2026-W46: ruling=ratify tier=auto messages=7
Red flags:   none
Plan:        v0002 current; new [2]; pending none
Experiment:  exp-abendlicht protocol frozen, 84 days from 2026-11-16
Frontier:    10-5555-demo-0004 watchlist -> frontier (certainty 0.5, promise 0.4 -> 3.07); deep dive: n_of_1_candidate fr-10-5555-demo-0004 (weakest link 0.5, human coverage 0.67)
Weak paper:  10.5555/demo.0005 certainty 1.5 -> 1.0, promise 0.4, flags quality_weak
Lexicon:     4 nodes, 3 links, 1 open questions; scout keywords time restricted eating, vagal tone
Tracker:     garmin ok, samples=9, sleep_nights=3, workouts=1, daily_vendor_scores=18; repoll replaced 31 rows with the same counts; Apple Health copy dropped 5 rows; canonical keys changed: none
LLM calls:   14 to FakeProvider, 0 network requests; git commits: 10
```

The demo dashboard is checked in at [docs/demo/index.html](docs/demo/index.html).

Then set up your own ledger, outside this repo:

```sh
python3 -m homunculus init-ledger ~/homunculus-ledger-<you>
export HOMUNCULUS_LEDGER=~/homunculus-ledger-<you>
git -C "$HOMUNCULUS_LEDGER" add -A && git -C "$HOMUNCULUS_LEDGER" commit -m "Ledger skeleton"
python3 -m homunculus init-plan
export HEALTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
python3 -m homunculus doctor
python3 -m homunculus serve
```

`init-ledger` writes 14 files and runs `git init`, but commits nothing, so the `git commit` line puts the skeleton into the history. `init-plan` writes and commits plan v0001 with no fixed sessions, and the first weekly review proposes them from `config/goals.md`. `doctor` prints one line per check: token, bind host, exposure, push, plan, git, roster, freshness, budget, halt. `serve` refuses to start without a `HEALTH_TOKEN` of at least 32 bytes and binds 127.0.0.1:8787.

`python3 -m homunculus --help` lists all 33 commands. The global flag `--dry-run` swaps every model for `FakeProvider`.

Run the tests with:

```sh
python3 -m unittest discover -s tests
```

That is 1120 tests in 30 files, about 25 seconds, with no network.

## Status and limits

Every run so far used synthetic data, from the demo and the test fakes. No real Health Auto Export payload, vendor API response or Devin Bot session has been checked against the code.

Marked UNVERIFIED in the code and docs:

- 44 markers in the five tracker adapters: endpoint behavior, field paths and token formats the vendor docs do not pin down.
- The `Tailscale-User-Login` header name and whether `tailscale serve` keeps the client's Host.
- The Devin Automation field names, the `STATE_FILE` format of script steps, which Bot mode bills least, and whether the GitHub app can grant read on one repo and write on another.
- A model ID for "Gemini 4 Carbon" and "Gemini 4 Neon". Until one is confirmed, Amygdala runs on Opus 5.5, the same family as Hippocampus, and the packet records that.
- Whether Health Auto Export v2 carries a per-reading `source` with Summarize off.

Known gaps:

- No job picks papers for a deep dive. Someone runs `homunculus deep-dive` by hand.
- No code fills the dose inputs, so the dose part of promise is 0 in every appraisal.
- PDFs from the full-text resolver are stored with `text_extraction="pending"`. Only JATS XML and plain text can be quoted.
- Without `ffm_kg` in `profile/owner.json`, energy availability stays `None`. Then every kcal change is rejected, and `ed_watch` counts only logged 0 kcal days.

The owner still has to provide goals with dates, scout keywords and reference papers, the devices and the canonical source per metric, the profile (time zone, sleep target, `ffm_kg`), medications, an Anthropic key with a spend limit, the Devin account and its setup, and a doctor who confirms the red-flag thresholds. [docs/ROADMAP.md](docs/ROADMAP.md) lists the open questions and the next steps.

## Intended purpose

Personal training and lifestyle planning tool for one healthy adult, for personal non-commercial use. Not intended to diagnose, prevent, monitor, predict, treat or alleviate any disease or injury.

homunculus is not a medical device. Red flags are computed by code and sent as fixed German texts that send the owner to a doctor or to 112, and they never name a diagnosis. A second user would make the owner a controller of health data under the GDPR.

## Repo map

| Path | Content |
|---|---|
| `homunculus/` | The engine: 24 modules, standard library only |
| `homunculus/trackers/` | 10 modules: pollers for Garmin, WHOOP, Google, Withings and Polar, plus their shared base |
| `agents/` | System prompts for the 11 model roles |
| `config/roster.json` | Roles, models, effort, writable ledger paths, daily caps |
| `devin/bots/` | General instructions for the 4 Bots |
| `devin/automations/a/`, `devin/automations/b/` | Generated Automation specs for each path (14 and 6) |
| `devin/blueprint.yaml`, `devin/app/` | Devin environment steps, the dashboard as a Devin App |
| `relay/` | Cloudflare Worker for path (a) ingest |
| `docs/` | Brief, architecture, novelty lane, evidence weights, trackers, Devin docs, runbook, roadmap |
| `docs/demo/` | The demo dashboard, 7 static pages |
| `tests/` | 30 test files and the tracker fakes |
| `tools/leak_guard.py`, `.githooks/` | Leak guard and the hooks that call it |
| `.github/workflows/ci.yml` | Tests on Python 3.11 and 3.13, leak guard, demo |

License: not chosen yet.

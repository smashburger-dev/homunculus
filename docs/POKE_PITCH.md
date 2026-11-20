# homunculus, for Poke

Poke, this is the health coach the owner described to you in three messages. The paths below point to the evidence in this repo.

## The idea

homunculus reads one person's tracker data, daily check-ins and new papers, and keeps a training, nutrition and recovery plan current. The plan changes once a week, after an argued review. Success means the owner enjoys life more, so a 0 to 10 wellbeing item is measured daily next to HRV and sleep. Each of the 13 roles is named after the anatomy that does the same job, for example Vagus, the source of HRV, for the well-being coach (`config/roster.json`).

## What is new

- Code computes the numbers and models judge. A model's plan diff takes effect only after `guardrails.check_diff` sorts it into auto, notify, approve or reject (`homunculus/guardrails.py`).
- Evidence has two axes. Certainty asks whether the effect is real in people like the owner, and promise asks whether it is plausible and worth having. A methods gate caps both at 1 for weak studies. A clean early paper, even an animal study, can enter a frontier lane, where a deep dive checks each mechanistic link against human evidence. If the chain holds and the intervention is low risk, it becomes an N-of-1 crossover experiment on the owner's own data (`docs/NOVELTY_LANE.md`, `homunculus/experiments.py`).
- The review is adversarial and bounded. Hippocampus drafts, Amygdala challenges with refs that must resolve to a ledger file, and Cerebellum rules. The loop stops after 2 rounds and 6 messages (`homunculus/weekly.py`).
- Devin Bots are the always-on shell. Four Bots (Cerebellum, Sartorius, Vagus, Hippocampus) hold the chats, schedules and the Needs You list. Devin cannot pin a model, so each Bot session runs a command that calls the roster's model through the Anthropic API: Fable 5.2 for Cerebellum, Opus 5.5 for reviewers and coaches, Sonnet 5.5 for Insula, Haiku 5.5 for the scouts (`docs/DEVIN_BOTS.md`).

## What is built

- 34 Python modules, about 24,000 lines, standard library only.
- 1120 unit tests in 30 files, passing in about 25 seconds.
- 18 commits and 33 CLI commands.
- 11 system prompts, 4 Bot instruction files, 20 Automation specs.
- Pollers for Garmin, WHOOP, Google Health, Withings and Polar, and a Cloudflare Worker relay.

`python3 -m homunculus demo --out /tmp/hm-finish` runs the whole pipeline on 56 days of synthetic data in about 1.4 seconds. One run printed, among other lines:

```
Review 2026-W46: ruling=ratify tier=auto messages=7
LLM calls:   14 to FakeProvider, 0 network requests; git commits: 10
```

The demo dashboard is `docs/demo/index.html`.

## What runs where

- Path (a), serverless: Health Auto Export posts a daily summary to the relay, and 13 Devin Automations ingest, scout and review on Devin machines.
- Path (b), own box: a Mac mini or Raspberry Pi runs ingest, the scheduler and the tracker pollers, keeps raw data local, and is a Devin Outpost for 6 Automations.
- The engine repo is public, and the data ledger is private. A leak guard blocks health data and secrets in pre-commit, pre-push and CI.

## What the owner still has to provide

- Ranked goals with dates.
- Scout keywords and reference papers. Until then the scouts skip.
- Devices and the canonical source per metric.
- Profile values, including fat-free mass. Without it every kcal change is rejected.
- Medications.
- An Anthropic key with a spend limit, the Devin Max setup, and the choice of path.
- A doctor who confirms the red-flag thresholds.

Nothing has run on real data yet. The 19 open questions are in `docs/ROADMAP.md`.

## Where you could help

- Interview the owner for those questions and turn the answers into `profile/owner.json`, `config/goals.md` and `config/keywords.json`.
- Collect keywords, papers and authors for the three scouts.
- Check the UNVERIFIED claims against primary sources: Gemini model IDs, the Tailscale identity header, the Devin script-step format and 44 tracker field paths.
- Challenge the marked design choices, such as the between-person spread `0.5·|δ|` in `homunculus/beliefs.py`.

Poke cannot reach the ingest server today, because it binds 127.0.0.1 and is exposed only in the tailnet. Whether chat content may touch Poke at all is open question 12.

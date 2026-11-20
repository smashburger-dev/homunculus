# homunculus architecture

Status: design, 2026-11-20. Claims marked UNVERIFIED were not confirmed against a primary source while this was written.

## Purpose

homunculus coaches one person. It reads their tracker data (Apple Health through Health Auto Export, later Garmin), their daily check-ins and new research, and keeps a training, nutrition and recovery plan up to date. The plan changes once a week, after an argued review. On any day the coaches adjust the session only inside bounds the plan already sets.

Better numbers are a means. Success means the owner enjoys training and life more, so a daily 0–10 wellbeing score sits next to every physiological metric and every experiment.

## Principles

1. Code computes every number. Baselines, loads, z-scores, certainty scores, posteriors and p-values come from deterministic Python. LLMs read them, argue about them and write prose. A number in LLM output that appears nowhere in the data is a bug.
2. Fail closed. Missing data yields `None`, and the plan keeps its last ratified state. A failing guardrail rejects the change. The server refuses to start without a token. When the budget runs out, jobs are deferred, never quietly moved to a cheaper model.
3. A human approves anything above a threshold. LLMs emit a typed plan diff, and `guardrails.check_diff` sorts it into `auto`, `notify` (24 h veto), `approve` (explicit yes) or `reject`. No prose plan is ever executed.
4. Personal tool, not a medical device. The intended purpose, shown in the README and dashboard: "Personal training and lifestyle planning tool for one healthy adult, for personal non-commercial use. Not intended to diagnose, prevent, monitor, predict, treat or alleviate any disease or injury." Red flags send the owner to a doctor and never name a diagnosis.
5. Python 3.11+ standard library only. LLM providers are called over HTTPS with `urllib`.

## Bot roster

Eleven LLM bots and two code components. The owner talks to Sartorius and Vagus daily and to Cerebellum about goals.

| Bot | Anatomy, and why it fits | Role | Model: primary / alternative | Talks to owner | Cadence |
|---|---|---|---|---|---|
| Cerebellum | Compares intended with actual movement and corrects the error | Orchestrator. Holds goals, rules on the weekly review, relays goals | Fable 5.2, effort high / Gemini 4 Carbon (availability UNVERIFIED) | Yes, goal talks | Weekly ruling, monthly goal review, on message |
| Hippocampus | Consolidates the day's memories during sleep | Weekly reviewer. Drafts the plan diff, answers challenges, designs experiments | Opus 5.5, effort high / Gemini frontier ("Gemini 4 Neon", ID UNVERIFIED) | No | Weekly |
| Amygdala | Detects threats | Adversarial critic. Can block a diff | Paid Gemini frontier model / Opus 5.5 with its own prompt | No | Weekly, plus 10% re-extraction audit |
| Thalamus | Gates which signals reach the cortex | Extracts paper fields with quote spans; code scores them | Opus 5.5 via Batch, effort medium / Sonnet 5.5 (also second extractor) | No | Weekly (Thu) |
| Motor Cortex | Plans movement | Proposes sessions and novel exercises as feature records | Opus 5.5 / Gemini frontier | No | Weekly (Sat) |
| Insula | Interoception: links body signals to feelings | Explains code-detected conflicts between check-ins and tracker data | Sonnet 5.5 / Haiku 5.5 | No | Weekly (Sat) |
| Sartorius | The tailor's muscle | Fitness coach: morning session, post-session RPE, gym feel | Opus 5.5, effort low, cached prefix / Sonnet 5.5 | Yes, daily | 07:00, on message |
| Vagus | Rest-and-digest nerve, source of HRV | Well-being coach: sleep, mood, stress, evening check-in | Opus 5.5, effort low, cached prefix / Sonnet 5.5 | Yes, daily | 07:00, 21:00, on message |
| Retina | Turns light into signal | Paper scout (biology, physiology, neurobiology) | Haiku 5.5 via Batch / code-only ranking | At setup, for keywords | Daily 03:00 |
| Spindle | Muscle spindle, senses stretch | Workout and sport-methods scout | Haiku 5.5 via Batch / code-only ranking | At setup | Daily 03:00 |
| Papilla | Taste bud | Nutrition and supplement scout | Haiku 5.5 via Batch / code-only ranking | At setup | Daily 03:00 |
| Brainstem | Keeps the vital signs running | Ingest server for Health Auto Export, check-ins, owner messages | Code | No | On event |
| Pineal | Pineal gland, sets the circadian clock | Scheduler, single git writer, budget guard, alert delivery | Code | Templated alerts | Every minute |

Changes against the draft roster:

- Amygdala runs on a different model family from Hippocampus, because same-family critics share blind spots. Without paid Gemini access it falls back to Opus 5.5, and the packet records that critic and drafter share a family.
- The owner's "SWE2 flash" is a Devin session mode, not an API model. If Haiku is down, the scouts fall back to code ranking.
- The three scouts share one code path with three keyword sets, capped at 30 candidates each per day.
- Thalamus fills a form, and a script scores it. It no longer judges papers itself.

Opus 5.5 and Fable 5.2 cannot turn thinking off. `effort` is the only cost lever, and thinking bills as output.

## Deterministic core

### Metrics (`metrics.py`)

Everything lands in `daily(date, key, value, source, n, quality)`, keyed on the local wake date. Each metric has one canonical source by configured priority (watch > garmin > phone). Sources are never averaged. SDNN (Apple) and RMSSD (Garmin, Apple "Recovery HRV") are separate series. With insufficient coverage, every derived value is `None`.

- HRV: `x_d = mean(ln sdnn)` inside the sleep window (≥ 2 samples). `m7` needs 5 of 7 days. `μ60, σ60` cover days −67…−8, so the current week stays out of its own baseline. `SWC = 0.5·σ60`, states suppressed / normal / elevated.
- Resting HR: median and MAD over days −35…−8, `z = Δ / (1.4826·MAD)`, floor 1 bpm.
- Sleep: TST, 14-day debt against the owner's target, midpoint SD, social jetlag, Sleep Regularity Index from stage segments (Phillips 2017, UNVERIFIED; needs ≥ 80% wear). Deep and REM minutes drive no decision, because wrist staging agrees poorly with polysomnography.
- Load: sRPE (`CR-10 × minutes`) is primary, Edwards TRIMP secondary. Acute (λ = 0.25) and chronic (λ ≈ 0.069) EWMA are shown separately, plus monotony, strain and the week-over-week change. ACWR drives nothing (Impellizzeri et al., Sports Med 2021).
- Weight trend `T_t = T_{t−1} + 0.1·(w_t − T_{t−1})`, rate as 14-day slope in %BW/week. VO2max as a 90-day slope only; the watch underestimates by about 6 mL/kg/min (Lambe 2025).
- Readiness: capped z-scores of HRV, −RHR, TST, −midpoint SD and wellness, weights from `config/readiness.json`. The output lists each contribution and each missing input, and with fewer than 3 inputs it is `None`.

A Health Auto Export history import gives baselines on day one. Without it, HRV states appear after about 67 days.

### Conflict detection (`insula.py`)

`O_d` is the mean z of HRV m7, −RHR and TST. `S_d` is the mean item z against each item's own 28-day baseline. `D_d = S_d − O_d`. Rule R1 fires at |D| ≥ 1.5 on 3 of 5 days with one sign. R2 fires on one day at |D| ≥ 2.5 when S and O have opposite signs and each |·| ≥ 1. R3 regresses sRPE load on TRIMP over 60 days and flags a session residual z ≥ 2 ("felt much harder than heart rate says"). An input failing its quality check yields `DATA_GAP` instead. Each record stores numbers, window and rule ID. Insula explains the conflicts; it never creates them.

### Evidence scoring (`evidence.py`, rubric `thal-1`)

Thalamus fills `paper.json`: design, n, population, outcome, effect with CI, registration, attrition, blinding, funding, I². Each field carries a verbatim quote, and a quote that is not a substring of the fetched abstract voids its field. Code overwrites title, authors, journal and `update_to` from Crossref.

Certainty `c` runs 0–4. RCTs and meta-analyses of RCTs start at 4, cohorts at 2, cross-sectional and acute mechanistic studies at 1, animal at 0.5, in vitro at 0.25. A retraction sets 0, an expression of concern caps at 1. Downgrades cover bias, imprecision against the owner's SWC, inconsistency, population mismatch and trust (preprint, product-linked funding, implausible SMD > 1, GRIM failure). Non-randomized studies can gain for large effects or dose-response, and replications add up to +1.

A paper becomes `actionable` at c ≥ 2 when a systematic review or two independent RCTs in a matching population stand behind it. A single RCT, or 1 ≤ c < 2, can only seed an N-of-1 experiment. Below 1 it goes to the watchlist. Amygdala re-extracts 10% of papers, and any field disagreement blocks scoring.

### Belief ledger (`beliefs.py`)

`beliefs/events.jsonl` is append-only. `fold(events, as_of)` rebuilds the views. A literature effect becomes a prior for the owner's own effect: `μ0 = s(c)·δ`, `σ0² = (k(c)·SE)² + τ_het²`, with shrink `s` = 1/0.8/0.5/0.25 and inflation `k` = 1/1.5/2.5/4 for c = 4…1. τ_het defaults to `0.5·|δ|` (UNVERIFIED design choice), because an individual's response varies around the population mean. Personal results update the prior with a normal-normal step. Variance grows by `σ0²/365` per day, so a belief whose P(benefit) decays below 0.8 becomes `stale` and is queued for re-testing. A retraction replays the fold without the source and flags every decision that cited it. Priors are deduplicated by registration ID. The constants are versioned and change only in a reviewed commit.

### N-of-1 experiments (`experiments.py`)

Only reversible, fast-acting, low-risk interventions qualify. The baseline is 21–28 days of one locked outcome from one locked source. Then come m pairs of L-day blocks, AB or BA per pair, with w washout days excluded at the start of each block.

Before the start, code computes `n_eff = n(1−ρ)/(1+ρ)`, `SE = sqrt((2σ²/n_eff + τ_drift²)/m)` and `MDE = (t_{.975,m−1} + t_{.80,m−1})·SE`. It refuses the experiment when m < 6 (with 4 pairs no result can reach p < 0.05), when MDE > 2·SWC, or when the baseline mean lies outside ±1σ of the 90-day mean. `protocol.json` is committed before day 1 with its exclusion rules. The allocation seed is `int(sha256(protocol_bytes)[:16], 16)`, so nobody can re-roll after peeking.

Analysis is an exact sign-flip test on the pair differences plus a normal-normal update with t-inflated variance. The decision is adopt at P(benefit) ≥ 0.8 and P(harm) < 0.05, reject below 0.2, otherwise extend by 2 pairs once. There is one primary outcome; everything else is exploratory. Training adaptations do not reverse, so novel exercises are tested on acute session outcomes, never ABAB.

### Guardrails and change budget (`guardrails.py`)

| Change | Tier |
|---|---|
| Weekly sRPE load ≤ +10% vs trailing 4-week mean | auto |
| Swap within the same pattern and load | notify |
| Load +10–20%; novel exercise; supplement; kcal change > 5%; justification from a single study | approve |
| Load > +20%, or > +30% over 2 weeks; more than 1 added hard session per week | reject |
| Return after ≥ 7 days off | capped at 70% of the prior week |

These caps are conservative, not optimal. GRONORUN (Buist 2008) found the same injury rate under the 10% rule as under faster progression. Auto changes adding up to more than 20% over 3 weeks escalate to approval.

A novel exercise enters at RPE ≤ 6 and ≤ 2 sets, with no eccentric overload, and progresses after 2 pain-free sessions. The solver admits it only when its feature record (pattern, contraction, energy system, joint load) satisfies the injury constraints, keeps pull:push between 1:1 and 2:1, and stays at most 1 novel exercise per session and 2 per week.

Nutrition has floors. Energy availability stays ≥ 30 kcal/kg FFM/day as a 7-day mean, and changes below 35 need approval. The kcal target moves at most 10% or 250 kcal a week, loss is capped at 1% BW/week, and no fast longer than 24 h is recommended. `ed_watch` fires on intake under the floor on 3 days a week, restrictive language, or more than one weigh-in a day. It pauses all deficit goals and points to professional help.

`config/forbidden.json` blocks prescription drugs, the WADA Prohibited List, dehydration cuts above 2–3% of body mass and medical altitude protocols. Supplements come from an allowlist of Kölner Liste or Informed Sport tested products and are checked against `profile/meds.json`. Geyer 2004 found undeclared steroids in 14.8% of 634 supplements.

Red flags are computed by code and sent as fixed German templates, with no LLM in the alert path. A doctor should confirm the thresholds.

- Urgent, sent at once, coaching stops: chest pain, fainting or severe breathlessness ("Ruf den Notruf 112 an"); any watch heart-rate notification; blood pressure ≥ 180/120; self-harm language (Telefonseelsorge 0800 111 0 111).
- Same day: RHR Δ ≥ 10 bpm for 3 days; RHR Δ ≥ 5 with respiratory rate +2/min or wrist temperature +0.5 °C (intense training pauses); SpO₂ < 92% on 3 resting readings; new AF burden; unintended loss ≥ 5% in 30 days; dark urine after training; focal bone pain; 3 missed periods.

`scan_text` rejects bot output with diagnostic vocabulary in English or German.

## Ledger repo and messages

There are two repos. Devin may access `homunculus`, the engine code. `homunculus-ledger-<owner>` is private and holds only data, and the box has the only deploy key. Raw samples stay in SQLite on the box. Git holds aggregates computed by code, with no GPS and no device names.

```
config/      goals.md roster.json cadence.json budget.json keywords.json readiness.json
             guardrails.json forbidden.json supplements.json exercises.json HALT (optional)
profile/     owner.json meds.json
data/        daily/2026/2026-11-20.json  baselines/2026-W47.json  conflicts/2026-W47.json
             freshness.json  alerts.jsonl
checkins/    2026/2026-11-20.json
evidence/    candidates/2026-11-20.jsonl  papers/<doi-slug>.json  cache/<api>/<sha>.json
quarantine/  <candidate-id>.txt
beliefs/     events.jsonl  views/<belief-id>.json
experiments/ exp-0003/{protocol.json, data.csv, result.json}
plans/       v0012.json  v0012.md  CURRENT  pending/<id>.json
reviews/     2026-W47/{packet.json, 01-hippocampus-draft.json, ..., 06-cerebellum-ruling.json}
mail/        <bot>/inbox/<msg-id>.json  owner/inbox/<msg-id>.json
ops/         runs.jsonl  budget.jsonl  heartbeats/<job>.json  incidents/<id>.json
```

Each message is one immutable JSON file:

```json
{"schema": 1, "id": "m-3f9a1c2b7d10", "from": "amygdala", "to": ["hippocampus"], "cc": ["cerebellum"],
 "kind": "challenge", "thread": "review/2026-W47", "round": 1, "max_rounds": 2,
 "created_at": "2026-11-22T19:05:00Z", "refs": ["reviews/2026-W47/packet.json#/hrv"],
 "data_sha": "<sha256 of packet>", "run_id": "r-2026W47-amygdala-1", "model": "claude-opus-5-5",
 "body": "markdown", "items": [{"cid": "c1", "severity": "blocker", "claim": "...", "ref": "evidence/papers/10-1000-x.json"}]}
```

Kinds: `evidence.candidate`, `evidence.appraisal`, `data.anomaly`, `checkin.received`, `goal.update`, `review.draft`, `challenge`, `rebuttal`, `ruling`, `plan.ratified`, `plan.rollback`, `ask.owner`, `alert`.

Rules:

- Scheduled message IDs are `sha256(from|kind|thread|round|data_sha)[:12]`, so a rerun writes an identical file. Pineal skips a `(job, period)` already marked `ok` in `ops/runs.jsonl`.
- Only Pineal commits, one at a time, under a file lock. `roster.json` lists each bot's writable paths, and `Ledger.write_json` raises on others. Only the ruling step writes `plans/`.
- Commits carry the bot's identity (`Amygdala <amygdala@homunculus.local>`) and trailers `Msg-Id`, `Run-Id`, `Model`, `Tokens-In`, `Tokens-Out`, `Usd`, `Kind`. `git log --author=amygdala` and `git diff v0011..v0012 -- plans/` then answer who changed what and why.
- Rollback is a new commit pointing `plans/CURRENT` back. History is never rewritten.

## Weekly cycle

Daily, in the owner's time zone:

- On every Health Auto Export push, Brainstem stores the data and checks heart-rate notifications at once.
- Hourly, Pineal checks freshness, heartbeat, budget and red flags.
- At 03:00 the scouts run. Code queries PubMed, Europe PMC and bioRxiv, deduplicates, applies the Crossref retraction veto and stores abstracts in `quarantine/`. Haiku (Batch) only classifies design, population and relevance into enums.
- At 06:30 Pineal finalizes yesterday's daily rows, baselines, readiness and conflicts.
- At 07:00 Sartorius and Vagus send the morning message built from `plans.modulate_day`. They may shorten or swap a session within the plan's bounds and never add load. At 21:00 Vagus asks for the evening check-in.

Weekly:

1. Thursday: Thalamus extracts the top 10 candidates, and code scores them.
2. Saturday: code writes the week's conflicts. Insula explains them and drafts at most 3 owner questions. Motor Cortex proposes session changes, and the solver filters them.
3. Sunday 17:00: `weekly.build_packet` writes `packet.json` with week aggregates, baselines, conflicts, actionable papers, belief changes, experiments, the current plan, guardrail context and budget. Its sha256 is the `data_sha` every later message cites.
4. Hippocampus drafts the review and a typed `plan_diff`. Code attaches the `check_diff` tier.
5. Amygdala round 1 sends challenges with `cid`, severity (`blocker`, `major`, `minor`) and a `ref` that must resolve to a packet pointer or ledger file. Code drops any without one. The checklist in `agents/amygdala.md` covers regression to the mean, multiplicity, confounders, measurement change, missingness, the noisier signal, the evidence gate, Goodhart, cumulative budget, wellbeing and dropped dissent.
6. Hippocampus answers every `cid` with `accept` and the change, or `rebut` and a ref.
7. Amygdala round 2 may only challenge diff paths that changed in round 1.
8. Hippocampus answers again. A thread holds at most 6 messages. A seventh, or the same output hash twice, closes it with an alert.
9. Sunday 21:00: Cerebellum ratifies, ratifies in part, or keeps the plan. Code reruns `check_diff`. An `auto` diff takes effect Monday 00:00, `notify` after a 24 h veto window, `approve` once the owner says yes, and a `reject` diff is dropped. A blocker still open keeps the current plan, extends `valid_until` by 7 days and sends `ask.owner`.

The owner gets at most 3 approval requests a week, batched. Monthly: goal talk with Cerebellum, experiment close-outs, budget report.

Stale data (HRV or sleep older than 36 h, workouts older than 72 h) makes the coaches say "keine Daten seit X" and bars Hippocampus from load increases. `config/HALT` stops everything except ingest and red flags.

## Devin reality check

The Devin docs as of November 2026 show:

- The v3 Sessions API has no model field. `devin_mode` is an enum, so no session can be pinned to Opus 5.5 or Fable 5.2, and Devin Bots have no per-bot model setting.
- Billing counts agent actions and VM time, and no public dollar price per ACU exists (UNVERIFIED).
- Schedules are deprecated in favor of Automations (RRULE, webhook triggers, per-run ACU limits). Knowledge and playbooks are moving to Skills.
- Paid plans train on customer data until Data Controls opt out.

Eleven bots as Devin sessions would pay for idle VMs, break the model tiers and put health data on Cognition's machines. The split:

| Job | Runs on |
|---|---|
| All eleven LLM bots | Python on the box, calling the Anthropic Messages API (Gemini REST for Amygdala) with the model from `roster.json` |
| Ingest, scheduling, metrics, scoring, guardrails, commits | Python on the box |
| Building the engine | Devin sessions opening PRs against `homunculus` |
| Repair after a failed job | Devin Automation on a webhook. Pineal sends a scrubbed incident (job, traceback without locals, commit sha) |
| Monthly code-health review | Scheduled Devin Automation on the engine repo |
| Scouts, if the owner wants them inside Devin | A separate workspace with no secrets and no ledger. It writes schema-valid candidates to `homunculus-scout-feed`, and Pineal pulls and re-validates them |

Every Automation gets an ACU limit, and data-control opt-out is on. `docs/DEVIN_SETUP.md` has the steps.

## Privacy and safety

- Brainstem refuses to start without a token of at least 32 bytes, compares with `hmac.compare_digest`, binds to 127.0.0.1, caps `limit` at 5000, and drops GPS routes and device names at ingest. The seed's `SWARM_URL` forwarding is gone, and Pineal reads SQLite directly. Raw payloads sit in a `chmod 700` directory for 30 days.
- LLMs get the packet or a smaller context pack, never raw samples, names, birthdates or locations. Fable 5.2 requires 30-day retention and cannot run under zero data retention, so Cerebellum sees goal-level context and aggregates only.
- Anthropic's commercial API does not train on inputs and deletes them within 30 days. The Gemini free tier is never used. The paid tier keeps data 55 days for abuse monitoring, and logging must be off.
- Network: `tailscale serve` only, never `funnel`, with ACLs limited to the owner's devices.

Prompt injection is handled by structure (Beurer-Kellner, Tramèr et al. 2025). Scout and Thalamus calls have no tools. Fetched text reaches a model only from `quarantine/`, inside a delimited data block, and only schema-valid JSON comes back. Acting bots never see raw web text. The only consequential output is a plan diff, and it must pass `guardrails`. A citation counts once code has resolved its DOI or PMID and matched the title at ≥ 0.9. GPT-4o fabricated 19.9% of citations in a 2025 JMIR study.

Law, as inference and not legal advice: the GDPR household exemption likely covers one person. A second user makes the owner a controller of Art. 9 health data, with consent, processor agreements and probably a DPIA. The MDR excludes lifestyle and well-being software by intended purpose (Recital 19). The banned-vocabulary check keeps it that way.

## Dashboard

`dashboard.build` writes static HTML with inline CSS and SVG sparklines, without JavaScript frameworks or CDNs, after every Pineal commit. Pages: Today (readiness components, session, freshness, alerts), Plan (version and diff to parent), Week (review thread), Open challenges, Experiments, Budget, and a Check-in form that posts to Brainstem.

Brainstem serves `site/` on 127.0.0.1, and `tailscale serve` exposes it over HTTPS inside the tailnet. The dashboard checks the identity header `tailscale serve` adds (UNVERIFIED) against the owner's login, while `/health` keeps requiring the token. GitHub Pages is out, because a Pages site from a private repo is public on most plans.

## Cost

Anthropic first-party prices per million tokens: Fable 5.2 $8/$40, Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50. Batch halves them.

| Bots | Weekly volume | $/week |
|---|---|---|
| Retina, Spindle, Papilla (Haiku, Batch) | 0.6M in, 0.15M out | 0.10 |
| Thalamus (Opus, Batch) | 10 × 30K in, 3K out | 1.0 |
| Insula (Sonnet) | 7 × 15K in, 2K out | 0.35 |
| Sartorius, Vagus (Opus, ~80% cache hits) | 140 turns × 20K context, 1K out | 5 |
| Motor Cortex (Opus) | 60K in, 10K out | 0.45 |
| Hippocampus, 3 turns | 450K in, 45K out | 2.7 |
| Amygdala, 2 turns (Opus price as proxy) | 250K in, 15K out | 1.3 |
| Cerebellum (Fable) | 150K in, 15K out | 1.8 |
| Total | | about 13; budget 20–35 with thinking |

Every call appends `usage` to `ops/budget.jsonl`, priced from `config/budget.json`. The default weekly cap is $40. At 80% the coaches drop to effort low and the scouts pause. At 100% only ingest, red flags and the weekly review run, and an alert goes out. The Devin plan ($20 Pro or $200 Max) and repair sessions come on top. Week one measures real thinking-token use.

## What the owner had not mentioned yet

- An emergency path. Code-detected red flags need fixed text, an emergency number, a doctor contact, and a coaching stop.
- Eating-disorder risk. Daily weight feedback with calorie targets is a known trigger, hence the energy floor, weekly-averaged weights and `ed_watch`.
- Supplement contamination and drug interactions.
- Measurement limits: SDNN vs RMSSD, unreliable wrist sleep stages, VO2max as a trend only, double-counted steps across devices, baselines reset by device or firmware changes.
- Cold start. Trends need 4–9 weeks of data or a history import.
- Statistics discipline: preregistration, one primary outcome, ≥ 6 pairs, regression to the mean.
- Hallucinated and retracted citations, and prompt injection from scraped pages.
- Retention per provider, and the GDPR duties that start with a second user.
- Approval fatigue, a kill switch, rollback, stale-data behavior and a hard budget.
- Enjoyment as a measured outcome, through the daily wellbeing item.
- A human coach or doctor for Everest, an MMA fight or an Ironman. Altitude illness, weight cuts and head impact are beyond what a solo swarm can supervise.
- Git keeps aggregates forever, so deleting data later means rewriting history.

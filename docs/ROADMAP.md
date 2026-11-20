# Roadmap

Status on 2026-11-20. The engine passes 1120 tests and runs end to end on synthetic data. It has not seen real data, a real vendor response or a real Devin Bot session yet.

## Open questions for the owner

These come from `open_questions` in [MODULES.json](MODULES.json), merged where two asked the same thing and updated for the Devin Bots design. The setup now targets Devin Max, with Sartorius on iMessage and Cerebellum and Vagus on Slack, so the old questions about the Devin plan and the chat surface are folded into questions 11 and 15.

1. Goals for the next 12 months, ranked: which of Ironman, an MMA fight, Everest, weight loss or weight gain are real, and by which dates? Everest and MMA need an external coach or doctor and stricter rules.
2. Keywords and reference papers or authors for Retina (biology, physiology, neurobiology), Spindle (sports and training methods) and Papilla (nutrition, supplements). With an empty `config/keywords.json` every scout run ends as `skipped`.
3. Devices: what do you wear at night and in training (Apple Watch, Garmin, both, another)? Which one is canonical per metric (HRV, RHR, sleep, workouts, steps)? Do you want Garmin data in v1, and can you enable the Garmin Connect Personal API?
4. Can Health Auto Export run with Summarize off for sleep and workouts v2, with heart-rate notifications included? Do you have a full history export for the cold start?
5. Profile for `profile/owner.json`: sex, birth year, time zone, sleep target, work days, and a measured HRmax, resting HR or lab VO2max if you have one. Also fat-free mass (`ffm_kg`), from a DEXA scan or a scale estimate. Without it every kcal change is rejected.
6. Medical: diagnoses, medications (beta-blockers invalidate the HR rules), allergies, any history of disordered eating? Should calorie numbers be hidden by default? Is there a doctor who can confirm the red-flag thresholds, and which doctor contact should alerts name? The emergency number 112 is assumed.
7. Risk appetite for experiments: behavioral changes only, or also allowlisted supplements, fasting and sleep timing? Will you commit to 6 to 8 weeks per crossover experiment, with at most 1 or 2 running at once? Can someone blind supplement trials?
8. Should the daily 0 to 10 wellbeing item be a co-primary outcome in every experiment? Will you fill in the 30-second morning form daily, and if not, which 3 items matter most?
9. Approval policy: is the cap of 3 approval requests a week right? Should notify-tier changes, which take effect after a 24 h veto window, also need your explicit yes?
10. Weekly LLM budget ceiling (default $40), and which bots get cut first at 80%? Do you accept paying beyond the free OpenAlex budget?
11. Path (a), serverless, or path (b), own box? For path (b): which machine (Mac mini, Raspberry Pi), and is it on your tailnet?
12. Consent on data location: may aggregates sit in a private GitHub repo? May chat content touch Slack, iMessage through Devin, Cognition's machines, Cloudflare (path a relay) or Poke, or only your box and the model APIs?
13. Do you accept that Cerebellum on Fable 5.2 cannot run under zero data retention (30-day retention), so it sees only goal-level context and aggregates?
14. Do you have paid Gemini API access, and which exact models do you mean by "Gemini 4 Carbon" and "Gemini 4 Neon"? Without it Amygdala runs on Opus 5.5, the same family as Hippocampus.
15. Is the chat split right: Sartorius on iMessage, Cerebellum and Vagus on Slack in a private workspace? Only one Bot can hold an iMessage number.
16. Path (b) push channel for red flags: a self-hosted ntfy server, or ntfy.sh with an access token?
17. Single user only, or friends later? A second user triggers GDPR controller duties for health data and changes the MDR exposure.
18. Should experiments and load increases pause during competition blocks (fight camp, taper) and during an intended cut, and should weight alerts be off then?
19. Menstrual cycle tracking: applicable, and wanted as a confounder in reviews and as a red-flag input?

## Known gaps

Each item below is true of the code as it is now.

- Energy availability needs `ffm_kg`. `init-ledger` writes `"ffm_kg": null`, so `weekly._ea` returns `None` for every day. `guardrails._ea_finding` then rejects every kcal change with `ea_unknown`, and `ed_watch` can count only logged 0 kcal days, more than one weigh-in a day, and restrictive language. This fails closed, but it means no nutrition change goes through until the owner sets `ffm_kg`.
- Red flags look at today and yesterday only. `pineal._job_red_flags` evaluates those two days. On path (a), a summary that arrives late, for example after two days without unlocking the phone, brings older days that no red-flag rule ever checks.
- The `ingest-health` spec stays in `devin/automations/a/`, but it is locked. `ingest --llm-copy` refuses unless `config/devin.json` holds `{"llm_copy_ingest": true}`. The spec exists for owners without the relay. A model copies the payload in that path, so a changed digit would pass unnoticed.
- The dashboard login check is a boundary against other tailnet devices, not against the box itself. `tailscale serve` connects from 127.0.0.1, so any local process can send `Tailscale-User-Login` and a `*.ts.net` Host and pass as the owner. It can then post check-ins and owner messages. The only mitigation is a separate `homunculus` user with nothing else running under it ([RUNBOOK.md](RUNBOOK.md)).
- Vendor endpoints are UNVERIFIED. The tracker adapters carry 44 markers: 15 in Garmin, 11 in Google, 8 in WHOOP, 6 in Withings, 4 in Polar. Tests run against `tests/tracker_fakes.py`, never a real response. Google needs a manual auth test before its poller is worth enabling.
- Path (a) does not run three Pineal jobs. No Automation includes `tracker_poll`, `frontier_review` or `ingest-prune`. On path (a), no direct tracker data arrives, and the monthly frontier quota check never runs.
- The roster calls Cerebellum's monthly job a goal review. The `monthly_report` job sends a fixed German template with the month's LLM spend and the number of finished experiments. Goal talks happen only when the owner writes to Cerebellum.
- Nothing picks papers for a deep dive. Someone runs `homunculus deep-dive` by hand.
- No code fills the dose inputs, so the dose part of promise is 0 in every appraisal.
- `homunculus lexicon export-keywords --write` closes the loop from deep-dive questions to scout keywords, but no job runs it.
- PDFs from the full-text resolver are stored with `text_extraction="pending"`. Only JATS XML and plain text become quotable text.
- No code enforces the cap of 3 approval requests a week. Only the Cerebellum Bot instructions state it.
- [ARCHITECTURE.md](ARCHITECTURE.md) lists a monthly code-health review as a Devin Automation on the engine repo. No spec for it exists in `devin/automations/`.

## Deliberate trade-offs

- Code computes every number, and models only argue. A model cannot fix a wrong baseline, so metric bugs need code fixes.
- Missing data yields `None`, and the plan keeps its last ratified state. Stale HRV or sleep (older than 36 h) bars load increases.
- Load caps are conservative: +10% a week auto, +10 to 20% needs approval, above +20% is rejected. GRONORUN (Buist 2008) found the same injury rate under the 10% rule as under faster progression, so the caps cost little.
- ACWR drives nothing (Impellizzeri et al. 2021). Deep and REM minutes drive nothing, because wrist staging agrees poorly with polysomnography.
- Amygdala shares a model family with Hippocampus until a Gemini model ID is confirmed. The packet records it.
- Path (a) trades fidelity for no hardware: daily data, red flags twice a day, and summaries in the private ledger's git history.
- Git keeps aggregates forever. Deleting data later means rewriting the ledger's history.
- Without a history import, HRV states appear after about 67 days.

## Next steps

1. Owner: answer questions 1 to 6, 11 and 12. They block a useful first week.
2. Owner: run `python3 -m homunculus demo --out /tmp/hm-finish` and the tests on your own machine, and run `git config core.hooksPath .githooks` in every clone of the engine.
3. Owner: `init-ledger`, fill in `profile/owner.json` (with `ffm_kg`), `config/goals.md`, `config/keywords.json` and the `tz` in `config/cadence.json`, commit the skeleton, run `init-plan`, push the ledger to a private repo.
4. Set up Devin with [DEVIN_PROMPT.md](DEVIN_PROMPT.md) and run every Automation once with `--dry-run`.
5. On the first real runs, check the UNVERIFIED Devin facts (Automation field names, `STATE_FILE` format, script output format, GitHub app permissions) and record the answers in [DEVIN_BOTS.md](DEVIN_BOTS.md).
6. Send the first real Health Auto Export payload and check the per-reading `source` strings with Summarize off. On path (b), import the history.
7. First real week: compare Devin usage with the caps daily, measure the thinking tokens, and adjust `config/budget.json`.
8. Have a doctor confirm the red-flag thresholds in `guardrails.py`.
9. Write the code-health Automation spec or drop it from ARCHITECTURE.md.
10. Check the Garmin poller against a real response first, then the other adapters, and remove each UNVERIFIED marker once its field is confirmed.
11. Close the engine gaps: a path (a) Automation for `frontier_review`, a schedule for `lexicon export-keywords`, a job that picks deep-dive papers, the dose inputs, PDF text extraction, and red flags for every day a late summary brings.
12. Move Amygdala to a paid Gemini model once its ID is confirmed.
13. Choose a license.

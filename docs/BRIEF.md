# homunculus: design brief

The cortical homunculus is the map of the body inside the brain. This project builds that map for one person: a swarm of always-on agents that reads their tracker data, their subjective check-ins and the newest research, and turns it into a training, nutrition and recovery plan that keeps updating.

The goal is not better numbers for their own sake. The goal is that the person enjoys life more.

## What the owner asked for (summary of three messages to Poke)

1. A personalized health coach that works from every data point in Apple Health, Garmin and similar trackers: past workouts, sleep sessions, heart rate, HRV and more.
2. It customizes existing workouts and proposes novel exercises based on goals. Goals can be ambitious: Ironman, MMA fight, climbing Everest, losing or gaining weight.
3. Always-on Devin bots search the newest papers in biology, fitness, nutrition and neurobiology, plus new workouts from all sports, and synthesize new approaches. Another bot weighs new evidence empirically (methodology, results, significance).
4. Roles the owner listed:
   - one fast, small scraper for papers and workouts (nutrition, biology, neurobiology); it may ask the owner for keywords and references up front
   - a health-data puller for Apple Watch and other trackers
   - two personal bots the owner talks to most: a fitness coach and a well-being coach (sleep, mood, gym feel); their subjective input is later weighed against tracker data
   - a weekly reviewer that takes all data, all subjective input and all papers and works out next steps for the workout and nutrition plan, resolving conflicts between subjective feelings and tracked performance
   - an adversarial bot that pokes the reviewer and the orchestrator with hard questions: were new papers weighed properly on methodology, results, significance; were subjective feelings and the data on specific days weighed properly
   - an orchestrator the owner talks to less often, about general goals, which relays goals to the bots that need them
5. Model tiers: orchestrator = highest frontier model (Fable 5.2, or the owner's named alternative "Gemini 4 Carbon"). Reviewers, judges, synthesizers = frontier (Opus 5.5, or "Gemini 4 Neon"). Scrapers and data pullers = small fast models (Haiku 5.5, or "SWE2 flash"). The owner expects 3-4 frontier bots and 4-5 small scrapers, about 10 bots total, 2-3 of which the user talks to regularly.
6. Every bot gets an anatomical name that fits its role. The owner suggested Cerebellum for the orchestrator and another brain region for the research bot.
7. Storage: a private GitHub repo per user holds everything. Optional Tailscale server or an always-on server for a personal dashboard (simple HTML is fine) the user opens on phone or laptop.
8. Tone: orchestrator and reviewers talk to each other in a neutral tone. The personal coaches talk to the user personally.
9. The owner wants to know what else the architecture needs that they have not mentioned.

## Existing piece

`docs/_seed_webhook.py`: a working stdlib-only local server. The iOS app "Health Auto Export" POSTs JSON (`{"data": {"metrics": [{"name", "units", "data": [{"date", "qty" | "Min"/"Avg"/"Max" | sleep fields, "source"}]}], "workouts": [...]}}`) to `POST /health`. It stores samples in SQLite (`samples(metric, unit, date, field, value, source)`, `workouts(name, start, end, raw)`), keeps raw payloads, forwards batches to `SWARM_URL`, and serves `GET /metrics /latest /samples /summary /workouts` with a bearer token. This becomes the Brainstem ingest layer.

## Draft roster (to be challenged)

| Bot | Anatomy | Role | Tier |
|---|---|---|---|
| Cerebellum | error correction, compares intended vs actual movement | orchestrator, goals, compares plan vs outcome | frontier top |
| Hippocampus | memory consolidation during sleep | weekly review and plan update | frontier |
| Amygdala | threat detection | adversarial critic | frontier |
| Thalamus | gates which signals reach cortex | evidence appraisal, decides which papers count | frontier |
| Motor Cortex | plans movement | synthesizes novel exercises and sessions | frontier |
| Insula | interoception, links body signals to feelings | reconciles subjective check-ins with tracker data | mid |
| Sartorius | the tailor's muscle | personal fitness coach | mid/frontier, user-facing |
| Vagus | rest-and-digest, HRV | personal well-being coach | mid/frontier, user-facing |
| Brainstem | vital signs | health data ingest | small + deterministic code |
| Retina | sees | paper scout | small |
| Spindle | muscle spindle, senses stretch | workout and sport-methods scout | small |
| Papilla | taste buds | nutrition and supplement scout | small |
| Pineal (code, not an LLM) | circadian clock | scheduler for cadences | code |

## Engineering principles (from the owner's other projects)

- Deterministic code is authoritative for numbers. An LLM never computes a baseline, a training load or a statistic; it reads them.
- Fail closed on privacy. Health data stays in the user's private repo and local server.
- Python standard library first, so it runs anywhere Devin runs. Python 3.11+.
- Code identifiers and comments in English.

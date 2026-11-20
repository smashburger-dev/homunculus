# Owner input: do not drown novel papers (2026-11-20)

The owner's words, condensed: systematic reviews and meta-analyses come first. A very new paper, even one that so far only worked in rats, must not be dropped automatically when its building blocks are well founded and its methodology holds. A paper with shaky methodology does not get the weight of a solid one. A hard threshold that only lets meta-analyses through makes it impossible to ever try something new. The bot in charge should feel prompted to dig deeper on promising novel work and store what it learns in a growing lexicon. Full text should come from as many legal sources as possible, including the owner's own paywall account, archive.org and the Wayback Machine for conference PDFs.

## Design consequences

1. **Two axes instead of one score.**
   - *Certainty* (GRADE-like): how sure are we that the effect is real in people like the owner? Meta-analyses and RCTs lead here. This axis decides the strength of plan changes.
   - *Promise*: how plausible and valuable is it if true? It scores mechanistic coherence (Bradford Hill plausibility, coherence, biological gradient), whether each link of the causal chain is supported in humans, conserved pathways, a translatable dose (human equivalent dose via body-surface-area Km factors), methodological quality of the study itself (randomization, blinding, preregistration, sample size), and expected benefit.
   - Methodology gates both axes. A sloppy study gets low certainty AND capped promise. A clean animal study can have low certainty but high promise.
2. **Frontier lane.** A paper with low certainty, high promise and clean methods enters status `frontier`:
   - Thalamus opens a *deep dive*: it decomposes the claim into a chain of mechanistic links and searches for human evidence for each link separately ("is the mechanism shown in humans? the biomarker? the dose?"). Each link gets its own certainty.
   - If the chain holds, the intervention is low risk (guardrails allowlist: behavioral, food-based, timing, training variables, allowlisted supplements) and it is testable with the owner's tracker data, it becomes an **N-of-1 experiment candidate**. The personal result then updates the belief, so the owner's own data can promote a frontier idea.
   - High-risk interventions never enter the frontier lane as experiments. They stay as "watch" until human data exist.
   - A frontier quota keeps novelty alive: at most one frontier experiment at a time, at least one frontier candidate reviewed per month.
3. **Lexicon (knowledge feedback loop).** The ledger keeps `evidence/lexicon/`: concepts, mechanisms and interventions as nodes, papers as typed edges (supports, contradicts, mechanism-of, replicates, retracts). Every deep dive writes its link verdicts there, so later papers on the same mechanism start from what is already known. Open questions from deep dives become scout keywords for Retina, closing the loop.
4. **Full-text resolver, legal sources only.** It tries these in order: Unpaywall (best OA location), Europe PMC / PMC open access, OpenAlex `best_oa_location`, Semantic Scholar `openAccessPdf`, CORE, arXiv / bioRxiv / medRxiv, Internet Archive Scholar (scholar.archive.org), and Wayback Machine snapshots of the PDF URL (CDX API). Last is the owner's own access: a publisher TDM API key (Elsevier, Springer Nature, Wiley) or an institutional proxy session, configured by the owner in the private ledger's secrets. The system never enters credentials itself and never uses shadow libraries. Fetched text lands in quarantine like every other fetched text.

## As implemented

The code follows the design above with these rules and limits. Values marked DC in the code are design choices, not sourced numbers.

### Status rules (`evidence.appraise`)

- Methods gate (`evidence.quality`): the checks are randomized and blinded outcome (experimental designs only), preregistered, and sample size (n ≥ 20, or ≥ 10 for animal and in vitro). Unknown counts as failed. Methods are weak when fewer than half the checks pass, on outcome switching or on a GRIM failure. Weak methods cap certainty at 1 and promise at 1, and the gate follows the capped certainty, so a weak paper drops from `actionable` to `n_of_1_only`.
- Promise runs 0–4: 4 × (0.30 coherence + 0.25 human links + 0.10 conserved pathway + 0.15 dose + 0.20 benefit). Coherence is a third each for plausible (every link has support), coherent (no link contested) and dose-response. Human links is the deep dive's coverage. Benefit is the lower CI bound in the claim's direction over 2 SWC (over d = 0.5 without a SWC), clamped to 0–1; without a direction from a linked belief it is 0.
- Dose: `homunculus hed <dose> <species>` converts an animal dose with the Km factors, but no code fills the dose inputs yet, so the dose component is 0 in every appraisal.
- A paper enters `frontier` when no blocker holds: `vetoed`, `concern` (expression of concern or a pooled retracted study), `discarded`, `certain_enough` (c ≥ 2), `low_promise` (< 2.5), `weak_methods`. Otherwise it keeps its ordinary gate.
- Plausible, coherent, conserved pathway and coverage come only from a deep dive. Before one, promise stays at 1.2 or below, so `frontier` appears only after a dive. `score-paper` stores the appraisal on a ledger paper record, and `--appraise` also prints it.

### Deep dive (`deepdive.py`, `homunculus deep-dive`)

- No job picks papers for a dive. Someone runs `homunculus deep-dive evidence/papers/<slug>.json --source <quarantined text>` (repeatable). A paper with a hard blocker (audit block, veto, concern, discard, weak methods) is refused before any LLM call.
- Thalamus returns the claim and a contiguous chain of at most 8 links (mechanism, biomarker, dose, outcome). Evidence whose quote is not in the source text is dropped.
- A cited paper counts only when the ledger has scored it: it brings its ledger certainty and design. Unscored citations count 0. The paper under review never counts as evidence for its own chain, and retracted papers are ignored.
- A link has human support when a scored human-design paper with certainty ≥ 0.5 supports it, or when the lexicon already marks it human. Support and contradiction start from the lexicon's earlier verdict for the same link. A link whose strongest contradiction is at least as strong as its support is contested.
- The chain holds at weakest link ≥ 0.5, human coverage ≥ 2/3 and no contested link. The decision is `n_of_1_candidate` when the chain holds, the category is behavioral, food, timing, training or supplement, nothing matches `config/forbidden.json`, a supplement passes `guardrails.supplement_check`, and the outcome is a tracker key. Everything else is `watch`, with the reasons listed.
- The record lands in `evidence/deepdives/<slug>.json`, with belief id `fr-<slug>`. Link verdicts go to `evidence/lexicon/events.jsonl`.
- Weak, contested or non-human links become open questions. `homunculus lexicon export-keywords --write` merges their keywords into `config/keywords.json` for all three scouts; no job runs it.

### Quota and review (`weekly.py`, `pineal.py`)

- The packet's `frontier` section lists up to 3 untested `n_of_1_candidate` dives, best chain support first. A dive whose paper carries a hard blocker never appears. With no candidate, the best `watch` dive appears once, when no packet of that calendar month has surfaced one yet.
- At most 1 frontier experiment runs at a time; an experiment without a result, or with result `extend`, counts as running. A Hippocampus draft that would exceed this is re-asked once, and a second violation aborts the review with the plan unchanged.
- Amygdala round 1 must challenge every frontier promotion with an item whose `ref` is the deep-dive file. A challenge that misses one is re-asked once, then the review aborts.
- `guardrails.check_diff` puts any op that cites `evidence/deepdives/` into the approve tier, so the owner says yes to every frontier-backed change.
- The `frontier_review` job runs monthly at 08:15. It writes `evidence/frontier/<month>.json` for the previous month and, when that month surfaced no candidate, tells the owner the next one. `homunculus frontier [week]` prints a week's section.
- An N-of-1 result on a frontier belief updates that belief through `experiments.record_result`, as for every other experiment.

### Full text (`fulltext.py`, `homunculus fulltext`)

The resolver tries the sources in the order of section 4. The owner's publisher keys and proxy session live in `$HOMUNCULUS_SECRETS/fulltext.json` (default `~/.local/state/homunculus/fulltext.json`), outside every git tree, not in the ledger. Output goes to `quarantine/`. PDFs are stored as they are with `text_extraction="pending"`, because the standard library cannot extract PDF text reliably; only JATS XML and plain text become text a dive can quote.

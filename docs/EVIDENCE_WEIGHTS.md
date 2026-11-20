# Evidence rubric thal-2

Status: design, 2026-11-20. Replaces `thal-1` in `homunculus/evidence.py`. Written so an engineer can change `evidence.py` and `tests/test_evidence.py` from this file alone. Values marked DC are design choices calibrated against the cited sources but not taken from them (UNVERIFIED as numbers). All DC values go into the versioned constants block at the top of `evidence.py` and change only in a reviewed commit, like the belief constants.

## Why thal-1 is not enough

1. Imprecision checks only the CI against SWC. A 16-person RCT with SMD 1.4 and a CI clear of SWC loses nothing for imprecision. GRADE rates down when the sample falls short of the optimal information size (OIS), even when the CI looks fine (GRADE 6, https://pubmed.ncbi.nlm.nih.gov/21839614/; GRADE 34, https://pubmed.ncbi.nlm.nih.gov/35934265/).
2. With sex weighted .25, a male-only study scores 0.75 for a female owner and passes the population gate. Participants in sport science are 66% male and 31% of studies are male-only (Cowley 2021, https://researchportal.northumbria.ac.uk/en/publications/invisible-sportswomen-the-sex-data-gap-in-sport-and-exercise-scie/).
3. Animal and in-vitro studies are held down only by the population penalty. Replications can lift an animal study to 1.5 and let it seed a belief prior. About a third of highly cited animal studies translated into positive human RCTs (Hackam & Redelmeier 2006, https://pmc.ncbi.nlm.nih.gov/articles/PMC1782020).
4. Publication bias, the fifth GRADE domain, is missing. Positive-result rates are 81-82% in kinesiology and sport medicine (Twomey 2021; Büttner 2020 BJSM, https://bjsm.bmj.com/content/early/2020/07/22/bjsports-2019-101863.abstract).
5. A preprint can become `actionable`.
6. Upgrades have no total cap, and an observational study can reach 4.
7. `to_prior` shrinks only by certainty. In 25 sport-science replications the effect fell by a median of 75%, with an original n of about 17 (Murphy et al. 2025, https://pmc.ncbi.nlm.nih.gov/articles/PMC12513899/). Across Cochrane RCTs a just-significant result overstates the true effect about 1.7-fold (van Zwet, Schwab, Senn 2021, https://arxiv.org/pdf/2011.15004).

## Contract changes

| Item | thal-1 | thal-2 |
|---|---|---|
| `RUBRIC_VERSION` | `'thal-1'` | `'thal-2'`. Old priors keep `rubric_version`, so a replay can tell them apart. |
| `population_match` weights | sex .25, age .25, training .3 exact, condition .2 | sex .30, age .15, training .35 × ordinal, condition .20 |
| `score()` signature | `(paper, owner, *, update_to, swc_paper_units, replications=0, independent_rcts=0)` | adds `includes_retracted: bool = False` (code-derived by scouts from Crossref, never extracted) |
| Adjustment names | bias, imprecision, inconsistency, population, indirectness, trust, large_effect, dose_response, replication | bias, imprecision, inconsistency, indirectness (now holds the population penalty), publication_bias, trust, large_effect, dose_response, replication. `population` is gone. |
| Caps | concern cap 1, max 4 | several caps, each reported as a flag `cap:<name>` |
| `Score` dataclass | unchanged | unchanged. New flags only: `mechanistic_only`, `below_ois`, `n_unknown`, `pi_crosses_null`, `few_studies`, `funnel_asymmetry`, `small_studies`, `short_duration`, `cap:*`. |
| Gate `actionable` | c ≥ 2, (meta_rct or ≥ 2 independent RCTs), pop ≥ 0.75 | also needs `peer_reviewed is True`, no expression of concern, design not `mechanistic_human`, and a meta_rct needs `k_studies ≥ 2` |
| `to_prior` | shrink by c only | shrink by the smallest of c, z and n factors; τ_het can widen from a prediction interval; mechanistic designs never seed |
| `PAPER_SCHEMA` | | new fields below; `required` grows, so the Thalamus prompt in `agents.py` and `tests/test_prompts.py` change too |

Loading an old thal-1 `paper.json` must not fail on the missing fields. Treat a missing new field as `{"value": null, "quote": null}` before scoring. Rescoring old papers under thal-2 is a replay event in the belief ledger, not a silent overwrite.

## New `PAPER_SCHEMA` fields

Each carries a verbatim quote like every other field.

| Path | Type | Meaning |
|---|---|---|
| `duration_weeks` | number ≥ 0 or null | intervention length |
| `outcome/is_acute` | boolean or null | true for single-session outcomes |
| `effect/pi_lo`, `effect/pi_hi` | number or null | 95% prediction interval, same scale as the CI (meta only) |
| `funnel_asymmetry` | boolean or null | the paper reports a significant asymmetry test (Egger or similar) |
| `median_study_n` | number ≥ 1 or null | median sample size of included studies (meta only) |

`includes_retracted` is not in the schema. It is a keyword argument of `score()`.

## Constants

```python
RUBRIC_VERSION = 'thal-2'
BASE_CERTAINTY = {'meta_rct': 4.0, 'rct_parallel': 4.0, 'rct_crossover': 4.0,
                  'cohort_prospective': 2.0, 'case_control': 2.0,
                  'cross_sectional': 1.0, 'mechanistic_human': 1.0,
                  'animal': 0.5, 'in_vitro': 0.25, 'review_opinion': 0.0}        # unchanged
UPGRADABLE = frozenset({'cohort_prospective', 'case_control', 'cross_sectional'})   # unchanged
MECHANISTIC_ONLY = frozenset({'animal', 'in_vitro'})
MAX_CERTAINTY = 4.0
CAPS = {'concern': 1.0, 'includes_retracted': 1.0, 'tiny_n': 1.0, 'mechanistic_human': 1.5,
        'preprint': 3.0, 'product_funding': 3.0, 'upgraded_observational': 3.0}   # DC except concern
TINY_N = 10                                  # DC
OIS_CONSTANT = 31.4                          # total n = 4 (z_.975 + z_.80)^2 / d^2 = 31.36 / d^2
OIS_DEFAULT_D = 0.5                          # DC
OIS_MIN_D = 0.2                              # DC, so OIS never exceeds 785
CROSSOVER_RHO = 0.5                          # DC
POPULATION_WEIGHTS = {'sex': 0.30, 'age': 0.15, 'training': 0.35, 'condition': 0.20}   # DC
TRAINING_ORDER = ('untrained', 'recreational', 'trained', 'elite')
POPULATION_GATE = 0.75
ACTIONABLE_MIN = 2.0
SEED_MIN = 1.0
IMPLAUSIBLE_SMD = 1.0
UPGRADE_CAP = 2.0
REPLICATION_STEP, REPLICATION_CAP = 0.5, 1.0
SHRINK = {4: 1.0, 3: 0.8, 2: 0.5, 1: 0.25}   # unchanged
INFLATE = {4: 1.0, 3: 1.5, 2: 2.5, 1: 4.0}   # unchanged
TAU_HET_FRACTION = 0.5                       # unchanged
SHRINK_Z = ((1.96, 0.25), (3.0, 0.5), (4.0, 0.75), (math.inf, 1.0))   # z below bound -> factor; DC
SHRINK_N = ((20, 0.3), (50, 0.5), (100, 0.75), (math.inf, 1.0))      # n below bound -> factor; DC
SHRINK_N_UNKNOWN = 0.3                       # DC
```

## Order of evaluation

1. Veto (retraction, missing design). Return at once.
2. Base certainty from the design.
3. If the design is in `MECHANISTIC_ONLY`: `c = base`, no domains, no upgrades, no replication, gate `watchlist`, flag `mechanistic_only`. Return.
4. Domains D1 to D6, each summed then floored at its own floor.
5. Upgrades (capped at +2 total).
6. Replication.
7. `c = min(cap, max(0, base + Σ adjustments))`, where `cap` is the smallest applicable cap (`MAX_CERTAINTY` when none applies). Only the binding cap adds a flag `cap:<name>`, and only when it is lower than the unclamped value. Ties go to the first name in `CAPS` order.
8. Gate.

The clamp runs once, after replication. A study at `base + domains = -0.5` that gains +1 replication ends at 0.5, not 1.0.

## Veto and caps

| Rule | Effect |
|---|---|
| `update_to` contains retraction, withdrawal or removal, from `publisher` or `retraction-watch` (dedupe identical entries) | `c = 0`, `vetoed=True`, gate `discard`, flag `retracted` |
| design missing or `review_opinion` | `c = 0`, gate `discard`, flag `design_missing` for a missing one |
| expression of concern or partial retraction | cap `concern` 1.0, never `actionable` |
| `includes_retracted=True` (meta-analysis pooling a retracted DOI) | cap 1.0 (DC) |
| `n_total < 10` | cap `tiny_n` 1.0 (DC) |
| `mechanistic_human` | cap 1.5, never `actionable` (DC) |
| `peer_reviewed is not True` | cap `preprint` 3.0, never `actionable` |
| `funding_industry_product is True` | cap 3.0 (DC) |
| upgradable design that received any upgrade | cap `upgraded_observational` 3.0 (DC) |

Crossref deposits Retraction Watch data in `update-to` with `source` `publisher` or `retraction-watch`, and the same retraction can appear twice (https://crossref.org/documentation/retrieve-metadata/retraction-watch/). Coverage of expressions of concern and corrections is weaker than for retractions.

## Domains

Unknown handling is part of the rule. "unknown" means the value is null after quote verification.

| Domain, floor | Applies to | Check | Δ | Flag |
|---|---|---|---|---|
| D1 bias, -2 | rct_parallel, rct_crossover | attrition > 20% / unknown | -0.5 / -0.25 | `high_attrition` / `attrition_unknown` |
| | rct_* | outcome assessor not blinded / unknown | -0.5 / -0.25 | `unblinded_outcome` / `blinding_unknown` |
| | rct_*, meta_rct, cohort, case_control | `outcome_switched is True` | -1 | `outcome_switched` |
| | rct_*, meta_rct (PROSPERO) | no `registration_id` and `preregistered` not True | -0.5 | `unregistered` |
| | cohort_prospective, case_control | attrition as above | -0.5 / -0.25 | as above |
| D2 imprecision, -2 | all non-mechanistic | CI crosses 0 and both ±SWC | -2 | `ci_crosses_both` |
| | | CI crosses 0 only | -1 | `ci_crosses_null` |
| | | CI clear of 0, one bound inside ±SWC | -0.5 | `ci_near_swc` |
| | | no usable CI | -1 | `no_ci` |
| | | SWC unknown: CI crosses 0 / does not | -1 / -0.5 | `swc_unknown` |
| | SMD and MD only | `n_total < OIS` | -1 | `below_ois` |
| | SMD and MD only | `n_total` unknown | -1 (DC) | `n_unknown` |
| D3 inconsistency, -1.5 | meta_rct | I² ≤ 50 / 50 < I² ≤ 75 / > 75 / unknown | 0 / -0.5 / -1 / -0.5 | `moderate_heterogeneity` / `high_heterogeneity` / `i2_unknown` |
| | meta_rct | `k_studies < 3` (unknown: 0) | -0.5 (DC) | `few_studies` |
| | meta_rct | PI crosses 0 while the CI does not | -0.5 | `pi_crosses_null` |
| D4 indirectness, -2 | all non-mechanistic | `population_match < 0.5` / `< 0.75` | -1 / -0.5 | `population_mismatch` / `population_partial` |
| | | `outcome/is_surrogate is True` | -0.5 | `surrogate_outcome` |
| | | `outcome/is_acute is False` and `duration_weeks < 4` (either unknown: 0) | -0.5 (DC) | `short_duration` |
| D5 publication bias, -1 | meta_rct | `funnel_asymmetry is True` and `k_studies ≥ 10` | -1 | `funnel_asymmetry` |
| | meta_rct | `k_studies < 10` and `median_study_n < 20` (either unknown: 0) | -0.5 | `small_studies` |
| D6 trust, -2 | all non-mechanistic | `peer_reviewed is not True` | -0.5 | `preprint` |
| | | `funding_industry_product is True` | -0.5 | `product_linked_funding` |
| | | SMD with \|est\| > 1 | -1 | `implausible_smd` |
| | | GRIM failure in `reported_means` | -1 | `grim_fail` |

D2 details:
- The CI is on the comparison scale: log for RR, as `_interval` does today. SWC for RR is `|log(1 - x)|`, so a 10% relative change is `|log 0.9| = 0.1054`. `swc_paper_units` must be positive.
- OIS: `ceil(OIS_CONSTANT / d²)` with `d = |est|` for SMD, `|est| / sd_pop` for MD with a known `sd_pop`, else `OIS_DEFAULT_D`; then `d = max(d, OIS_MIN_D)`. For `rct_crossover` multiply by `(1 - CROSSOVER_RHO) / 2 = 0.25` before comparing. Skipped for RR. Funnel-asymmetry tests need ≥ 10 studies and a null test does not exclude bias (Cochrane Handbook v5.1 §10.4.3.1, archived edition).
- The OIS check is in addition to the CI check, and D2 floors at -2 as a whole.

D3 bands follow the overlapping Cochrane ranges (0-40, 30-60, 50-90, 75-100), which the Handbook warns against using as rigid cutoffs (v5.1 §9.5.2, archived edition). Cut points 50 and 75 are kept from thal-1.

### Population match

```
pop = .30·sex + .15·age + .35·training + .20·condition
sex       = 1 if paper sex == owner sex or paper sex == 'mixed', else 0
age       = 1 if |age_mean - owner.age| <= 10, else 0
training  = 1 at ordinal distance 0, 0.5 at distance 1, 0 at distance >= 2 (TRAINING_ORDER)
condition = 1 if paper says 'healthy' and the owner has no conditions, or the paper condition is one of the owner's
```

Unknown values earn 0, as in thal-1. A sex mismatch alone now scores 0.70, below the 0.75 gate.

## Upgrades and replication

- Only for `UPGRADABLE` designs, and only when D1 > -1 and D2 == 0 (thal-1 needed D1 > -1 only).
- Large effect: RR ≥ 2 or ≤ 0.5, or standardized \|d\| ≥ 0.8: +1. Very large: RR ≥ 5 or ≤ 0.2: +2 instead. GRADE 9 and the CDC ACIP handbook ch. 9 (https://www.cdc.gov/acip-grade-handbook/hcp/chapter-9-domains-increasing-ones-certainty-in-the-evidence/index.html) give these thresholds; ACIP asks for consistency across at least 2 studies, which this rubric does not check.
- Dose-response: +1.
- Upgrades sum to at most +2, and any upgrade triggers cap `upgraded_observational` 3.0.
- Replication: +0.5 per independent same-direction replication, at most +1. Not for `MECHANISTIC_ONLY`.

## Gate

Checked in this order:

| Gate | Condition |
|---|---|
| `discard` | vetoed, or `c <= 0` |
| `watchlist` | design in `MECHANISTIC_ONLY`, or `c < 1` |
| `actionable` | all of: `c >= 2`; backed (`meta_rct` with `k_studies >= 2`, or `independent_rcts >= 2`); `pop >= 0.75`; `peer_reviewed is True`; no expression of concern; design is not `mechanistic_human` |
| `n_of_1_only` | everything else |

A `meta_rct` with unknown `k_studies` is not backed (DC).

## Prior (`to_prior`)

Returns `None` when vetoed, when the gate is `watchlist` or `discard`, when `c < SEED_MIN`, for `MECHANISTIC_ONLY`, or when the effect is underspecified (unchanged).

```
level   = min(4, floor(c))
SE      = (hi - lo) / 3.92                 # comparison scale; × sd_pop for SMD, as _effect_raw does
z       = |est| / SE                       # comparison scale, before scaling
S_Z     = first factor in SHRINK_Z whose bound z is below
S_N     = first factor in SHRINK_N whose bound n_total is below; SHRINK_N_UNKNOWN if n_total unknown
s       = min(SHRINK[level], S_Z, S_N)
mu0     = s · delta
tau_het = 0.5 · |delta|; with a prediction interval: max(0.5 · |delta|, (pi_hi - pi_lo) / 3.92 · scale)
sd0     = sqrt((INFLATE[level] · SE)² + tau_het²)
```

Bands: z < 1.96 → 0.25; 1.96 ≤ z < 3 → 0.5; 3 ≤ z < 4 → 0.75; z ≥ 4 → 1.0. n < 20 → 0.3; 20 ≤ n < 50 → 0.5; 50 ≤ n < 100 → 0.75; n ≥ 100 → 1.0. Anchors: van Zwet's 1/1.7 ≈ 0.59 at z ≈ 2, Murphy's 0.25 at n ≈ 17, OSC 2015's mean r falling from 0.403 to 0.197 (https://dspace.library.uu.nl/bitstream/1874/329878/1/aac4716.full.pdf; psychology, not sport). The prediction interval uses the same scale as the CI (log for RR).

## Test cases

Fixture owner for every case: `OwnerProfile(sex='female', age=30, training_status='trained', conditions=(), swc=...)`. `swc_paper_units = 0.2` for SMD, `0.1054` (`|log 0.9|`) for RR. Unless a case says otherwise: `peer_reviewed=True`, `funding_industry_product` null, `outcome_switched` null, `update_to=[]`, `includes_retracted=False`, `replications=0`, `independent_rcts=0`, `outcome/is_surrogate` false, `is_acute` and `duration_weeks` null, `condition='healthy'`. "pop 1.0" means sex mixed, age within 10 years, trained, healthy.

Adjustments list non-zero entries in the order bias, imprecision, inconsistency, indirectness, publication_bias, trust, large_effect, dose_response, replication.

| # | Input | Adjustments | c | Gate | Flags to assert | Prior |
|---|---|---|---|---|---|---|
| 1 | meta_rct, k 12, n_total 900, I² 30, registration_id set, funnel_asymmetry false, mixed, age 28, trained; SMD 0.35 [0.22, 0.48] | none (OIS 257 ≤ 900) | 4.0 | actionable | none | s = 1.0 (z = 5.28) |
| 2 | rct_parallel, n 16, male, age 22, untrained, unregistered, attrition 10, blinding null; SMD 1.4 [0.3, 2.5] | bias -0.75, imprecision -1 (OIS 17 > 16), indirectness -1 (pop 0.35), trust -1 | 0.25 | watchlist | `blinding_unknown`, `unregistered`, `below_ois`, `population_mismatch`, `implausible_smd` | None |
| 3 | animal, SMD 2.0 [1.0, 3.0], replications 2 | none | 0.5 | watchlist | `mechanistic_only` | None |
| 4 | case 1 + `update_to=[{'type': 'retraction', 'source': 'retraction-watch'}]` | none | 0.0 | discard | `retracted`, `vetoed=True` | None |
| 5 | case 1 + `update_to=[{'type': 'expression_of_concern'}]` | none | 1.0 | n_of_1_only | `expression_of_concern`, `cap:concern` | s = 0.25 (level 1) |
| 6 | meta_rct, k 6, n_total 84, I² 85, PI [-0.4, 1.6], median_study_n 14, registered, pop 1.0; SMD 0.6 [0.3, 0.9] | imprecision -1 (OIS 88 > 84), inconsistency -1.5, publication_bias -0.5 | 1.0 | n_of_1_only | `below_ois`, `high_heterogeneity`, `pi_crosses_null`, `small_studies` | s = 0.25 (SHRINK[1]; z = 3.92 → 0.75; n 84 → 0.75); τ_het = max(0.3, 2.0/3.92 = 0.51) × sd_pop |
| 7 | rct_crossover, n 24, mixed, age 27, trained, registered, blinded, attrition 4, product-linked funding; SMD 0.45 [0.10, 0.80] | imprecision -1.5 (CI -0.5; OIS 156 × 0.25 = 39 > 24, -1), trust -0.5 | 2.0 | n_of_1_only | `ci_near_swc`, `below_ois`, `product_linked_funding` | s = 0.5 (z = 2.52 → 0.5; n 24 → 0.5) |
| 8 | case 7 with `independent_rcts=2` | as case 7 | 2.0 | actionable | as case 7 | s = 0.5 |
| 9 | rct_parallel, n 400, registered, blinded, attrition 5, pop 1.0, `peer_reviewed=False`, `independent_rcts=2`; SMD 0.5 [0.30, 0.70] | trust -0.5 | 3.0 (3.5 capped) | n_of_1_only | `preprint`, `cap:preprint` | s = 0.8 (level 3; z = 4.9 → 1.0; n 400 → 1.0) |
| 10 | cohort_prospective, n 5000, attrition 10, pop 1.0, dose_response true; RR 0.45 [0.35, 0.58] | large_effect +1, dose_response +1 | 3.0 (4.0 capped) | n_of_1_only | `cap:upgraded_observational` | s = 0.8 |
| 11 | mechanistic_human, n 12, is_surrogate true, pop 1.0, replications 2; SMD 0.9 [0.3, 1.5] | imprecision -1 (OIS 39 > 12), indirectness -0.5, replication +1 | 0.5 | watchlist | `below_ois`, `surrogate_outcome` | None |
| 12 | rct_parallel, `independent_rcts=2`, n 200, male, age 30, trained, healthy, registered, blinded, attrition 5; SMD 0.5 [0.22, 0.78] | indirectness -0.5 (pop 0.70) | 3.5 | n_of_1_only | `population_partial` | s = 0.75 (level 3 → 0.8; z = 3.5 → 0.75; n 200 → 1.0) |

Extra unit tests for the new pieces:

- `population_match`: male-only study for a female owner, everything else matching → 0.70. Recreational study for a trained owner, everything else matching → 0.825. Untrained study for an elite owner → training term 0.
- OIS: SMD 0.1 uses d = 0.2 and OIS 785. MD 5 with `sd_pop` 10 gives d 0.5, OIS 126. RR skips OIS. `n_total` null with SMD gives `n_unknown`, -1.
- D5: `funnel_asymmetry=True` with k 8 has no effect (the test is invalid below 10 studies); with k 12 gives -1.
- Caps: a paper with both `tiny_n` and `preprint` ends at 1.0 and flags only `cap:tiny_n`.
- Clamp order: base 1, domains -1.5, replication +1 → 0.5, not 1.0.
- `includes_retracted=True` on case 1 → 1.0, `cap:includes_retracted`, n_of_1_only.
- A thal-1 `paper.json` without the new fields scores without raising.

## thal-1 tests that will change

Where the same input gives a different result, update the expected values rather than keeping both:

| Input | thal-1 | thal-2 | Cause |
|---|---|---|---|
| case 2 | 1.25, n_of_1_only | 0.25, watchlist | OIS penalty, new weights |
| animal, population extracted as matching (pop 1.0), SMD 0.8 [0.5, 1.1], replications 2 | 1.5, n_of_1_only, seeds a prior | 0.5, watchlist, no prior | `MECHANISTIC_ONLY` |
| case 9 | 3.5, actionable | 3.0, n_of_1_only | preprint cap and gate |
| case 10 | 4.0, n_of_1_only | 3.0, n_of_1_only | upgrade cap |
| case 12 | 4.0, actionable | 3.5, n_of_1_only | sex weight .30 |
| any `population` adjustment | named `population` | folded into `indirectness` | domain merge |
| `meta_rct` without `k_studies` | backed | not backed | k ≥ 2 rule |

## Source caveats

- The Cochrane I² and funnel pages cited are the archived v5.1 Handbook.
- GRADE 6 (2011) is partly superseded by GRADE 34 (2022) and Core GRADE 2 (BMJ 2025;389:e081904). The exact Core GRADE wording on rating down 2-3 levels for very small samples is UNVERIFIED.
- OSC 2015 covers psychology. Lundh 2017 (industry funding RR 1.27, Cochrane MR000033) covers drug and device trials, not supplements. The preprint evidence (Zeraatkar 2022; Davidson 2023, ratio of odds ratios 0.88 [0.71, 1.09]) covers COVID trials only.
- van Zwet's median exaggeration ratio of 2.90 comes from the preprint and is UNVERIFIED in the final version; the rubric uses the 1.7 figure.
- Sport-science context for the n thresholds: median n 19 in J Sports Sci, 12 of 120 papers with an a-priori power analysis (Abt 2020, https://pubmed.ncbi.nlm.nih.gov/32558628/); z-curve average power 53% over 89 studies (Mesquida 2023, https://sportrxiv.org/index.php/server/preprint/view/267).

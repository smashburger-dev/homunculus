# Trackers for homunculus

Status: research synthesis, 2026-11-20. Draft for `docs/TRACKERS.md`. Claims marked UNVERIFIED were not confirmed against a primary source. Where two research reports disagreed, the one citing the vendor's own page wins, and the conflict is noted.

homunculus today ingests Apple Health through Health Auto Export (HAE) into Brainstem (`ingest.py`). `store.SOURCES` is `('watch','phone','garmin','other')`, `metrics.py` picks one source per daily key by priority (`watch > garmin > phone`), and keeps SDNN (`hrv_sdnn_ln`) and RMSSD (`hrv_rmssd_ln`) as separate series. This file covers what it takes to add other trackers, and which ones are worth a direct poller.

## Decisions

| Source | Path | Adapter in `homunculus/trackers/` | Phase |
|---|---|---|---|
| Apple Watch, iPhone | HAE pushes to Brainstem | none, `ingest.py` stays the hub | live |
| Garmin | Connect Personal API, pull | `garmin.py` | 1 |
| WHOOP | Developer API v2, pull | `whoop.py` | 1 (only if worn) |
| Google / Fitbit (incl. Fitbit Air, Pixel Watch) | Google Health API v4, pull | `google.py` | 2, after a manual auth test |
| Withings | Public API, pull | `withings.py` | 2 |
| Polar | AccessLink v3, pull | `polar.py` | 2 |
| Oura | Apple Health via HAE only | none, blocked by the API agreement | n/a |
| Android Health Connect | HC Webhook app pushes to Brainstem | none, a new Brainstem route | only if an Android phone joins |
| Terra, Junction, Rook, Thryve, Spike | not used | none | n/a |

Every adapter polls. None uses webhooks, for two reasons: every vendor webhook needs a public HTTPS endpoint, and the architecture allows `tailscale serve` only, never `funnel`. Webhooks also lose events (WHOOP, Google, Oura and Withings all tell you to reconcile by polling anyway), so the poll is needed regardless.

## Rules that apply to every source

- Sources are never averaged. One source per daily key, by priority. Vendor scores (recovery, strain, Body Battery, readiness, sleep score) are stored under vendor-prefixed keys such as `whoop.recovery` and never feed readiness, guardrails or the red-flag rules.
- HRV method matters more than vendor. Apple's `heart_rate_variability` is SDNN. Garmin, WHOOP, Google, Polar and Withings `rmssd` are RMSSD, each over a different window. They never share a baseline. A change of the HRV or RHR source restarts that baseline.
- One path per source. If Garmin is polled directly, Garmin data that also arrives through Apple Health must be dropped at ingest, or workouts and steps count twice.
- Re-poll a trailing window (3 days) every run and replace that window per source. Garmin, WHOOP and Google have no "updated since" filter, records get rescored after late syncs, and Google may change timestamps when it re-buckets.
- A failed poll leaves values `None` and records the failure in `data/freshness.json`. Nothing is guessed or carried forward.
- Tokens and API keys never go into git. Google and WHOOP require encryption at rest for tokens and data, so the box needs FileVault (or LUKS) and tokens go to the macOS Keychain or a `0600` file outside every git work tree.
- LLMs see code-computed aggregates from the weekly packet, never raw vendor payloads. That is already the architecture's rule, and it is what keeps WHOOP, Garmin and Google terms workable.

---

## Apple Health via Health Auto Export (hub)

Status: live in homunculus. Official REST automation docs, "Last updated: October 31, 2026": https://help.healthyapps.dev/en/health-auto-export/automations/rest-api/

- Access: HAE Premium ($1.99/month, $6.99/year or $24.99 lifetime; prices vary by country): https://apps.apple.com/app/id1115567069
- Auth: custom HTTP headers set in the automation. Brainstem checks `Authorization: Bearer` or `X-Token` with `hmac.compare_digest`. HAE also sends `automation-name`, `automation-id`, `automation-aggregation`, `automation-period` and `session-id`; log `session-id` to group batched requests.
- Delivery: push. Sync frequency is "a desired background cadence, not a guaranteed schedule". iOS reads health data only while the phone is unlocked, with Background App Refresh on and Low Power Mode off.
- Recommended automations:
  1. Health metrics, JSON v2, Since Last Sync, Summarize off, Batch on, every 1 h.
  2. Workouts with "Include Workout Metrics" on, Since Last Sync, every 1 h.
  3. Catch-up over Previous 7 Days, once a day.
- Summarize must stay off. `metrics.py` needs at least 2 SDNN readings inside the sleep window, and buckets would also merge sources. Whether v2 carries a per-reading `source` string with Summarize off is UNVERIFIED; check the JSON wiki (https://github.com/Lybron/health-auto-export/wiki/API-Export---JSON-Format) before trusting `HEALTH_SOURCE_MAP`.
- Freshness: overnight HRV and sleep arrive only after the first unlock in the morning. The 07:00 coach run must check `sleep_nights` for today's wake date. If it is missing, retry until about 09:00, send the templated "open the phone" note, then fall back to the last ratified plan.
- Other vendors that write to Apple Health end up here too, under their own source names. The table below says what each writes. All of it is third-party reporting and UNVERIFIED unless a vendor page is cited.

| Vendor app writes to Apple Health | Sleep stages | HR | HRV | RHR | SpO2 | Resp. | Temp | VO2max | Workouts | Weight |
|---|---|---|---|---|---|---|---|---|---|---|
| Garmin Connect (support page, 403, from snippet) | yes | yes | no (UNVERIFIED) | not listed | not listed | no | no | no | yes, no GPS | yes |
| WHOOP (dcrainmaker, sensai.fit) | asleep/awake only | in workouts | no | once per wake-up | 4.0+ | 4.0+ | no | no | yes, with HR | no |
| Oura (vendor page) | yes | 1-min | no | no | no | yes | no | no | yes | yes |
| Google Health iOS 5.05 (9to5mac, 2026-08-03) | yes | yes | limited (UNVERIFIED) | ? | ? | ? | ? | ? | yes | ? |
| Polar Flow (vendor page, about 6 years old) | duration only | workout avg | no | no | no | no | no | no | yes | yes |
| Withings (new accounts need Withings+, Terra blog) | ? | ? | ? | ? | ? | ? | ? | ? | ? | yes |

Sources: https://support.garmin.com/en-GB/?faq=lK5FPB9iPF5PXFkIpFlFPA, https://dcrainmaker.com/2022/04/exporting-health-actually.html, https://www.sensai.fit/blog/wearable-apple-health-integration-what-syncs-oura-whoop-garmin, https://support.ouraring.com/hc/en-us/articles/360025438734-Apple-Health-Integration, https://9to5mac.com/2026/08/03/google-health-adds-two-way-apple-health-syncing-on-iphone/, https://support.polar.com/en/support/connecting_polar_flow_with_apple_health, https://tryterra.co/blog/withings-apple-health-sync-subscription-new-accounts

The pattern is clear: the hub path loses HRV for every third-party device. Whoever wants Garmin, WHOOP, Google or Polar RMSSD needs that vendor's API.

---

## Garmin

Status: Connect Personal API, live since 2026-10-21, OpenAPI 3.0.1 "version 1.0.0". Base URL `https://personal.connectapi.garmin.com`. Spec extracted from the Swagger page bundle. Official Python client: https://github.com/garmin/connect-personal-api-python (last commit 2026-11-19).

Sources: https://developer.garmin.com/gc-developer-program/overview/, https://developer.garmin.com/gc-developer-program/personal/personal-api, https://developer.garmin.com/gc-developer-program/personal/swagger-docs/, https://developer.garmin.com/gc-developer-program/personal/personal-faq/

- Access: self-serve, no application. In Connect Web open https://connect.garmin.com/app/settings/personalApiToken, Create Key, accept the T&C, pick an expiry of 30, 90, 180 or 365 days. The key is shown once. Garmin emails about a week before expiry. Deleting the key revokes every token minted from it. The FAQ says it is "not intended for commercial applications serving multiple end users", which fits one owner. Price: none published; press calls it free.
- Auth: no OAuth. `POST /personalapitoken-service/personal-api/personal-api-key/token` with `Authorization: ApiKey <key>` returns a JWT. The JWT lasts 24 h or until the key expires, whichever comes first. No refresh token: mint a new JWT. Cache it and renew when less than 1 h remains, reading `exp` from the JWT payload. Scopes are not documented (a reader comment lists `PERSONAL_API_*_READ`, UNVERIFIED).
- Delivery: pull only. Rate limits are not documented (UNVERIFIED), and Garmin's own client has no 429 handling. Forum reports describe 48 h lockouts after aggressive logins on the old scraped endpoints, so back off hard on 429.
- Recommended cadence: JWT about every 23 h. Activities list every 30 min, newest first, stop at the last known id. Sleep, HRV and daily summary for yesterday and today hourly 06:00-12:00, then every 3 h until 22:00. Re-pull D-3 to D0 nightly. Backfill in 28-31-day chunks, serially, with jitter.

Endpoints used (all GET, dates are the user's local `YYYY-MM-DD`):

| Data | Endpoint | Max range |
|---|---|---|
| Sleep | `/sleep-service/sleep/dailySleepSummariesByDate?startDate&endDate` | 31 d |
| HRV night + 5-min readings | `/hrv-service/hrv/{date}` | 1 d |
| Daily summary | `/usersummary-service/v2/dailysummaries?startDate&endDate` | 1 year |
| HR day series + RHR | `/wellness-service/wellness/dailyHeartRate?date=` | 1 d |
| SpO2 | `/wellness-service/wellness/daily/spo2acclimation/{date}` | 1 d |
| VO2max | `/metrics-service/metrics/maxmet/daily/{start}/{end}` | not stated |
| Weight | `/weight-service/weight/all/{start}/{end}` | 364 d |
| Activities | `/activitylist-service/activities/search/activities?startDate&endDate&start&limit` | offset paging, default limit 99 |
| Activity FIT | `/download-service/files/activity/{id}` | ZIP with FIT |

Timestamps come in three formats. Sleep uses epoch ms (`*TimestampGMT`, `*TimestampLocal`). HRV and SpO2 use ISO-8601 without offset, paired GMT and Local ("without GMT offset or 'Z' indicator!" in the spec). Activities use `"yyyy-MM-dd HH:mm:ss"` in GMT and local. Derive the offset as Local minus GMT and write ISO-8601 with that offset, as HAE does.

| Canonical key | Garmin field | Store row | Note |
|---|---|---|---|
| `hrv_rmssd_ln` | `hrvReadings[].hrvValue` (5-min, integer ms) | `Sample('heart_rate_variability_rmssd', 'ms', ts=readingTimeLocal+offset, 'qty', v, 'garmin')` | RMSSD over 5-min windows during sleep (Garmin health-science page). Blogs saying SDRR are wrong. |
| (vendor) | `hrvSummary.lastNightAvg`, `weeklyAvg`, `baseline.*`, `status` | `garmin.hrv_last_night`, `garmin.hrv_weekly_avg`, `garmin.hrv_status_code` | not canonical |
| `rhr` | `heartRate.restingValue` | `Sample('resting_heart_rate', 'count/min', ts=<date>T12:00+offset, 'qty', v, 'garmin')` | Spec: holds "the absolute minimum if no resting value". Garmin's own fallback, so mark `quality='sparse'` when `DailyHeartRateDTO.restingHeartRate` is absent. |
| `tst_h`, sleep | `sleepTimeSeconds`, `deep/light/rem/awakeSleepSeconds` | `SleepNight(wake_date=calendarDate, core_h=light, ..., source='garmin')` | No hypnogram, so no `SleepSegment`. light ≈ Apple core (UNVERIFIED). `calendarDate` as wake day is UNVERIFIED, test it. Naps excluded already. |
| `load_srpe`, `load_edwards` | activity summary + FIT `record` HR | `Workout(id='garmin:<activityId>', hr_series from FIT, ...)` | `active_kcal = calories - bmrCalories` (UNVERIFIED). `is_indoor` from type key (`indoor_*`, `treadmill`). Drop lat/lon, `locationName`, `owner*`. |
| `active_kcal` | `calories.burnedActive` | `Sample('active_energy', 'kcal', <date>T12:00, 'qty', v, 'garmin')` | one daily total |
| `steps` | `/stats/steps/daily` | `Sample('step_count', 'count', <date>T12:00, 'qty', v, 'garmin')` | |
| `vo2max` | `generic.vo2MaxPreciseValue` | `Sample('vo2_max', 'ml/kg/min', ...)` | estimate, slope only |
| `weight_kg` | `weight` | `Sample('weight_body_mass', 'kg', ...)` | documented kg; the old internal API returned grams, so treat values > 1000 as grams |
| `spo2_min` | `spO2SingleValues` | `Sample('blood_oxygen_saturation', '%', ...)` | readings only; `averageSpO2Value` goes to `garmin.spo2_sleep_avg` |
| `resp_rate` | sleep `averageRespirationValue` | `Sample('respiratory_rate', 'count/min', ts=sleep end, ...)` | the all-day respiration average is a different context, skip it |
| `wrist_temp_c` | none | none | not in the Personal API |
| (vendor) | Body Battery, stress, `sleepScores.overall`, training effect, training load, fitness age, `sleepNeed` | `garmin.body_battery_max`, `garmin.stress_avg`, `garmin.sleep_score`, ... | never canonical |

Gotchas:
- The Personal T&C text appears only at key creation and could not be fetched. Whether it allows sending derived data to Anthropic or Google is UNVERIFIED. Read it at key creation and record its version in the ledger. Business terms reportedly ban sharing with services Garmin has not approved and ban AI training (https://the5krunner.com/2026/10/14/garmin-api-intervals-third-party-apps/). Mitigation: LLMs see only code aggregates.
- Brand guidelines want attribution where Garmin data is shown: https://developer.garmin.com/brand-guidelines/overview/. The dashboard labels Garmin-sourced values.
- FIT files delivered by the API may lack VO2max, recovery time and performance condition (the5krunner, Runalyze, UNVERIFIED for personal keys).
- The API is weeks old, endpoints carry internal service names, and Garmin paused its program for most of 2026. Validate responses and fail closed.
- Do not use `python-garminconnect` or `garth`. `garth` was deprecated in June 2026 (https://github.com/matin/garth). Both log in with username and password, and Cloudflare reportedly blocks them.
- Wrist RMSSD is biased and single nights are noisy (https://labfront.com/article/garmin-enhanced-bbi-hrv-accuracy-validation). Garmin's HRV status needs about 3 weeks of warm-up.

---

## WHOOP

Status: Developer API v2, GA since 2025-07-01, base `https://api.prod.whoop.com/developer`. v1 is unsupported and its webhooks were removed on 2025-11-01. On 2026-09-23 cycles gained `step_count`. https://developer.whoop.com/docs/api-changelog

- Access: free while you own a WHOOP and have a membership. Each app starts on the Sandbox tier, capped at 10 members, with no approval. One person fits. No official "personal" tier exists, and personal use is not explicitly addressed (UNVERIFIED as policy). Create the app manually in the Developer Dashboard. https://developer.whoop.com/docs/developing/app-approval, https://developer.whoop.com/docs/developing/support
- Auth: OAuth2 authorization code. Authorize `https://api.prod.whoop.com/oauth/oauth2/auth`, token `https://api.prod.whoop.com/oauth/oauth2/token`. Scopes `read:recovery read:cycles read:workout read:sleep read:profile read:body_measurement offline`. `state` is required and must be exactly 8 characters. PKCE is not documented, so use a confidential client with the secret on the box. Whether `http://localhost` redirects are accepted is UNVERIFIED; the docs show HTTPS and custom schemes. Access tokens last 3600 s in the example. Refresh tokens rotate, and a refresh invalidates old access tokens, so two concurrent refreshes lock you out: serialize them and persist the new token before use. `DELETE /v2/user/access` revokes. https://developer.whoop.com/docs/developing/oauth
- Delivery: poll. Rate limits 100 req/min and 10,000 req/day with `X-RateLimit-*` headers, 429 on excess (official page: https://developer.whoop.com/docs/developing/rate-limiting; the hub report's "third party, UNVERIFIED" is superseded). Paging: `limit` max 25, `start` inclusive, `end` exclusive, `nextToken`, sorted by start descending. No `updated_since`.
- Recommended cadence: every 30 min 05:00-10:00 for sleep and recovery, every 2 h otherwise, over a sliding window `start = now - 3 days` for cycle, recovery, sleep and workout. That is 4 to 8 calls per run. Body measurements once a day. Backfill by omitting `start`, about 15 calls per year per collection; no documented history cap (UNVERIFIED).

| Canonical key | WHOOP field | Store row | Note |
|---|---|---|---|
| `hrv_rmssd_ln` | `recovery.score.hrv_rmssd_milli` (one nightly value) | `Sample('heart_rate_variability_rmssd', 'ms', ts=sleep midpoint, 'nightly', v, 'whoop')` | needs the `nightly` field path in `metrics.py` (see TRACKER_CONTRACT.md), since `min_n=2` would drop it |
| `rhr` | `recovery.score.resting_heart_rate` | `Sample('resting_heart_rate', 'count/min', ts=sleep end, 'qty', v, 'whoop')` | measured in sleep, so lower than Apple's daytime RHR |
| `spo2_min` | none | none | `spo2_percentage` is a nightly mean, stored as `whoop.spo2_sleep_avg` |
| `wrist_temp_c` | `recovery.score.skin_temp_celsius` | `Sample('skin_temperature', '°C', ts=sleep end, 'qty', v, 'whoop')` | absolute; Apple's metric is a different sensor. Per-source baseline. |
| `resp_rate` | `sleep.score.respiratory_rate` | `Sample('respiratory_rate', 'count/min', ts=sleep end, ...)` | unit not stated, presumably breaths/min (UNVERIFIED) |
| `tst_h`, sleep | `sleep.score.stage_summary.*_milli` | `SleepNight(wake_date=local date of end, deep_h=slow_wave, core_h=light, rem_h, awake_h, asleep_h=light+sws+rem, total_h=in_bed, source='whoop')` | skip `nap=true`; no hypnogram; light ≈ core (UNVERIFIED) |
| loads | `workout` | `Workout(id='whoop:<uuid>', name=sport_name, hr_avg, hr_max, hr_series=(), active_kcal=None, ...)` | WHOOP kJ is probably total energy (UNVERIFIED), so no `active_kcal`. Zone durations go to `raw_json`. No HR series means `load_edwards` is `None` for WHOOP-only days. |
| `steps` | `cycle.step_count` | `Sample('step_count', 'count', ts=cycle end, ...)` | a cycle runs sleep onset to sleep onset, so date it by its end |
| `vo2max` | none | none | |
| `weight_kg` | `body.weight_kilogram` | none | a profile value, likely user-entered; skip |
| (vendor) | `recovery_score`, cycle `strain`, `kilojoule`, `sleep_performance_percentage`, `sleep_consistency_percentage`, `sleep_efficiency_percentage`, `user_calibrating` | `whoop.recovery`, `whoop.day_strain`, `whoop.cycle_kcal_total`, `whoop.sleep_performance`, ... | never canonical |

Only ingest `score` when `score_state == 'SCORED'`. `PENDING_SCORE` and `UNSCORABLE` are from memory (UNVERIFIED); treat anything else as "not yet".

Gotchas, ToS from https://developer.whoop.com/api-terms-of-use/ (effective 2026-10-06), paraphrased:
- §4.2(2) bars building databases or "permanent copies" of WHOOP data, unless the data owner expressly authorizes it. One owner storing their own data is arguably covered; that is a reading, not WHOOP's statement. Store canonical rows, not raw payloads, keep provenance, and support purge on revocation (§7.1).
- §4.2(3) bars using WHOOP data to train or improve any AI model. Inference is not named. Sending data to an LLM API is still a third-party transfer (§4.2(1) needs opt-in consent). Only code aggregates reach the LLMs, and the providers are no-training.
- §3.1(10)-(11): no medical-device features or medical advice. Matches the intended purpose.
- §3.2(1): never embed credentials in open-source code. The client secret lives in `.env` on the box.
- §2.4: encrypt in transit and at rest; report incidents within 48 h.
- §5: no public statements about WHOOP or implied endorsement without approval. README says "optional WHOOP API adapter", no logo.
- The first days are calibration (`user_calibrating`), records get rescored after sleep edits, naps have no recovery.
- Avoid libraries that log in with your WHOOP password (e.g. `ald0405/whoop-data`); §4.2(2) bars scraping.

---

## Google / Fitbit

Status: the legacy Fitbit Web API lost support on 2026-09-30 and was scheduled to turn off on 2026-11-02 with `410 Gone` (that it happened is UNVERIFIED). The successor is the Google Health API v4, GA, REST at `https://health.googleapis.com/v4`. `v4beta` is a preview without SLA. https://developers.google.com/health/about, https://developers.google.com/health/release-notes

Fitbit Air exists: a screenless band, announced 2026-05-08, $99.99, with 24/7 HR, SpO2, RHR, HRV and sleep stages, listed as a compatible device in the data-type docs (https://blog.google/intl/en-au/products/devices-services/introducing-the-all-new-fitbit-air/, https://developers.google.com/health/data-types). The hub report's "no official source" is superseded. The Fitbit app is now "Google Health".

- Access: a Google Cloud project, enable `health.googleapis.com`, a "Web Server" OAuth client, add the scopes on Data Access (https://developers.google.com/health/setup). The account must be linked in the Google Health app, else `400 FAILED_PRECONDITION / ACCOUNT_NOT_LINKED`. All Health scopes are Restricted. Set the consent screen user type to "Personal" (only the creating account can authorize, no verification, no 7-day test-token expiry, irreversible; https://support.google.com/cloud/answer/15549945). Whether restricted Health scopes can be granted in Personal mode is UNVERIFIED: test it first, before writing code. The fallback, External + Testing, kills refresh tokens every 7 days. No API fee is documented (UNVERIFIED), and whether access needs Premium is not stated.
- Auth: Google OAuth 2.0 authorization code, `https://accounts.google.com/o/oauth2/v2/auth` with `access_type=offline` and `prompt=consent`, token at `https://oauth2.googleapis.com/token`. Access token 3599 s. Refresh tokens do not rotate; they die when revoked, after about 6 months unused, or when the 100-token-per-client limit evicts the oldest. Handle `refresh_token_expires_in` if it appears. PKCE and a loopback redirect are standard for Google's "Desktop app" clients; whether the Health API accepts a Desktop client is UNVERIFIED, so the adapter supports both loopback and paste-the-code.
- Scopes, prefix `https://www.googleapis.com/auth/googlehealth`: `.sleep.readonly`, `.activity_and_fitness.readonly`, `.health_metrics_and_measurements.readonly`. Skip `.location.readonly`. https://developers.google.com/health/scopes
- Delivery: poll `GET /v4/users/me/dataTypes/{type}/dataPoints?filter=...&pageSize=&pageToken=`, and `dataPoints:reconcile` for sleep and exercise. Filters allow only `>=`, `<` and `AND`. Page size default 1440, max 10,000, but sleep and exercise max 25. No updated-since filter. Rate limit 300 req/min per user; back off on 429 and 504. Use `dataSourceFamily=users/me/dataSourceFamilies/google-wearables` so Apple data synced into Google does not come back. https://developers.google.com/health/endpoints, https://developers.google.com/health/rate-limits
- Recommended cadence: daily types for the last 3 days every 60 min. After wake-up, reconcile sleep every 30 min until `metadata.processed == true`. Exercise sessions every 60 min, then `heart-rate` samples over each new session interval. About 20 requests per day.

| Canonical key | Google data type and field | Store row | Note |
|---|---|---|---|
| `hrv_rmssd_ln` | `daily-heart-rate-variability.averageHeartRateVariabilityMilliseconds` | `Sample('heart_rate_variability_rmssd', 'ms', ts=main sleep midpoint, 'nightly', v, 'google')` | rMSSD during sleep; never `hrv_sdnn_ln` |
| `rhr` | `daily-resting-heart-rate.beatsPerMinute` | `Sample('resting_heart_rate', 'count/min', <date>T12:00+offset, 'qty', v, 'google')` | int64 arrives as a JSON string |
| sleep | `sleep` via reconcile, `metadata.mainSleep`, `stages[]` | `SleepSegment` per stage (LIGHT→core, DEEP→deep, REM→rem, AWAKE→awake, ASLEEP→asleep, RESTLESS→awake) and `SleepNight(wake_date=civil end date)` | skip `nap`; only rows with `processed == true` are final |
| loads | `exercise` + `heart-rate` samples | `Workout(id='google:<name>', active_kcal=caloriesKcal, hr_avg, hr_max and hr_series from heart-rate)` | no max HR in the summary; drop GPS |
| `active_kcal` | `dailyRollUp` of `active-energy-burned.kcal` | daily `Sample('active_energy', 'kcal', ...)` | |
| `steps` | `dailyRollUp` of `steps` | daily `Sample('step_count', ...)` | |
| `vo2max` | `daily-vo2-max.vo2Max` | `Sample('vo2_max', 'ml/kg/min', ...)` | keep `estimated`; flag quality |
| `weight_kg` | `weight.weightGrams` | `Sample('weight_body_mass', 'kg', v/1000)` | |
| `spo2_min` | minute `oxygen-saturation` | readings as `Sample('blood_oxygen_saturation', '%', ...)` | sparse after filtering. `daily-oxygen-saturation.averagePercentage` goes to `google.spo2_sleep_avg`. `lowerBoundPercentage` is a confidence bound, not a minimum, so skip it. |
| `resp_rate` | `daily-respiratory-rate.breathsPerMinute` | `Sample('respiratory_rate', 'count/min', ...)` | |
| `wrist_temp_c` | `daily-sleep-temperature-derivations.nightlyTemperatureCelsius` | `Sample('skin_temperature', '°C', ...)` | on the Air an internal device sensor, not wrist skin |
| (vendor) | `entropy`, `cardioFitnessLevel`, active zone minutes | `google.hrv_entropy`, `google.cardio_fitness_level`, `google.azm` | the API has no readiness, sleep score or stress type |

Gotchas, policy https://developers.google.com/health/policies/health-api-developer-user-data-policy (effective 2026-03-24):
- Limited Use applies, derived data included. Transfers to third parties only for a user-visible feature with consent. No ads, brokers, credit, medical-device function or IRB research. No explicit LLM clause in either Health policy. Sending aggregates to Anthropic is a third-party transfer, defensible for one owner who is developer and consenting user (an interpretation, not legal advice).
- Tokens and data must be encrypted at rest: Keychain for tokens, FileVault for SQLite.
- The Terms ask to store data at the granularity it was collected, in a clause about data given to Fitbit; its scope is ambiguous.
- Data is re-bucketed, timestamps can change, sleep can appear before `processed`. Replace the poll window per source.
- Do not use `python-fitbit`; it targets the dead API.
- Alternative for an iPhone owner: Google Health iOS 5.05 writes to Apple Health, and HAE picks it up. Whether Fitbit rMSSD lands in Apple's SDNN field is UNVERIFIED; until checked, map the "Google Health" source name to a source that is not in the `hrv_sdnn_ln` priority.

---

## Oura

Status: API v2, spec 1.43, `https://api.ouraring.com/v2/usercollection/*`. V1 is sunset. Personal Access Tokens stopped working in December 2025; OAuth2 only. https://cloud.ouraring.com/v2/docs

- Access: register an app at https://cloud.ouraring.com/oauth/applications, up to 10 users without approval. Gen3 and Ring 4 need an active membership.
- Auth: authorization code with PKCE (S256, "Recommended"). Authorize `https://cloud.ouraring.com/oauth/authorize`, token `https://api.ouraring.com/oauth/token`. Refresh tokens are single-use and rotate. Every request needs a `User-Agent`.
- Delivery: webhooks recommended, polling with `start_date`/`end_date` and `next_token` works. Rate limits per token and app, numbers not published (UNVERIFIED).
- Data: per-period `sleep` with 5-min and 30-s stage strings ('1' deep, '2' light, '3' REM, '4' awake), `average_hrv` (method not stated, UNVERIFIED), `lowest_heart_rate`, `average_breath`, `daily_readiness.temperature_deviation` (relative °C), `daily_spo2`, `vO2_max`, `heartrate` rows with a `source` tag.

Why no adapter: the "Oura API and MCP Agreement" (effective 2026-06-08, https://cloud.ouraring.com/legal/api-agreement) says in §4(d) that a non-aggregator may process Oura data with LLMs only through Oura's MCP server, and the API may not be used to give an AI model "input or data". §3(m) bars storing or caching MCP data without written consent, and §6(g) makes a breach material. There is no carve-out for your own data. homunculus stores in SQLite and its bots read aggregates, so the API path is very likely a breach. Whether derived numbers count is UNVERIFIED. This is a reading of the contract, not legal advice.

What to do instead: Oura writes sleep stages, 1-min HR, respiratory rate, workouts, active energy and weight to Apple Health, and HAE delivers them. HRV, RHR, temperature, SpO2 and VO2max are not written, so an Oura-only owner has no HRV in homunculus. Whether the agreement reaches data that left through Apple Health is open (UNVERIFIED). Map the Oura source name in `HEALTH_SOURCE_MAP` to `oura` so its rows stay identifiable and purgeable.

---

## Polar

Status: AccessLink API v3, `https://www.polaraccesslink.com/v3/`, GA. The transactional endpoints for activity and exercise (2025-08-18) and physical info (2026-01-13) are deprecated; use the non-transactional ones. SleepWise, Cardio Load, continuous HR and Elixir biosensing are beta. https://www.polar.com/accesslink-api/

- Access: any Polar Flow user creates a client at https://admin.polaraccesslink.com, free, no approval. The agreement (§6) lets Polar charge later. The user must have accepted all mandatory consents or the API answers 403.
- Auth: authorize `https://flow.polar.com/oauth2/authorization` (`response_type=code`, code valid 10 min, no documented PKCE). Token `POST https://polarremote.com/v2/oauth2/token` with Basic auth. The access token lasts about one year (`expires_in` 31535999) and there is no refresh token. Scope `accesslink.read_all`. After the exchange, register the user once with `POST /v3/users`.
- Delivery: poll. Rate limit per 15 min: 500 + 20 × users; per 24 h: 5000 + 100 × users, with `RateLimit-*` headers on every response. Backfill is the hard limit: exercises only from the last 30 days and only those uploaded after registration; sleep and Nightly Recharge lists only 28 days. Data you do not pull in time is gone. Poll at least daily, better every 3 h.

| Canonical key | Polar field | Store row | Note |
|---|---|---|---|
| `hrv_rmssd_ln` | `nightly-recharge.heart_rate_variability_avg` | `Sample('heart_rate_variability_rmssd', 'ms', ts=sleep midpoint, 'nightly', v, 'polar')` | RMSSD averaged over 4 h starting 30 min after sleep onset |
| `rhr` | none reliable | none | `physical-info.resting-heart-rate` is user-maintained; `heart_rate_avg` is a sleep mean. Store as `polar.sleep_hr_avg`. |
| sleep | `hypnogram` (`"HH:MM"` → code, 30-s epochs), `light_sleep`, `deep_sleep`, `rem_sleep` seconds | `SleepSegment` (0 awake, 1 rem, 2 and 3 core, 4 deep, 5 unspecified) and `SleepNight` | the hypnogram has no date or zone: anchor it at `sleep_start_time` and roll past midnight yourself |
| loads | `/v3/exercises/{id}?samples=true` | `Workout(id='polar:<id>', hr_series from samples, ...)` | `start-time` plus `start-time-utc-offset` |
| `resp_rate` | `breathing_rate_avg` | `Sample('respiratory_rate', 'count/min', ...)` | |
| `wrist_temp_c` | biosensing `sleep_time_skin_temperature_celsius` | `Sample('skin_temperature', '°C', ...)` | absolute; `deviation_from_baseline_celsius` goes to `polar.skin_temp_deviation` |
| `spo2_min` | biosensing `blood_oxygen_percent` | `Sample('blood_oxygen_saturation', '%', ...)` | a spot test, not a night measurement |
| `vo2max`, `weight_kg` | `physical-info` | skip | user-maintained or fitness-test values |
| (vendor) | `ans_charge` (-10 to +10), `nightly_recharge_status` (1-6), cardio load | `polar.ans_charge`, `polar.nightly_recharge`, `polar.cardio_load` | never canonical |

Gotchas: the Polar API Agreement (2025-08-22, https://www.polar.com/en/legal/polar-api-agreement) allows use for apps "in the Polar ecosystem" (§2.1), sharing with external parties only with the member's explicit permission (§3.1.1), and requires naming Polar as the source (§3.1.5). It says nothing about AI. An LLM provider probably counts as an external party; with the owner's own consent this looks fine (UNVERIFIED). Continuous HR `sample_time` is `HH:mm:ss` in device time without a zone, so travel breaks it. The webhook signature algorithm is UNVERIFIED, which does not matter for a poller. Re-authorize once a year; alert 14 days before `expires_at`.

---

## Withings

Status: Public API, `https://wbsapi.withings.net`, GA. https://developer.withings.com/api-reference/

- Access: plan "Start for Free", self-serve, 10 active users (https://static.withings.com/content/api/plans/plans.html). Accounts created after 2026-10-12 need Withings+ to share data with a free-plan app; sharing pauses when Withings+ lapses. Older accounts are unaffected. SpO2, ECG, AFib and AHI sit in "Advanced Biomarkers" (price on request); whether SpO2 comes through on the free plan is UNVERIFIED.
- Auth: authorize `https://account.withings.com/oauth2_user/authorize2`; the code is valid 30 s, so the exchange must run automatically from the loopback handler, not by pasting. Token `POST /v2/oauth2` with `action=requesttoken` and the client secret. No documented PKCE. Access token 3 h, refresh token 1 year and rotating; the old refresh token stays valid 8 h or until the new access token is first used. Scopes `user.metrics,user.activity`. https://developer.withings.com/developer-guide/v3/integration-guide/advanced-research-api/get-access/access-and-refresh-tokens/
- Delivery: poll with `lastupdate`, which Withings supports and the others do not. Rate limit 120 req/min per app. Endpoints: `/measure action=getmeas` (`meastypes`, `lastupdate`, `more`/`offset`), `/v2/measure action=getactivity|getworkouts`, `/v2/sleep action=get` (max 7 days per call) and `action=getsummary` (`lastupdate`).
- Recommended cadence: hourly, `lastupdate` = last success minus 1 h.

| Canonical key | Withings field | Store row | Note |
|---|---|---|---|
| `weight_kg` | meastype 1 | `Sample('weight_body_mass', 'kg', value × 10^unit)` | the scale; the best weight source there is |
| (body comp) | meastype 6 fat %, 8 fat mass, 76 muscle, 77 water, 88 bone | `withings.fat_pct`, ... | not canonical keys today |
| `rhr` | sleep summary `hr_min` | skip | a sleep minimum, not RHR; `withings.sleep_hr_min` |
| `hrv_rmssd_ln` | sleep `get` `rmssd` series | `Sample('heart_rate_variability_rmssd', 'ms', ts, 'qty', v, 'withings')` | RMSSD over "a few seconds" (docs). `sdnn_1` is a 1-min SDNN and must not go to `hrv_sdnn_ln`. |
| sleep | `state` 0 awake, 1 light, 2 deep, 3 rem, 4 manual, 5 unspecified | `SleepSegment` (4 and 5 → unspecified) and `SleepNight` from `getsummary` | sleep mat or watch |
| `resp_rate` | `rr_average` | `Sample('respiratory_rate', 'count/min', ...)` | |
| `wrist_temp_c` | meastype 73 skin temp | `Sample('skin_temperature', '°C', ...)` | 71 body temperature goes to `withings.body_temp` |
| `spo2_min` | meastype 54 | `Sample('blood_oxygen_saturation', '%', ...)` | plan-dependent |
| `vo2max` | meastype 123 | `Sample('vo2_max', 'ml/kg/min', ...)` | estimate |
| `bp_sys`, `bp_dia` | meastypes 10 systolic, 9 diastolic (codes not in the research report, UNVERIFIED) | `Sample('blood_pressure', 'mmHg', field 'systolic'/'diastolic')` | the red flag at ≥ 180/120 reads these |
| loads | `getworkouts` | `Workout` with avg/max HR, no series | |
| (vendor) | `sleep_score`, `sleep_efficiency` | `withings.sleep_score`, ... | never canonical |

Gotchas: retries cover only about 5 h, missed notifications never come back, which is why the adapter polls with `lastupdate`. No AI clause in the API terms (UNVERIFIED, https://www.withings.com/legal/api-terms-of-use); a separate "AI model training contract" exists for the Advanced plan, which suggests training without it is not allowed and inference is. Field availability depends on device and region.

---

## Android Health Connect (hub, only with an Android phone)

Health Connect is an on-device store with no cloud API. Reading it needs an Android app with `READ_HEALTH_DATA_IN_BACKGROUND`; without `READ_HEALTH_DATA_HISTORY` reads reach back 30 days. https://developer.android.com/health-and-fitness/health-connect/data-types

The export app to use is HC Webhook (https://github.com/mcnaveen/health-connect-webhook, AGPL-3.0, Play Store `com.hcwebhook.app`): 31 data types, interval or scheduled sync, JSON POST, WorkManager. Its auth headers, intervals and retry behavior are not in the README; a mirror says it has no retries (UNVERIFIED). It would need a new Brainstem POST route and a parser into the same `Sample`/`SleepSegment`/`Workout` rows. Fitbit, Garmin, Polar and Oura all write into Health Connect, and Garmin does not share HRV status, Body Battery or training load there (https://www.androidcentral.com/wearables/garmin/heres-everything-garmin-will-and-wont-share-with-google-health-connect).

---

## Aggregators (not used)

None of them sells a personal tier, and each would be a processor holding special-category health data in its cloud, which breaks fail-closed and local-first.

| Vendor | Cheapest plan | Source |
|---|---|---|
| Terra | from $399/month billed annually | https://tryterra.co/pricing |
| Junction (ex-Vital) | $300/month flat, sandbox with synthetic devices | https://www.junction.com/pricing |
| Rook | $399/month, webhooks +$99 | competitor page, UNVERIFIED |
| Thryve | EUR 499/month after a 25-user trial | competitor page, UNVERIFIED |
| Spike | $450/month | https://www.spikeapi.com/wearable-api |

Open Wearables (MIT, Docker Compose, https://openwearables.io/wearable-api) self-hosts the same idea. It is a second service with its own database and only pays off with three or more cloud vendors.

---

## Recommended setup

| If you wear | Do this | You get | You lose |
|---|---|---|---|
| Apple Watch | HAE automations as above | everything canonical, SDNN | nothing |
| Apple Watch + Garmin | HAE, plus the Garmin poller; set `HEALTH_SOURCE_MAP` so Garmin-origin rows from HAE are dropped (`api_sources`) | Garmin RMSSD, VO2max, FIT HR | nothing; pick one HRV and one sleep source and lock them |
| Garmin, iPhone without Watch | Garmin poller, HAE for phone steps and check-ins | RMSSD, sleep totals, workouts | sleep segments, so no Sleep Regularity Index |
| WHOOP | WHOOP poller (Sandbox app) | nightly RMSSD, RHR, sleep totals, workouts without HR series | `load_edwards`, VO2max, SRI; accept the §4.2(2) storage risk or use WHOOP → Apple Health → HAE and lose HRV |
| Fitbit Air, Pixel Watch, other Fitbit | Test Google Personal auth first. If it works, the Google poller. If not, Google Health iOS → Apple Health → HAE | RMSSD, stages, HR series | with the HAE path, HRV is UNVERIFIED |
| Oura | Oura → Apple Health → HAE, source mapped to `oura` | stages, HR, resp. rate, workouts | HRV, RHR, temperature, SpO2: the API is off limits for an LLM-backed tool |
| Polar | Polar poller every 3 h, never less than daily | RMSSD, hypnogram, exercise HR | history older than 28-30 days |
| Withings scale or sleep mat | Withings poller hourly | weight, body comp, BP, sleep mat stages | new accounts need Withings+ |
| Android phone | HC Webhook → Brainstem route | whatever the vendor writes to HC | HRV for most vendors |

## Own box vs Devin automations

| Piece | Box (Pineal) | Devin Automation script with `STATE_FILE` |
|---|---|---|
| HAE ingest | yes, `tailscale serve` | a daily summary under 200 KB to a webhook automation only; coarse, and health data lands on Cognition VMs |
| Garmin poller | yes | works mechanically: the API key is a static org secret, the JWT is minted per run |
| Polar poller | yes | works mechanically: the 1-year token is a static secret |
| Google poller | yes | works mechanically: Google refresh tokens do not rotate |
| WHOOP poller | yes | breaks: refresh tokens rotate, org secrets are static, and Devin says not to keep secrets in `STATE_FILE` |
| Withings poller | yes | breaks, same reason |
| Any vendor webhook | needs `funnel`, which the architecture forbids | Devin webhook triggers want `X-Webhook-Secret`/Bearer/query secret; vendors send their own HMAC headers, so none fits (UNVERIFIED per vendor) |

Devin script limits: 60 s default timeout, 300 s max, `STATE_FILE` 64 KiB, at most 10 items dispatched per run by default (50 max), no retry of a failed script, and no ledger deploy key, since that key lives only on the box. The architecture keeps health data off Cognition machines, so the recommendation is the box for every poller, scheduled by Pineal. Devin keeps building the engine and repairing failed jobs from scrubbed incidents.

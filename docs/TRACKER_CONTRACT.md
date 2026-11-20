# Contract: `homunculus/trackers/`

Status: design, 2026-11-20. For an engineer who has not read the research. The research and the per-vendor reasoning are in `docs/TRACKERS.md`. This file is the public API and the rules the code must keep. Python 3.11+, standard library only, like the rest of homunculus.

## Scope

Direct pollers for vendors with a usable personal API:

| Module | `name` (= `store` source) | Auth | Phase |
|---|---|---|---|
| `garmin.py` | `garmin` | API key → JWT | 1 |
| `whoop.py` | `whoop` | OAuth2 code, confidential client, rotating refresh | 1 |
| `google.py` | `google` | OAuth2 code + PKCE, non-rotating refresh | 2 |
| `withings.py` | `withings` | OAuth2 code, confidential client, rotating refresh | 2 |
| `polar.py` | `polar` | OAuth2 code, Basic auth, 1-year token, no refresh | 2 |

Not adapters: Apple Health (HAE pushes to `ingest.py`), Oura (blocked by its API agreement §4(d)), Health Connect (push app, a future Brainstem route), commercial aggregators. `registry.BLOCKED` names Oura and the legacy Fitbit API so `tracker auth oura` fails with the reason instead of "unknown tracker".

No adapter uses webhooks. All poll, scheduled by Pineal on the box.

## Changes outside the package

These are contract changes to existing modules. Each needs its own test.

### `store.py`

```python
SCHEMA_VERSION: int = 2
SOURCES = ('watch', 'phone', 'garmin', 'whoop', 'google', 'polar', 'withings', 'oura', 'other')
```

- `oura` exists only so `HEALTH_SOURCE_MAP` can tag Oura-origin rows arriving through HAE. No adapter writes it.
- The SQL tables have no CHECK on `source`, so the new names need no table rewrite. Migration 2 adds one table:

```sql
CREATE TABLE tracker_state (
    "name" TEXT PRIMARY KEY, "cursor_json" TEXT NOT NULL, "last_ok" TEXT, "last_attempt" TEXT,
    "last_error" TEXT, "newest_data_ts" TEXT, "requests_today" INTEGER NOT NULL DEFAULT 0,
    "requests_day" TEXT);
```

- New dataclass and methods:

```python
@dataclass(frozen=True)
class TrackerState:
    name: str
    cursor_json: str          # canonical_json of the adapter cursor, <= 4096 bytes
    last_ok: str | None       # ISO-8601 UTC
    last_attempt: str | None
    last_error: str | None    # error kind only: 'auth_required' | 'rate_limited' | 'http_5xx' | 'schema' | 'network'
    newest_data_ts: str | None
    requests_today: int
    requests_day: str | None  # YYYY-MM-DD UTC the counter belongs to

class Store:
    def tracker_state(self, name: str) -> TrackerState | None
    def put_tracker_state(self, row: TrackerState) -> None
    def replace_source_window(self, source: str, since: str, until: str, batch: 'TrackerBatch') -> dict[str, int]
    def purge_source(self, source: str) -> dict[str, int]
```

- `replace_source_window` runs in one transaction. For each of `samples` (by `ts`), `sleep_segments` (by `start`), `sleep_nights` (by `wake_date`), `workouts` (by `start`) and `daily` rows whose `key` starts with `f'{source}.'` (by `date`), it deletes rows with that `source` in `since <= col < until`, then inserts the batch. It raises `ValueError` if any batch row has another source or lies outside the window. This is the only write path for adapters. It exists because Google re-buckets records and changes their timestamps, and every vendor rescores records after late syncs: a plain upsert would keep stale duplicates.
- `purge_source` deletes every row of one source in every table and its `tracker_state`. `tracker revoke --purge` calls it (WHOOP terms §7.1 require deletion on revocation).
- `put_daily` raises `ValueError` for a key containing `.` unless the prefix before the dot equals the row's `source`. `metrics.DAILY_KEYS` never contain a dot.

### `ingest.py`

- `IngestConfig` gains `api_sources: frozenset[str] = frozenset()`, loaded from `HEALTH_API_SOURCES` (comma list, each in `SOURCES`).
- `parse_payload` drops every row whose normalized source is in `api_sources` and counts it in `skipped['api_source']`. Reason: Garmin Connect, WHOOP and Google Health all write to Apple Health. If the same vendor is also polled directly, workouts and steps count twice, because the workout ids differ (`garmin:<id>` vs the HAE id) and both rows carry `source='garmin'`.
- `HEALTH_SOURCE_MAP` values may now be any of the new `SOURCES`.

### `metrics.py`

1. Nightly summaries. WHOOP, Google and Polar give one RMSSD value per night, and `_Spec('hrv_rmssd_ln', min_n=2)` would turn it into `None`. Rule: rows with `field == 'nightly'` are a separate path. For the canonical source of the day, if it has any `field == 'qty'` readings in the sleep window, use them as today (`mean_ln`, `min_n=2`). Otherwise, if it has exactly one `field == 'nightly'` row in the window, value = `ln(v)`, `n = 1`, quality `ok`. More than one nightly row from one source for one night: `None`, quality `sparse`. An adapter emits one field kind per source and never both.
2. Locked sources. `MetricsConfig` gains `locked: Mapping[str, str]` (key → source), read from `config/metrics.json` key `"locked"`. For a locked key the priority list is ignored: only that source counts, and if it is missing the value is `None`. `hrv_state` and `rhr_state` filter their series to rows whose `source` equals the lock. Changing a lock is a commit to `config/` and restarts the baseline. Default locks: `hrv_sdnn_ln: watch`. Everything else stays unlocked until the owner adds a second tracker.
3. `wrist_temp_c` reads `('apple_sleeping_wrist_temperature', 'skin_temperature')`. The two are different sensors, so this key must be locked whenever both exist.
4. `spo2_min` reads readings only. Nightly averages never enter it; they live under vendor keys.

Readiness, guardrails, `insula.py` and the red-flag rules read only `DAILY_KEYS`. A test asserts that no key containing `.` reaches them.

## Package layout

```
homunculus/trackers/
  __init__.py      # re-exports registry.build, registry.enabled, base types
  base.py          # types, HTTP client, token store, OAuth helpers, TrackerSource protocol
  units.py         # every conversion in one place
  registry.py
  run.py           # poll orchestration used by CLI and Pineal
  cli.py           # 'tracker' subcommands
  garmin.py whoop.py google.py withings.py polar.py
```

## `base.py`

### Errors

```python
class TrackerError(Exception): ...
class AuthRequired(TrackerError): ...      # no token, invalid_grant, key expired, 401 after one refresh
class RateLimited(TrackerError):           # retries exhausted on 429
    retry_after_s: float | None
class UpstreamError(TrackerError): ...     # 5xx after retries, network failure
class SchemaError(TrackerError): ...       # response failed validation; poll result discarded
class HostNotAllowed(TrackerError): ...
```

### HTTP

```python
@dataclass(frozen=True)
class Request:
    method: str                      # 'GET' | 'POST' | 'DELETE'
    url: str                         # absolute https URL, query already encoded
    headers: Mapping[str, str]
    body: bytes | None = None
    timeout_s: float = 30.0

@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]       # keys lower-cased
    body: bytes

Transport = Callable[[Request], Response]

def urllib_transport(req: Request) -> Response
    # https only; follows no redirects; returns non-2xx as a Response instead of raising;
    # raises UpstreamError on socket errors and timeouts

class HttpClient:
    def __init__(self, transport: Transport, *, allow_hosts: frozenset[str], user_agent: str,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None, max_retries: int = 4, max_requests: int = 200) -> None
    requests_made: int
    def request(self, method: str, url: str, *, params: Mapping[str, str] | None = None,
                headers: Mapping[str, str] | None = None, form: Mapping[str, str] | None = None,
                json_body: Any = None) -> Response
    def get_json(self, url: str, *, params=None, headers=None) -> Any
```

Behavior:
- The host of every URL must be in `allow_hosts`, else `HostNotAllowed` before any I/O.
- Retry on 429, 500, 502, 503, 504 up to `max_retries`. Wait `Retry-After` seconds if present (integer or HTTP date), else `X-RateLimit-Reset` / `RateLimit-Reset` if it parses as seconds, else `min(60, 2**attempt) * (0.5 + rng.random())`. A wait above 300 s raises `RateLimited` at once instead of sleeping; the next scheduled poll picks it up.
- 401 and 403 are returned, never retried here. The adapter refreshes once, then raises `AuthRequired`.
- `max_requests` per poll run is a hard budget; exceeding it raises `RateLimited(None)` and keeps the cursor where the last complete step left it.
- Never log URLs with query strings, headers or bodies. Log method, host, path template, status, duration.

### Tokens

Tokens never go into the ledger's git history. The contract request said "token store in the private ledger"; that would put long-lived credentials into a repo whose history is kept forever and pushed to GitHub. Tokens live next to it instead.

```python
@dataclass(frozen=True)
class Token:
    access_token: str
    refresh_token: str | None
    expires_at: float                # epoch seconds; float('inf') if the vendor gives none
    scope: str
    token_type: str = 'Bearer'
    refresh_expires_at: float | None = None
    obtained_at: float = 0.0
    extra: Mapping[str, str] = field(default_factory=dict)   # e.g. Polar x_user_id, Withings userid

class TokenStore(Protocol):
    def load(self, name: str) -> Token | None
    def save(self, name: str, token: Token) -> None      # atomic
    def delete(self, name: str) -> None

class FileTokenStore:
    def __init__(self, root: Path) -> None
    # root defaults to $HOMUNCULUS_SECRETS or ~/.local/state/homunculus/trackers
    # dir 0o700, files 0o600, write tmp + fsync + os.replace
    # raises ValueError if root or any parent contains a .git entry

class KeychainTokenStore:
    def __init__(self, service: str = 'homunculus.trackers', runner: Callable = subprocess.run) -> None
    # macOS 'security add-generic-password -U -s <service> -a <name> -w <json>' / find-generic-password -w

def default_token_store() -> TokenStore   # Keychain on darwin when 'security' exists, else FileTokenStore
```

Google and WHOOP require encryption at rest. Keychain covers tokens; FileVault covers SQLite. `tracker status` prints a warning when `fdesetup status` does not say FileVault is on (darwin only; skipped elsewhere).

### OAuth2 helpers

```python
@dataclass(frozen=True)
class OAuthConfig:
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str | None
    scopes: tuple[str, ...]
    scope_sep: str = ' '                   # Withings uses ','
    redirect_uri: str = 'http://127.0.0.1:8765/callback'
    use_pkce: bool = True
    client_auth: str = 'body'              # 'body' | 'basic'
    state_len: int = 32                    # WHOOP: 8
    extra_authorize: Mapping[str, str] = field(default_factory=dict)   # Google: access_type=offline, prompt=consent
    extra_token: Mapping[str, str] = field(default_factory=dict)       # Withings: action=requesttoken
    token_parser: Callable[[Any, float], Token] | None = None          # Withings wraps the token in {'status', 'body'}

def pkce_challenge(verifier: str) -> str   # base64url(sha256(verifier)) without '='; method S256
def pkce_pair(rng: secrets.SystemRandom | None = None) -> tuple[str, str]
    # (verifier, pkce_challenge(verifier)); verifier: 64 chars from [A-Za-z0-9-._~]

def new_state(length: int) -> str          # [A-Za-z0-9], secrets module
def build_authorize_url(cfg: OAuthConfig, state: str, challenge: str | None) -> str

def wait_for_code(cfg: OAuthConfig, state: str, *, timeout_s: float = 300.0,
                  on_ready: Callable[[], None] = lambda: None) -> str
    # binds 127.0.0.1 on the redirect port only; serves exactly one request on the redirect path;
    # compares state with hmac.compare_digest; returns code; replies with a plain German page
    # "Fertig, du kannst das Fenster schliessen."; error param -> AuthRequired(error)

def parse_pasted_redirect(text: str, state: str) -> str   # manual mode: user pastes the full redirect URL

def exchange_code(http: HttpClient, cfg: OAuthConfig, code: str, verifier: str | None, now: float) -> Token
def refresh_token(http: HttpClient, cfg: OAuthConfig, token: Token, now: float) -> Token
    # keeps the old refresh_token when the response omits one (Google)

class TokenManager:
    def __init__(self, name: str, store: TokenStore, http: HttpClient, cfg: OAuthConfig,
                 clock: Callable[[], float] = time.time, lock_dir: Path | None = None) -> None
    def access_token(self) -> str
        # refresh when expires_at - now < 120 s; under fcntl.flock on <lock_dir>/<name>.lock;
        # re-load the token after taking the lock (another process may have refreshed);
        # save the new token BEFORE returning it; invalid_grant -> delete nothing, raise AuthRequired
    def force_refresh(self) -> str          # used once after a 401
```

The order inside the lock matters for WHOOP and Withings, whose refresh tokens rotate: load, refresh, save, then use. A crash between refresh and save locks you out; the save happens before any other request.

### The adapter protocol

```python
@dataclass(frozen=True)
class TrackerBatch:
    samples: tuple[Sample, ...] = ()
    sleep_segments: tuple[SleepSegment, ...] = ()
    sleep_nights: tuple[SleepNight, ...] = ()
    workouts: tuple[Workout, ...] = ()
    events: tuple[HealthEvent, ...] = ()
    daily_vendor_scores: tuple[DailyValue, ...] = ()

@dataclass(frozen=True)
class PollResult:
    batch: TrackerBatch
    window: tuple[str, str]           # (since, until) the batch replaces, ISO-8601 with offset
    new_cursor: Mapping[str, Any]     # JSON-serializable, <= 4096 bytes canonical
    complete: bool                    # False if the request budget or a rate limit cut the run short
    requests_made: int
    warnings: tuple[str, ...] = ()    # e.g. 'garmin:rhr_min_fallback 2026-11-19'

class AuthUI(Protocol):
    def show_url(self, url: str) -> None
    def ask(self, prompt: str) -> str
    def info(self, text: str) -> None

@dataclass(frozen=True)
class AuthStatus:
    ok: bool
    expires_at: float | None
    refresh_expires_at: float | None
    detail: str                       # no secrets

class TrackerSource(Protocol):
    name: ClassVar[str]                          # one of store.SOURCES
    hosts: ClassVar[frozenset[str]]
    auth_kind: ClassVar[str]                     # 'oauth2' | 'api_key'
    overlap_days: ClassVar[int]                  # trailing window re-read every poll, default 3
    backfill_chunk_days: ClassVar[int]
    max_history_days: ClassVar[int | None]       # Polar 28, others None
    vendor_keys: ClassVar[frozenset[str]]        # every allowed '<name>.<x>' key

    def authorize(self, ui: AuthUI, *, manual: bool = False) -> AuthStatus
    def auth_status(self) -> AuthStatus
    def revoke(self) -> None                     # vendor revoke call if one exists, then TokenStore.delete
    def poll(self, since_cursor: Mapping[str, Any] | None, *, now: datetime, tz: str,
             backfill_days: int = 0) -> PollResult
```

Rules every `poll` keeps:
- Pure apart from HTTP and the token store. No SQLite, no ledger, no file writes. `run.py` writes.
- Window: `since = min(cursor['watermark'] - overlap_days, backfill position)`, `until = now`. First run without a cursor and with `backfill_days == 0`: `since = now - overlap_days`.
- Backfill moves backwards in `backfill_chunk_days` steps, oldest last, within `max_requests`. The cursor stores `backfill_before`; the next run continues. `complete=False` until it reaches the requested start.
- Every row's `source == self.name`. Every vendor key is in `vendor_keys` and starts with `f'{name}.'`.
- Timestamps are ISO-8601 with the local offset of the measurement (`2026-11-20T06:42:00+01:00`), the same format HAE writes, because store range queries compare strings.
- Workout ids are `f'{name}:{vendor_id}'`. `raw_json` is `canonical_json` of the summary with GPS, location names, device serials and owner names removed (`strip_vendor_raw`).
- Unknown enums, missing required fields or a wrong type in a record: skip that record and add a warning. A response whose top-level shape is wrong: raise `SchemaError`, keep the old cursor.
- Never emit a canonical row from a vendor field the mapping table below does not list.

## `run.py`

```python
@dataclass(frozen=True)
class TrackerConfig:
    enabled: tuple[str, ...]
    per_source: Mapping[str, Mapping[str, Any]]     # cadence and options, from config/trackers.json
    tz: str

def load_tracker_config(ledger: Ledger, tz: str) -> TrackerConfig
def due(cfg: TrackerConfig, states: Mapping[str, TrackerState | None], now: datetime) -> list[str]
def poll_one(store: Store, source: TrackerSource, *, now: datetime, tz: str, backfill_days: int = 0,
             dry_run: bool = False) -> dict[str, Any]
    # loads cursor, calls poll, replace_source_window(window), saves TrackerState; on AuthRequired
    # records last_error='auth_required' and returns; never raises for vendor errors
def poll_due(store: Store, cfg: TrackerConfig, sources: Mapping[str, TrackerSource], now: datetime) -> list[dict]
def freshness_entries(store: Store, names: Sequence[str]) -> dict[str, dict]
    # -> data/freshness.json 'trackers' block: {name: {last_ok, last_error, newest_data_ts, auth_expires_at}}
```

`config/trackers.json` in the ledger holds only non-secret settings:

```json
{"enabled": ["garmin"],
 "garmin": {"poll_minutes": 60, "key_expires_on": "2027-11-19"},
 "whoop": {"poll_minutes": 60},
 "google": {"poll_minutes": 60, "data_source_family": "google-wearables"},
 "withings": {"poll_minutes": 60},
 "polar": {"poll_minutes": 180}}
```

Secrets come from the environment only (an `.env` outside git): `HOMUNCULUS_GARMIN_API_KEY`, `HOMUNCULUS_WHOOP_CLIENT_ID`, `HOMUNCULUS_WHOOP_CLIENT_SECRET`, `HOMUNCULUS_GOOGLE_CLIENT_ID`, `HOMUNCULUS_GOOGLE_CLIENT_SECRET`, `HOMUNCULUS_WITHINGS_CLIENT_ID`, `HOMUNCULUS_WITHINGS_CLIENT_SECRET`, `HOMUNCULUS_POLAR_CLIENT_ID`, `HOMUNCULUS_POLAR_CLIENT_SECRET`. A missing secret for an enabled tracker is a `ConfigError` at startup, not at poll time.

### Pineal jobs

| Job | Cadence | Essential | Does |
|---|---|---|---|
| `tracker_poll` | hourly | yes (ingest runs under HALT) | `poll_due`: polls each source whose `poll_minutes` have passed since `last_attempt`. `period_key` is at most hourly, so `poll_minutes` below 60 is rejected by `load_tracker_config`. |
| `tracker_morning` | daily 06:20 | yes | polls every enabled source before `finalize` at 06:30 |
| `tracker_reconcile` | daily 03:30 | no | D-3 to D0 for every source, plus one backfill chunk if one is pending |
| `tracker_expiry` | daily 09:00 | no | templated alert 14 days and 2 days before `key_expires_on` (Garmin), `expires_at` (Polar) or `refresh_expires_at` (Withings), and at once on `auth_required` |

Freshness and alerts go through Pineal as everything else does. Alert texts are fixed German templates, e.g. "Garmin-Schluessel laeuft am 2027-11-19 ab. Neuen Schluessel anlegen und `homunculus tracker auth garmin` ausfuehren."

## `registry.py`

```python
REGISTRY: Mapping[str, Callable[..., TrackerSource]] = {
    'garmin': GarminSource, 'whoop': WhoopSource, 'google': GoogleHealthSource,
    'withings': WithingsSource, 'polar': PolarSource,
}
BLOCKED: Mapping[str, str] = {
    'oura': 'Oura API Agreement §4(d): API data may not be AI input. Use Oura -> Apple Health -> HAE.',
    'fitbit': 'Legacy Fitbit Web API is shut down. Use google.',
}
def build(name: str, *, env: Mapping[str, str], cfg: TrackerConfig, transport: Transport = urllib_transport,
          token_store: TokenStore | None = None, clock: Callable[[], float] = time.time) -> TrackerSource
    # KeyError-free: unknown -> ValueError listing REGISTRY and BLOCKED
def enabled(cfg: TrackerConfig) -> list[str]
```

## CLI

Entry `python -m homunculus tracker ...` (add `homunculus/__main__.py` dispatching to `trackers.cli.main`).

| Command | Does | Exit |
|---|---|---|
| `tracker auth <name> [--manual] [--port N]` | Garmin: reads the key from env, mints a JWT, prints expiry. OAuth vendors: prints the URL, waits on the loopback port; `--manual` asks for the pasted redirect URL. Withings refuses `--manual` (code valid 30 s). Polar then calls `POST /v3/users`. | 0 ok, 2 denied or timeout |
| `tracker poll [<name> ...] [--all] [--backfill DAYS] [--dry-run]` | `poll_one` per source. `--dry-run` prints row counts per table and window, writes nothing. | 0 all ok, 3 some incomplete or failed, 2 any auth required |
| `tracker status [--json]` | per source: enabled, auth ok, expiry, last ok, last error, newest data, requests today, FileVault warning | 0 |
| `tracker revoke <name> [--purge]` | vendor revoke where one exists (WHOOP `DELETE /v2/user/access`), delete token; `--purge` asks "y/N" and calls `Store.purge_source` | 0 |

No command prints a token, key or code. `--json` output is stable and documented in `cli.py`.

## Canonical source priority

`metrics.py` picks one source per key. These are the defaults the contract adds to `config/metrics.json`. Order matters only when the owner wears two devices at the same time. Keys marked "lock" must be locked to one source once a second source exists, because their baselines are per source.

| Key | Default priority | Lock | Why |
|---|---|---|---|
| `hrv_sdnn_ln` | watch | lock: watch | only Apple reports SDNN; Withings `sdnn_1` is a 1-min SDNN and is excluded |
| `hrv_rmssd_ln` | garmin, polar, whoop, google, withings, watch | lock | RMSSD windows differ per vendor (5-min readings, 4 h window, last SWS, whole night) |
| `rhr` | watch, garmin, whoop, google | lock | WHOOP is nocturnal, Garmin falls back to the day minimum, Apple is daytime-derived |
| `tst_h`, `sleep_mid_min`, `sleep_start_min` | watch, garmin, whoop, google, polar, withings, phone | lock | the device worn at night; staging rules differ |
| `weight_kg` | withings, garmin, google, watch, phone | no | a scale beats manual entries; WHOOP and Polar profile values are never used |
| `vo2max` | watch, garmin, google, withings | lock | each vendor's estimate has its own bias; slopes only |
| `steps` | watch, garmin, google, whoop, phone | no | one source per day avoids double counting |
| `active_kcal` | watch, garmin, google, polar, withings | no | WHOOP kJ is total energy, excluded |
| `resp_rate` | watch, garmin, whoop, google, polar, withings | lock | sleep-context means only |
| `wrist_temp_c` | watch, google, whoop, polar, withings | lock | absolute nightly values; vendor deviations are vendor keys |
| `spo2_min` | watch, garmin, google, withings, polar | no | readings and spot tests only, never nightly means |
| `load_srpe`, `load_edwards` | watch, garmin, polar, google, whoop, withings | no | one source per day; WHOOP and Withings lack HR series, so Edwards TRIMP is `None` on their days |
| `bp_sys`, `bp_dia` | withings, watch, phone | no | cuff readings |

## Vendor scores

- Stored as `DailyValue(date=<local wake date or calendar date>, key=f'{name}.{x}', value, source=name, n=1, quality='ok')`.
- `date` for sleep-derived scores is the wake date; for day scores (strain, Body Battery) the local calendar date.
- Each adapter lists its keys in `vendor_keys`. Initial sets:
  - garmin: `hrv_last_night`, `hrv_weekly_avg`, `hrv_status_code`, `body_battery_max`, `body_battery_min`, `stress_avg`, `sleep_score`, `sleep_need_min`, `training_load`, `fitness_age`, `spo2_sleep_avg`
  - whoop: `recovery`, `day_strain`, `workout_strain_max`, `cycle_kcal_total`, `sleep_performance`, `sleep_consistency`, `sleep_efficiency`, `calibrating`, `spo2_sleep_avg`, `disturbances`
  - google: `hrv_entropy`, `cardio_fitness_level`, `azm`, `spo2_sleep_avg`
  - polar: `ans_charge`, `nightly_recharge`, `cardio_load`, `skin_temp_deviation`, `sleep_hr_avg`
  - withings: `sleep_score`, `sleep_efficiency`, `sleep_hr_min`, `fat_pct`, `fat_mass_kg`, `muscle_mass_kg`, `body_temp_c`
- Codes as numbers: `hrv_status_code` 0 none, 1 poor, 2 low, 3 unbalanced, 4 balanced (Garmin status names, mapping is ours). `calibrating` 0 or 1.
- Never canonical: no `metrics`, readiness, `insula`, guardrail or red-flag code reads them. `weekly.build_packet` may include them under `vendor_context`, each with its vendor name, so a bot can say "WHOOP rated recovery 34%" and nothing more.

## Unit conversions (`units.py`)

All functions are pure, take floats or strings, and raise `ValueError` on garbage. Canonical units: ms for HRV, count/min for HR and respiration, hours for sleep durations in `SleepNight`, seconds for `Workout.duration_s`, kcal, kg, % (0-100) for SpO2, °C, ml/kg/min.

| Function | Rule |
|---|---|
| `kj_to_kcal(x)` | `x / 4.184` |
| `ms_to_h(x)` | `x / 3_600_000` (WHOOP `*_milli`) |
| `s_to_h(x)` | `x / 3600` (Garmin, Polar, Withings seconds) |
| `min_to_h(x)` | `x / 60` (Google `minutesIn*`) |
| `grams_to_kg(x)` | `x / 1000` (Google `weightGrams`) |
| `garmin_weight_kg(x)` | `x / 1000 if x > 1000 else x` (documented kg, legacy grams) |
| `withings_value(value, unit)` | `value * 10 ** unit` (integer exponent, may be negative) |
| `spo2_percent(x)` | `x * 100 if 0 < x <= 1.0 else x`; reject outside 50-100 after conversion |
| `int64_str(x)` | Google int64 as JSON string → `float(int(x))` |
| `offset_from_seconds(s)` | Google `"-14400s"` → `"-04:00"` |
| `offset_from_garmin(local, gmt)` | `local - gmt` rounded to 15 min → `"+01:00"` |
| `iso_with_offset(utc_dt, offset)` | UTC datetime + `"+HH:MM"` → `"2026-11-20T06:42:00+01:00"` |
| `epoch_ms_to_iso(ms, offset)` | Garmin sleep timestamps |
| `epoch_s_to_iso(s, tz_name)` | Withings `date` plus its IANA `timezone` via `zoneinfo` |
| `iso_duration_s(text)` | `"PT1H2M3.5S"` → `3723.5` (Polar exercise duration; also accept plain seconds, format UNVERIFIED) |
| `polar_hypnogram(start_iso, mapping)` | `{"HH:MM": code}` → sorted `(start, end, stage)`; each epoch is 30 s, a clock time earlier than the previous one means the next day |
| `oura_stage`, `whoop_stage`, `google_stage`, `polar_stage`, `withings_stage` | vendor code → `SLEEP_STAGES` per the tables in TRACKERS.md |

Stage maps, fixed:

| Vendor | awake | core | deep | rem | asleep | unspecified |
|---|---|---|---|---|---|---|
| Google | AWAKE, RESTLESS | LIGHT | DEEP | REM | ASLEEP | other |
| Polar | 0 | 2, 3 | 4 | 1 | | 5 |
| Withings | 0 | 1 | 2 | 3 | | 4, 5 |

WHOOP and Garmin have no hypnogram; they only fill `SleepNight`.

## Per-adapter specifics

### `garmin.py`
- Hosts `personal.connectapi.garmin.com`. Token: `POST /personalapitoken-service/personal-api/personal-api-key/token`, header `Authorization: ApiKey <key>`, JWT cached in the token store with `expires_at` from the JWT `exp` (payload base64url-decoded, signature not checked; only used for scheduling). Renew when < 1 h remains.
- Calls per poll: activities list (stop at the cursor's `last_activity_id`), FIT download for each new activity (parse `record` messages for HR; a minimal FIT reader for record messages, field 3 heart_rate and 253 timestamp, is part of this module), sleep range, HRV per day in the window, daily summaries range, dailyHeartRate per day only when `restingValue` is missing, weight range, VO2max range.
- RHR `quality='sparse'` warning when only the min fallback exists. HRV readings → `field='qty'`.
- `backfill_chunk_days = 28`.

### `whoop.py`
- Hosts `api.prod.whoop.com`. `OAuthConfig(use_pkce=False, client_auth='body', state_len=8, scopes=(...,'offline'))`.
- Lists `/v2/cycle`, `/v2/recovery`, `/v2/activity/sleep`, `/v2/activity/workout` with `start`, `end`, `limit=25`, `nextToken`. Body daily.
- Only `score_state == 'SCORED'` produces rows. Recovery joins its sleep by `sleep_id`; HRV and RHR take the sleep's times. `nap == true` → skipped.
- HRV → `field='nightly'`, `active_kcal=None`, `hr_series=()`.
- `backfill_chunk_days = 90`.

### `google.py`
- Hosts `health.googleapis.com`, `oauth2.googleapis.com`, plus `accounts.google.com` for the authorize URL only. `OAuthConfig(use_pkce=True, extra_authorize={'access_type': 'offline', 'prompt': 'consent'})`.
- On 400 `FAILED_PRECONDITION` with `ACCOUNT_NOT_LINKED` raise `AuthRequired('account_not_linked')`.
- Sleep and exercise via `:reconcile`, page size 25. Sleep with `metadata.processed != true` is left out and keeps `complete=False` until processed.
- `dataSourceFamily` from config, default `google-wearables`. Skip records with `recordingMethod == 'MANUAL'` for HR, HRV and SpO2.
- HRV → `field='nightly'`. `backfill_chunk_days = 14` (the strictest rollup limit).

### `withings.py`
- Hosts `wbsapi.withings.net`, plus `account.withings.com` for authorize. `OAuthConfig(use_pkce=False, scope_sep=',', extra_token={'action': 'requesttoken'}, token_parser=withings_token)`; refresh with `action=requesttoken&grant_type=refresh_token`.
- Withings answers HTTP 200 with a JSON `status`; non-zero status codes map to `AuthRequired` (401-style codes) or `UpstreamError`. The code list is UNVERIFIED; treat any unknown non-zero status as `UpstreamError`.
- Cursor uses `lastupdate` (epoch s) per endpoint; window replacement still applies to the overlap.
- Sleep `get` at most 7 days per call. HRV `rmssd` series → `field='qty'`.

### `polar.py`
- Hosts `www.polaraccesslink.com`, `polarremote.com`, plus `flow.polar.com` for authorize. `OAuthConfig(use_pkce=False, client_auth='basic', scopes=('accesslink.read_all',))`. No refresh; `expires_at` from `expires_in`; `extra['x_user_id']` from the token response.
- `max_history_days = 28`; a gap longer than that since `last_ok` adds warning `polar:history_lost <dates>` and an alert.
- Nightly Recharge HRV → `field='nightly'`. Exercises with `samples=true`.

## Tests to write

`tests/trackers/` with fixtures under `tests/fixtures/trackers/<name>/`. Fixture JSON is hand-written from the vendor specs, never captured from the owner's account.

```python
class FakeTransport:
    def __init__(self, routes: Mapping[tuple[str, str], Sequence[Response]]) -> None
        # key (method, 'host/path'), values served in order; query params recorded, not matched
    calls: list[Request]
    def __call__(self, req: Request) -> Response   # unknown route -> AssertionError
def fixture_response(name: str, file: str, status: int = 200, headers: Mapping[str, str] = {}) -> Response
class FakeClock:   # .now, .advance(s), .sleep records waits instead of sleeping
class MemoryTokenStore(TokenStore)
```

Base:
- `HttpClient` refuses a host outside `allow_hosts` without calling the transport.
- 429 with `Retry-After: 7` sleeps 7 s then succeeds; 429 with `Retry-After: 900` raises `RateLimited` without sleeping; 503 four times raises `UpstreamError` after the jittered waits (seeded `rng`).
- `max_requests=3` and a fourth call raises `RateLimited(None)`.
- `pkce_challenge('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk') == 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'` (RFC 7636 appendix B).
- `wait_for_code` with a wrong `state` raises; with the right one returns the code; binds 127.0.0.1 only.
- `TokenManager` refreshes when 60 s remain, saves before returning, keeps the old refresh token when the response has none, and raises `AuthRequired` on `invalid_grant`. Two threads calling `access_token` at expiry cause exactly one refresh call.
- `FileTokenStore` refuses a root inside a git work tree; file mode is 0600.

Store and ingest:
- Migration 1 → 2 on a populated v1 database keeps every row and creates `tracker_state`.
- `replace_source_window` deletes a Google sleep row whose start moved by 5 min and inserts the new one; rows of other sources in the same window survive; a batch row outside the window raises.
- `put_daily` rejects `whoop.recovery` with `source='garmin'` and rejects `recovery` from a tracker.
- `parse_payload` with `api_sources={'garmin'}` drops a Garmin Connect workout from HAE and counts it.

Metrics:
- One WHOOP `nightly` HRV row in the sleep window gives `hrv_rmssd_ln = ln(v)`, `n=1`; two nightly rows from one source give `None, 'sparse'`.
- With `locked={'hrv_rmssd_ln': 'garmin'}` and only WHOOP data, the value is `None`.
- No key containing `.` reaches readiness (property test over a store with vendor rows).

Adapters, one file each:
- Garmin: JWT minted once for two polls inside 23 h and again after; `restingValue` fallback produces the sparse warning; activity datetimes become `+01:00` ISO from the GMT and local pair; weight 81500 becomes 81.5 kg; FIT fixture with 3 record messages gives a 3-point `hr_series`; owner name and lat/lon absent from `raw_json`.
- WHOOP: `state` is 8 chars; pagination over two pages with `nextToken`; `PENDING_SCORE` record yields no rows; a nap yields no `SleepNight`; `kilojoule` never reaches `active_kcal`; a rotating refresh writes the new refresh token before the next API call (assert call order in `FakeTransport.calls` and the store).
- Google: int64 strings parsed; `"-14400s"` offset applied; `ACCOUNT_NOT_LINKED` raises `AuthRequired`; unprocessed sleep keeps `complete=False`; LIGHT/DEEP/REM/AWAKE map to core/deep/rem/awake.
- Withings: `value=7125, unit=-2` gives 71.25 kg; HTTP 200 with non-zero `status` raises; `lastupdate` advances only after a complete run; `--manual` refused.
- Polar: hypnogram crossing midnight produces monotonic segments on the next date; a 30-day gap since `last_ok` warns `history_lost`; 403 on missing consents raises `AuthRequired('polar_consents')`.
- Every adapter: all rows carry `source == name`, all vendor keys are in `vendor_keys`, all timestamps parse with an offset, and a top-level schema break raises `SchemaError` while the cursor stays unchanged.

CLI:
- `tracker status --json` never contains a substring of the fake token or key.
- `tracker auth oura` exits non-zero with the `BLOCKED` reason.

## Open points to settle by hand before coding

1. Google: does a "Personal" consent screen grant the restricted Health scopes? If not, the adapter ships with weekly re-consent and an alert, or not at all.
2. WHOOP and Withings: is `http://127.0.0.1:<port>/callback` accepted as a redirect URI? If not, register an HTTPS redirect on the tailnet name served by Brainstem (`/oauth/<name>/callback`), which needs a small Brainstem route instead of the loopback listener.
3. Garmin: read the Personal T&C at key creation and record the version in `config/trackers.json`. If it bars sending derived data to LLM providers, Garmin values stay out of the packet.
4. Garmin `calendarDate`: confirm on a real night that it is the wake day.
5. HAE v2: confirm per-reading `source` strings exist with Summarize off, since `api_sources` filtering depends on them.

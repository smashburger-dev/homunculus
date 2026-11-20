# Runbook: the own box

Path (b) from [DEVIN_SETUP.md](DEVIN_SETUP.md): a Mac mini or Raspberry Pi runs Brainstem (`homunculus serve`) and Pineal (`homunculus tick`), keeps the SQLite store and the raw payloads, and optionally serves as a Devin Outpost. It also works without Devin at all: then Pineal runs every job and the dashboard is the only surface.

Commands below assume a dedicated user `homunculus`, the engine at `~/homunculus` and the ledger at `~/homunculus-ledger`. Replace paths to match.

## 1. Prepare the machine

- Disk encryption on: FileVault on macOS (`fdesetup status`), LUKS on the Pi. The SQLite store and the raw payloads hold health data, and Google and WHOOP terms require encryption at rest.
- Python 3.11 or newer (`python3 --version`). No packages to install.
- Tailscale installed and logged in, with the box and your phone in the same tailnet. ACLs limited to your own devices.
- A separate user `homunculus` for every process below, including the Outpost worker, so a Devin session on the box cannot read your other files.

## 2. Engine, ledger, secrets

```sh
git clone https://github.com/<you>/homunculus ~/homunculus
git clone git@github.com:<you>/homunculus-ledger-<you>.git ~/homunculus-ledger
```

Give the box a deploy key with write access to the ledger repo only. The box is the only machine that pushes the ledger on this path.

Turn on the leak-guard hooks in the engine clone, once per clone:

```sh
git -C ~/homunculus config core.hooksPath .githooks
```

`pre-commit` scans the staged blobs and `pre-push` scans every commit the remote does not have yet (`tools/leak_guard.py`), so health data or a secret cannot reach the public engine repo from this machine. Do the same in every other clone of the engine you commit from.

Secrets go into one env file outside every git tree, readable by `homunculus` only:

```sh
mkdir -p ~/.config/homunculus && chmod 700 ~/.config/homunculus
touch ~/.config/homunculus/env && chmod 600 ~/.config/homunculus/env
```

Its keys (values from the provider consoles; generate `HEALTH_TOKEN` with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`):

```sh
HOMUNCULUS_LEDGER=/Users/homunculus/homunculus-ledger
HEALTH_TOKEN=
ANTHROPIC_API_KEY=
HOMUNCULUS_OWNER_LOGIN=
HEALTH_OWNER_HOST=
HOMUNCULUS_MAILTO=
HOMUNCULUS_PUSH_URL=
HOMUNCULUS_PUSH_TOKEN=
```

`HOMUNCULUS_OWNER_LOGIN` is your Tailscale login; the dashboard compares it with the header `tailscale serve` adds. `HEALTH_OWNER_HOST` is the box's tailnet name (`<box>.<tailnet>.ts.net`); left empty, any `*.ts.net` host counts.

`HOMUNCULUS_PUSH_URL` and `HOMUNCULUS_PUSH_TOKEN` are the phone channel, and path (b) requires both. Pineal POSTs every owner text it sends (red flags, budget and failed-job notices, plan ratifications, and the coach messages when the box runs them) to the URL with the token as `Authorization: Bearer`, then keeps the copy in `mail/owner/inbox/`. Without the URL the texts land in that folder only and nothing rings on the phone; `tick --devin-path b` runs its jobs but exits 1, and `serve` and `doctor` warn about it. The URL must be `https://`. An ntfy topic works: create a topic with a long random name on a self-hosted ntfy server or on ntfy.sh, protect it with an access token (on ntfy.sh that needs an account; which plan allows it is UNVERIFIED), subscribe to it in the ntfy app, and put the token into `HOMUNCULUS_PUSH_TOKEN`. Without a token anyone who knows the topic name can read the alerts, and `doctor` warns. Urgent alerts go out at ntfy priority `urgent`, same-day alerts at `high`. When a push fails, Pineal does not mark the alert as delivered and retries on the next tick. The texts leave the box for the push server, so prefer a server you run.

Tracker tokens (Garmin, WHOOP and the rest) go to the Keychain or `$HOMUNCULUS_SECRETS`, never into this file's directory tree in git ([TRACKER_CONTRACT.md](TRACKER_CONTRACT.md)). Both stores encrypt at rest: the Keychain itself, the 0600 files through FileVault or LUKS. Neither isolates the tokens from other processes of the same user, so the separate `homunculus` user is the only boundary.

One wrapper loads the env file for every service, `~/.local/bin/homunculus-run`, mode 700:

```sh
#!/bin/sh
set -a
. "$HOME/.config/homunculus/env"
set +a
cd "$HOME/homunculus" && exec /usr/bin/env python3 -m homunculus "$@"
```

Check the setup:

```sh
~/.local/bin/homunculus-run doctor
```

`doctor` fails on a short token, a non-loopback bind host, a missing git repo, a bad roster, a missing first plan or a push URL that is not `https://`, and warns on stale data, budget and a missing push URL or push token.

## 3. Services

Four jobs run on the box:

| Job | Command | When |
|---|---|---|
| Brainstem | `homunculus-run serve` | always, restart on exit |
| Pineal | `homunculus-run tick` (with Devin path b: `homunculus-run tick --devin-path b`) | every minute |
| Ledger push | `git -C ~/homunculus-ledger push --quiet origin HEAD` | hourly |
| Outpost worker (optional) | `devin worker start --outpost=<name>` | always, restart on exit |

`tick` takes a lock, so a slow tick and the next one never overlap. `--devin-path b` leaves out the jobs the Devin Automations run on this box (`devin_layer.box_jobs`): the coach messages, the weekly review and the monthly report. Without Devin, use plain `tick`.

### macOS: launchd

`~/Library/LaunchAgents/local.homunculus.serve.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.homunculus.serve</string>
  <key>ProgramArguments</key>
  <array><string>/Users/homunculus/.local/bin/homunculus-run</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/Users/homunculus/Library/Logs/homunculus-serve.log</string>
</dict>
</plist>
```

`local.homunculus.tick.plist` is the same with `<string>tick</string>` (plus `<string>--devin-path</string><string>b</string>` with Devin), no `KeepAlive`, and `<key>StartInterval</key><integer>60</integer>`. `local.homunculus.push.plist` runs `/usr/bin/git -C /Users/homunculus/homunculus-ledger push --quiet origin HEAD` with `StartInterval` 3600. The Outpost plist runs `devin worker start --outpost=<name>` with `KeepAlive`.

Load each one as the `homunculus` user:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.homunculus.serve.plist
launchctl print gui/$(id -u)/local.homunculus.serve | head
```

Logs never hold payloads: Brainstem logs routes and status codes only.

### Raspberry Pi: systemd

`/etc/systemd/system/homunculus-serve.service`:

```ini
[Unit]
Description=homunculus Brainstem
After=network-online.target
Wants=network-online.target

[Service]
User=homunculus
ExecStart=/home/homunculus/.local/bin/homunculus-run serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/homunculus

[Install]
WantedBy=multi-user.target
```

`homunculus-tick.service` (`Type=oneshot`, `ExecStart=/home/homunculus/.local/bin/homunculus-run tick`, add `--devin-path b` with Devin) with `homunculus-tick.timer`:

```ini
[Unit]
Description=homunculus Pineal tick

[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s
Persistent=true

[Install]
WantedBy=timers.target
```

`homunculus-push.service` (`Type=oneshot`, `ExecStart=/usr/bin/git -C /home/homunculus/homunculus-ledger push --quiet origin HEAD`) with a timer on `OnCalendar=hourly`. `devin-outpost.service` like the serve unit with `ExecStart=devin worker start --outpost=<name>`.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now homunculus-serve.service homunculus-tick.timer homunculus-push.timer
journalctl -u homunculus-serve -n 50
```

## 4. Dashboard

Brainstem binds 127.0.0.1:8787 and serves `site/`, which Pineal rebuilds after every commit. Publish it inside the tailnet only:

```sh
tailscale serve --bg 8787
tailscale serve status
```

The flag syntax changed between Tailscale releases; check `tailscale serve --help` on your version (UNVERIFIED for older ones).

The login check keeps out other tailnet devices, not local processes. `tailscale serve` also connects from 127.0.0.1, so any process on the box can send `Tailscale-User-Login` and a `*.ts.net` Host to port 8787 and pass as you. Run nothing on the box that you would not let post check-ins and owner messages, and keep every service under the `homunculus` user. Open `https://<box>.<tailnet>.ts.net/site/` on the phone. Never run `tailscale funnel`: it puts the page and the ingest route on the public internet.

The Devin App option for the dashboard is in [devin/app/README.md](../devin/app/README.md).

## 5. Health Auto Export

The phone needs the Tailscale app connected. In Health Auto Export, three REST automations, all to `https://<box>.<tailnet>.ts.net/health` with the header `Authorization: Bearer <HEALTH_TOKEN>`:

1. Health metrics, JSON v2, Since Last Sync, Summarize off, Batch on, every hour.
2. Workouts with Include Workout Metrics on, Since Last Sync, every hour.
3. Catch-up over Previous 7 Days, once a day.

Summarize must stay off: `metrics.py` needs at least two SDNN readings inside the sleep window. iOS reads health data only while the phone is unlocked, with Background App Refresh on and Low Power Mode off, so overnight data arrives after the first unlock. Details and sources are in [TRACKERS.md](TRACKERS.md).

Import the history once, so baselines exist from day one instead of after about 67 days. Export everything from Health Auto Export as one JSON file, copy it to the box, then:

```sh
~/.local/bin/homunculus-run import-history /path/to/export.json
rm /path/to/export.json
```

## 6. Devin Outpost

With an Outpost, the Bots' sessions run their commands on the box: the same checkouts, the SQLite store and the env file. Inference stays in Devin's cloud; every command and file stays local. The machine only opens outbound connections.

1. Install the Devin CLI as the `homunculus` user and sign in (follow the Outposts page).
2. Register the box: `devin worker start --outpost=<name>`, then make it a service as in section 3.
3. In each Automation from `devin/automations/b/`, choose the Outpost as the machine (`runs_on: outpost`).
4. Start Pineal with `tick --devin-path b`.

Each session can read what the `homunculus` user can read, and whatever a session reads reaches Devin's cloud model. That includes the env file and the tracker tokens, because neither the Keychain nor the 0600 files separate processes of one user. The Bot instructions keep sessions on the commands; data-control opt-out must be on. On this path the Devin GitHub app needs no write access to the ledger, because the box pushes.

## 7. Backups

- The ledger: pushed hourly to the private GitHub repo. It holds aggregates, plans, reviews and the audit trail.
- The SQLite store: back it up daily with SQLite's online backup, never with a plain file copy while Brainstem writes:

  ```sh
  python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
      ~/homunculus-ledger/.private/health.sqlite /Volumes/<encrypted-backup>/health-$(date +%F).sqlite
  ```

  Keep the target encrypted (an encrypted Time Machine disk or an encrypted volume) and outside every git tree. Keep 14 daily copies.
- Raw payloads in `.private/raw/` are pruned after 30 days by the `ingest-prune` job; they need no backup beyond that.
- The env file and tracker tokens: in your password manager, not in a backup folder.

Restore: clone the ledger, copy the newest SQLite backup to `.private/health.sqlite`, recreate the env file, start the services, run `doctor`.

## 8. Daily operation

- `homunculus-run doctor`: one-line status per check.
- `homunculus-run pending`, `approve <id>`, `veto <id>`, `rollback <version>`: plan changes.
- `homunculus-run halt` stops everything except ingest and red flags; `resume` undoes it.
- An urgent red flag pauses coaching until you clear it. `homunculus-run coaching show` lists the holds; after a doctor has seen you, `coaching clear` lifts all of them, `coaching clear <rule_id>` one rule's.
- A fresh ledger needs a first plan before anything coaches or reviews: `homunculus-run init-plan` (starter plan, or `--from FILE`). `doctor` fails on `plan` until it exists.
- A failed job leaves `ops/incidents/<id>.json` (frames only, no locals). With `HOMUNCULUS_REPAIR_URL` and `HOMUNCULUS_REPAIR_SECRET` set, Pineal posts the scrubbed incident to a Devin repair Automation on the engine repo.
- Stale data (HRV or sleep older than 36 h, workouts older than 72 h) shows on the dashboard and in the coaches' messages. Check the phone's Health Auto Export log first.

# The dashboard as a Devin App

`dashboard.build` writes static HTML (inline CSS and SVG, no JavaScript framework, no CDN) into `site/` of the ledger checkout. A Devin App can serve that folder at a private URL. A `tailscale serve` page on the box is the fallback.

## What an App gives

From [docs/DEVIN_BOTS.md](../../docs/DEVIN_BOTS.md), read 2026-11-20:

- A stable URL, `https://<name>--<id>.preview.devinapps.com`.
- Always private: opening it needs a Devin sign-in. That is the access control, so the page needs no token of its own.
- SQLite on the App's own machine.
- It sleeps 5 minutes after the last request and wakes on the next one.
- Apps are still rolling out. If the account does not have them, use the fallback below.

## Setup

Ask the Hippocampus Bot to create the App, with these steps for its machine:

1. Clone the engine (read) and the ledger (read only; the App never pushes).
2. On every wake, and at most once an hour while awake:

   ```sh
   git -C "$HOMUNCULUS_LEDGER" pull --ff-only
   cd ~/homunculus && python3 -m homunculus dashboard
   ```

3. Serve `$HOMUNCULUS_LEDGER/site/` read-only, for example `python3 -m http.server --directory "$HOMUNCULUS_LEDGER/site" --bind 127.0.0.1 8080`, and point the App at that port. How an App maps a local port to its URL is not documented (UNVERIFIED); follow the App page.

Path (a): the App's SQLite starts empty whenever its machine is rebuilt. Run `python3 -m homunculus devin-restore` before `dashboard`; it calls `devin_layer.restore_store`, which rebuilds the store from `data/summaries/`, `data/daily/` and `checkins/` without writing to the ledger.

Path (b): the ledger in git holds the aggregates the dashboard reads, but raw-sample views (sparklines over the last days) need the box's SQLite. On path (b) prefer the fallback.

## What does not work in the App

- The check-in form posts to Brainstem's `/checkin`, which needs the owner header from `tailscale serve` or the token. Behind the App's Devin sign-in neither exists, so the form stays inert. Send check-ins through Vagus in chat instead.
- No write path at all: approvals go through Cerebellum's Decisions in Needs You.

## Fallback: tailscale serve on the box

The box runs `homunculus serve`, which serves `site/` on 127.0.0.1 next to the ingest routes. `tailscale serve` publishes it over HTTPS inside the tailnet only, and the dashboard checks the login header it adds against `HOMUNCULUS_OWNER_LOGIN`. Never use `tailscale funnel`, which makes the page public. Steps are in [docs/RUNBOOK.md](../../docs/RUNBOOK.md#4-dashboard).

GitHub Pages is out: a Pages site from a private repo is public on most plans.

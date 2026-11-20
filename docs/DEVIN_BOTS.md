# Devin Bots: what they can do for homunculus

Read on 2026-11-20 from the official docs, three days after the launch on 2026-11-17. Every claim links its page. Where a page does not say something, this file says "not documented".

## What a Bot is

A Bot owns an outcome, not a task. It has one chat, plans the work, starts a child Devin session per piece (each with its own machine), follows up, and reports back in the same chat. [bots](https://docs.devin.ai/work-with-devin/bots)

| Capability | What the docs say | Source |
|---|---|---|
| Persistence | One chat per Bot. "Recycle" ends the current session and a fresh one starts. | [bots](https://docs.devin.ai/work-with-devin/bots) |
| Instructions | People write them on the General page. The Bot reads them at the start of every session. | [bots](https://docs.devin.ai/work-with-devin/bots) |
| Memory | Files the Bot writes itself (preferences, corrections, project details). Everyone who can use the Bot can read them. | [bots](https://docs.devin.ai/work-with-devin/bots) |
| Automations | Schedules (iCalendar RRULE, own time zone), webhooks (`X-Webhook-Secret` header, payload up to 200 KB goes into the session prompt), Slack, Teams, GitHub, Linear. The Bot sets them up from chat and may ask for approval. Script steps keep a JSON `STATE_FILE` between runs and dispatch at most 10 new items per run by default, 50 at most. | [automations](https://docs.devin.ai/product-guides/automations) |
| Needs you | One list with these types: Decision (the Bot marks its pick), Review PR, Merge PR, Permission request, Report, FYI. | [bots](https://docs.devin.ai/work-with-devin/bots) |
| Bot to Bot | Granting Bot A access to Bot B lets A read B's conversation and sessions and message it. A creator's private Bots connect to each other without a prompt. | [access](https://docs.devin.ai/work-with-devin/bots/access-and-security) |
| Connections | Per-Bot MCP servers, plugins and secrets. A private Bot picks up its creator's sign-ins without asking. | [connections](https://docs.devin.ai/work-with-devin/bots/connections) |
| Chat surfaces | The web app; Slack, where each Bot gets its own app and handle; iMessage, where a number links to only one Bot at a time and Enterprise has no iMessage. | [slack](https://docs.devin.ai/work-with-devin/bots/slack), [imessage](https://docs.devin.ai/work-with-devin/bots/imessage) |
| Models | A Bot has a mode, and a "Child Capability" mode for the sessions it starts (Normal, Fast, Lite, Ultra, Fusion). The Cloud picker has model cards, and a session that picks a specific model stays on it. The Bot docs do not say whether a Bot can pin an exact model. | [bots](https://docs.devin.ai/work-with-devin/bots), [chatgpt billing](https://docs.devin.ai/admin/billing/chatgpt) |
| Billing | Bot usage is its chat plus every session it starts, billed to the creator. The usage-limits page covers Max, Teams and Enterprise only. Press says Pro does not include Bots (aitoolranked, not official). | [usage-limits](https://docs.devin.ai/work-with-devin/bots/usage-limits) |
| Bot API | Not documented. The API index lists sessions, automations, schedules, knowledge, playbooks and secrets, but no Bots endpoints. | [llms.txt](https://docs.devin.ai/llms.txt) |

## Two neighbours that matter

- **Devin Apps** are web apps that Devin serves from its own machine at a stable URL (`https://<name>--<id>.preview.devinapps.com`). They use SQLite on the same machine, sleep 5 minutes after the last request and wake on the next one. They are always private: opening one requires a Devin sign-in. Apps are still rolling out. [apps](https://docs.devin.ai/product-guides/devin-apps)
- **Outposts** run Devin sessions on your own machine, for example a Mac mini, through `devin worker start --outpost=<name>`. The machine only opens outbound connections. Inference stays in Devin's cloud, while every command and file runs locally. [outposts](https://docs.devin.ai/cloud/outposts/overview)

## What this means for the design

1. **Bots are the always-on shell. The judgement call runs through homunculus.** A Bot's session runs `python -m homunculus run-bot <name>`, which calls the exact model from `roster.json` with the API key the Bot holds as a secret. Fable 5.2, Opus 5.5 and Haiku 5.5 therefore stay pinned, whatever Devin mode the Bot runs in. All numbers come from code.
2. **Fewer Bots than roles.** A Bot pays for its chat and its sessions. The plan is four Devin Bots: Cerebellum, Sartorius, Vagus and Hippocampus. Hippocampus runs the bounded loop with Amygdala and Thalamus as API calls inside its weekly session. Retina, Spindle and Papilla become Automation scripts with `STATE_FILE` cursors, so a session starts only when something new arrives. Insula and Motor Cortex are API calls inside the Saturday job.
3. **Only one Bot can sit on iMessage.** That one should be the coach the owner talks to most. The other two user-facing Bots go on Slack, each with its own handle, in a private workspace.
4. **Ingest needs a decision.** An App sits behind a Devin sign-in, so Health Auto Export cannot POST to it. There are two paths:
   - No own server: Health Auto Export sends a daily summary (under 200 KB) to a Devin webhook automation. The session runs `homunculus ingest --stdin` and commits aggregates to the private ledger repo. It is cheap and simple but only daily and coarse.
   - Own box: a Mac mini or Raspberry Pi runs `homunculus serve` behind Tailscale Funnel or a tunnel and commits aggregates every hour. This gives full fidelity. The same box can be an Outpost, so raw data never leaves it.
5. **The dashboard fits a Devin App.** It needs a private URL with a sign-in, phone access and SQLite on the machine, and an App gives all three. A Tailscale-served page on the own box is the fallback while Apps are not enabled for the account.
6. **Privacy.** Whatever a Bot reads, Devin's cloud model reads too. Turn on data-control opt-out. Bots see aggregates and the ledger, never raw payloads.
7. **The Needs You list replaces our approval UI.** Plan changes in the approve tier become a Decision item with the recommended option marked. Red flags become a Report.

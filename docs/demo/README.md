# Demo dashboard

These seven pages are the static dashboard of `python3 -m homunculus demo`. Every value is synthetic and the person is fictional. Each page carries a banner that says so (`<p class="demo" data-synthetic="demo">`), next to the intended-purpose statement in the footer.

| Page | Content |
| --- | --- |
| `index.html` | Heute: readiness, today's session, metrics, data freshness |
| `plan.html` | current plan and its diff to the parent version |
| `week.html` | review thread of the week |
| `challenges.html` | open challenges and subjective/objective conflicts |
| `experiments.html` | N-of-1 experiments and the research frontier |
| `budget.html` | LLM spend per bot against the weekly cap |
| `checkin.html` | morning check-in form |

The pages have no JavaScript and load nothing from outside. All links are relative, so they work from any sub-path, for example `https://<user>.github.io/homunculus/demo/`. The check-in form posts to the relative URL `checkin`; a static host has no such endpoint, so submitting it saves nothing.

## How this copy was generated

```bash
python3 -m homunculus demo --out /tmp/hm-site
```

Defaults: 56 days, seed 7. This copy covers 2026-09-21 to 2026-11-15 with review week 2026-W46. The run uses `FakeProvider` for every LLM call and an in-process fake for Crossref, so it needs no API key and opens no socket. `demo.step_dashboard` calls `dashboard.build(..., demo=True)`, which adds the banner and the relative form action.

## Regenerate

From the repo root:

```bash
rm -rf /tmp/hm-site
python3 -m homunculus demo --out /tmp/hm-site
rm -f docs/demo/*.html
cp /tmp/hm-site/site/*.html docs/demo/
python3 tools/leak_guard.py
```

`--out` must be a new or empty directory outside the repo. The data window ends on the most recent Sunday in Europe/Berlin, so a later run shifts every date; the story (cold, travel week, data gap, strain days) stays the same for the same seed. Update the dates in the section above after regenerating.

## Publishing

GitHub Pages serves this folder at `/homunculus/demo/` when the repo's Pages source is the `main` branch, folder `/docs`.

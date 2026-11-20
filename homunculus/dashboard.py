"""Static HTML dashboard: Today, Plan, Week, Open challenges, Experiments, Budget, Check-in.

Every page is one self-contained HTML document with inline CSS and inline SVG
sparklines. There is no JavaScript and no external resource; a Content
Security Policy meta tag enforces that in the browser. Every value that comes
from data, plans or bot messages passes through ``html.escape``.

The render functions are pure. ``build`` reads the store and the ledger,
computes the day's inputs with the deterministic core and writes the pages
atomically. Every page shows the intended-purpose statement.
"""

from __future__ import annotations

import html
import json
import math
import os
import tempfile
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from homunculus import checkins, deepdive, lexicon, metrics, plans
from homunculus.guardrails import Alert, render_alert
from homunculus.metrics import MetricsConfig, Readiness
from homunculus.plans import PlanError
from homunculus.store import Ledger, Store

INTENDED_PURPOSE_EN: str = (
    "Personal training and lifestyle planning tool for one healthy adult, for personal "
    "non-commercial use. Not intended to diagnose, prevent, monitor, predict, treat or "
    "alleviate any disease or injury."
)
INTENDED_PURPOSE_DE: str = (
    "Persönliches Werkzeug zur Trainings- und Lebensstilplanung für eine gesunde erwachsene "
    "Person, zur persönlichen, nicht kommerziellen Nutzung. Nicht dazu bestimmt, Krankheiten "
    "oder Verletzungen zu diagnostizieren, zu verhüten, zu überwachen, vorherzusagen, zu "
    "behandeln oder zu lindern."
)

PAGES: tuple[tuple[str, str], ...] = (
    ("index.html", "Heute"),
    ("plan.html", "Plan"),
    ("week.html", "Woche"),
    ("challenges.html", "Einwände"),
    ("experiments.html", "Experimente"),
    ("budget.html", "Budget"),
    ("checkin.html", "Check-in"),
)

# Daily keys on the Today page: (key, German label, decimals, unit). Weight is
# left out on purpose: daily weight feedback is a known eating-disorder trigger.
TODAY_KEYS: tuple[tuple[str, str, int, str], ...] = (
    ("hrv_sdnn_ln", "HRV (ln SDNN)", 2, ""),
    ("rhr", "Ruhepuls", 0, " bpm"),
    ("tst_h", "Schlafdauer", 1, " h"),
    ("load_srpe", "Trainingslast (sRPE)", 0, ""),
    ("steps", "Schritte", 0, ""),
    ("resp_rate", "Atemfrequenz", 1, " /min"),
)
SERIES_DAYS = 28
ALERT_DAYS = 2  # today and yesterday

# ARCHITECTURE.md: HRV or sleep older than 36 h, workouts older than 72 h are stale.
FRESHNESS_THRESHOLDS_H: Mapping[str, float] = {
    "heart_rate_variability": 36.0,
    "sleep_segments": 36.0,
    "workouts": 72.0,
}
FRESHNESS_LABELS_DE: Mapping[str, str] = {
    "heart_rate_variability": "HRV", "sleep_segments": "Schlaf", "workouts": "Training",
}
_STALE_BLOCKING = ("heart_rate_variability", "sleep_segments")

BAND_DE: Mapping[str, str] = {"low": "niedrig", "normal": "normal", "high": "hoch"}
COMPONENT_DE: Mapping[str, str] = {
    "hrv": "HRV", "rhr": "Ruhepuls (umgekehrt)", "tst": "Schlafdauer",
    "midpoint_sd": "Schlafrhythmus (umgekehrt)", "wellness": "Wohlbefinden",
}
ACTION_DE: Mapping[str, str] = {
    "as_planned": "wie geplant", "shorten": "kürzen", "swap": "tauschen", "rest": "Ruhetag",
}
REASON_DE: Mapping[str, str] = {
    "red_flag": "Warnsignal aktiv", "no_session": "keine Einheit geplant",
    "stale_data": "Daten veraltet", "readiness_low": "Bereitschaft niedrig",
    "readiness_normal": "Bereitschaft normal", "readiness_high": "Bereitschaft hoch",
    "readiness_unknown": "Bereitschaft unbekannt", "swap_not_allowed": "Tausch nicht erlaubt",
    "no_lighter_session": "keine leichtere Einheit",
}
KIND_DE: Mapping[str, str] = {
    "review.draft": "Entwurf", "challenge": "Einwand", "rebuttal": "Antwort", "ruling": "Entscheidung",
    "plan.ratified": "Plan ratifiziert", "plan.rollback": "Plan zurückgesetzt",
    "ask.owner": "Frage an dich", "alert": "Warnung", "data.anomaly": "Datenauffälligkeit",
    "goal.update": "Ziel", "evidence.candidate": "Studienkandidat",
    "evidence.appraisal": "Studienbewertung", "checkin.received": "Check-in",
}
RULE_DE: Mapping[str, str] = {
    "R1_sustained": "Gefühl und Körperdaten mehrere Tage gegensätzlich",
    "R2_single_day": "Gefühl und Körperdaten an einem Tag gegensätzlich",
    "R3_session_residual": "Einheit härter empfunden als der Puls zeigt",
    "DATA_GAP": "Datenlücke",
}
SEVERITY_ORDER: Mapping[str, int] = {"blocker": 0, "major": 1, "minor": 2}
SEVERITY_DE: Mapping[str, str] = {"blocker": "Blocker", "major": "wichtig", "minor": "klein"}
DECISION_DE: Mapping[str, str] = {
    "adopt": "übernehmen", "reject": "verwerfen", "inconclusive": "unentschieden", "extend": "verlängern",
}
DIVE_DE: Mapping[str, str] = {"n_of_1_candidate": "N-of-1-Kandidat", "watch": "beobachten"}
VERDICT_DE: Mapping[str, str] = {"supported": "gestützt", "contested": "umstritten", "open": "offen"}
LEVEL_DE: Mapping[str, str] = {
    "normal": "normal", "reduced": "reduziert (ab 80 %)", "core_only": "nur Kernjobs (100 %)",
}
REDUCE_AT = 0.8

_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

_CSS = """
:root{color-scheme:light dark;--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#5f5f5a;
--line:#e3e3de;--accent:#0f766e}
@media (prefers-color-scheme:dark){:root{--bg:#121413;--card:#1a1d1c;--fg:#e6e6e1;
--muted:#a2a29b;--line:#2c302e;--accent:#5eead4}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
header,main,footer{max-width:46rem;margin:0 auto;padding:0 1rem}
header{padding-top:1rem}
nav{display:flex;flex-wrap:wrap;gap:.25rem .9rem;margin:.5rem 0 1rem}
nav a{color:var(--muted);text-decoration:none;padding:.2rem 0}
nav a[aria-current=page]{color:var(--fg);font-weight:600;border-bottom:2px solid var(--accent)}
h1{font-size:1.35rem;margin:.2rem 0}
h2{font-size:1.05rem;margin:0 0 .6rem}
h3{font-size:1rem;margin:.8rem 0 .3rem}
a{color:var(--accent)}
section,article{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:1rem;margin:0 0 1rem}
table{width:100%;border-collapse:collapse}
th,td{padding:.4rem .3rem;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:.85rem}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.muted{color:var(--muted)}
.big{font-size:1.6rem;font-weight:600;font-variant-numeric:tabular-nums}
.badge{display:inline-block;border:1px solid var(--accent);color:var(--accent);
border-radius:999px;padding:0 .55rem;font-size:.8rem;white-space:nowrap}
.alert{border-left:4px solid var(--fg);padding:.4rem .8rem;margin:.5rem 0}
.alert.urgent{border-left-width:8px;font-weight:600}
.spark,.bar{color:var(--accent);vertical-align:middle}
.body{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:.5rem 0}
code{overflow-wrap:anywhere}
ul{padding-left:1.2rem;margin:.3rem 0}
fieldset{border:1px solid var(--line);border-radius:8px;margin:0 0 .8rem;padding:.6rem .8rem}
fieldset label{display:inline-block;min-width:2.6rem;padding:.35rem .2rem}
textarea{width:100%;font:inherit;background:var(--bg);color:var(--fg);
border:1px solid var(--line);border-radius:8px;padding:.5rem}
button{background:var(--accent);color:var(--bg);border:0;border-radius:8px;
padding:.65rem 1.3rem;font:inherit;font-weight:600}
footer{color:var(--muted);font-size:.85rem;padding-bottom:2rem}
""".strip()


# --------------------------------------------------------------------------
# Formatting helpers


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(value: Any, digits: int = 1, unit: str = "") -> str:
    """German number with fixed decimals, '–' for missing or non-finite values."""
    if not _finite(value):
        return "–"
    text = f"{float(value):,.{digits}f}".replace(",", " ").replace(".", ",")
    return text + unit


def _signed(value: Any, digits: int = 2) -> str:
    text = _num(value, digits)
    return f"+{text}" if _finite(value) and value > 0 else text


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))


def _label(table: Mapping[str, str], key: Any) -> str:
    return table.get(str(key), str(key))


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]], num_cols: Sequence[int] = ()) -> str:
    """Rows are pre-escaped HTML cells."""
    def cell(tag: str, i: int, content: str) -> str:
        cls = ' class="num"' if i in num_cols else ""
        return f"<{tag}{cls}>{content}</{tag}>"
    head = "".join(cell("th", i, _e(h)) for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(cell("td", i, c) for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _section(title: str, inner: str) -> str:
    return f"<section><h2>{_e(title)}</h2>{inner}</section>"


def _p(text: str, cls: str = "muted") -> str:
    return f'<p class="{cls}">{_e(text)}</p>'


def _nav(current: str) -> str:
    links = (
        f'<a href="{name}"{" aria-current=page" if name == current else ""}>{_e(label)}</a>'
        for name, label in PAGES
    )
    return "<nav>" + "".join(links) + "</nav>"


def _page(current: str, title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
        '<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex,nofollow">'
        f"<title>{_e(title)} · homunculus</title><style>{_CSS}</style></head><body>"
        f"<header><h1>{_e(title)}</h1>{_nav(current)}</header>"
        f"<main>{body}</main>"
        f'<footer><p lang="de">{_e(INTENDED_PURPOSE_DE)}</p>'
        f'<p lang="en">{_e(INTENDED_PURPOSE_EN)}</p></footer>'
        "</body></html>\n"
    )


# --------------------------------------------------------------------------
# SVG


def _coord(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _points(values: Sequence[float | None], width: int, height: int) -> list[tuple[float, float] | None]:
    present = [v for v in values if v is not None]
    if not present:
        return [None] * len(values)
    lo, hi, pad = min(present), max(present), 2.0
    step = (width - 2 * pad) / max(len(values) - 1, 1)

    def y(v: float) -> float:
        return height / 2 if hi == lo else pad + (hi - v) / (hi - lo) * (height - 2 * pad)
    return [None if v is None else (pad + i * step, y(v)) for i, v in enumerate(values)]


def _runs(points: Sequence[tuple[float, float] | None]) -> list[list[tuple[float, float]]]:
    runs: list[list[tuple[float, float]]] = [[]]
    for pt in points:
        if pt is None:
            runs.append([])
        else:
            runs[-1].append(pt)
    return [r for r in runs if r]


def _run_svg(run: Sequence[tuple[float, float]]) -> str:
    if len(run) == 1:
        x, y = run[0]
        return f'<circle cx="{_coord(x)}" cy="{_coord(y)}" r="1.5" fill="currentColor"/>'
    pts = " ".join(f"{_coord(x)},{_coord(y)}" for x, y in run)
    return (f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.5" '
            'stroke-linejoin="round" stroke-linecap="round"/>')


def _svg(width: int, height: int, cls: str, inner: str) -> str:
    return (f'<svg class="{cls}" xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" aria-hidden="true" focusable="false">{inner}</svg>')


def _check_size(width: Any, height: Any) -> None:
    for value in (width, height):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("width and height must be positive integers")


def _last_marker(points: Sequence[tuple[float, float] | None]) -> str:
    last = next((pt for pt in reversed(points) if pt is not None), None)
    if last is None:
        return ""
    return f'<circle cx="{_coord(last[0])}" cy="{_coord(last[1])}" r="2.2" fill="currentColor"/>'


def sparkline_svg(values: Sequence[float | None], width: int = 120, height: int = 24) -> str:
    """Inline SVG line, oldest to newest; None and non-finite values leave gaps."""
    _check_size(width, height)
    points = _points([float(v) if _finite(v) else None for v in values], width, height)
    shapes = "".join(_run_svg(r) for r in _runs(points)) + _last_marker(points)
    return _svg(width, height, "spark", shapes)


def _zbar_svg(z: float, width: int = 90, height: int = 12, cap: float = 3.0) -> str:
    """Diverging bar for a capped z contribution around a centre line."""
    mid = width / 2
    span = max(-cap, min(cap, z)) / cap * (mid - 1)
    x, w = (mid, span) if span >= 0 else (mid + span, -span)
    inner = (f'<line x1="{_coord(mid)}" y1="0" x2="{_coord(mid)}" y2="{height}" stroke="currentColor" '
             f'stroke-opacity=".35"/><rect x="{_coord(x)}" y="3" width="{_coord(w)}" '
             f'height="{height - 6}" rx="1" fill="currentColor"/>')
    return _svg(width, height, "bar", inner)


def _share_svg(share: float, width: int = 200, height: int = 10) -> str:
    fill = max(0.0, min(1.0, share)) * width
    inner = (f'<rect width="{width}" height="{height}" rx="5" fill="currentColor" fill-opacity=".18"/>'
             f'<rect width="{_coord(fill)}" height="{height}" rx="5" fill="currentColor"/>')
    return _svg(width, height, "bar", inner)


# --------------------------------------------------------------------------
# Today


def _alert_html(alert: Alert) -> str:
    try:
        text = render_alert(alert, {})
    except (KeyError, ValueError):
        text = f"Warnsignal {alert.rule_id} am {alert.date}."
    label = "Dringend" if alert.level == "urgent" else "Heute klären"
    return f'<div class="alert {_e(alert.level)}" role="alert"><strong>{label}:</strong> {_e(text)}</div>'


def _alerts_section(alerts: Sequence[Alert]) -> str:
    if not alerts:
        return ""
    ordered = sorted(alerts, key=lambda a: (a.level != "urgent", a.date, a.rule_id))
    return _section("Warnsignale", "".join(_alert_html(a) for a in ordered))


def _readiness_section(rd: Readiness) -> str:
    if rd.score is None:
        missing = ", ".join(_label(COMPONENT_DE, k) for k in rd.missing) or "–"
        return _section("Bereitschaft", _p("Keine Bereitschaft berechnet: weniger als 3 Eingänge.")
                        + _p(f"Fehlt: {missing}"))
    head = (f'<p><span class="big">{_e(_signed(rd.score))}</span> '
            f'<span class="badge">{_e(_label(BAND_DE, rd.band))}</span></p>')
    rows = [(_e(_label(COMPONENT_DE, k)), _e(_signed(v)), _zbar_svg(v))
            for k, v in sorted(rd.contributions.items())]
    missing = _p("Fehlt: " + ", ".join(_label(COMPONENT_DE, k) for k in rd.missing)) if rd.missing else ""
    return _section("Bereitschaft", head + _table(("Eingang", "Beitrag", ""), rows, (1,)) + missing)


def _exercise_item(ex: Mapping) -> str:
    parts = [f"{ex.get('sets', '–')} × {ex.get('reps', '–')}"]
    if _finite(ex.get("load_kg")):
        parts.append(_num(ex["load_kg"], 1, " kg"))
    if _finite(ex.get("rir")):
        parts.append(f"RIR {_num(ex['rir'], 0)}")
    if ex.get("novel"):
        parts.append("neu")
    return f"<li><code>{_e(ex.get('exercise_id', '?'))}</code>: {_e(', '.join(parts))}</li>"


def _session_html(session: Mapping | None) -> str:
    if not session:
        return _p("Keine Einheit heute.")
    facts = (f"{session.get('kind', '?')}, {_num(session.get('duration_min'), 0, ' min')}, "
             f"Ziel-RPE {_num(session.get('target_rpe'), 0)}")
    items = "".join(_exercise_item(ex) for ex in session.get("exercises") or ())
    return f"<p>{_e(facts)}</p>" + (f"<ul>{items}</ul>" if items else "")


def _day_section(day_plan: Mapping) -> str:
    if not day_plan:
        return _section("Einheit", _p("Kein ratifizierter Plan vorhanden."))
    action = f'<p><span class="badge">{_e(_label(ACTION_DE, day_plan.get("action")))}</span></p>'
    reasons = ", ".join(_label(REASON_DE, r) for r in day_plan.get("reason_codes") or ())
    return _section("Einheit", action + _session_html(day_plan.get("session"))
                    + (_p(f"Grund: {reasons}") if reasons else ""))


def _series_of(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _metric_rows(daily: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    known = {k: (label, digits, unit) for k, label, digits, unit in TODAY_KEYS}
    order = [k for k, *_ in TODAY_KEYS if k in daily] + sorted(k for k in daily if k not in known)
    rows = []
    for key in order:
        label, digits, unit = known.get(key, (key, 1, ""))
        series = _series_of(daily[key])
        rows.append((_e(label), _e(_num(series[-1] if series else None, digits, unit)), sparkline_svg(series)))
    return rows


def _metrics_section(daily: Mapping[str, Any]) -> str:
    if not daily:
        return _section("Messwerte", _p("Keine Tageswerte."))
    return _section("Messwerte", _table(("Wert", "Heute", f"{SERIES_DAYS} Tage"), _metric_rows(daily), (1,)))


def _fresh_row(key: str, info: Mapping) -> tuple[str, str, str]:
    latest, age = info.get("latest"), info.get("age_h")
    if info.get("stale"):
        status = f"keine Daten seit {latest}" if latest else "keine Daten"
        state = f'<span class="badge">{_e(status)}</span>'
    else:
        state = _e("aktuell")
    return _e(_label(FRESHNESS_LABELS_DE, key)), _e(_num(age, 0, " h")), state


def _freshness_section(freshness: Mapping) -> str:
    rows = [_fresh_row(k, v) for k, v in sorted(freshness.items()) if isinstance(v, Mapping)]
    if not rows:
        return _section("Datenstand", _p("Kein Datenstand bekannt."))
    return _section("Datenstand", _table(("Quelle", "Alter", "Status"), rows, (1,)))


def render_today(daily: Mapping[str, Any], readiness: Readiness, day_plan: Mapping,
                 freshness: Mapping, alerts: Sequence[Alert]) -> str:
    """Today page. daily maps key -> values oldest to newest (last = today) or a scalar."""
    body = (_alerts_section(alerts) + _readiness_section(readiness) + _day_section(day_plan)
            + _metrics_section(daily) + _freshness_section(freshness))
    return _page("index.html", "Heute", body)


# --------------------------------------------------------------------------
# Plan


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _resolve(doc: Any, path: str) -> Any:
    """Value at a JSON pointer, None when it does not exist."""
    for token in (_unescape(t) for t in path.split("/")[1:]):
        if isinstance(doc, Mapping):
            doc = doc.get(token)
        elif isinstance(doc, list) and token.isdigit() and int(token) < len(doc):
            doc = doc[int(token)]
        else:
            return None
    return doc


def _diff_ops(parent: Mapping, plan: Mapping) -> list[dict]:
    def top(path: str) -> str:
        parts = path.split("/")
        return _unescape(parts[1]) if len(parts) > 1 else ""
    return [op for op in plans.compute_diff(parent, plan) if top(op["path"]) not in plans.PROTECTED_KEYS]


def _diff_section(plan: Mapping, parent: Mapping | None) -> str:
    if parent is None:
        return _section("Änderungen", _p("Kein Vorgänger."))
    ops = _diff_ops(parent, plan)
    title = f"Änderungen gegenüber v{int(parent.get('version', 0)):04d}"
    if not ops:
        return _section(title, _p("Keine inhaltlichen Änderungen."))
    rows = [(_e(op["op"]), f"<code>{_e(op['path'])}</code>",
             _e(_json(_resolve(parent, op["path"]))) if op["op"] != "add" else "–",
             _e(_json(op["value"])) if "value" in op else "–") for op in ops]
    return _section(title, _table(("Op", "Pfad", "Vorher", "Nachher"), rows))


def _plan_header(plan: Mapping) -> str:
    rows = [
        ("Version", f"v{int(plan.get('version', 0)):04d}"),
        ("Gültig ab", plan.get("effective_from") or "–"),
        ("Gültig bis", plan.get("valid_until") or "offen"),
        ("Autor", plan.get("author") or "–"),
        ("Freigegeben durch", plan.get("ratified_by") or "–"),
        ("Wochenlast (RPE × min)", _num(plans.weekly_load(plan), 0)),
    ]
    return _section("Überblick", _table(("Feld", "Wert"), ((_e(k), _e(v)) for k, v in rows)))


def _weekday(session: Mapping) -> str:
    wd = session.get("weekday")
    return plans.WEEKDAYS_DE[wd] if isinstance(wd, int) and 0 <= wd <= 6 else "?"


def _sessions_section(plan: Mapping) -> str:
    sessions = sorted(plan.get("sessions") or (), key=lambda s: (s.get("weekday", 9), str(s.get("id"))))
    if not sessions:
        return _section("Einheiten", _p("Keine Einheiten geplant."))
    inner = "".join(f"<h3>{_e(_weekday(s))} · <code>{_e(s.get('id', '?'))}</code></h3>{_session_html(s)}"
                    for s in sessions)
    return _section("Einheiten", inner)


def _nutrition_section(plan: Mapping) -> str:
    n = plan.get("nutrition") or {}
    rows = [
        ("Kalorienziel", _num(n.get("kcal_target"), 0, " kcal")),
        ("Protein", _num(n.get("protein_g_per_kg"), 1, " g/kg")),
        ("Hinweise", n.get("notes_de") or "–"),
    ]
    supps = [f"{s.get('name', '?')}: {_num(s.get('dose'), 1)} {s.get('unit', '')} "
             f"({s.get('tested_by') or 'ohne Prüfsiegel'})" for s in plan.get("supplements") or ()]
    items = "".join(f"<li>{_e(s)}</li>" for s in supps)
    return _section("Ernährung", _table(("Feld", "Wert"), ((_e(k), _e(v)) for k, v in rows))
                    + "<h3>Supplemente</h3>" + (f"<ul>{items}</ul>" if items else _p("Keine.")))


def render_plan(plan: Mapping, parent: Mapping | None) -> str:
    """Plan page: header, sessions, nutrition and the content diff to the parent version."""
    if not plan:
        return _page("plan.html", "Plan", _section("Plan", _p("Noch kein ratifizierter Plan.")))
    body = (_plan_header(plan) + _diff_section(plan, parent) + _sessions_section(plan)
            + _nutrition_section(plan))
    return _page("plan.html", "Plan", body)


# --------------------------------------------------------------------------
# Week and challenges


def _is_message(obj: Any) -> bool:
    return isinstance(obj, Mapping) and all(isinstance(obj.get(k), str) for k in ("id", "kind", "thread"))


# Turn order inside one review round; messages of one run share created_at.
_KIND_ORDER: Mapping[str, int] = {"review.draft": 0, "challenge": 1, "rebuttal": 2, "ruling": 3, "ask.owner": 4}


def _msg_key(msg: Mapping) -> tuple:
    rnd = msg.get("round")
    return (str(msg.get("created_at", "")), rnd if isinstance(rnd, int) else 0,
            _KIND_ORDER.get(str(msg.get("kind")), len(_KIND_ORDER)), str(msg.get("id")))


def _recipients(msg: Mapping) -> str:
    to = ", ".join(str(x) for x in msg.get("to") or ())
    cc = ", ".join(str(x) for x in msg.get("cc") or ())
    return f"{to} (cc {cc})" if cc else to


def _item_html(item: Any) -> str:
    if not isinstance(item, Mapping):
        return f"<li>{_e(item)}</li>"
    head = " · ".join(str(item[k]) for k in ("cid", "severity", "action") if item.get(k))
    text = item.get("claim") or item.get("change") or ""
    ref = f' <code>{_e(item["ref"])}</code>' if item.get("ref") else ""
    return f"<li><strong>{_e(head)}</strong> {_e(text if isinstance(text, str) else _json(text))}{ref}</li>"


def _message_html(msg: Mapping) -> str:
    meta = (f"{msg.get('from', '?')} → {_recipients(msg)} · Runde {msg.get('round', '–')} · "
            f"{msg.get('created_at', '')} · {msg.get('model', '')}")
    items = "".join(_item_html(it) for it in msg.get("items") or ())
    return (f"<article><h2>{_e(_label(KIND_DE, msg['kind']))}</h2>{_p(meta)}"
            f'<div class="body">{_e(msg.get("body", ""))}</div>'
            + (f"<ul>{items}</ul>" if items else "") + "</article>")


def render_week(messages: Sequence[Mapping]) -> str:
    """Review thread, oldest message first; bodies shown as escaped plain text."""
    msgs = sorted((m for m in messages if _is_message(m)), key=_msg_key)
    if not msgs:
        return _page("week.html", "Woche", _section("Review", _p("Noch kein Review-Thread.")))
    threads = ", ".join(sorted({m["thread"] for m in msgs}))
    body = _p(f"Thread: {threads} · {len(msgs)} Nachrichten") + "".join(_message_html(m) for m in msgs)
    return _page("week.html", "Woche", body)


def _answers(msg: Mapping) -> list[Mapping]:
    raw = list(msg.get("items") or ()) + list(msg.get("answers") or ())
    return [a for a in raw if isinstance(a, Mapping) and a.get("cid") is not None]


def _answered(msgs: Sequence[Mapping]) -> set[tuple[str, str]]:
    return {(m["thread"], str(a["cid"])) for m in msgs if m["kind"] == "rebuttal" for a in _answers(m)}


def _open_challenges(messages: Sequence[Mapping]) -> list[tuple[Mapping, Mapping]]:
    """(message, item) for every challenge item no rebuttal in the same thread answered."""
    msgs = [m for m in messages if _is_message(m)]
    done = _answered(msgs)
    found = [(m, it) for m in msgs if m["kind"] == "challenge" for it in m.get("items") or ()
             if isinstance(it, Mapping) and (m["thread"], str(it.get("cid"))) not in done]
    return sorted(found, key=lambda p: (SEVERITY_ORDER.get(str(p[1].get("severity")), 9), _msg_key(p[0])))


def _challenges_section(messages: Sequence[Mapping]) -> str:
    open_items = _open_challenges(messages)
    if not open_items:
        return _section("Offene Einwände", _p("Keine offenen Einwände."))
    rows = [(_e(_label(SEVERITY_DE, it.get("severity"))), _e(it.get("claim", "")),
             f"<code>{_e(it.get('ref', ''))}</code>",
             _e(f"{m['thread']} · {m.get('from', '?')} · {it.get('cid', '?')}")) for m, it in open_items]
    blockers = sum(1 for _, it in open_items if it.get("severity") == "blocker")
    head = _p(f"{len(open_items)} offen, davon {blockers} Blocker.")
    return _section("Offene Einwände", head + _table(("Schwere", "Einwand", "Beleg", "Herkunft"), rows))


def _span(window: Any) -> str:
    if not isinstance(window, Sequence) or isinstance(window, str) or not window:
        return "–"
    first, last = str(window[0]), str(window[-1])
    return first if first == last else f"{first} bis {last}"


def _conflicts_section(conflicts: Sequence[Mapping]) -> str:
    if not conflicts:
        return _section("Offene Konflikte", _p("Keine offenen Konflikte."))
    rows = [(_e(_label(RULE_DE, c.get("rule_id"))), _e(_span(c.get("window"))),
             _e(c.get("interpretation_de") or "–"), _e(c.get("question_for_owner_de") or "–")) for c in conflicts]
    head = _p(f"{len(conflicts)} Konflikte zwischen Gefühl und Körperdaten oder Datenlücken, erkannt von Insula.")
    return _section("Offene Konflikte", head + _table(("Regel", "Zeitraum", "Deutung", "Frage an dich"), rows))


def render_challenges(messages: Sequence[Mapping], conflicts: Sequence[Mapping] = ()) -> str:
    """Challenge items without an answer, blockers first; then the week's Insula conflicts.

    conflicts: data/conflicts/<week>.json entries, optionally joined with Insula's
    interpretation_de and question_for_owner_de.
    """
    body = _challenges_section(messages) + _conflicts_section([c for c in conflicts if isinstance(c, Mapping)])
    return _page("challenges.html", "Einwände", body)


# --------------------------------------------------------------------------
# Experiments


def _protocol_rows(p: Mapping) -> list[tuple[str, str]]:
    return [
        ("Intervention", f"{p.get('intervention', '?')} gegen {p.get('comparator', '?')}"),
        ("Zielgröße", f"{p.get('outcome_key', '?')} ({p.get('source', '?')})"),
        ("Design", f"{p.get('design', '?')}, {p.get('pairs', '?')} Paare à {p.get('block_days', '?')} Tage, "
                   f"{p.get('washout_days', '?')} Tage Auswaschung"),
        ("Start", str(p.get("start_date", "–"))),
    ]


def _result_rows(r: Mapping) -> list[tuple[str, str]]:
    return [
        ("Entscheidung", _label(DECISION_DE, r.get("decision"))),
        ("Mittlere Paardifferenz", f"{_signed(r.get('d_bar'), 3)} (SE {_num(r.get('se'), 3)})"),
        ("p-Wert", _num(r.get("p_value"), 3)),
        ("P(Nutzen) / P(Schaden)", f"{_num(r.get('p_benefit'), 2)} / {_num(r.get('p_harm'), 2)}"),
    ]


def _experiment_html(exp: Mapping) -> str:
    protocol = exp.get("protocol") or {}
    result = exp.get("result")
    status = "abgeschlossen" if result else "läuft"
    rows = _protocol_rows(protocol) + (_result_rows(result) if result else [])
    table = _table(("Feld", "Wert"), ((_e(k), _e(v)) for k, v in rows))
    diffs = (result or {}).get("pair_diffs") or ()
    spark = f"<p>Paardifferenzen {sparkline_svg(list(diffs))}</p>" if diffs else ""
    eid = exp.get("id") or protocol.get("id") or "?"
    return (f'<article><h2><code>{_e(eid)}</code> <span class="badge">{_e(status)}</span></h2>'
            f"{table}{spark}</article>")


def render_experiments(experiments: Sequence[Mapping], extra: str = "") -> str:
    """experiments: [{'id', 'protocol': protocol.json, 'result': result.json or None}]; extra: pre-rendered sections."""
    if not experiments:
        return _page("experiments.html", "Experimente", _section("Experimente", _p("Keine Experimente.")) + extra)
    ordered = sorted(experiments, key=lambda x: str(x.get("id", "")))
    return _page("experiments.html", "Experimente", "".join(_experiment_html(x) for x in ordered) + extra)


# --------------------------------------------------------------------------
# Frontier lane and lexicon


def _dive_row(rec: Mapping) -> tuple[str, ...]:
    claim, chain = rec.get("claim") or {}, rec.get("chain") or {}
    coverage = chain.get("coverage")
    return (_e(claim.get("intervention", "?")), _e(claim.get("outcome", "?")), _e(_label(DIVE_DE, rec.get("decision"))),
            _e(_num(chain.get("weakest"), 1)), _e(_num(coverage * 100 if _finite(coverage) else None, 0, " %")),
            _e("; ".join(str(r) for r in rec.get("reasons") or ()) or "–"))


def render_frontier(dives: Sequence[Mapping]) -> str:
    """Deep-dive records (evidence/deepdives/*.json) as one section."""
    if not dives:
        return ""
    head = _p("Neue Arbeiten mit niedriger Sicherheit, aber hohem Potenzial und sauberer Methodik. "
              "Code prüft die Wirkkette Glied für Glied.")
    rows = [_dive_row(d) for d in sorted(dives, key=lambda d: str(d.get("belief_id", "")))]
    headers = ("Intervention", "Zielgröße", "Entscheidung", "Schwächstes Glied", "Am Menschen gezeigt", "Gründe")
    return _section("Frontier", head + _table(headers, rows, (3, 4)))


def _node_label(nodes: Mapping, node: Any) -> str:
    entry = nodes.get(node)
    return str(entry.get("label")) if isinstance(entry, Mapping) and entry.get("label") else str(node)


def _link_row(nodes: Mapping, link: Mapping) -> tuple[str, ...]:
    return (_e(_node_label(nodes, link.get("src"))), _e(_node_label(nodes, link.get("dst"))),
            _e(_label(VERDICT_DE, link.get("verdict"))), _e(_num(link.get("support"), 1)),
            _e("ja" if link.get("human") else "nein"))


def render_lexicon(view: Mapping) -> str:
    """Link verdicts and open questions of evidence/lexicon/view.json as one section."""
    links = [x for x in (view.get("links") or {}).values() if isinstance(x, Mapping)]
    if not links:
        return ""
    nodes = view.get("nodes") or {}
    rows = [_link_row(nodes, x) for x in sorted(links, key=lambda x: str(x.get("src")))]
    table = _table(("Von", "Nach", "Urteil", "Stützung", "Mensch"), rows, (3,))
    return _section("Lexikon", table + _questions_html(view))


def _questions_html(view: Mapping) -> str:
    questions = [q for q in view.get("questions") or () if isinstance(q, Mapping)]
    items = "".join(f"<li>{_e(q.get('text', ''))}</li>" for q in questions)
    return f"<p>Offene Fragen:</p><ul>{items}</ul>" if items else ""


# --------------------------------------------------------------------------
# Budget


def _usd(row: Mapping) -> float:
    value = row.get("usd")
    return float(value) if _finite(value) else 0.0


def _tokens(row: Mapping, key: str) -> int:
    value = row.get(key)
    return int(value) if _finite(value) else 0


def _level(spent: float, cap: float) -> str:
    if cap <= 0 or spent >= cap:
        return "core_only"
    return "reduced" if spent / cap >= REDUCE_AT else "normal"


def _by_bot(rows: Sequence[Mapping]) -> list[tuple[str, float, int, int]]:
    acc: dict[str, list[float]] = {}
    for r in rows:
        slot = acc.setdefault(str(r.get("bot", "?")), [0.0, 0, 0])
        slot[0] += _usd(r)
        slot[1] += _tokens(r, "tokens_in")
        slot[2] += _tokens(r, "tokens_out")
    return sorted(((b, v[0], int(v[1]), int(v[2])) for b, v in acc.items()), key=lambda t: (-t[1], t[0]))


def _per_day(rows: Sequence[Mapping]) -> list[float]:
    acc: dict[str, float] = {}
    for r in rows:
        day = str(r.get("date", ""))
        acc[day] = acc.get(day, 0.0) + _usd(r)
    return [acc[d] for d in sorted(acc)]


def render_budget(budget_rows: Sequence[Mapping], cap_usd: float) -> str:
    """Spend of the given rows (normally this ISO week) against the weekly cap."""
    rows = [r for r in budget_rows if isinstance(r, Mapping)]
    spent = math.fsum(_usd(r) for r in rows)
    share = spent / cap_usd if cap_usd > 0 else 1.0
    head = (f'<p><span class="big">{_e(_num(spent, 2, " $"))}</span> '
            f'<span class="muted">von {_e(_num(cap_usd, 2, " $"))} ({_e(_num(share * 100, 0, " %"))})</span></p>'
            f"<p>{_share_svg(share)}</p>"
            f'<p>Stufe: <span class="badge">{_e(LEVEL_DE[_level(spent, cap_usd)])}</span></p>')
    days = _per_day(rows)
    trend = f"<p>Pro Tag {sparkline_svg(days)}</p>" if days else ""
    bots = [(_e(b), _e(_num(u, 2, " $")), _e(f"{i:,}".replace(",", " ")),
             _e(f"{o:,}".replace(",", " "))) for b, u, i, o in _by_bot(rows)]
    table = _table(("Bot", "Kosten", "Tokens ein", "Tokens aus"), bots, (1, 2, 3)) if bots else _p("Keine Aufrufe.")
    return _page("budget.html", "Budget", _section("Diese Woche", head + trend) + _section("Nach Bot", table))


# --------------------------------------------------------------------------
# Build: ledger and store reads


def _local_day(now: datetime, tz: str) -> str:
    moment = now if now.tzinfo else now.replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(ZoneInfo(tz)).date().isoformat()


def _shift(day: str, n: int) -> str:
    return (Date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _iso_week(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _series(store: Store, key: str, day: str) -> list[float | None]:
    rows = {r.date: r.value for r in store.daily(key, _shift(day, -(SERIES_DAYS - 1)), _shift(day, 1))}
    return [rows.get(_shift(day, k)) for k in range(-(SERIES_DAYS - 1), 1)]


def _readiness(store: Store, day: str, cfg: MetricsConfig) -> Readiness:
    r = metrics.baselines(store, day, cfg)["readiness"]
    return Readiness(r["score"], r["band"], dict(r["contributions"]), list(r["missing"]))


def _alert_from_row(row: Any) -> Alert | None:
    try:
        return Alert(str(row["level"]), str(row["rule_id"]), str(row["date"]), dict(row.get("data") or {}))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _recent_alerts(ledger: Ledger, day: str) -> list[Alert]:
    days = {_shift(day, -k) for k in range(ALERT_DAYS)}
    seen: dict[tuple[str, str], Alert] = {}
    for row in ledger.read_jsonl("data/alerts.jsonl"):
        alert = _alert_from_row(row)
        if alert is not None and alert.date in days:
            seen.setdefault((alert.rule_id, alert.date), alert)
    return list(seen.values())


def _day_plan(ledger: Ledger, day: str, rd: Readiness, fresh: Mapping, alerts: Sequence[Alert]) -> dict:
    try:
        plan = plans.current(ledger)
    except PlanError:
        return {}
    red = any(a.level == "urgent" or a.date == day for a in alerts)
    stale = any(fresh.get(k, {}).get("stale", True) for k in _STALE_BLOCKING)
    return plans.modulate_day(plan, Date.fromisoformat(day).weekday(), rd.band, red, stale)


def _today_html(store: Store, ledger: Ledger, now: datetime, cfg: MetricsConfig, day: str) -> str:
    daily = {key: _series(store, key, day) for key, *_ in TODAY_KEYS}
    rd = _readiness(store, day, cfg)
    fresh = metrics.freshness(store.latest_ts(), now, FRESHNESS_THRESHOLDS_H)
    alerts = _recent_alerts(ledger, day)
    return render_today(daily, rd, _day_plan(ledger, day, rd, fresh, alerts), fresh, alerts)



def _plan_pair(ledger: Ledger) -> tuple[dict, dict | None]:
    try:
        plan = plans.current(ledger)
    except PlanError:
        return {}, None
    parent = plan.get("parent")
    if not isinstance(parent, int):
        return plan, None
    try:
        return plan, plans.get(ledger, parent)
    except PlanError:
        return plan, None


def _read_json_safe(ledger: Ledger, rel: str) -> Any:
    try:
        return ledger.read_json(rel)
    except ValueError:  # json.JSONDecodeError and bad UTF-8 included
        return None


def _all_messages(ledger: Ledger) -> list[dict]:
    rels = ledger.glob("mail/*/inbox/*.json") + ledger.glob("reviews/*/*.json")
    found: dict[str, dict] = {}
    for rel in rels:
        obj = _read_json_safe(ledger, rel)
        if _is_message(obj):
            found.setdefault(obj["id"], obj)
    return list(found.values())


def _latest_review(messages: Sequence[Mapping]) -> list[Mapping]:
    threads = {m["thread"] for m in messages if m["thread"].startswith("review/")}
    if not threads:
        return []
    latest = max(threads)
    return [m for m in messages if m["thread"] == latest]


def _conflicts_week(ledger: Ledger, review: Sequence[Mapping]) -> str | None:
    """ISO week of the latest review thread, else of the latest conflicts file."""
    weeks = sorted(m["thread"].split("/", 1)[1] for m in review)
    if weeks:
        return weeks[-1]
    files = ledger.glob("data/conflicts/*.json")
    return files[-1].rsplit("/", 1)[-1][:-len(".json")] if files else None


def _insula_by_id(ledger: Ledger, week: str) -> dict[str, Mapping]:
    out = _read_json_safe(ledger, f"reviews/{week}/00-insula-reconciliation.json")
    items = out.get("items") if isinstance(out, Mapping) else None
    return {str(i.get("conflict_id")): i for i in items or () if isinstance(i, Mapping)}


def _week_conflicts(ledger: Ledger, review: Sequence[Mapping]) -> list[dict]:
    """Conflicts of that week, joined with Insula's interpretation and owner question."""
    week = _conflicts_week(ledger, review)
    rec = _read_json_safe(ledger, f"data/conflicts/{week}.json") if week else None
    if not isinstance(rec, Mapping):
        return []
    by_id = _insula_by_id(ledger, week)
    keys = ("interpretation_de", "question_for_owner_de")
    return [{**c, **{k: by_id.get(str(c.get("id")), {}).get(k) for k in keys}}
            for c in rec.get("conflicts") or () if isinstance(c, Mapping)]


def _experiments(ledger: Ledger) -> list[dict]:
    out = []
    for rel in ledger.glob("experiments/*/protocol.json"):
        folder = rel.rsplit("/", 1)[0]
        protocol = _read_json_safe(ledger, rel)
        if isinstance(protocol, Mapping):
            result = _read_json_safe(ledger, f"{folder}/result.json")
            out.append({"id": protocol.get("id") or folder.split("/")[-1], "protocol": protocol,
                        "result": result if isinstance(result, Mapping) else None})
    return out


def _frontier_html(ledger: Ledger) -> str:
    dives = [d for d in (_read_json_safe(ledger, rel) for rel in ledger.glob(f"{deepdive.DIVES_DIR}/*.json"))
             if isinstance(d, Mapping)]
    view = _read_json_safe(ledger, lexicon.VIEW_PATH)
    return render_frontier(dives) + render_lexicon(view if isinstance(view, Mapping) else {})


def _budget_inputs(ledger: Ledger, now: datetime) -> tuple[list[dict], float]:
    week = _iso_week(now)
    rows = [r for r in ledger.read_jsonl("ops/budget.jsonl") if r.get("week") == week]
    cap = (ledger.read_json("config/budget.json", default={}) or {}).get("weekly_cap_usd", 40.0)
    return rows, float(cap) if _finite(cap) else 40.0


def _write_atomic(path: Path, text: str) -> Path:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def build(store: Store, ledger: Ledger, out_dir: Path, now: datetime, cfg: MetricsConfig) -> list[Path]:
    """Render every page and write it atomically into out_dir; returns paths in PAGES order."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    day = _local_day(now, cfg.tz)
    review = _latest_review(_all_messages(ledger))
    pages = {
        "index.html": _today_html(store, ledger, now, cfg, day),
        "plan.html": render_plan(*_plan_pair(ledger)),
        "week.html": render_week(review),
        "challenges.html": render_challenges(review, _week_conflicts(ledger, review)),
        "experiments.html": render_experiments(_experiments(ledger), _frontier_html(ledger)),
        "budget.html": render_budget(*_budget_inputs(ledger, now)),
        "checkin.html": _page("checkin.html", "Check-in", f"<section>{checkins.morning_form_html(day)}</section>"),
    }
    return [_write_atomic(out / name, pages[name]) for name, _ in PAGES]

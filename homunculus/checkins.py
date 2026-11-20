"""Subjective scales: daily check-in items, session RPE and symptoms.

The owner answers a short morning form (Likert 1-7 items plus a 0-10
wellbeing item), rates each session on Borg CR-10, and may tick symptoms.
Coaches extract the same fields from chat as structured output. Every item is
stored as a ``CheckinItem`` stamped with ``WORDING_VERSION`` so a later change
of wording never mixes with old answers.

Validation fails closed: an out-of-range or malformed value rejects the whole
submission instead of being clamped. Personal z-scores compare today's answer
with the owner's own trailing baseline and are ``None`` when the baseline is
too thin.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from homunculus import stats
from homunculus.store import CheckinItem, Store

WORDING_VERSION: str = "de-1"


@dataclass(frozen=True)
class ItemSpec:
    key: str
    prompt_de: str
    lo: int
    hi: int
    inverted: bool


def _likert(key: str, prompt_de: str, inverted: bool = False) -> ItemSpec:
    return ItemSpec(key, prompt_de, 1, 7, inverted)


ITEMS: Mapping[str, ItemSpec] = MappingProxyType({spec.key: spec for spec in (
    _likert("sleep_quality", "Wie gut hast du geschlafen? (1 = sehr schlecht, 7 = sehr gut)"),
    _likert("energy", "Wie viel Energie hast du heute? (1 = gar keine, 7 = sehr viel)"),
    _likert("mood", "Wie ist deine Stimmung? (1 = sehr schlecht, 7 = sehr gut)"),
    _likert("soreness", "Wie stark ist dein Muskelkater? (1 = keiner, 7 = sehr stark)", inverted=True),
    _likert("stress", "Wie gestresst fühlst du dich? (1 = gar nicht, 7 = sehr stark)", inverted=True),
    _likert("motivation", "Wie viel Lust hast du heute auf Training? (1 = gar keine, 7 = sehr viel)"),
    ItemSpec("wellbeing", "Wie geht es dir heute insgesamt? (0 = sehr schlecht, 10 = ausgezeichnet)", 0, 10, False),
    ItemSpec("session_rpe", "Wie anstrengend war die Einheit insgesamt? (0 = Ruhe, 10 = maximal)", 0, 10, False),
)})

SESSION_ITEM = "session_rpe"
# Items answered once per day; session RPE is per workout and has no daily baseline.
DAILY_ITEMS: tuple[str, ...] = tuple(k for k in ITEMS if k != SESSION_ITEM)

SYMPTOMS: tuple[str, ...] = (
    "fever", "sore_throat", "chest_pain", "dizziness", "palpitations",
    "fainting", "short_breath", "dark_urine", "focal_bone_pain", "pain_3plus",
)
URGENT_SYMPTOMS: frozenset[str] = frozenset({"chest_pain", "fainting", "short_breath"})
SYMPTOM_LABELS_DE: Mapping[str, str] = MappingProxyType({
    "fever": "Fieber",
    "sore_throat": "Halsschmerzen",
    "chest_pain": "Schmerzen oder Druck in der Brust",
    "dizziness": "Schwindel",
    "palpitations": "Herzrasen oder Herzstolpern",
    "fainting": "Ohnmacht oder kurz weggetreten",
    "short_breath": "Starke Atemnot",
    "dark_urine": "Dunkler Urin nach dem Training",
    "focal_bone_pain": "Punktueller Knochenschmerz",
    "pain_3plus": "Schmerz seit 3 oder mehr Tagen",
})

SYMPTOM_PREFIX = "symptom:"
NOTE_ITEM = "note"
NOTE_MAX = 2000
REF_MAX = 200
MAX_BACKDATE_DAYS = 7
# Form keys that are not scale items: the form date (read by record), the
# workout a session RPE belongs to, ticked symptoms and the free-text note.
META_KEYS: frozenset[str] = frozenset({"date", "workout_id", "symptoms", NOTE_ITEM})

RESTRICTIVE_TERMS: tuple[str, ...] = (
    # German
    "nichts gegessen", "kaum gegessen", "fast nichts gegessen", "nichts essen",
    "hungern", "gehungert", "hungere", "mahlzeit ausgelassen", "mahlzeiten ausgelassen",
    "essen ausgelassen", "essen verdienen", "essen verdient", "zu fett", "fühle mich fett",
    "fett gefühlt", "kalorien kompensieren", "kalorien abtrainieren", "wieder abtrainieren",
    "abführmittel", "erbrechen nach dem essen", "nur wasser", "nicht essen dürfen",
    # English
    "didn't eat", "did not eat", "barely ate", "hardly ate", "skipped meals", "skip meals",
    "skipped a meal", "skipping meals", "starve", "starved", "starving", "earn my food",
    "earned my food", "burn it off", "feel fat", "felt fat", "too fat", "laxative",
    "laxatives", "purge", "purged", "purging", "not allowed to eat",
)

_TRUE = frozenset({"1", "on", "true", "yes", "ja"})
_FALSE = frozenset({"", "0", "off", "false", "no", "nein"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CheckinError(ValueError):
    """A check-in submission is malformed or out of range."""


# --------------------------------------------------------------------------
# Value parsing


def _scalar(key: str, raw: Any) -> Any:
    """Unwrap a single-element list (as from parse_qs); reject several values."""
    if not isinstance(raw, (list, tuple)):
        return raw
    if len(raw) > 1:
        raise CheckinError(f"{key}: expected one value, got {len(raw)}")
    return raw[0] if raw else None


def _coerce_text_number(key: str, raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise CheckinError(f"{key}: not a number: {raw!r}") from None


def _parse_number(key: str, raw: Any) -> float | None:
    """Integral finite number, or None for an empty answer."""
    if isinstance(raw, str):
        raw = _coerce_text_number(key, raw)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CheckinError(f"{key}: not a number: {raw!r}")
    value = float(raw)
    if not math.isfinite(value) or not value.is_integer():
        raise CheckinError(f"{key}: expected a whole number, got {raw!r}")
    return value


def _scale_value(spec: ItemSpec, raw: Any) -> float | None:
    value = _parse_number(spec.key, _scalar(spec.key, raw))
    if value is not None and not spec.lo <= value <= spec.hi:
        raise CheckinError(f"{spec.key}: {value:g} outside {spec.lo}..{spec.hi}")
    return value


def _truthy(key: str, raw: Any) -> bool:
    value = _scalar(key, raw)
    if value is None or isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE or text in _FALSE:
        return text in _TRUE
    raise CheckinError(f"{key}: expected a yes/no value, got {value!r}")


def _optional_text(key: str, raw: Any, limit: int) -> str | None:
    value = _scalar(key, raw)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CheckinError(f"{key}: expected text")
    text = value.strip()
    if len(text) > limit:
        raise CheckinError(f"{key}: longer than {limit} characters")
    return text or None


def _split_symptoms(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    parts = list(raw) if isinstance(raw, (list, tuple)) else [raw] if raw is not None else []
    if not all(isinstance(s, str) for s in parts):
        raise CheckinError("symptoms: expected a list of names")
    return [s.strip() for s in parts if s.strip()]


def _symptom_names(form: Mapping[str, Any]) -> list[str]:
    """Ticked symptoms in SYMPTOMS order, from 'symptoms' and 'symptom:<name>' keys."""
    named = set(_split_symptoms(form.get("symptoms")))
    unknown = sorted(named.difference(SYMPTOMS))
    if unknown:
        raise CheckinError(f"unknown symptom(s): {', '.join(unknown)}")
    for name in SYMPTOMS:
        key = SYMPTOM_PREFIX + name
        if key in form and _truthy(key, form[key]):
            named.add(name)
    return [s for s in SYMPTOMS if s in named]


# --------------------------------------------------------------------------
# Item construction

ItemFactory = Callable[..., CheckinItem]


def _factory(date: str, ts: str, origin: str) -> ItemFactory:
    def make(item: str, value: float | None = None, text: str | None = None, ref: str | None = None) -> CheckinItem:
        return CheckinItem(date, item, value, text, ref, ts, WORDING_VERSION, origin)
    return make


def _scale_items(form: Mapping[str, Any], make: ItemFactory) -> list[CheckinItem]:
    ref = _optional_text("workout_id", form.get("workout_id"), REF_MAX)
    out = []
    for key, spec in ITEMS.items():
        value = _scale_value(spec, form.get(key))
        if value is not None:
            out.append(make(key, value, ref=ref if key == SESSION_ITEM else None))
    return out


def _collect(form: Mapping[str, Any], *, date: str, ts: str, origin: str) -> list[CheckinItem]:
    _check_date(date)
    make = _factory(date, ts, origin)
    items = _scale_items(form, make)
    items += [make(SYMPTOM_PREFIX + name, 1.0) for name in _symptom_names(form)]
    note = _optional_text(NOTE_ITEM, form.get(NOTE_ITEM), NOTE_MAX)
    if note is not None:
        items.append(make(NOTE_ITEM, text=note))
    return items


def _check_date(day: Any) -> Date:
    if not isinstance(day, str) or not _ISO_DATE_RE.match(day):
        raise CheckinError(f"invalid date: {day!r}")
    try:
        return Date.fromisoformat(day)
    except ValueError:
        raise CheckinError(f"invalid date: {day!r}") from None


def _known_form_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    if key.startswith(SYMPTOM_PREFIX):
        return key[len(SYMPTOM_PREFIX):] in SYMPTOMS
    return key in ITEMS or key in META_KEYS


def validate(form: Mapping[str, Any], *, date: str, ts: str, origin: str = "form") -> list[CheckinItem]:
    """Turn a submitted form into CheckinItems, or raise CheckinError.

    Symptoms become items 'symptom:<name>' with value 1.0, free text the item
    'note'. Unknown keys, out-of-range values and empty submissions are errors.
    The form's own 'date' field is ignored here; ``record`` resolves it.
    """
    unknown = sorted(str(k) for k in form if not _known_form_key(k))
    if unknown:
        raise CheckinError(f"unknown field(s): {', '.join(unknown)}")
    items = _collect(form, date=date, ts=ts, origin=origin)
    if not items:
        raise CheckinError("empty check-in")
    return items


# --------------------------------------------------------------------------
# HTTP handler


def _local_now(now: datetime, tz: str) -> datetime:
    aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return aware.astimezone(ZoneInfo(tz))


def _form_day(raw: Any, today: Date) -> str:
    value = _scalar("date", raw)
    if value is None or value == "":
        return today.isoformat()
    day = _check_date(value)
    if not today - timedelta(days=MAX_BACKDATE_DAYS) <= day <= today:
        raise CheckinError(f"date {value} outside the last {MAX_BACKDATE_DAYS} days")
    return day.isoformat()


def record(store: Store, form: Mapping[str, Any], now: datetime, tz: str) -> tuple[int, dict]:
    """POST /checkin handler: (http_status, {'stored': n, 'symptoms': [...]}).

    The check-in date is the form's 'date' field when it lies within the last
    MAX_BACKDATE_DAYS local days, else today's local date in ``tz``. A naive
    ``now`` is taken as UTC. Invalid input stores nothing and returns 400.
    """
    local = _local_now(now, tz)
    try:
        day = _form_day(form.get("date"), local.date())
        items = validate(form, date=day, ts=local.isoformat(timespec="seconds"))
    except CheckinError as exc:
        return 400, {"error": str(exc)}
    stored = store.put_checkins(items)
    return 200, {"stored": stored, "symptoms": symptom_flags(items)}


# --------------------------------------------------------------------------
# Coach structured output


def _coach_schema() -> dict:
    props: dict[str, Any] = {
        key: {"type": "integer", "minimum": spec.lo, "maximum": spec.hi, "description": spec.prompt_de}
        for key, spec in ITEMS.items()
    }
    props["workout_id"] = {"type": "string", "minLength": 1, "maxLength": REF_MAX,
                           "description": "Workout id the session_rpe belongs to"}
    props["symptoms"] = {"type": "array", "items": {"type": "string", "enum": list(SYMPTOMS)},
                         "uniqueItems": True}
    props[NOTE_ITEM] = {"type": "string", "maxLength": NOTE_MAX}
    return {"type": "object", "properties": props, "additionalProperties": False}


COACH_CHECKIN_SCHEMA: dict = _coach_schema()
_COACH_KEYS = frozenset(COACH_CHECKIN_SCHEMA["properties"])


def from_chat(extracted: Mapping[str, Any], *, date: str, ts: str) -> list[CheckinItem]:
    """CheckinItems with origin 'coach' from a coach's 'checkin' output.

    Unknown keys are ignored. Out-of-range values raise CheckinError instead
    of being clamped. Nothing extracted yields an empty list.
    """
    known = {k: v for k, v in extracted.items() if k in _COACH_KEYS}
    return _collect(known, date=date, ts=ts, origin="coach")


# --------------------------------------------------------------------------
# Morning form


def _e(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _scale_fieldset(spec: ItemSpec) -> str:
    options = "".join(
        f'<label><input type="radio" name="{_e(spec.key)}" value="{n}"> {n}</label>'
        for n in range(spec.lo, spec.hi + 1)
    )
    return f"<fieldset><legend>{_e(spec.prompt_de)}</legend>{options}</fieldset>"


def _symptom_fieldset() -> str:
    boxes = "".join(
        f'<label><input type="checkbox" name="symptoms" value="{_e(name)}"> {_e(SYMPTOM_LABELS_DE[name])}</label>'
        for name in SYMPTOMS
    )
    return f"<fieldset><legend>Beschwerden heute (nur falls zutreffend)</legend>{boxes}</fieldset>"


def morning_form_html(date: str, action: str = "/checkin") -> str:
    """Escaped static HTML morning form. Works without JavaScript."""
    parts = [
        f'<form method="post" action="{_e(action)}" accept-charset="utf-8" '
        f'class="checkin" data-wording="{_e(WORDING_VERSION)}">',
        f"<h2>Morgen-Check-in {_e(date)}</h2>",
        f'<input type="hidden" name="date" value="{_e(date)}">',
        *(_scale_fieldset(ITEMS[key]) for key in DAILY_ITEMS),
        _symptom_fieldset(),
        f'<label>Notiz (optional)<textarea name="{NOTE_ITEM}" maxlength="{NOTE_MAX}" rows="3"></textarea></label>',
        '<button type="submit">Speichern</button>',
        "</form>",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Scores and flags


def _min_baseline(window: int) -> int:
    """Baseline days required: half the window, at least 3."""
    return max(3, math.ceil(window / 2))


def _daily_series(rows: Sequence[CheckinItem]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        if row.item in DAILY_ITEMS and row.value is not None:
            out.setdefault(row.item, {})[row.date] = row.value
    return out


def _item_z(spec: ItemSpec, by_date: Mapping[str, float], day: str, min_n: int) -> float | None:
    today = by_date.get(day)
    base = [v for d, v in by_date.items() if d != day]
    if today is None or len(base) < min_n:
        return None
    z = stats.zscore(today, stats.mean(base), stats.sd(base))
    if z is None:
        return None
    return 0.0 - z if spec.inverted else z


def subjective_scores(store: Store, day: str, window: int = 28) -> dict[str, float | None]:
    """Per daily item: z of today's answer vs days -window..-1, capped at +-3.

    Inverted items (soreness, stress) are sign-flipped so that higher always
    means better. None when today's answer is missing, the baseline has fewer
    than max(3, ceil(window/2)) days, or the baseline has no spread.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    d = _check_date(day)
    since = (d - timedelta(days=window)).isoformat()
    until = (d + timedelta(days=1)).isoformat()
    series = _daily_series(store.checkins(since, until))
    min_n = _min_baseline(window)
    return {key: _item_z(ITEMS[key], series.get(key, {}), day, min_n) for key in DAILY_ITEMS}


def composite(scores: Mapping[str, float | None]) -> float | None:
    """Mean of the non-None scores; None with fewer than 3."""
    values = [v for v in scores.values() if v is not None]
    return stats.mean(values) if len(values) >= 3 else None


def symptom_flags(items: Sequence[CheckinItem]) -> list[str]:
    """Symptom names reported (value > 0) in items, in SYMPTOMS order."""
    present = {
        it.item[len(SYMPTOM_PREFIX):]
        for it in items
        if it.item.startswith(SYMPTOM_PREFIX) and it.value
    }
    return [s for s in SYMPTOMS if s in present]


def _terms_pattern(terms: Sequence[str]) -> re.Pattern[str]:
    folded = sorted({t.casefold() for t in terms}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in folded) + r")(?!\w)")


_RESTRICTIVE_RE = _terms_pattern(RESTRICTIVE_TERMS)


def restrictive_language(text: str) -> bool:
    """True if text contains a RESTRICTIVE_TERMS phrase (case-insensitive, whole words)."""
    if not text:
        return False
    normalized = " ".join(text.replace("’", "'").casefold().split())
    return _RESTRICTIVE_RE.search(normalized) is not None

"""Coaching holds from red-flag alerts, shared by Pineal (scheduler, chat) and the dashboard.

An urgent alert pauses coaching until the owner confirms a doctor has seen them
(a row in COACHING_CLEARED_LOG). A prompt alert holds for its rule's
PROMPT_HOLD_DAYS, other prompt rules for ALERT_DAYS.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import timedelta
from types import MappingProxyType
from typing import Mapping

from homunculus.store import Ledger

ALERTS_LOG = "data/alerts.jsonl"
COACHING_CLEARED_LOG = "data/coaching_cleared.jsonl"  # owner-confirmed doctor clearance per urgent alert
ALERT_DAYS = 2  # today and yesterday count as active
# Days a prompt alert keeps the session at rest, counting its own day; other prompt rules get ALERT_DAYS
# (the alert day and the next morning). Urgent alerts hold until cleared in COACHING_CLEARED_LOG.
PROMPT_HOLD_DAYS: Mapping[str, int] = MappingProxyType({"dark_urine": 3, "focal_bone_pain": 7})


def _shift(day: str, n: int) -> str:
    return (Date.fromisoformat(day) + timedelta(days=n)).isoformat()


def alert_key(row: Mapping) -> tuple[str, str]:
    return str(row.get("rule_id")), str(row.get("date"))


def _age_days(row: Mapping, day: str) -> int | None:
    try:
        return (Date.fromisoformat(day) - Date.fromisoformat(str(row.get("date"))[:10])).days
    except ValueError:
        return None


def holds(row: Mapping, day: str, cleared: set[tuple[str, str]]) -> bool:
    """An urgent alert holds until cleared; a prompt alert for its rule's PROMPT_HOLD_DAYS."""
    age = _age_days(row, day)
    if age is None or age < 0:
        return False
    if row.get("level") == "urgent":
        return alert_key(row) not in cleared
    return age < PROMPT_HOLD_DAYS.get(str(row.get("rule_id")), ALERT_DAYS)


def cleared(ledger: Ledger) -> set[tuple[str, str]]:
    return {alert_key(r) for r in ledger.read_jsonl(COACHING_CLEARED_LOG)}


def recent_alerts(ledger: Ledger, day: str) -> list[dict]:
    """Alert rows of the last ALERT_DAYS days plus every alert still holding; 'hold' marks the latter."""
    days = {_shift(day, -k) for k in range(ALERT_DAYS)}
    done = cleared(ledger)
    out = []
    for r in ledger.read_jsonl(ALERTS_LOG):
        hold = holds(r, day, done)
        if hold or str(r.get("date")) in days:
            out.append({**r, "hold": hold})
    return out


def coaching_holds(ledger: Ledger, day: str) -> list[dict]:
    """Urgent alerts that pause coaching on day until the owner clears them."""
    return [a for a in recent_alerts(ledger, day) if a["hold"] and a.get("level") == "urgent"]

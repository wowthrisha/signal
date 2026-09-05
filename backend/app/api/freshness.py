"""How old is the bar behind this card — measured in **sessions, never clock time**.

The source of truth is the card's `session_date` compared against
`max(session_date)` in `bar`. It is not `datetime.now()`, and the difference is
not pedantry:

  * On a Monday morning the newest bar is Friday's. That is **two calendar days**
    old and **zero sessions** behind — the freshest data that exists. A
    wall-clock rule marks it STALE and cries wolf every week.
  * Diwali, Holi and every other exchange holiday do the same thing on an
    irregular calendar no `timedelta` encodes.
  * Over a long weekend the gap is three or four days and still zero sessions.

The exchange calendar is not derived from a weekday rule either. It is read
from the sessions that actually exist in `bar`, so a holiday is simply a date
that is absent — the same convention `normalize.loader.load_sessions` uses.

Thresholds live in `configs/freshness.json`, not in the template, so the policy
is inspectable and changeable without touching render code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "freshness.json"

FRESH = "FRESH"
DELAYED = "DELAYED"
STALE = "STALE"
UNKNOWN = "UNKNOWN"

# States that come from the pipeline and override a freshness verdict. A bar we
# do not believe is not "fresh" in any useful sense, and a corporate action we
# could not adjust for makes the return itself wrong — saying FRESH next to
# either would be answering a question nobody asked.
STATUS_STALE = "STALE"
STATUS_UNADJUSTED = "CORP_ACTION_UNADJUSTED"
OVERRIDE_STATUSES = (STATUS_STALE, STATUS_UNADJUSTED)


@dataclass(frozen=True)
class Policy:
    """Session-distance cut points. Loaded from config; defaults match it."""

    fresh_max_sessions_behind: int = 0
    delayed_max_sessions_behind: int = 2

    @classmethod
    def load(cls, path: Path | None = None) -> "Policy":
        p = Path(path) if path else CONFIG_PATH
        if not p.is_file():
            return cls()
        raw = json.loads(p.read_text())
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in fields})

    def as_dict(self) -> dict:
        return {
            "fresh_max_sessions_behind": self.fresh_max_sessions_behind,
            "delayed_max_sessions_behind": self.delayed_max_sessions_behind,
        }


def sessions_behind(
    session_date: date | None,
    calendar: Sequence[date],
) -> int | None:
    """How many *sessions* separate this bar from the newest one we hold.

    `calendar` is the ascending list of session dates present in `bar`. A date
    that is not on the calendar has no defined distance and returns None rather
    than a guess — that is a data problem and the card should say so.
    """
    if session_date is None or not calendar:
        return None
    try:
        idx = list(calendar).index(session_date)
    except ValueError:
        return None
    return (len(calendar) - 1) - idx


def classify(
    session_date: date | None,
    calendar: Sequence[date],
    *,
    status: str | None = None,
    policy: Policy | None = None,
) -> tuple[str, int | None]:
    """`(state, sessions_behind)`.

    A pipeline status of STALE or CORP_ACTION_UNADJUSTED wins outright: those
    describe whether the number can be trusted at all, which strictly precedes
    how recent it is.
    """
    if status in OVERRIDE_STATUSES:
        return status, sessions_behind(session_date, calendar)

    pol = policy or Policy.load()
    behind = sessions_behind(session_date, calendar)
    if behind is None:
        return UNKNOWN, None
    if behind <= pol.fresh_max_sessions_behind:
        return FRESH, behind
    if behind <= pol.delayed_max_sessions_behind:
        return DELAYED, behind
    return STALE, behind

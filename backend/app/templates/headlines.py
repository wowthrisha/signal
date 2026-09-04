"""Card headlines. **Templates only — no generation** (CLAUDE.md hard rule 7).

Every string a card can show is in this file, and every number inside one is
substituted from a field the pipeline already computed. Nothing here calls a
model, and nothing here computes a quantity: if a headline says "over 4
sessions", 4 came out of the CUSUM's `bars`, not out of a sentence.

The corporate-action headline is not a template at all — it is the exchange's
own `purpose` line, passed through verbatim. A dividend announcement already
has an authoritative wording and paraphrasing it would only add a way to be
wrong.
"""
from __future__ import annotations

EVENT_JUMP = "JUMP"
EVENT_DRIFT = "DRIFT"
EVENT_CORP_ACTION = "CORP_ACTION"
EVENT_MARKET_REGIME = "MARKET_REGIME"

# Deliberately descriptive, never predictive: these say what was observed, not
# what it implies. "What we do NOT claim" in CLAUDE.md is enforced here, in the
# only place the product speaks in sentences.
_JUMP = "Single-session move well outside this stock's own recent range"
_JUMP_CORROBORATED = "Single-session move on a day with a filed corporate action"
_DRIFT = "Sustained one-directional drift over {bars} sessions"
_DRIFT_SHORT = "Sustained one-directional drift"
_REGIME = "Market-wide move — {n_extreme} of {n_universe} symbols moved together"
_FALLBACK = "Movement recorded; no template for this event type"


def headline(
    event_type: str,
    payload: dict | None = None,
    *,
    purpose: str | None = None,
) -> str:
    """One sentence for one event. `purpose` is the exchange's own wording."""
    payload = payload or {}

    if event_type == EVENT_CORP_ACTION:
        return purpose.strip() if purpose else "Corporate action effective this session"

    if event_type == EVENT_JUMP:
        return _JUMP_CORROBORATED if int(payload.get("i") or 0) >= 2 else _JUMP

    if event_type == EVENT_DRIFT:
        bars = payload.get("bars")
        return _DRIFT.format(bars=bars) if bars else _DRIFT_SHORT

    if event_type == EVENT_MARKET_REGIME:
        return _REGIME.format(
            n_extreme=payload.get("n_extreme", "?"),
            n_universe=payload.get("n_universe", "?"),
        )

    return _FALLBACK

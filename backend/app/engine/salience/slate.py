"""The slate — which admitted cards actually reach the digest (§7, §10).

`tiers.classify` decides whether a symbol-session is *allowed* to be shown.
This module decides which of the allowed ones are *worth* a finite screen. The
two are kept apart on purpose: the gate is a statement about evidence, the
slate is a statement about attention, and only the second one is allowed to
drop something that genuinely cleared the bar.

Three rules, in order:

1. **One card per instrument.** A symbol that fired both a JUMP and a DRIFT on
   the same day is one thing that happened, not two. The better-tiered event
   wins, ties broken by `U`.
2. **Rank by tier, then `U` descending.** Exactly `tiers.order`'s key. Nothing
   is summed here either.
3. **At most `MAX_PER_SECTOR` per sector, then `MAX_CARDS` overall.** A sector
   that moved together produces correlated cards; without a cap, one bad day
   for banks fills the whole digest and the user learns one fact five times.

The sector cap is applied *before* the overall cap so that diversity survives
truncation rather than being whatever the top-5 happened to contain.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from app.engine.salience.tiers import TIER_RANK, TIER_SUPPRESSED

# §10's digest is a screenful, not a feed.
MAX_CARDS = 5
MAX_PER_SECTOR = 2

# Reasons a candidate did not become a card. These are the only values that
# appear in the digest's `filtered_reasons`, so they are named here rather than
# spelled inline at the call site.
REASON_EXPLAINED = "explained_by_market"
REASON_THRESHOLD = "below_threshold"
REASON_CONFIDENCE = "low_confidence"


@dataclass(frozen=True)
class Candidate:
    """One symbol-session that the pipeline already scored.

    Carries the trace, not a re-derivation: `tier` and `u` come from the stored
    event, so the slate cannot disagree with the gate that admitted it.
    """

    isin: str
    symbol: str
    sector_id: str | None
    tier: str
    u: float | None
    i: int
    c: float
    event_type: str
    session_date: object
    total_return: float | None
    explained_return: float | None
    residual: float | None
    headline: str = ""

    @property
    def admitted(self) -> bool:
        return self.tier in TIER_RANK and self.tier != TIER_SUPPRESSED

    @property
    def sort_key(self) -> tuple:
        # Tier, then U descending, then symbol so the order is total and the
        # digest is byte-identical across runs (the replay rule, applied to the
        # presentation layer).
        return (
            TIER_RANK.get(self.tier, 9),
            -(self.u if self.u is not None else -1.0),
            self.symbol,
        )


def collapse_by_instrument(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Rule 1. Best event per ISIN, by the same key the slate ranks on."""
    best: dict[str, Candidate] = {}
    for cand in candidates:
        cur = best.get(cand.isin)
        if cur is None or cand.sort_key < cur.sort_key:
            best[cand.isin] = cand
    return sorted(best.values(), key=lambda c: c.sort_key)


def build(
    candidates: Iterable[Candidate],
    *,
    max_cards: int = MAX_CARDS,
    max_per_sector: int = MAX_PER_SECTOR,
) -> tuple[list[Candidate], list[Candidate]]:
    """Return `(slate, dropped)`.

    `dropped` holds admitted candidates that a cap removed — they are not
    failures of evidence and the digest counts them under `below_threshold`
    only because there is no truthful "we ran out of room" bucket in the v1
    response shape. Keeping them separate here means that bucket can be split
    later without touching the ranking.
    """
    ranked = collapse_by_instrument(c for c in candidates if c.admitted)

    slate: list[Candidate] = []
    dropped: list[Candidate] = []
    per_sector: dict[str | None, int] = {}

    for cand in ranked:
        if len(slate) >= max_cards:
            dropped.append(cand)
            continue
        # A missing sector cannot be capped against — we do not know that two
        # unclassified symbols are correlated, and inventing a bucket for them
        # would silently cap them as if they were one sector.
        key = cand.sector_id
        if key is not None and per_sector.get(key, 0) >= max_per_sector:
            dropped.append(cand)
            continue
        slate.append(cand)
        if key is not None:
            per_sector[key] = per_sector.get(key, 0) + 1

    return slate, dropped

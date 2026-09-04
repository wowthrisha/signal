"""Deduplication key — spec §5.

    dedup_key = sha1(isin ‖ session_date ‖ event_type ‖ magnitude_bucket), UNIQUE

The spec writes concatenation; this module joins with "|". A separator is not
cosmetic: bare concatenation lets ("AB", "C") and ("A", "BC") collide, which for
an ISIN-plus-date key would silently merge two different instruments' events.
The separator cannot appear in any component (ISINs are alphanumeric, dates are
ISO, event types are uppercase identifiers, buckets are integers).

Bucketing exists so that a symbol re-detected at a slightly different magnitude
within the same session collapses to one row rather than flapping.
"""
from __future__ import annotations

import hashlib
import math
from datetime import date

# 1 % bands. A move is "the same event" if it lands in the same band.
BUCKET_WIDTH = 0.01
# Everything at or beyond 20 % is one bucket: past that the exact size does not
# change what the user is told, and finer buckets would only cause flapping.
MAX_BUCKET = 20


def magnitude_bucket(magnitude: float, width: float = BUCKET_WIDTH) -> int:
    """Signed magnitude -> non-negative bucket index.

    Uses |magnitude|: direction is carried by event_type and payload, so an up
    move and a down move of the same size are not the same event and must not
    share a bucket — that is what event_type in the key is for.
    """
    if magnitude is None or not math.isfinite(magnitude):
        return 0
    return min(int(abs(magnitude) / width), MAX_BUCKET)


def dedup_key(
    isin: str,
    session_date: date | str,
    event_type: str,
    bucket: int,
) -> str:
    """sha1 over the four identity components, hex-encoded."""
    parts = (str(isin), str(session_date), str(event_type), str(int(bucket)))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

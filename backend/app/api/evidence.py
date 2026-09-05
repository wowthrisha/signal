"""Evidence — the primary source behind a card. **Retrieval and provenance only.**

Nothing here generates, summarises or paraphrases. An evidence row records what
the exchange said, when it said it, when we fetched it, and a checksum of the
fields we derived the row from. A reader who distrusts a card can check the
source; that is the entire purpose.

Two honesty constraints shape the design, and both cost coverage:

**`url` is nullable and stays null for corporate actions.** The NSE
corporate-actions feed is a structured API listing with no per-record permalink.
There is a company homepage and there is a filtered listing page, and putting
either in a field labelled "view original" would be citation theatre — a link
that looks like a source, resolves to something else, and is worse than no link
because it manufactures confidence. So the field is null and the card says the
original is not linkable.

**`published_at_basis` exists because we do not have a filing timestamp.** The
feed carries an ex-date, not a publication time. Writing the ex-date into
`published_at` and saying nothing would imply we know when the company filed.
The basis field says `EX_DATE`, so the row states what its own timestamp means.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

# Source tiers, spec §10. Lower is closer to the primary record.
TIER_EXCHANGE = 1
TIER_COMPANY_IR = 2
TIER_REGULATOR = 3

SOURCE_NSE_CA = "NSE Corporate Actions"
DOC_TYPE_CA = "Corporate action record"

# What `published_at` actually is, per row.
BASIS_EX_DATE = "EX_DATE"
BASIS_FILED_AT = "FILED_AT"

VALID_BASES = (BASIS_EX_DATE, BASIS_FILED_AT)


class EvidenceValidationError(ValueError):
    """A row that would misrepresent its own provenance."""


@dataclass(frozen=True)
class Evidence:
    isin: str
    session_date: date
    event_type: str
    source_tier: int
    source_name: str
    document_type: str
    title: str
    published_at: datetime
    published_at_basis: str
    retrieved_at: datetime
    checksum: str
    url: str | None = None

    def validate(self) -> None:
        """Both timestamps are required and the basis must be declared.

        A row missing either timestamp is not "partial evidence" — it is a
        provenance claim that cannot be checked, which is worse than no row.
        """
        if self.published_at is None:
            raise EvidenceValidationError("published_at is required")
        if self.retrieved_at is None:
            raise EvidenceValidationError("retrieved_at is required")
        if self.published_at_basis not in VALID_BASES:
            raise EvidenceValidationError(
                f"published_at_basis must be one of {VALID_BASES}, "
                f"got {self.published_at_basis!r}"
            )
        if self.source_tier not in (TIER_EXCHANGE, TIER_COMPANY_IR, TIER_REGULATOR):
            raise EvidenceValidationError(f"invalid source_tier {self.source_tier!r}")
        if not self.title.strip():
            raise EvidenceValidationError("title is required")

    def as_dict(self) -> dict:
        return {
            "source_tier": self.source_tier,
            "source_name": self.source_name,
            "document_type": self.document_type,
            "title": self.title,
            "published_at": self.published_at.isoformat(),
            "published_at_basis": self.published_at_basis,
            "retrieved_at": self.retrieved_at.isoformat(),
            "url": self.url,
            "linkable": self.url is not None,
            "checksum": self.checksum,
        }


def checksum(*parts: object) -> str:
    """sha1 over the exact fields the row was derived from, "|"-joined.

    A separator is not cosmetic here for the same reason it is not in
    `engine.dedup`: bare concatenation lets two different field splits collide.
    """
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


def from_corp_action(
    *,
    isin: str,
    ex_date: date,
    purpose: str,
    ca_type: str,
    source: str,
    ingested_at: datetime,
) -> Evidence:
    """Build one evidence row from a corporate-action record we already hold.

    No network call. This is a re-reading of the ingest we ran, which is the
    point: provenance should not require a second fetch that could disagree
    with the data the card was built from.
    """
    published = datetime.combine(ex_date, time(0, 0), tzinfo=timezone.utc)
    ev = Evidence(
        isin=isin,
        session_date=ex_date,
        event_type="CORP_ACTION",
        source_tier=TIER_EXCHANGE,
        source_name=SOURCE_NSE_CA,
        document_type=DOC_TYPE_CA,
        # The exchange's own subject line, verbatim. Not paraphrased.
        title=purpose.strip(),
        published_at=published,
        published_at_basis=BASIS_EX_DATE,
        retrieved_at=ingested_at,
        # No per-record permalink exists in this feed. See the module docstring.
        url=None,
        checksum=checksum(isin, ex_date, ca_type, purpose.strip(), source),
    )
    ev.validate()
    return ev


_INSERT = """
INSERT INTO evidence (isin, session_date, event_type, source_tier, source_name,
                      document_type, title, published_at, published_at_basis,
                      retrieved_at, url, checksum)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""

_SELECT = """
SELECT isin, session_date, source_tier, source_name, document_type, title,
       published_at, published_at_basis, retrieved_at, url, checksum
FROM evidence
WHERE isin = ANY(%s) AND session_date BETWEEN %s AND %s
ORDER BY isin, session_date, source_tier, checksum
"""

_CORP_ACTIONS = """
SELECT isin, ex_date, purpose, ca_type, source, ingested_at
FROM corp_action
ORDER BY isin, ex_date, purpose
"""


def backfill(conn) -> dict:
    """Populate `evidence` from the corporate actions already ingested.

    Idempotent — the checksum is the primary key component, so re-running
    inserts nothing new.
    """
    with conn.cursor() as cur:
        cur.execute(_CORP_ACTIONS)
        rows = cur.fetchall()
        written, skipped = 0, 0
        for isin, ex_date, purpose, ca_type, source, ingested_at in rows:
            try:
                ev = from_corp_action(
                    isin=isin, ex_date=ex_date, purpose=purpose,
                    ca_type=ca_type, source=source, ingested_at=ingested_at,
                )
            except EvidenceValidationError:
                skipped += 1
                continue
            cur.execute(_INSERT, (
                ev.isin, ev.session_date, ev.event_type, ev.source_tier,
                ev.source_name, ev.document_type, ev.title, ev.published_at,
                ev.published_at_basis, ev.retrieved_at, ev.url, ev.checksum,
            ))
            written += 1
    conn.commit()
    return {"corp_actions_read": len(rows), "evidence_rows": written,
            "skipped_invalid": skipped}


def load_for(conn, isins, start: date, end: date) -> dict[tuple[str, date], list[dict]]:
    """Evidence keyed by `(isin, session_date)`, for the digest to attach.

    Keyed without `event_type` so a JUMP on a corporate-action day still shows
    the filing: the card is about the instrument on the day, and the reader
    asking "why did this move?" wants the record that exists, not only one
    matching the detector's label.
    """
    if not isins:
        return {}
    out: dict[tuple[str, date], list[dict]] = {}
    with conn.cursor() as cur:
        cur.execute(_SELECT, (list(isins), start, end))
        for (isin, sd, tier, name, doc, title, pub, basis, ret, url, chk) in cur.fetchall():
            ev = Evidence(
                isin=isin, session_date=sd, event_type="", source_tier=tier,
                source_name=name, document_type=doc, title=title,
                published_at=pub, published_at_basis=basis, retrieved_at=ret,
                url=url, checksum=chk,
            )
            out.setdefault((isin, sd), []).append(ev.as_dict())
    return out

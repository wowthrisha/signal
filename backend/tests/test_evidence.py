"""The evidence layer records provenance and never invents it.

Two failure modes are worth more than coverage here:

  * a row that cannot substantiate its own timestamp claim, and
  * a link that looks like a source and is not one.

Both are tested directly, because both are the kind of defect that makes a
product *more* convincing while making it less true.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.api import digest as digest_api
from app.api import evidence as ev

NOW = datetime(2026, 9, 5, 3, 37, tzinfo=timezone.utc)


def _row(**over):
    base = dict(
        isin="INE745G01043", session_date=date(2026, 8, 28), event_type="CORP_ACTION",
        source_tier=ev.TIER_EXCHANGE, source_name=ev.SOURCE_NSE_CA,
        document_type=ev.DOC_TYPE_CA, title="Dividend - Rs 8 Per Share",
        published_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        published_at_basis=ev.BASIS_EX_DATE, retrieved_at=NOW,
        checksum="abc", url=None,
    )
    base.update(over)
    return ev.Evidence(**base)


# --- validation ------------------------------------------------------------

def test_a_row_without_published_at_fails_validation():
    with pytest.raises(ev.EvidenceValidationError, match="published_at"):
        _row(published_at=None).validate()


def test_a_row_without_retrieved_at_fails_validation():
    with pytest.raises(ev.EvidenceValidationError, match="retrieved_at"):
        _row(retrieved_at=None).validate()


def test_published_at_must_declare_what_it_is():
    """We have an ex-date, not a filing time. A row that does not say which is
    making a provenance claim it cannot support."""
    with pytest.raises(ev.EvidenceValidationError, match="published_at_basis"):
        _row(published_at_basis="GUESSED").validate()


def test_the_two_timestamps_are_distinct_fields():
    row = _row()
    assert row.published_at != row.retrieved_at
    d = row.as_dict()
    assert d["published_at"] != d["retrieved_at"]


def test_an_invalid_source_tier_is_rejected():
    with pytest.raises(ev.EvidenceValidationError, match="source_tier"):
        _row(source_tier=9).validate()


# --- construction from the feed we already have ----------------------------

def test_a_corp_action_row_carries_no_url_rather_than_a_stand_in():
    """The feed has no per-record permalink. A homepage in this field would be
    citation theatre — it looks like a source and resolves to something else."""
    row = ev.from_corp_action(
        isin="INE745G01043", ex_date=date(2026, 8, 28),
        purpose="Dividend - Rs 8 Per Share", ca_type="DIVIDEND",
        source="nse_corporate_actions", ingested_at=NOW,
    )
    assert row.url is None
    assert row.as_dict()["linkable"] is False
    assert "nseindia.com" not in str(row.as_dict())


def test_the_title_is_the_exchange_wording_verbatim():
    row = ev.from_corp_action(
        isin="INE745G01043", ex_date=date(2026, 8, 28),
        purpose="  Dividend - Rs 8 Per Share  ", ca_type="DIVIDEND",
        source="nse_corporate_actions", ingested_at=NOW,
    )
    assert row.title == "Dividend - Rs 8 Per Share"


def test_the_basis_says_ex_date_not_filed_at():
    row = ev.from_corp_action(
        isin="INE745G01043", ex_date=date(2026, 8, 28), purpose="Bonus 1:1",
        ca_type="BONUS", source="nse_corporate_actions", ingested_at=NOW,
    )
    assert row.published_at_basis == ev.BASIS_EX_DATE
    assert row.published_at.date() == date(2026, 8, 28)


def test_the_checksum_changes_when_any_source_field_changes():
    a = ev.checksum("INE1", date(2026, 8, 28), "DIVIDEND", "Rs 8", "nse")
    b = ev.checksum("INE1", date(2026, 8, 28), "DIVIDEND", "Rs 9", "nse")
    assert a != b


# --- integration -----------------------------------------------------------

def test_backfill_is_idempotent(conn):
    first = ev.backfill(conn)
    assert first["corp_actions_read"] > 0, "no corporate actions to back-fill"
    second = ev.backfill(conn)
    assert first["corp_actions_read"] == second["corp_actions_read"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM evidence")
        n = cur.fetchone()[0]
    ev.backfill(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM evidence")
        assert cur.fetchone()[0] == n, "re-running the backfill duplicated rows"


def test_a_card_with_a_corporate_action_renders_evidence(conn):
    ev.backfill(conn)
    digest_api.seed_watchlist(conn)
    d = digest_api.build_digest(conn, lookback=5)
    assert d["cards"], (
        "no cards surfaced — every assertion below would pass on an empty list"
    )
    with_ev = [c for c in d["cards"] if c["evidence"]]
    assert with_ev, "no card carries evidence, so this test asserts nothing"
    row = with_ev[0]["evidence"][0]
    for field in ("source_tier", "source_name", "document_type", "title",
                  "published_at", "published_at_basis", "retrieved_at", "linkable"):
        assert field in row


def test_a_card_without_evidence_returns_an_empty_list_not_null(conn):
    """Empty is a truthful state — most price-detected movements have no filing
    behind them, which is what Tier C means. It must not be null, or the UI
    cannot tell "none" from "not loaded"."""
    ev.backfill(conn)
    digest_api.seed_watchlist(conn)
    d = digest_api.build_digest(conn)
    assert d["cards"], "no cards surfaced — the loop below would not execute"
    for card in d["cards"]:
        assert isinstance(card["evidence"], list)


# --- one row per document per session --------------------------------------
#
# The exchange sometimes lists the same document twice: a re-file minutes after
# the first, under a fresh sequence id. A card counting rows then states a
# filing count that is wrong, and a reader who opens both links gets the same
# PDF twice.
#
# The fixture is built here rather than read from the ingested data. `evidence`
# is not among conftest's SEED_TABLES, so the throwaway database holds none of
# it — the first version of these guards skipped silently and proved nothing.
# Building the rows also makes the duplicate deterministic instead of depending
# on whichever re-files the exchange happened to publish.

_SESSION = date(2026, 8, 5)
_URL_A = "https://nsearchives.nseindia.com/corporate/PROBE_A.pdf"
_URL_B = "https://nsearchives.nseindia.com/corporate/PROBE_B.pdf"

# `event_type` is NOT NULL and part of the primary key. The real ingest writes
# an empty string — the row is about the instrument on the day, not about a
# detector's label — and `load_for` reads it back as one.
_INSERT_EVIDENCE = """
INSERT INTO evidence (isin, session_date, event_type, source_tier, source_name,
                      document_type, title, published_at, published_at_basis,
                      retrieved_at, url, checksum)
VALUES (%s, %s, '', 1, 'NSE Corporate Announcements', %s, %s, %s, 'FILED_AT',
        %s, %s, %s)
"""


def _probe_isin(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT isin FROM instrument ORDER BY isin LIMIT 1")
        row = cur.fetchone()
    if row is None:
        pytest.skip("no instruments in this database")
    return row[0]


def _seed_duplicate(conn) -> tuple[str, datetime, datetime]:
    """Two rows for ONE document, filed 6 minutes apart under different
    sequence ids, plus a genuinely different document on the same session."""
    isin = _probe_isin(conn)
    early = datetime(2026, 8, 4, 14, 58, 7, tzinfo=timezone.utc)
    late = datetime(2026, 8, 4, 15, 5, 7, tzinfo=timezone.utc)
    retrieved = datetime(2026, 8, 6, 3, 0, 0, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM evidence WHERE isin = %s AND session_date = %s",
                    (isin, _SESSION))
        # The same URL twice. Different document_type and title, because that
        # is how the exchange actually re-files: a fresh announcement record
        # pointing at a document it already published.
        cur.execute(_INSERT_EVIDENCE, (isin, _SESSION, "Board Meeting",
                                       "Outcome of board meeting", early,
                                       retrieved, _URL_A, "probe-early"))
        cur.execute(_INSERT_EVIDENCE, (isin, _SESSION, "Press Release",
                                       "Outcome of board meeting", late,
                                       retrieved, _URL_A, "probe-late"))
        # A different document, same session, same boilerplate title — the row
        # that a title-based dedupe would have destroyed.
        cur.execute(_INSERT_EVIDENCE, (isin, _SESSION, "Press Release",
                                       "Outcome of board meeting", late,
                                       retrieved, _URL_B, "probe-other"))
    return isin, early, late


def test_the_same_document_is_never_listed_twice_for_one_session(conn):
    isin, _early, _late = _seed_duplicate(conn)
    rows = ev.load_for(conn, [isin], _SESSION, _SESSION)[(isin, _SESSION)]
    urls = [r["url"] for r in rows]
    assert len(urls) > 0
    assert urls.count(_URL_A) == 1, (
        f"the re-filed document is still listed {urls.count(_URL_A)} times"
    )
    assert len(rows) == 2, f"expected 2 documents, got {len(rows)}: {urls}"


def test_deduplication_keeps_the_earliest_publication(conn):
    """Publication is when the document first became public, and
    `temporal_relation` is derived from exactly that instant — keeping the
    later copy would move a filing across the PRECEDES/FOLLOWS boundary and
    change what the card says about ordering."""
    isin, early, late = _seed_duplicate(conn)
    assert early < late, "the fixture must carry two different instants"
    rows = ev.load_for(conn, [isin], _SESSION, _SESSION)[(isin, _SESSION)]
    kept = [r for r in rows if r["url"] == _URL_A]
    assert len(kept) == 1
    assert kept[0]["published_at"] == early.isoformat(), (
        f"kept the {kept[0]['published_at']} copy, not the earliest at "
        f"{early.isoformat()}"
    )
    # And the surviving row is the early one throughout, not a hybrid.
    assert kept[0]["document_type"] == "Board Meeting"


def test_nothing_that_is_not_a_duplicate_is_collapsed(conn):
    """The destructive failure mode. Deduplicating on `title` would have looked
    right — in the ingested database 867 groups share an (isin, session, title)
    and only 7 of those share a URL, so the rest are distinct documents behind
    the exchange's boilerplate wording. The fixture reproduces exactly that
    shape: two documents, one title."""
    isin, _early, late = _seed_duplicate(conn)
    rows = ev.load_for(conn, [isin], _SESSION, _SESSION)[(isin, _SESSION)]
    urls = {r["url"] for r in rows}
    assert urls == {_URL_A, _URL_B}, (
        f"a distinct document was collapsed away: {urls}"
    )
    titles = {r["title"] for r in rows}
    assert titles == {"Outcome of board meeting"}, (
        "the fixture no longer exercises the shared-title case"
    )


def test_a_row_without_a_url_is_never_collapsed(conn):
    """A corporate-action row carries no URL. Two of them are not shown to be
    the same document by having nothing to compare, so they are both kept."""
    isin = _probe_isin(conn)
    when = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM evidence WHERE isin = %s AND session_date = %s",
                    (isin, _SESSION))
        for n in ("one", "two"):
            cur.execute(_INSERT_EVIDENCE, (isin, _SESSION, "Corporate Action",
                                           f"Dividend {n}", when, when,
                                           None, f"probe-ca-{n}"))
    rows = ev.load_for(conn, [isin], _SESSION, _SESSION)[(isin, _SESSION)]
    assert len(rows) == 2, f"an unlinkable row was collapsed: {rows}"
    assert all(r["url"] is None and r["linkable"] is False for r in rows)


def test_the_stored_checksum_is_not_a_content_fingerprint(conn):
    """Why the key is the URL and not `checksum`.

    `checksum` is `sha1(isin|seq_id|exchdisstime|source)` — an identity for the
    announcement *record*, not a fingerprint of the document. The two rows in
    the fixture point at one PDF and carry different checksums, so a dedupe
    keyed on it removes nothing. Asserted rather than assumed, because it is
    the obvious thing for a later reader to reach for."""
    isin, _early, _late = _seed_duplicate(conn)
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*), count(DISTINCT checksum) FROM evidence
                       WHERE isin=%s AND session_date=%s AND url=%s""",
                    (isin, _SESSION, _URL_A))
        total, distinct = cur.fetchone()
    assert total == 2 and distinct == 2, (
        "the two records of one document no longer carry distinct checksums; "
        "if checksum has become a content hash, load_for's key should be "
        "revisited"
    )
    # The real ingest builds it the same way, from record identity only.
    assert ev.checksum("A", 1, "t", "s") != ev.checksum("A", 2, "t", "s")

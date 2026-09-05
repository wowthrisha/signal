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

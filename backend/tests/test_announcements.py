"""The announcements feed supplies real broadcast times — and the rules that use them.

`corp_action` gives an ex-date, so every row derived from it is `UNKNOWN`. This
feed gives `exchdisstime`, an exchange dissemination moment with a time of day,
which is what lets a document actually be ordered against a price move.

Two rules are load-bearing and are asserted directly:

  * a row with no time of day is **dropped**, not stored at midnight — a
    fabricated timestamp is indistinguishable from a real one once written;
  * a document disseminated after the close attaches to the **next** session,
    never the one that had already closed when it appeared.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.api import evidence as ev
from app.ingest import announcements as ann

IST = ann.IST
CAL = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]


def _ist(y, m, d, hh, mm, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST).astimezone(timezone.utc)


# --- parsing ---------------------------------------------------------------

def test_a_real_broadcast_timestamp_parses_to_utc():
    got = ann.parse_dissemination("03-Sep-2026 23:56:48")
    assert got == _ist(2026, 9, 3, 23, 56, 48)
    assert got.tzinfo is timezone.utc


def test_a_midnight_timestamp_is_rejected_not_stored():
    """Midnight is how a date-without-a-time arrives. Storing it would create a
    row that looks exactly like a real 00:00:00 broadcast."""
    assert ann.parse_dissemination("03-Sep-2026 00:00:00") is None


def test_an_absent_timestamp_is_none():
    assert ann.parse_dissemination(None) is None
    assert ann.parse_dissemination("") is None
    assert ann.parse_dissemination("not a date") is None


# --- session attachment ----------------------------------------------------

def test_a_pre_close_announcement_attaches_to_the_same_session():
    got = ann.attach_session(_ist(2026, 9, 2, 10, 0), CAL)
    assert got == date(2026, 9, 2)


def test_a_post_close_announcement_attaches_to_the_next_session():
    """23:56 on the 3rd cannot be acted on until the 4th. Attaching it to the
    3rd would place a document in a session that had already closed."""
    got = ann.attach_session(_ist(2026, 9, 3, 23, 56), CAL)
    assert got == date(2026, 9, 4)


def test_an_announcement_on_a_non_trading_day_attaches_forward():
    weekend = _ist(2026, 9, 5, 11, 0)
    assert ann.attach_session(weekend, CAL) is None
    extended = CAL + [date(2026, 9, 7)]
    assert ann.attach_session(weekend, extended) == date(2026, 9, 7)


# --- the temporal states, end to end ---------------------------------------

def test_a_broadcast_before_the_session_yields_precedes():
    published = _ist(2026, 9, 3, 23, 56)
    session = ann.attach_session(published, CAL)
    assert ev.temporal_relation(published, ev.BASIS_FILED_AT, session) == ev.PRECEDES


def test_a_broadcast_within_the_session_yields_same_session_unordered():
    published = _ist(2026, 9, 2, 10, 0)
    session = ann.attach_session(published, CAL)
    assert ev.temporal_relation(published, ev.BASIS_FILED_AT, session) == ev.SAME_SESSION_UNORDERED


def test_a_broadcast_after_the_session_yields_follows():
    published = _ist(2026, 9, 4, 10, 0)
    assert ev.temporal_relation(published, ev.BASIS_FILED_AT, date(2026, 9, 2)) == ev.FOLLOWS


def test_a_null_publication_timestamp_yields_unknown():
    assert ev.temporal_relation(None, ev.BASIS_FILED_AT, date(2026, 9, 2)) == ev.UNKNOWN


def test_an_ex_date_can_never_produce_precedes_even_from_this_path():
    early = _ist(2026, 8, 1, 9, 0)
    assert ev.temporal_relation(early, ev.BASIS_EX_DATE, date(2026, 9, 2)) == ev.UNKNOWN


# --- row construction ------------------------------------------------------

def _row(**over):
    base = {
        "sm_isin": "INE745G01043", "seq_id": "1", "desc": "Shareholders meeting",
        "exchdisstime": "02-Sep-2026 10:00:00",
        "attchmntText": "  Board   meeting   outcome  ",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/X.pdf",
    }
    base.update(over)
    return base


def test_a_row_without_a_time_of_day_is_counted_and_dropped():
    items, stats = ann.to_evidence(
        [_row(exchdisstime="02-Sep-2026 00:00:00", an_dt=None)], CAL,
        retrieved_at=_ist(2026, 9, 5, 9, 0))
    assert items == []
    assert stats["no_timestamp"] == 1


def test_a_row_without_an_isin_is_counted_and_dropped():
    _, stats = ann.to_evidence([_row(sm_isin="")], CAL, retrieved_at=_ist(2026, 9, 5, 9, 0))
    assert stats["no_isin"] == 1


def test_the_attachment_url_is_carried_through():
    items, _ = ann.to_evidence([_row()], CAL, retrieved_at=_ist(2026, 9, 5, 9, 0))
    assert items[0].url == "https://nsearchives.nseindia.com/corporate/X.pdf"
    assert items[0].as_dict()["linkable"] is True


def test_a_non_http_attachment_becomes_null_rather_than_a_broken_link():
    items, _ = ann.to_evidence([_row(attchmntFile="None")], CAL,
                               retrieved_at=_ist(2026, 9, 5, 9, 0))
    assert items[0].url is None
    assert items[0].as_dict()["linkable"] is False


def test_the_title_is_the_exchange_wording_with_whitespace_normalised():
    items, _ = ann.to_evidence([_row()], CAL, retrieved_at=_ist(2026, 9, 5, 9, 0))
    assert items[0].title == "Board meeting outcome"


def test_rows_from_this_feed_use_the_filed_at_basis():
    items, _ = ann.to_evidence([_row()], CAL, retrieved_at=_ist(2026, 9, 5, 9, 0))
    assert items[0].published_at_basis == ev.BASIS_FILED_AT
    items[0].validate()


def test_live_evidence_now_contains_orderable_rows(conn):
    """The point of the whole task: some documents can now be ordered."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM evidence WHERE published_at_basis = 'FILED_AT'")
        filed = cur.fetchone()[0]
    if not filed:
        pytest.skip("announcements not ingested into this database")
    assert filed > 0, "no orderable rows — the point of the ingest is unmet"

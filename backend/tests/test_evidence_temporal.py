"""Did the documentary evidence exist before the market move?

The question this file guards is narrower than it looks, and the narrowness is
the point. A corporate action's **ex-date** is when the action takes effect. The
**announcement** that moved the price typically precedes it by weeks. So an
ex-date carries no information about when the document was published, and using
it to order a filing against a price move would manufacture a claim the data
cannot support.

Hence the central rule, asserted directly below: an ex-date alone can never
produce `PRECEDES`. It produces `UNKNOWN`, however convenient the alternative
would be.

The second thing guarded here is language. Evidence sharing a symbol and a
session with a movement is *association*. Nothing in this system establishes
causation, so no rendered string may imply it — and `FOLLOWS` in particular must
read as a disclaimer, since a document published after a move cannot be what the
move reflected.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.api import evidence as ev

SESSION = date(2026, 8, 28)
STATIC = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"


def _at(d: date, hh: int = 9, mm: int = 15) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)


# --- the rule the whole module exists for ----------------------------------

def test_an_ex_date_alone_can_never_become_precedes():
    """Even when the ex-date is days before the move. An effective date is not
    a filing date and must not be laundered into one."""
    earlier = _at(date(2026, 8, 20))
    assert ev.temporal_relation(earlier, ev.BASIS_EX_DATE, SESSION) == ev.UNKNOWN


def test_an_ex_date_on_the_session_is_unknown_not_same_session():
    """Sharing a date with the move is not evidence the *document* did. The
    document's publication time is still unknown."""
    assert ev.temporal_relation(_at(SESSION), ev.BASIS_EX_DATE, SESSION) == ev.UNKNOWN


def test_a_missing_publication_timestamp_is_unknown():
    assert ev.temporal_relation(None, ev.BASIS_FILED_AT, SESSION) == ev.UNKNOWN


def test_a_missing_session_is_unknown():
    assert ev.temporal_relation(_at(SESSION), ev.BASIS_FILED_AT, None) == ev.UNKNOWN


def test_an_unrecognised_basis_is_unknown():
    assert ev.temporal_relation(_at(SESSION), "GUESSED", SESSION) == ev.UNKNOWN


# --- the three states a real filing timestamp can produce ------------------

def test_a_filing_on_an_earlier_session_precedes():
    rel = ev.temporal_relation(_at(date(2026, 8, 27)), ev.BASIS_FILED_AT, SESSION)
    assert rel == ev.PRECEDES


def test_a_filing_on_a_later_session_follows():
    rel = ev.temporal_relation(_at(date(2026, 8, 31)), ev.BASIS_FILED_AT, SESSION)
    assert rel == ev.FOLLOWS


def test_a_filing_on_the_same_session_is_unordered_not_precedes():
    """Bars are end-of-day. Same-session means we cannot see which came first
    inside the session, and PRECEDES would assert intra-session ordering we
    have no data for — not even when the filing timestamp is 09:15."""
    rel = ev.temporal_relation(_at(SESSION, 9, 15), ev.BASIS_FILED_AT, SESSION)
    assert rel == ev.SAME_SESSION_UNORDERED


# --- wording ---------------------------------------------------------------

@pytest.mark.parametrize("relation,expected", [
    (ev.PRECEDES, "Published before the move"),
    (ev.SAME_SESSION_UNORDERED, "Same session; ordering unknown"),
    (ev.FOLLOWS, "Published after the move"),
    (ev.UNKNOWN, "Timing unknown"),
])
def test_each_state_renders_its_exact_wording(relation, expected):
    assert ev.TEMPORAL_LABEL[relation] == expected
    assert expected in STATIC.read_text(), "the label is not wired into the page"


CAUSAL = [
    r"\bcaused by\b", r"\bthe reason for\b", r"\bbecause of this (?:filing|document)\b",
    r"\bexplains the move\b", r"\bdue to this\b", r"\bresulted in\b",
]


@pytest.mark.parametrize("relation", [ev.PRECEDES, ev.SAME_SESSION_UNORDERED,
                                      ev.FOLLOWS, ev.UNKNOWN])
def test_no_state_label_makes_a_causal_claim(relation):
    label = ev.TEMPORAL_LABEL[relation].lower()
    for pattern in CAUSAL:
        assert not re.search(pattern, label), f"{relation} label is causal: {label!r}"


def test_follows_cannot_render_as_a_causal_explanation():
    """A document published after a move cannot be what the move reflected.
    The page must say so rather than presenting it as an explanation."""
    page = STATIC.read_text()
    assert "cannot be what the movement reflected" in page
    block = page[page.index("function temporalHTML"):]
    block = block[:block.index("function evidenceHTML")]
    for pattern in CAUSAL:
        assert not re.search(pattern, block, re.I), f"causal language near FOLLOWS: {pattern}"


def test_the_page_disclaims_causation_for_the_ordered_states():
    assert "does not claim this document caused the move" in STATIC.read_text()


# --- serialisation ---------------------------------------------------------

def test_the_relation_is_derived_not_stored():
    """A stored column would be a second copy of a pure function of three
    columns already present, and the two could drift."""
    row = ev.Evidence(
        isin="INE1", session_date=SESSION, event_type="CORP_ACTION",
        source_tier=1, source_name="x", document_type="y", title="t",
        published_at=_at(date(2026, 8, 20)), published_at_basis=ev.BASIS_EX_DATE,
        retrieved_at=_at(date(2026, 9, 5)), checksum="c",
    )
    d = row.as_dict()
    assert d["temporal_relation"] == ev.UNKNOWN
    assert d["temporal_label"] == "Timing unknown"


def test_every_live_evidence_row_is_unknown_because_no_filing_times_exist(conn):
    """The finding, asserted: this database holds zero publication timestamps,
    so no document can be ordered against a move. If a feed with real filing
    times is ingested, this test is expected to fail and should be updated
    deliberately."""
    with conn.cursor() as cur:
        cur.execute("SELECT published_at, published_at_basis, session_date FROM evidence")
        rows = cur.fetchall()
    if not rows:
        pytest.skip("no evidence rows in this database")
    relations = {ev.temporal_relation(p, b, sd) for p, b, sd in rows}
    assert relations == {ev.UNKNOWN}, f"unexpected relations present: {relations}"

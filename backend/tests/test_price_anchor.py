"""A price and the line beside it must describe the same session.

Both of the defects below shipped, and neither was visible in the markup —
they are properties of the payload, so they are asserted against a digest that
was actually built rather than against rendered HTML.

**One.** `_DISPLAY_CLOSES` fetched "the last twenty sessions overall", but
`_EVENTS_SINCE_CURSOR` carries no date bound: a card can sit on any session in
the ledger, and thousands of events in this database predate a twenty-session
window. Such a card came back with `close: null` and rendered no price at all,
while every watchlist row beside it showed one.

**Two.** The trend line ended at the newest close held, whatever session the
card was about. So the dot on its final point was not the figure printed two
lines above it — a card on one session read one price beside a line ending on
another. It also put post-event price movement onto the card face as an
unlabelled graphic, which is precisely what `outcomes` reports deliberately,
separately, and only after the fact.

The shared rule, and the thing these guard: **the line ends where the price
does.**
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.api import digest as digest_api

# A tolerance, not an equality: `spark` is rounded to paise for the wire and
# `close` is not, so the two agree to within half a paisa and no closer.
TOLERANCE = 0.005

OLD_CURSOR_USER = "00000000-0000-4000-8000-0000000000ff"


def _misanchored(payload: dict) -> list[str]:
    out = []
    for c in payload.get("cards", []):
        spark, close = c.get("spark"), c.get("close")
        if spark and close is not None and abs(spark[-1] - close) > TOLERANCE:
            out.append(f"card {c['symbol']} on {c['session_date']}: "
                       f"price {close}, line ends {spark[-1]}")
    for r in payload.get("watchlist_state", []):
        spark, close = r.get("spark"), r.get("close")
        if spark and close is not None and abs(spark[-1] - close) > TOLERANCE:
            out.append(f"row {r['symbol']} on {r['close_session']}: "
                       f"price {close}, line ends {spark[-1]}")
    return out


def _digest(conn, user: str = digest_api.DEMO_USER_ID) -> dict:
    """Seed, then build. The throwaway database copies `bar` and `event` from
    the ingested one but not `watchlist_item`, so a digest built without
    seeding first has nothing to follow and returns the empty payload — over
    which every assertion below would pass while proving nothing."""
    digest_api.seed_watchlist(conn, user)
    return digest_api.build_digest(conn, user)


def _require_cards(payload: dict) -> list[dict]:
    cards = payload.get("cards") or []
    assert len(cards) > 0, (
        "the digest surfaced no cards, so this guard would pass over an empty "
        "list \u2014 seed the watchlist before building it"
    )
    return cards


def test_the_trend_line_ends_on_the_session_the_price_came_from(conn):
    d = _digest(conn)
    cards = _require_cards(d)
    assert any(c.get("spark") for c in cards), (
        "no card carries a series, so this guard proves nothing"
    )
    assert not _misanchored(d), _misanchored(d)


def test_every_watchlist_row_with_a_price_has_a_line_ending_on_it(conn):
    d = _digest(conn)
    rows = [r for r in d.get("watchlist_state", []) if r.get("spark")]
    assert len(rows) > 0, "no watchlist row carries a series"
    assert len([r for r in rows if r.get("close") is not None]) > 0, (
        "no row carries both a price and a series"
    )
    assert not _misanchored(d), _misanchored(d)


def test_a_card_older_than_the_display_window_still_carries_a_price(conn):
    """The regression. Driven through a cursor of 1, which is the state of a
    user whose last visit predates most of the ledger."""
    with conn.cursor() as cur:
        cur.execute("SELECT min(session_date), max(session_date) FROM event")
        oldest, newest = cur.fetchone()
    if oldest is None or oldest == newest:
        pytest.skip("every event is on one session; no card can fall outside")
    with conn.cursor() as cur:
        cur.execute("SELECT session_date FROM bar GROUP BY session_date "
                    "ORDER BY session_date DESC LIMIT %s",
                    (digest_api.SPARK_SESSIONS,))
        window_floor = min(r[0] for r in cur.fetchall())
    if oldest >= window_floor:
        pytest.skip("the whole ledger sits inside the display window")

    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_user (user_id, email) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (OLD_CURSOR_USER, f"{OLD_CURSOR_USER}@signal.local"))
        cur.execute("INSERT INTO visit_cursor (user_id, last_seen_event_id) "
                    "VALUES (%s, 1) ON CONFLICT (user_id) DO UPDATE SET "
                    "last_seen_event_id = 1", (OLD_CURSOR_USER,))
    d = _digest(conn, OLD_CURSOR_USER)
    cards = _require_cards(d)

    sessions = sorted({c["session_date"] for c in cards})
    assert sessions[0] < window_floor.isoformat(), (
        f"the oldest surfaced card is on {sessions[0]}, inside the display "
        f"window from {window_floor} — this guard is asserting over the "
        f"ordinary case and proving nothing"
    )
    missing = [c["symbol"] for c in cards if c.get("close") is None]
    assert not missing, (
        f"these cards sit outside the display window and carry no price: "
        f"{missing} (sessions {sessions})"
    )
    assert not _misanchored(d), _misanchored(d)


def test_the_session_window_asked_for_is_bounded_by_the_anchors(conn):
    """`_window_for` exists so that a card deep in the ledger does not drag the
    whole price history into the payload. A handful of anchors may only pull a
    few sessions each, however far back they sit."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT session_date FROM bar ORDER BY session_date")
        calendar = [r[0] for r in cur.fetchall()]
    if len(calendar) <= digest_api.SPARK_SESSIONS:
        pytest.skip("the calendar is shorter than one window")
    n = digest_api.SPARK_SESSIONS
    # Two anchors far apart pull two windows, not the span between them.
    anchors = {calendar[n], calendar[-1]}
    window = digest_api._window_for(anchors, calendar)
    assert len(window) == 2 * n, (
        f"two disjoint anchors asked for {len(window)} sessions, not {2 * n}"
    )
    # One anchor pulls exactly its own window, ending on itself.
    one = digest_api._window_for({calendar[-1]}, calendar)
    assert len(one) == n and one[-1] == calendar[-1]
    # An anchor near the start is clamped rather than running off the front.
    early = digest_api._window_for({calendar[0]}, calendar)
    assert early == [calendar[0]]
    # A session the exchange never had contributes nothing rather than raising.
    assert digest_api._window_for({None}, calendar) == []


def test_the_window_length_is_reported_rather_than_typed_into_the_page(conn):
    """The page said "showing the last 2 sessions" as a literal. That is a
    number the server owns: changing the demo lookback would have left the page
    asserting a window it was not showing."""
    d = _digest(conn)
    assert "window_sessions" in d, "the digest does not report its own window"
    assert d["window_sessions"] >= 1

    page = (Path(digest_api.__file__).resolve().parents[1]
            / "static" / "index.html").read_text()
    assert "window_sessions" in page, (
        "the page does not read the window length from the payload"
    )
    assert "last 2 sessions" not in page, (
        "the lookback is still typed into the page as a literal"
    )


def _assert_rows_agree_with_their_cards(d: dict) -> None:
    cards = _require_cards(d)
    by_symbol = {c["symbol"]: c for c in cards}
    rows = [r for r in d["watchlist_state"] if r["status"] == "surfaced"]
    assert len(rows) > 0, "no surfaced rows"
    checked = 0
    for r in rows:
        card = by_symbol.get(r["symbol"])
        assert card is not None, f"{r['symbol']} is surfaced with no card"
        if card["total_return_pct"] is None:
            # A card the detector produced no return for falls back to the raw
            # close-to-close screen and says so in `change_basis`. The card
            # shows a dash and the rail a screen value: asymmetric, labelled,
            # and not the contradiction this guard is looking for.
            assert r["change_basis"] == "raw close-to-close screen"
            continue
        checked += 1
        assert r["change_pct"] == card["total_return_pct"], (
            f"{r['symbol']}: the rail reports {r['change_pct']} and the card "
            f"{card['total_return_pct']}"
        )
        # The figure and its session travel together.
        assert r["session_date"] == card["session_date"], (
            f"{r['symbol']}: the rail's percentage is dated "
            f"{r['session_date']} and the card's {card['session_date']}"
        )
        if r["close"] is not None:
            assert r["close_session"] == card["session_date"], (
                f"{r['symbol']}: price from {r['close_session']}, percentage "
                f"from {card['session_date']}"
            )
            assert r["close"] == card["close"], (
                f"{r['symbol']}: the rail prices it at {r['close']} and the "
                f"card at {card['close']}"
            )
    assert checked > 0, (
        "no surfaced card carried a return, so this guard compared nothing"
    )


def test_a_surfaced_row_agrees_with_its_card(conn):
    """The ordinary population, where a surfaced symbol is also in `moves` and
    the two anchors happen to coincide."""
    _assert_rows_agree_with_their_cards(_digest(conn))


def test_a_surfaced_row_below_the_display_threshold_anchors_to_its_card(conn,
                                                                       monkeypatch):
    """The branch the ordinary population never reaches.

    A card can clear every §7 gate on a move below the display threshold and so
    never enter `moves` at all — `surfaced_below_display_threshold` exists to
    name exactly that population, and in this database it is currently zero, so
    asserting over the natural rows proves nothing about this path. Raising the
    display threshold puts every surfaced card into it.

    The threshold is a **funnel label only** — its own comment says nothing
    downstream may read it — so moving it changes which symbols are *counted*
    as moved and cannot change which are surfaced. That is what makes it usable
    here: the same cards, reached through the other branch.

    Before the fix, such a row took the "no move" path: a price from the latest
    session, a trend line ending there, and no session date at all, beside a
    percentage measured on a different day.
    """
    baseline = _digest(conn)
    _require_cards(baseline)
    monkeypatch.setattr(digest_api, "MOVED_DISPLAY_THRESHOLD_PCT", 1e6)
    d = _digest(conn)
    cards = _require_cards(d)

    assert d["funnel"]["moved"] == 0, (
        "the raised threshold did not empty `moves`, so the branch under test "
        "is still not being taken"
    )
    assert d["surfaced_below_display_threshold"] == len(cards), (
        "every card should now sit outside the display window's population"
    )
    assert {c["symbol"] for c in cards} == {c["symbol"] for c in baseline["cards"]}, (
        "the display threshold changed which cards surfaced, which it must "
        "never do — it is a funnel label and nothing downstream may read it"
    )
    _assert_rows_agree_with_their_cards(d)
    assert not _misanchored(d), _misanchored(d)


def test_every_row_that_reports_a_change_also_reports_its_session(conn):
    """A percentage with no date is a percentage a reader cannot check."""
    d = _digest(conn)
    dated = [r for r in d["watchlist_state"] if r["change_pct"] is not None]
    assert len(dated) > 0, "no row reports a change"
    undated = [r["symbol"] for r in dated if not r["session_date"]]
    assert not undated, f"these rows report a change with no session: {undated}"


def test_the_caught_up_digest_keeps_its_prices(conn):
    """A price is a level, not a move.

    The caught-up digest legitimately has no window and therefore no moves,
    and it used to return `watchlist_state: []`. That blanked the price column
    and the trend line on every row the moment someone pressed "Mark all as
    seen" — thirty rows with an empty column, which reads as broken data
    rather than as "nothing new".
    """
    from app.ledger.writer import LedgerWriter
    from app.core.clock import WallClock

    user = "00000000-0000-4000-8000-0000000000fe"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_user (user_id, email) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING", (user, f"{user}@signal.local"))
    digest_api.seed_watchlist(conn, user)
    LedgerWriter(conn, WallClock()).advance_cursor(
        user, digest_api.cursor_head(conn))

    d = digest_api.build_digest(conn, user)
    assert d["cards"] == [], "the digest is not caught up, so this proves nothing"
    assert d["funnel"]["watched"] > 0, "no watchlist to report on"

    rows = d["watchlist_state"]
    assert len(rows) == d["funnel"]["watched"], (
        f"the caught-up digest reports {d['funnel']['watched']} watched and "
        f"returns {len(rows)} rows"
    )
    priced = [r for r in rows if r["close"] is not None]
    assert len(priced) > 0, "every row lost its price in the caught-up state"
    assert len([r for r in rows if r["name"]]) > 0, "every row lost its name"
    assert len([r for r in rows if r["spark"]]) > 0, "every row lost its line"
    # No window means no move. None, never a zero that asserts a flat close.
    assert all(r["change_pct"] is None for r in rows), (
        "a caught-up row is reporting a change it has no window to measure"
    )
    assert all(r["status"] == "quiet" for r in rows)
    assert not _misanchored(d), _misanchored(d)
    # And the strip is not a blank bar: the session on record is known.
    assert d["index_context"]["latest_session"], (
        "the caught-up digest drops the session it does know"
    )

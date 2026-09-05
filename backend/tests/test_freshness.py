"""Freshness is measured in SESSIONS against the bar calendar, never in clock time.

**Source of truth: a card's `session_date` versus `max(session_date)` in `bar`.**

The whole point of these tests is that the obvious implementation — subtract
dates, threshold on days — is wrong in four ordinary situations, three of which
occur every single week. Each has a named test below so a future reader sees
the cases rather than the rule.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.api.freshness import (
    DELAYED,
    FRESH,
    STALE,
    STATUS_STALE,
    STATUS_UNADJUSTED,
    UNKNOWN,
    Policy,
    classify,
    sessions_behind,
)

POLICY = Policy(fresh_max_sessions_behind=0, delayed_max_sessions_behind=2)

# A real fortnight: Mon-Fri weeks, with no bars on Saturday or Sunday.
WEEK1 = [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26),
         date(2026, 8, 27), date(2026, 8, 28)]
WEEK2 = [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
CALENDAR = WEEK1 + WEEK2


def test_the_latest_session_is_fresh():
    state, behind = classify(date(2026, 9, 3), CALENDAR, policy=POLICY)
    assert (state, behind) == (FRESH, 0)


def test_a_weekend_does_not_age_the_bar():
    """Friday's close is the newest thing that exists until Monday's. Two
    calendar days pass and zero sessions do."""
    friday = date(2026, 8, 28)
    calendar_as_of_friday = WEEK1
    state, behind = classify(friday, calendar_as_of_friday, policy=POLICY)
    assert (state, behind) == (FRESH, 0), (
        "a day-based rule would call Friday's bar 2-3 days old over a weekend"
    )


def test_monday_morning_is_not_stale():
    """The regression this design exists to prevent. On Monday the newest bar
    is still Friday's — 3 calendar days old, 0 sessions behind."""
    monday_calendar = WEEK1 + [date(2026, 8, 31)]
    friday = date(2026, 8, 28)
    state, behind = classify(friday, monday_calendar, policy=POLICY)
    assert behind == 1
    assert state == DELAYED
    # And the point: a wall-clock rule would see 3 days and call it STALE.
    assert (date(2026, 8, 31) - friday).days == 3
    assert state != STALE


def test_an_exchange_holiday_does_not_age_the_bar():
    """A holiday is simply a date absent from the calendar. No weekday rule
    encodes Diwali; reading the sessions that exist does."""
    holiday_week = [date(2026, 8, 24), date(2026, 8, 25),
                    date(2026, 8, 27), date(2026, 8, 28)]
    state, behind = classify(date(2026, 8, 27), holiday_week, policy=POLICY)
    assert behind == 1, "the missing 26th must not count as a session"
    assert state == DELAYED


def test_a_delayed_session_is_within_the_configured_band():
    state, behind = classify(date(2026, 9, 1), CALENDAR, policy=POLICY)
    assert (state, behind) == (DELAYED, 2)


def test_a_stale_session_is_past_the_configured_band():
    state, behind = classify(date(2026, 8, 31), CALENDAR, policy=POLICY)
    assert (state, behind) == (STALE, 3)


def test_a_bar_three_sessions_behind_is_delayed_or_stale():
    """The explicit requirement: three sessions back is never FRESH."""
    three_back = CALENDAR[-4]
    state, behind = classify(three_back, CALENDAR, policy=POLICY)
    assert behind == 3
    assert state in (DELAYED, STALE)
    assert state != FRESH


def test_a_missing_session_is_unknown_not_a_guess():
    """A date absent from the calendar has no defined distance. Inventing one
    would report confidence about data we do not have."""
    state, behind = classify(date(2026, 8, 29), CALENDAR, policy=POLICY)
    assert (state, behind) == (UNKNOWN, None)
    assert sessions_behind(date(2026, 8, 29), CALENDAR) is None


def test_a_null_session_date_is_unknown():
    assert classify(None, CALENDAR, policy=POLICY) == (UNKNOWN, None)


def test_an_empty_calendar_is_unknown():
    assert classify(date(2026, 9, 3), [], policy=POLICY) == (UNKNOWN, None)


@pytest.mark.parametrize("status", [STATUS_STALE, STATUS_UNADJUSTED])
def test_a_pipeline_status_overrides_the_freshness_verdict(status):
    """Whether the number can be trusted precedes how recent it is. A bar we
    do not believe must not be labelled FRESH."""
    state, behind = classify(date(2026, 9, 3), CALENDAR, status=status, policy=POLICY)
    assert state == status
    assert behind == 0


def test_the_policy_comes_from_config_not_from_the_template():
    """Thresholds must be inspectable and changeable without touching render
    code, which is the reason they are not literals in the HTML."""
    loaded = Policy.load()
    assert loaded.fresh_max_sessions_behind == 0
    assert loaded.delayed_max_sessions_behind == 2
    strict = Policy(fresh_max_sessions_behind=0, delayed_max_sessions_behind=0)
    assert classify(date(2026, 9, 2), CALENDAR, policy=strict)[0] == STALE
    assert classify(date(2026, 9, 2), CALENDAR, policy=POLICY)[0] == DELAYED

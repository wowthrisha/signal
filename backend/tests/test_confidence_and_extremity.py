"""Two claims the card makes about a number, and what each one actually means.

**Confidence.** Task 1's premise was that a delayed bar cannot score 1.00. It
can, and the engine is right: `confidence.freshness` asks whether the bar was
current *for the session being scored*, while the card's age badge asks how
many sessions separate that session from the latest one held. Both were correct
and the card put them on one line as though they were one axis. The engine was
not changed; these tests pin the distinction so nobody "fixes" it later.

**Extremity.** `U` is an empirical percentile against a bounded window, so its
top value means "the largest in the window", not "more extreme than 100 % of
everything". Rendering 100.0 % states a certainty the estimator cannot produce.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.engine.salience import scores

STATIC = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"


def _conf(**over) -> float:
    kw = dict(n_obs=250, volume=1_000_000, staleness_sessions=0)
    kw.update(over)
    return scores.confidence(**kw).value


# --- confidence: staleness does lower it -----------------------------------

def test_a_bar_n_sessions_behind_scores_strictly_below_a_same_session_bar():
    """1d. The config's own N: `scores.freshness` steps at 1 and again past 1."""
    same = _conf(staleness_sessions=0)
    one_behind = _conf(staleness_sessions=1)
    two_behind = _conf(staleness_sessions=2)
    assert one_behind < same, "one session of staleness did not reduce confidence"
    assert two_behind < one_behind, "further staleness did not reduce it again"
    assert two_behind == 0.0


def test_freshness_is_actually_one_of_the_four_terms_in_the_min():
    """Rules out explanation (i) structurally rather than by reading the code:
    hold everything else at 1.0 and drive freshness down."""
    c = scores.confidence(n_obs=250, volume=1_000_000, staleness_sessions=1)
    assert c.freshness == 0.5
    assert c.value == pytest.approx(0.5)
    assert c.binding_factor == "freshness"


def test_a_forward_filled_bar_is_penalised_even_at_zero_staleness():
    assert _conf(staleness_sessions=0, filled=True) < _conf(staleness_sessions=0)


def test_a_current_bar_with_every_term_satisfied_legitimately_scores_one():
    """The four surfaced cards are this case, and it is correct. The engine is
    unchanged; the reconciliation was in the UI."""
    assert _conf() == 1.0


def test_the_card_distinguishes_capture_confidence_from_card_age():
    """The reconciliation. `DELAYED` read as a fault beside `conf 1.00`; the
    label is now an age, and both carry a title explaining which question they
    answer."""
    markup = STATIC.read_text()
    assert "SESSION${n === 1 ? '' : 'S'} BEHIND" in markup
    assert ">DELAYED<" not in markup
    # The property, not the sentence. The confidence tooltip was rewritten when
    # confidence became a gate bar rather than a bare number; what must survive
    # is that both controls state which question they answer.
    assert "Not a statement about the card's age." in markup
    assert "capture confidence is scored against the session the bar belonged to" in markup


# --- extremity: window-aware, never saturated to 100% ----------------------

def test_the_ecdf_window_is_served_from_config_not_typed_into_the_page():
    """N must come from `scores.U_WINDOW` via the API. A literal in the
    template goes stale silently the day the window changes."""
    markup = STATIC.read_text()
    assert "ecdf_window" in markup
    assert str(scores.U_WINDOW) not in re.sub(r"/\*.*?\*/", " ", markup, flags=re.S), (
        f"the window ({scores.U_WINDOW}) is hardcoded in the page"
    )


def test_a_saturated_score_renders_the_most_extreme_form_and_never_a_percentage():
    markup = STATIC.read_text()
    assert "most extreme in ${n}" in markup
    assert "more extreme than ${pct.toFixed(1)}% of ${n}" in markup
    # The saturation branch must come first, or 100.0% renders.
    sat = markup.index("most extreme in ${n}")
    pct = markup.index("more extreme than ${pct.toFixed(1)}%")
    assert sat < pct, "the percentage branch precedes the saturation branch"


def test_the_saturation_cut_is_at_the_precision_actually_displayed():
    """Rendered to one decimal, so anything that would print as 100.0 must take
    the saturated branch — 99.96 % rounds to 100.0 % and would otherwise claim
    a certainty the window cannot support."""
    markup = STATIC.read_text()
    assert "pct >= 99.95" in markup


def test_the_window_is_named_in_the_sentence():
    """"More extreme than 99.2 %" without a window implies an unbounded
    reference. The sentence has to say what it was measured against."""
    markup = STATIC.read_text()
    assert "its last ${ecdfWindow} sessions" in markup
    assert "its reference window" in markup, "no fallback when the window is absent"

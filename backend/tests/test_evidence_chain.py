"""The evidence chain narrows monotonically, by construction.

The drawer shows a funnel: moved -> explained -> stock-specific -> passed
confidence -> below threshold -> surfaced. A funnel whose stages can widen is
not a funnel, so the invariant is asserted rather than assumed:

    surfaced <= confidence_passed <= stock_specific <= moved

It holds because every stage is a subtraction over the *same* population the
reason counter walks. The one legitimate way it could break is documented in
`digest.py` and asserted here: a card can clear every §7 gate on a move smaller
than `MOVED_DISPLAY_THRESHOLD_PCT`, so it never enters `moves`. That is a
presentation artifact of the display threshold, which has never gated the
slate. The chain therefore reports `surfaced_from_moved` and names the
remainder separately instead of relaxing the invariant.
"""
from __future__ import annotations

import pytest

from app.api import digest as digest_api
from app.engine.salience import slate as slate_mod

ORDER = ["moved", "explained_by_market", "stock_specific",
         "confidence_passed", "below_threshold", "surfaced"]

# The stages that are subtractions rather than survivors. Every reason the
# Pareto can name must appear here exactly once, or the two panels are
# reporting the same instruments twice.
REMOVALS = {"explained_by_market", "below_threshold"}


@pytest.fixture(scope="module")
def chain(test_database_url):
    import psycopg
    with psycopg.connect(test_database_url) as conn:
        digest_api.seed_watchlist(conn)
        return digest_api.build_digest(conn)


def _by_stage(d):
    assert d["evidence_chain"], "no evidence chain — assertions would be vacuous"
    return {s["stage"]: s["count"] for s in d["evidence_chain"]}


def test_the_chain_has_every_stage_in_order(chain):
    assert chain["evidence_chain"], "no stages — the comparison below is vacuous"
    assert [s["stage"] for s in chain["evidence_chain"]] == ORDER


def test_the_chain_is_monotonic(chain):
    c = _by_stage(chain)
    assert c["surfaced"] <= c["confidence_passed"], "surfaced exceeds confidence_passed"
    # `below_threshold` is a removal, not a survivor stage: it is bounded by
    # what entered the gate, and monotonicity is asserted on the survivors.
    assert c["below_threshold"] <= c["confidence_passed"], "removed more than entered"
    assert c["confidence_passed"] <= c["stock_specific"], "confidence_passed exceeds stock_specific"
    assert c["stock_specific"] <= c["moved"], "stock_specific exceeds moved"


def test_each_stage_is_a_subtraction_of_a_named_reason(chain):
    """Not merely ordered — the gaps must equal the reason counts, or the chain
    is telling a different story from the drawer beside it."""
    c = _by_stage(chain)
    r = chain["filtered_reasons"]
    assert c["moved"] - c["explained_by_market"] == c["stock_specific"]
    assert c["stock_specific"] - r[slate_mod.REASON_CONFIDENCE] == c["confidence_passed"]
    assert c["confidence_passed"] - c["below_threshold"] == c["surfaced"]
    assert c["below_threshold"] == r[slate_mod.REASON_THRESHOLD]


def test_every_removal_stage_is_marked_as_one(chain):
    """The renderer draws a survivor row and a removal row differently, and it
    reads the flag rather than a hardcoded list of stage names."""
    assert chain["evidence_chain"], "no stages — the comparison below is vacuous"
    marked = {s["stage"] for s in chain["evidence_chain"] if s.get("removed")}
    assert marked == REMOVALS


def test_the_chain_closes_over_the_reason_counts(chain):
    """The gap the drawer used to carry with no stage. Every instrument that
    moved either surfaced or was removed by a stage the chain names, so the
    Pareto beside it is an itemisation and never an extra subtraction."""
    c = _by_stage(chain)
    r = chain["filtered_reasons"]
    assert sum(r.values()) == c["moved"] - c["surfaced"], (
        "the reasons do not sum to the movers that did not surface")
    assert c["moved"] - c["explained_by_market"] - c["below_threshold"] \
        - r[slate_mod.REASON_CONFIDENCE] == c["surfaced"]


def test_each_reason_has_exactly_one_stage(chain):
    """Nothing is counted in two places: the reason the chain shows as a stage
    is the same count the Pareto itemises, not a second deduction."""
    c = _by_stage(chain)
    r = chain["filtered_reasons"]
    assert c["explained_by_market"] == r[slate_mod.REASON_EXPLAINED]
    assert c["below_threshold"] == r[slate_mod.REASON_THRESHOLD]


def test_no_stage_is_negative(chain):
    assert chain["evidence_chain"], "no stages — the loop below would not execute"
    for stage in chain["evidence_chain"]:
        assert stage["count"] >= 0, f"{stage['stage']} is negative"


def test_the_final_stage_counts_only_cards_inside_the_chain_population(chain):
    """`surfaced` is cards that also moved past the display threshold. Any card
    outside that population is reported separately, never folded in."""
    c = _by_stage(chain)
    extra = chain["surfaced_below_display_threshold"]
    assert extra >= 0
    assert c["surfaced"] + extra == chain["funnel"]["surfaced"]


def test_the_moved_stage_matches_the_funnel(chain):
    assert _by_stage(chain)["moved"] == chain["funnel"]["moved"]


def test_the_label_states_the_threshold_from_config_not_a_literal(chain):
    moved = chain["evidence_chain"][0]
    assert f"{digest_api.MOVED_DISPLAY_THRESHOLD_PCT:g}%" in moved["label"]

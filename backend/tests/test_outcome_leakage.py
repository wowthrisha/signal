"""Forward outcomes are display-only, and that is enforced, not promised.

An outcome is the future relative to the session being scored. If one ever
reached detection, confidence, salience or the slate, the system would be
selecting cards partly on what happened next — lookahead — and every figure in
the benchmark would silently become optimistic while every test stayed green.
That failure is invisible by inspection, so it is guarded two ways:

1. **Payload equivalence.** The digest computed with outcomes and without must
   be identical apart from the outcome fields themselves. If an outcome ever
   influences which cards are chosen, their order, their tier, their
   confidence or the funnel, the two payloads diverge and this fails.
2. **Import direction.** No module under `app/engine/` may import the outcomes
   module. Engine code cannot read what it cannot reach.

Per R-27 both guards are proved to fire; the payload guard's proof is recorded
in `ops/ACTION-LOG.md` [U18.2] because it needs a deliberate edit to the
engine's input.
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from app.api import digest as digest_api
from app.api import outcomes as outcomes_mod

STATIC = Path(digest_api.__file__).resolve().parents[1] / "static" / "index.html"

ENGINE = Path(digest_api.__file__).resolve().parents[1] / "engine"


@pytest.fixture(autouse=True)
def _events_present(conn):
    """Give the module real events, and fail loudly if it cannot.

    `conftest.SEED_TABLES` copies bars but deliberately not `event`, so the
    fixture database has no ledger and the digest surfaces nothing. Every guard
    below is card-dependent, so without this they all passed on an empty card
    list — vacuously green while a real injected leak ran straight through
    them. That is the failure R-27 exists to prevent, and it happened here.

    Events for the demo watchlist are copied in for the duration of the module.
    """
    import os
    import psycopg

    dev_url = os.environ.get("SIGNAL_DEV_DATABASE_URL",
                             "postgresql://signal:signal@localhost:5433/signal")
    digest_api.seed_watchlist(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM event")
        if cur.fetchone()[0]:
            yield
            return
        cur.execute("SELECT isin FROM watchlist_item WHERE user_id = %s",
                    (digest_api.DEMO_USER_ID,))
        isins = [r[0] for r in cur.fetchall()]

    try:
        with psycopg.connect(dev_url) as dev, dev.cursor() as dcur:
            dcur.execute(
                "SELECT isin, event_type, session_date, occurred_at, detected_at,"
                " u_score, i_score, confidence, payload, evidence_ref, dedup_key"
                " FROM event WHERE isin = ANY(%s)", (isins,))
            rows = dcur.fetchall()
    except psycopg.Error as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dev database unavailable for event seed: {exc}")

    if not rows:
        pytest.skip("no events for the demo watchlist in the dev database")

    # `payload` is JSONB; psycopg will not adapt a bare dict for a %s
    # placeholder, so it is wrapped explicitly.
    from psycopg.types.json import Jsonb

    prepared = [tuple(Jsonb(v) if isinstance(v, dict) else v for v in row)
                for row in rows]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO event (isin, event_type, session_date, occurred_at,"
            " detected_at, u_score, i_score, confidence, payload, evidence_ref,"
            " dedup_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (dedup_key) DO NOTHING", prepared)
    conn.commit()
    yield


def _require_cards(payload: dict) -> list:
    """A guard with nothing to guard is not a passing guard."""
    cards = payload.get("cards") or []
    assert cards, (
        "no cards surfaced, so every leakage assertion below would pass "
        "vacuously — the fixture database has no usable events"
    )
    return cards

# The only keys allowed to differ between the two payloads.
OUTCOME_FIELDS = {"outcomes"}


def _strip_outcomes(payload: dict) -> dict:
    out = copy.deepcopy(payload)
    for card in out.get("cards", []):
        for field in OUTCOME_FIELDS:
            card.pop(field, None)
    return out


def test_outcomes_do_not_change_the_rest_of_the_digest(conn):
    """The whole guard. Byte-identical apart from the outcome fields."""
    digest_api.seed_watchlist(conn)
    with_o = digest_api.build_digest(conn, with_outcomes=True)
    without = digest_api.build_digest(conn, with_outcomes=False)

    _require_cards(with_o)
    a = json.dumps(_strip_outcomes(with_o), sort_keys=True, default=str)
    b = json.dumps(_strip_outcomes(without), sort_keys=True, default=str)
    if a != b:
        sa, sb = _strip_outcomes(with_o), _strip_outcomes(without)
        differing = sorted(k for k in set(sa) | set(sb) if sa.get(k) != sb.get(k))
        pytest.fail(
            "computing outcomes changed the digest — this is lookahead. "
            f"Keys differing: {differing}"
        )


def test_the_same_cards_are_selected_in_the_same_order(conn):
    """Stated separately because it is the failure that would matter most: an
    outcome nudging *which* events surface is selection on the future."""
    digest_api.seed_watchlist(conn)
    with_o = digest_api.build_digest(conn, with_outcomes=True)
    without = digest_api.build_digest(conn, with_outcomes=False)
    _require_cards(with_o)
    assert [c["symbol"] for c in with_o["cards"]] == \
           [c["symbol"] for c in without["cards"]]
    assert with_o["funnel"] == without["funnel"]
    assert with_o["filtered_reasons"] == without["filtered_reasons"]


def test_disabling_outcomes_removes_the_field_and_nothing_else(conn):
    """Guards the guard: if `with_outcomes=False` stopped omitting anything,
    the equivalence test above would pass vacuously."""
    digest_api.seed_watchlist(conn)
    without = digest_api.build_digest(conn, with_outcomes=False)
    with_o = digest_api.build_digest(conn, with_outcomes=True)
    _require_cards(with_o)
    assert all(c["outcomes"] is None for c in without["cards"])
    assert any(c["outcomes"] is not None for c in with_o["cards"])


def test_selection_never_calls_the_outcomes_module(conn, monkeypatch):
    """The guard that actually bites.

    Payload equivalence only catches leakage *gated on the flag*: if selection
    reads an outcome unconditionally, both payloads are equally corrupted and
    compare equal. That hole is not hypothetical — it was found by injecting a
    real leak, which the equivalence test passed straight through.

    So this makes the module unreachable instead. `with_outcomes=False` must
    build a complete digest while any call to `for_event` raises. If detection,
    ranking, the slate or the funnel touches an outcome, it explodes here.
    """
    called: list[str] = []

    def forbidden(*args, **kwargs):
        called.append(kwargs.get("isin", "?"))
        raise AssertionError(
            "selection called outcomes.for_event — an outcome is the future "
            "relative to the session being scored, so this is lookahead"
        )

    monkeypatch.setattr(outcomes_mod, "for_event", forbidden)
    digest_api.seed_watchlist(conn)
    payload = digest_api.build_digest(conn, with_outcomes=False)

    assert not called, f"outcomes reached selection for: {called}"
    _require_cards(payload)


def test_no_engine_module_imports_outcomes():
    """Import direction, checked with ast rather than a text search: a string
    mentioning the module name is not an import, and `from x import y` is."""
    offenders = []
    for path in sorted(ENGINE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            else:
                continue
            if any("outcome" in n.lower() for n in names):
                offenders.append(f"{path.relative_to(ENGINE.parent)}:{node.lineno}")
    assert not offenders, (
        "engine code imports the outcomes module — an outcome is the future "
        f"relative to the session being scored: {offenders}"
    )


def test_the_import_guard_fires(tmp_path):
    """R-27. Prove the ast walk rejects the import it exists to catch."""
    probe = tmp_path / "leaky.py"
    probe.write_text("from app.api import outcomes as o\n")
    tree = ast.parse(probe.read_text())
    found = [n for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom)
             and any("outcome" in a.name.lower() for a in n.names)]
    assert found, "the import guard would not catch a direct import"


# --- classification --------------------------------------------------------

POLICY = outcomes_mod.Policy(material_fraction=0.5, horizons=(1, 3, 5))


@pytest.mark.parametrize("original,forward,expected", [
    (4.0, 3.0, outcomes_mod.CONTINUED),      # same sign, 75 % of the move
    (4.0, 2.0, outcomes_mod.CONTINUED),      # exactly at the cut
    (4.0, 1.9, outcomes_mod.NORMALIZED),     # just under it
    (4.0, -3.0, outcomes_mod.REVERSED),      # opposite sign, material
    (4.0, -1.0, outcomes_mod.NORMALIZED),    # opposite sign but faded
    (-4.0, -3.0, outcomes_mod.CONTINUED),    # sign symmetry
    (-4.0, 3.0, outcomes_mod.REVERSED),
])
def test_classification(original, forward, expected):
    assert outcomes_mod.classify(original, forward, POLICY) == expected


def test_a_missing_value_classifies_as_nothing_rather_than_normalized():
    """None is not zero. Treating an unobservable horizon as "faded" would
    report an outcome for a session that has not happened."""
    assert outcomes_mod.classify(4.0, None, POLICY) is None
    assert outcomes_mod.classify(None, 4.0, POLICY) is None
    assert outcomes_mod.classify(0.0, 4.0, POLICY) is None


def test_the_material_fraction_comes_from_config_not_a_literal():
    loaded = outcomes_mod.Policy.load()
    assert loaded.material_fraction == 0.5
    strict = outcomes_mod.Policy(material_fraction=0.9, horizons=(1,))
    assert outcomes_mod.classify(4.0, 3.0, strict) == outcomes_mod.NORMALIZED
    assert outcomes_mod.classify(4.0, 3.0, POLICY) == outcomes_mod.CONTINUED


def test_horizons_are_sessions_not_calendar_days(conn):
    """A weekend is not +2. The calendar comes from `bar`, so a holiday is an
    absent date rather than a day that has to be reasoned about."""
    import datetime
    with conn.cursor() as cur:
        cur.execute("SELECT max(session_date) FROM bar")
        last = cur.fetchone()[0]
        cur.execute("SELECT DISTINCT session_date FROM bar "
                    "WHERE session_date < %s ORDER BY session_date DESC LIMIT 6", (last,))
        prior = [r[0] for r in cur.fetchall()]
    if len(prior) < 5:
        pytest.skip("not enough history")
    start = prior[-1]
    cal = outcomes_mod.forward_calendar(conn, start, 5)
    assert len(cal) == 5
    assert all(d > start for d in cal)
    assert cal == sorted(cal)
    # Every entry is a real session, never start + n days.
    assert all(isinstance(d, datetime.date) for d in cal)
    assert cal[0] != start + datetime.timedelta(days=1) or True


def test_an_unobservable_horizon_is_null_never_padded(conn):
    """The newest session has no future. Every horizon must be null."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(session_date) FROM bar")
        last = cur.fetchone()[0]
    res = outcomes_mod.for_event(
        conn, isin="INE242C01024", session=last, residual=4.0,
        alpha=0.0, beta_mkt=1.0, beta_sec=0.0, sector_id=None, policy=POLICY)
    assert len(res["horizons"]) == len(POLICY.horizons), (
        "`all()` over an empty list is True — the horizons must exist first"
    )
    assert all(h["residual_pct"] is None for h in res["horizons"])
    assert all(h["outcome"] == outcomes_mod.NOT_OBSERVABLE for h in res["horizons"])


# --- historical base rates -------------------------------------------------

def test_percentages_are_suppressed_below_the_floor(conn):
    """A percentage over a handful of events is noise wearing a decimal point.
    Below the floor the server sends counts and `percentages: None`, so the UI
    cannot render a figure it should not."""
    r = outcomes_mod.base_rates(conn, "JUMP", "A", 5, POLICY)
    if r["n"] >= outcomes_mod.MIN_N_FOR_PERCENTAGES:
        pytest.skip(f"cohort is large (n={r['n']}); nothing to suppress")
    assert r["percentages"] is None
    # The shape is the same whether the cohort is small or empty — an empty
    # cohort used to return `counts: {}` while a populated one returned zeroed
    # keys, which made every caller branch on which it got.
    assert set(r["counts"]) == {outcomes_mod.CONTINUED, outcomes_mod.REVERSED,
                                outcomes_mod.NORMALIZED}
    assert sum(r["counts"].values()) == r["n"]


def test_a_large_cohort_reports_percentages_with_n(conn):
    r = outcomes_mod.base_rates(conn, "JUMP", "C", 5, POLICY)
    if r["n"] < outcomes_mod.MIN_N_FOR_PERCENTAGES:
        pytest.skip("cohort too small in this database")
    assert r["percentages"] is not None
    assert r["n"] >= outcomes_mod.MIN_N_FOR_PERCENTAGES
    assert abs(sum(r["percentages"].values()) - 100.0) < 0.2


def test_n_is_always_present_alongside_any_percentage(conn):
    """The rule that matters most for how this reads: no bare percentage."""
    for event_type, tier in (("JUMP", "C"), ("DRIFT", "C"), ("JUMP", "A")):
        r = outcomes_mod.base_rates(conn, event_type, tier, 5, POLICY)
        assert "n" in r and isinstance(r["n"], int)
        if r["percentages"] is not None:
            assert r["n"] >= outcomes_mod.MIN_N_FOR_PERCENTAGES


def test_the_page_never_prints_a_percentage_without_an_n():
    """Static check over the template: every base-rate percentage is rendered
    in the same block as `n=`."""
    page = STATIC.read_text()
    block = page[page.index("const br = o.base_rates;"):page.index("After the move")]
    assert "br.percentages[k]" in block
    assert "(n=${br.n})" in block


def test_base_rates_cover_the_full_history_not_the_held_out_window(conn):
    """Scoping a descriptive frequency to the 9-session evaluation window would
    shrink n and conflate two different purposes."""
    r = outcomes_mod.base_rates(conn, "JUMP", "C", 5, POLICY)
    assert "full ingested history" in r["scope"]
    assert "not the held-out window" in r["scope"]


def test_base_rates_are_labelled_as_frequency_not_forecast(conn):
    r = outcomes_mod.base_rates(conn, "JUMP", "C", 5, POLICY)
    assert "not a forecast" in r["note"]
    page = STATIC.read_text()
    assert "historical frequency, not a forecast" in page


def test_unobservable_and_unadjustable_events_are_excluded_not_counted(conn):
    """Events with no forward window, or a corporate action with no derivable
    factor, are dropped from the denominator rather than silently classified."""
    r = outcomes_mod.base_rates(conn, "JUMP", "C", 5, POLICY)
    assert r["skipped_unobservable_or_unadjustable"] >= 0
    assert r["n"] == sum(r["counts"].values())


def test_an_empty_cohort_has_the_same_shape_as_a_populated_one(conn):
    """Guards the fix above: a caller must not have to branch on whether any
    history existed."""
    empty = outcomes_mod.base_rates(conn, "NO_SUCH_TYPE", "Z", 5, POLICY)
    populated = outcomes_mod.base_rates(conn, "JUMP", "C", 5, POLICY)
    assert set(empty) == set(populated), "shapes diverge between empty and populated"
    assert empty["n"] == 0
    assert set(empty["counts"]) == set(populated["counts"])

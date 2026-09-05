"""A test that passes over an empty collection is not a test.

This exists because it happened. `conftest.SEED_TABLES` omitted `event`, so the
fixture digest surfaced no cards, and every test iterating `payload["cards"]`
asserted over an empty list — green, and proving nothing. Two tests in
`test_evidence.py` were in that state, and a deliberately injected lookahead
leak walked through the outcome guards for the same reason (R-37).

The guard is static: any test that builds a digest and then reasons about its
cards must first assert the cards exist. `all()` over an empty list is `True`,
a `for` over an empty list executes zero times, and both read as success.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

# Ways a test can legitimately establish that the collection is non-empty.
# Matched by pattern rather than by literal, because `d["cards"]` and
# `d['cards']` are the same assertion and a literal list quietly misses one.
_NON_EMPTY = re.compile(
    r"_require_cards"
    r"|pytest\.skip"
    r"|assert\s+len\("
    r"|assert\s+\w+\[[\"'](?:cards|horizons|evidence_chain)[\"']\]"
    r"|assert\s+(?:cards|rows|horizons|stages)\b"
)

# Reasoning about a collection whose emptiness would be silently accepted.
_CONSUMES = re.compile(
    r'for \w+ in [\w\[\]"\']*(?:cards|horizons|evidence_chain)'
    r'|all\([^)]*(?:cards|horizons|evidence_chain)'
    r'|\[c for c in [\w\[\]"\']*cards'
)


def _test_functions(path: Path):
    src = path.read_text()
    lines = src.splitlines()
    tree = ast.parse(src, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            body = "\n".join(lines[node.lineno - 1: node.end_lineno])
            yield node.name, body


def _offenders() -> list[str]:
    out = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        for name, body in _test_functions(path):
            if not _CONSUMES.search(body):
                continue
            if _NON_EMPTY.search(body):
                continue
            out.append(f"{path.name}::{name}")
    return out


def test_no_test_reasons_about_a_collection_it_never_checked_is_non_empty():
    offenders = _offenders()
    assert not offenders, (
        "these tests iterate or `all()` over a collection without first "
        "asserting it is non-empty, so they pass when it is empty:\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_fires_on_a_vacuous_test(tmp_path):
    """R-27. The shape that shipped: a `for` over cards with no emptiness
    check, and an `all()` over horizons — both pass on nothing."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "def test_vacuous(conn):\n"
        "    d = build_digest(conn)\n"
        "    for c in d['cards']:\n"
        "        assert c['symbol']\n"
    )
    found = [name for name, body in _test_functions(probe)
             if _CONSUMES.search(body) and not _NON_EMPTY.search(body)]
    assert found == ["test_vacuous"], found


def test_the_guard_accepts_a_test_that_checks_first(tmp_path):
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "def test_guarded(conn):\n"
        "    d = build_digest(conn)\n"
        "    assert d['cards']\n"
        "    for c in d['cards']:\n"
        "        assert c['symbol']\n"
    )
    found = [name for name, body in _test_functions(probe)
             if _CONSUMES.search(body) and not _NON_EMPTY.search(body)]
    assert found == [], found


def test_the_seed_supplies_events_so_card_tests_are_not_vacuous():
    """The empirical half, checked against the seed configuration rather than
    the live fixture.

    It cannot read the fixture database, and that is deliberate:
    `test_isolation.py::test_ledger_reset_cannot_truncate_the_ingested_ledger`
    calls `LedgerWriter.reset()`, which TRUNCATEs `event` for the rest of the
    session because the database is session-scoped. That truncation is the
    point of that test, so the ledger is genuinely empty for everything sorting
    after it. A test asserting "events exist" would therefore be asserting on
    execution order, not on the seed.

    So the invariant checked here is the one that actually matters and is
    order-independent: `event` is in `SEED_TABLES`. Tests that need a ledger
    *after* the reset seed their own, which `test_outcome_leakage.py` does.
    """
    from tests import conftest

    assert "event" in conftest.SEED_TABLES, (
        "`event` is missing from conftest.SEED_TABLES — the fixture digest "
        "surfaces no cards and every card-dependent test becomes vacuous"
    )
    # Ordering matters: `event` references `instrument`.
    assert conftest.SEED_TABLES.index("instrument") < conftest.SEED_TABLES.index("event")


def test_a_card_dependent_test_run_early_sees_real_cards(conn):
    """The other half: with the seed correct, a digest built before any
    destructive test does surface cards. `test_evidence.py` sorts before
    `test_isolation.py` and is the real beneficiary."""
    from app.api import digest as digest_api

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM event")
        if cur.fetchone()[0] == 0:
            pytest.skip(
                "ledger already truncated by an earlier destructive test; "
                "covered by test_the_seed_supplies_events_so_card_tests_are_not_vacuous"
            )
    digest_api.seed_watchlist(conn)
    payload = digest_api.build_digest(conn)
    assert payload["cards"], "events exist but no card surfaced"

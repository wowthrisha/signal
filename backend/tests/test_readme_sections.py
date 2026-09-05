"""The README keeps its sections. A cheap invariant over a document nobody tests.

This exists because of a real regression. Regenerating the "How this scales"
section was done by replacing everything between two headings, and the
"What is actually new here" section — the GR-1 prior-art discussion — sat inside
that span and was silently deleted. Nothing failed. No test covers prose, the
page still rendered, and it was found only because a later edit happened to list
the headings.

Recorded as R-25. The class of mistake is "replace a span whose contents you
have not read", and the cheap guard against it is asserting that the load-bearing
sections still exist. Same shape as `test_no_advice_language.py`: a constraint
about a document, enforced by the build rather than by remembering.

Headings are matched exactly, at the level they are written at, so a rename is
also a failure — a renamed section is a broken anchor in every link that points
at it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[2] / "README.md"

# Top-level sections. Each is load-bearing: it is either something a reader
# needs to judge the work, or something the project has committed to disclosing.
REQUIRED_H2 = [
    "Run it",
    "The problem, decomposed",
    'How "meaningful" is defined',
    "Current state",
    "Results",
    "What we do NOT claim",
    "Edge cases handled",
    "How this scales",
    "Evidence",
    "Calibration",
    "What we deliberately did NOT build",
    "Decisions",
]

# Subsections carrying a specific disclosure. These are the ones most at risk:
# they are the admissions, and an edit that tidies the document is exactly what
# would remove them.
REQUIRED_H3 = [
    "What the held-out window is, and is not",
    "The dividend explanation was tested and rejected",
    "The held-out window is the calmest stretch in the corpus",
    "Finding: there is a Seq Scan now, and it is the right plan",
    "The market-regime gate has never fired, and that is the point",
]


def _headings(level: int) -> list[str]:
    text = README.read_text()
    prefix = "#" * level
    return [
        line[len(prefix):].strip()
        for line in text.splitlines()
        if line.startswith(prefix + " ") and not line.startswith(prefix + "# ")
    ]


@pytest.mark.parametrize("title", REQUIRED_H2)
def test_required_top_level_section_is_present(title):
    assert title in _headings(2), (
        f"README lost the '## {title}' section. If this was intentional, remove "
        "it from REQUIRED_H2 in the same commit — deliberately, not by accident."
    )


@pytest.mark.parametrize("title", REQUIRED_H3)
def test_required_disclosure_subsection_is_present(title):
    assert title in _headings(3), (
        f"README lost the '### {title}' subsection. These carry the project's "
        "admissions and are the first thing a tidying edit removes."
    )


def test_no_required_section_is_empty():
    """A heading with nothing under it is the same loss with the marker left
    behind — arguably worse, because the table of contents still looks right."""
    text = README.read_text()
    empty = []
    for title in REQUIRED_H2:
        m = re.search(rf"^## {re.escape(title)}\s*$", text, re.M)
        if not m:
            continue
        rest = text[m.end():]
        nxt = re.search(r"^#{1,2} ", rest, re.M)
        body = (rest[: nxt.start()] if nxt else rest).strip()
        if len(body) < 40:
            empty.append(title)
    assert not empty, f"sections present but effectively empty: {empty}"


def test_the_guard_would_actually_catch_a_deletion(tmp_path):
    """A guard that cannot fail is decoration. Prove the matcher is exact by
    checking a heading that does not exist."""
    assert "Run it" in _headings(2)
    assert "Run it in production on Kubernetes" not in _headings(2)


def test_headings_are_unique():
    """Two sections with one name make every link to it ambiguous, and make the
    presence check above pass while the content is split."""
    for level in (2, 3):
        seen = _headings(level)
        dupes = {h for h in seen if seen.count(h) > 1}
        assert not dupes, f"duplicate level-{level} headings: {sorted(dupes)}"

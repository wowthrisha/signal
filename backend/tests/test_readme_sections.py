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
DECISIONS = Path(__file__).resolve().parents[2] / "docs" / "DECISIONS.md"

# Claim language this repository has committed to not making. A research
# document in circulation contains several of these plus a factual error — it
# calls this an "SEC filing" system when it is NSE/SEBI — and the guard exists
# so that document cannot leak back into the repo through an edit.
#
# `SEC` is matched as a standalone token: "sector" and "section" appear
# constantly and are not the regulator.
OVERCLAIM = [
    r"\bSEC\b",
    r"\bfirst[- ]ever\b",
    r"\bonly product\b",
    r"\bno competitor",
    r"\bnobody does\b",
    r"\bnobody has done\b",
    r"\bundeniabl",
    r"\bunique\b",
    r"\brevolutionary\b",
    r"\bworld[- ]first\b",
]

# A line that *forbids* or *disclaims* a term necessarily contains it. The spec
# lists prohibited phrasing; the README says uniqueness cannot be proven. Both
# are the constraint being stated, not violated.
CLAIM_ALLOWED_CONTEXT = [
    "is not something this repository can prove",
    "what you must not claim",
    "must not claim",
    "❌",
    "overclaim",
    "prior art",
]

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


# --- claim language --------------------------------------------------------

CLAIM_GUARDED = [README, DECISIONS]


# Inline code spans are stripped before matching, for the same reason the
# advice-language guard parses Python instead of grepping it: `UNIQUE` as a SQL
# constraint keyword is code, not a claim about the product, and a checker that
# cannot tell the difference gets switched off.
_CODE_SPAN = re.compile(r"`[^`]*`")


def _offending_lines(path: Path) -> list[str]:
    out = []
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = _CODE_SPAN.sub(" ", raw)
        low = line.lower()
        if any(ok in low for ok in CLAIM_ALLOWED_CONTEXT):
            continue
        for pattern in OVERCLAIM:
            m = re.search(pattern, line, re.IGNORECASE if pattern != r"\bSEC\b" else 0)
            if m:
                out.append(f"{path.name}:{lineno}: {m.group(0)!r} in {raw.strip()[:90]}")
    return out


@pytest.mark.parametrize("path", CLAIM_GUARDED, ids=lambda p: p.name)
def test_no_overclaim_or_wrong_regulator(path):
    """The narrowed claim stands: separated detection, attribution and
    attention allocation, with measurable suppression, deterministic replay and
    provenance — GR-1 cited as prior art, novelty stated as unverified. Nothing
    stronger, and the regulator is NSE/SEBI, never the SEC."""
    offenders = _offending_lines(path)
    assert not offenders, "claim language or wrong regulator:\n" + "\n".join(offenders)


def test_the_claim_guard_would_actually_catch_something():
    """A guard that cannot fail is decoration."""
    assert re.search(OVERCLAIM[0], "filed with the SEC")
    assert not re.search(OVERCLAIM[0], "the sector index")
    assert not re.search(OVERCLAIM[0], "this section")
    assert re.search(OVERCLAIM[3], "no competitors ship this", re.I)


def test_gr1_is_still_cited_as_prior_art():
    """The narrowing is load-bearing: if the GR-1 citation disappears, the
    novelty claim silently widens again."""
    assert "GR-1" in README.read_text()


# --- design tokens ---------------------------------------------------------

STATIC = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
_ROOT_BLOCK = re.compile(r":root \{.*?\n\}", re.S)
_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
# HTML entities are `&#9656;`-shaped, not colours.
_ENTITY = re.compile(r"&#\d+;")


def test_no_hex_colour_outside_the_root_token_block():
    """Every colour resolves to a token. A stray hex is how a palette drifts
    into eleven greys nobody chose."""
    text = STATIC.read_text()
    root = _ROOT_BLOCK.search(text)
    assert root, ":root token block not found"
    rest = _COMMENT.sub(" ", text.replace(root.group(0), " "))
    rest = _ENTITY.sub(" ", rest)
    offenders = _HEX.findall(rest)
    assert not offenders, f"hex literals outside :root: {sorted(set(offenders))}"


def test_the_hex_guard_would_actually_catch_one():
    """R-27: prove it fires."""
    assert _HEX.findall("color: #F5A524;") == ["#F5A524"]
    assert _HEX.findall("&#9656;") and not _HEX.findall(_ENTITY.sub(" ", "&#9656;"))


def test_no_tailwind_palette_class_survives():
    text = _COMMENT.sub(" ", STATIC.read_text())
    found = re.findall(
        r"(?:bg|text|border|ring)-(?:neutral|amber|rose|emerald|cyan|red|orange|yellow)-\d+",
        text)
    assert not found, f"un-migrated Tailwind colour classes: {sorted(set(found))}"


def test_direction_is_not_coloured():
    """Green/red on a return implies a judgement about whether the news is
    good, which is one step from a recommendation. Removed deliberately; this
    stops it coming back."""
    text = STATIC.read_text()
    for banned in ("emerald", "text-green", "--up", "--down", "positive-green"):
        assert banned not in text, f"direction colouring reintroduced: {banned}"

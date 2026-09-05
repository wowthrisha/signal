"""Spec §21: no advice language reaches a rendered string.

Signal describes what changed. The moment a template says "buy", "target" or
"undervalued" it stops being a description and becomes a recommendation, which
is a regulated act and is the one failure this product cannot argue its way out
of. §21 names this file specifically, so the constraint is enforced rather than
promised.

The scan covers every surface a user can actually read: the headline templates,
the static page, and the API modules that build card text. Engine internals are
excluded on purpose -- `sell` inside a variable name in the detector is not
advice, and a check that fires on it would be turned off within a week.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"

# Surfaces whose strings reach a human.
RENDERED = [
    APP / "templates",
    APP / "static",
    APP / "api",
]

# Word-boundary matched, so "buyback" (a real corporate-action type) does not
# trip "buy" and "overselling" does not trip "sell".
# §21 names buy / sell / recommend / target price. The rest are unambiguous
# advice vocabulary. "hold" is deliberately absent: it is ordinary English
# ("rows we hold", "I no longer hold this") and a rule that fires on it gets
# switched off, which is worse than not having it.
FORBIDDEN = [
    "buy", "sell", "recommend", "recommendation",
    "target price", "price target", "undervalued", "overvalued",
    "should invest", "outperform", "underperform",
    "bullish", "bearish",
]

# The disclaimer names the thing it disclaims, so it must be exempt or it fails
# the check it exists to satisfy.
ALLOWED_CONTEXT = [
    "does not provide investment advice",
    "no buy/sell recommendations",
    "buyback",
]

PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in FORBIDDEN) + r")\b", re.IGNORECASE
)


def _files():
    for root in RENDERED:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix in {".py", ".html", ".js", ".css"} and path.is_file():
                yield path


def _rendered_strings(path: Path):
    """(lineno, text) for everything on this surface a user could read.

    Python is parsed rather than grepped: a comment explaining "rows we hold"
    is not a rendered string, and a checker that cannot tell the difference
    trains people to ignore it. Docstrings are skipped for the same reason.
    HTML, JS and CSS are scanned whole, since their text mostly is the output.
    """
    if path.suffix == ".py":
        tree = ast.parse(path.read_text(), filename=str(path))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docstrings:
                    continue
                yield node.lineno, node.value
    else:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            yield lineno, line


def test_rendered_surfaces_carry_no_advice_language():
    offenders: list[str] = []
    for path in _files():
        for lineno, text in _rendered_strings(path):
            low = text.lower()
            if any(ok in low for ok in ALLOWED_CONTEXT):
                continue
            for m in PATTERN.finditer(text):
                offenders.append(
                    f"{path.name}:{lineno}: {m.group(0)!r} in {text.strip()[:90]!r}")
    assert not offenders, "advice language in a rendered surface:\n" + "\n".join(offenders)


def test_the_scan_would_actually_catch_something():
    """A guard that cannot fail is decoration. This proves the pattern bites."""
    assert PATTERN.search("We recommend you buy this stock")
    assert PATTERN.search("Target price: 1200")
    assert PATTERN.search("undervalued at these levels")
    # "buyback" is a real corporate-action type and must survive the scan.
    assert "buyback" in ALLOWED_CONTEXT


def test_the_disclaimer_is_present_on_the_page():
    page = (APP / "static" / "index.html").read_text()
    assert "does not provide investment advice" in page


@pytest.mark.parametrize("word", ["buy", "sell", "recommend", "target price"])
def test_each_forbidden_word_is_matched(word):
    assert PATTERN.search(f"prefix {word} suffix")

"""The backend must parse on the Python the image actually runs.

This exists because it happened, and because nothing else in the suite could
have caught it. `lab.py` was written on Python 3.13, where a nested same-quote
expression inside an f-string — `f"class='x{" hot" if last else ""}'"` — is
legal (PEP 701, new in 3.12). The deployment image is `python:3.11-slim`, where
it is a `SyntaxError` at import. Every test passed, `ast.parse` succeeded, and
the container went into a restart loop with the API never binding a port.

A syntax guard is the cheapest check for the widest failure: a module that does
not parse takes the whole app down at import, so no other test can run to tell
you about it.

**`ast.parse(feature_version=...)` does not catch this**, which is worth
stating because it is the obvious thing to reach for and it is silently
useless here: CPython's tokenizer handles f-strings the 3.12 way regardless of
`feature_version`, so the exact construct that broke the image parses cleanly
under `(3, 11)`. It is kept below for the grammar it *does* gate — `match`, a
`type` statement, and so on — and the f-string case is checked directly.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APP = Path(__file__).resolve().parents[1] / "app"
DOCKERFILE = REPO / "Dockerfile"

# PEP 701 — the release that made a delimiter legal inside its own f-string.
PEP701 = (3, 12)


def runtime_version() -> tuple[int, int]:
    """The base image's minor version, read out of the Dockerfile rather than
    typed here, so moving the base image moves the guard with it."""
    if not DOCKERFILE.is_file():
        pytest.skip("no Dockerfile in this checkout")
    m = re.search(r"^FROM\s+python:(\d+)\.(\d+)", DOCKERFILE.read_text(),
                  re.M | re.I)
    if not m:
        pytest.skip("the base image is not a pinned python: tag")
    return int(m.group(1)), int(m.group(2))


def _modules() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def nested_quote_fstrings(source: str, filename: str = "<probe>") -> list[str]:
    """f-strings that contain a string quoted with their own delimiter.

    Before PEP 701 an f-string ran from its opening delimiter to the very next
    unescaped one, so that delimiter could not appear anywhere inside — not in
    the text and not in a replacement field. Any occurrence inside is therefore
    3.12-or-later syntax, which makes the rule exact rather than heuristic.

    `tokenize` is what makes it checkable. From 3.12 it emits FSTRING_START /
    FSTRING_END around each literal, so the enclosing delimiter is known and
    every STRING token between the two is a nested quote; if such a token
    carries the enclosing delimiter, 3.11 would have ended the f-string there
    and the file will not parse. A regex cannot do this — scanning for the
    "next" delimiter reproduces the pre-3.12 lexing and so, by construction,
    never sees the nesting it is looking for.
    """
    out = []
    stack: list[tuple[str, int]] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):  # pragma: no cover - unparseable
        return out
    for tok in tokens:
        if tok.type == getattr(tokenize, "FSTRING_START", None):
            # Strip the prefix (f, rf, fr, F...) to leave the delimiter.
            stack.append((tok.string.lstrip("fFrRbBuU"), tok.start[0]))
        elif tok.type == getattr(tokenize, "FSTRING_END", None):
            if stack:
                stack.pop()
        elif tok.type == tokenize.STRING and stack:
            delim, line = stack[-1]
            if delim in tok.string:
                out.append(f"{filename}:{line}: "
                           f"f-string delimited by {delim} contains {tok.string}")
    return out


def test_every_backend_module_parses_on_the_deployment_runtime():
    major, minor = runtime_version()
    assert len(_modules()) > 0, "no modules found to check"
    offenders = []
    for path in _modules():
        try:
            ast.parse(path.read_text(), filename=str(path),
                      feature_version=(major, minor))
        except SyntaxError as exc:
            offenders.append(f"{path.relative_to(REPO)}:{exc.lineno}: {exc.msg}")
    assert not offenders, (
        f"these modules use grammar newer than the deployment runtime "
        f"(python {major}.{minor}, from the Dockerfile):\n  "
        + "\n  ".join(offenders)
    )


def test_no_module_nests_a_quote_inside_its_own_fstring():
    """The construct that actually broke the image."""
    major, minor = runtime_version()
    if (major, minor) >= PEP701:
        pytest.skip(f"the deployment runtime is python {major}.{minor}, "
                    "which supports PEP 701 f-strings")
    assert len(_modules()) > 0, "no modules found to check"
    offenders = []
    for path in _modules():
        offenders += nested_quote_fstrings(
            path.read_text(), str(path.relative_to(REPO)))
    assert not offenders, (
        f"these f-strings nest their own delimiter, which is PEP 701 syntax "
        f"and a SyntaxError on python {major}.{minor}. They parse on this "
        f"machine and the container will not start:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_fires_on_the_syntax_that_actually_broke_the_image():
    """R-27. Verbatim from `lab.py` at the commit that would not start."""
    broke = '''rows += (
    f"<span class='barfill{" hot" if last else ""}'></span>"
    f"<span>{escape(systems[n]["label"])}</span>"
)
'''
    hits = nested_quote_fstrings(broke)
    # Both f-strings are caught. The count is not asserted — one literal can
    # nest several strings — but each line must appear.
    assert len(hits) > 0, "the guard missed the construct entirely"
    lines = {h.split(":")[1] for h in hits}
    assert lines == {"2", "3"}, f"the guard missed one of the two: {hits}"


def test_the_guard_accepts_the_two_legal_ways_to_write_it():
    """A guard that rejects the fix as well as the bug is not usable. Both
    repairs — swap the inner quote, or hoist the expression — must pass."""
    swapped = 'x = f"<span>{escape(systems[n][\'label\'])}</span>"\n'
    hoisted = 'hot = " hot" if last else ""\ny = f"<span class=\'x{hot}\'></span>"\n'
    assert nested_quote_fstrings(swapped) == []
    assert nested_quote_fstrings(hoisted) == []
    # And an ordinary f-string with no nesting at all.
    assert nested_quote_fstrings('z = f"plain {value} text"\n') == []
    # A triple-quoted f-string may legitimately contain single quotes.
    assert nested_quote_fstrings('w = f"""a "quoted" {v} word"""\n') == []

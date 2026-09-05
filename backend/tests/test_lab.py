"""Signal Lab displays only what it can source.

The page exists to make the engine inspectable, which it cannot do if any
figure on it is a literal someone typed. So the guard is structural rather than
a review convention: the module is parsed and every numeric constant is
examined, and only the handful CSS legitimately needs are allowed.

Same shape as the advice-language and claim guards, and per R-27 there is a
test proving this one fires.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import lab as lab_mod
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
SOURCE = Path(lab_mod.__file__)

# Numbers CSS and string slicing need. Anything else must come from a query or
# an artifact.
ALLOWED = {0, 1, 2, 3, 4, 8, 12}

# The CSS block is one large string literal full of legitimate pixel and rem
# values. It is exempted by name; every other string in the module is scanned.
CSS_TEMPLATE = "_PAGE"

# A figure typed into markup looks like this: a decimal, or an integer long
# enough to be a count rather than a column index.
_FIGURE_IN_TEXT = re.compile(r"\b\d+\.\d+\b|\b\d{3,}\b")


def _numeric_literals(path: Path) -> list[tuple[int, object]]:
    """Numeric constants in Python code, excluding anything inside a string —
    the CSS block is one big string and its pixel values are not data.

    Module-level UPPERCASE assignments are also excluded. A number given a name
    at module scope is declared structure that a reviewer can see and argue
    with (`RISK_ROW_COLUMNS = 7`); the thing this guard exists to catch is a
    figure typed inline into markup, where it silently becomes indistinguishable
    from one that was measured.
    """
    tree = ast.parse(path.read_text(), filename=str(path))

    declared: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and all(
            isinstance(t, ast.Name) and t.id.isupper() for t in node.targets
        ):
            for sub in ast.walk(node.value):
                declared.add(id(sub))

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool) and id(node) not in declared:
            out.append((node.lineno, node.value))
    return out


def _figures_in_strings(path: Path) -> list[tuple[int, str]]:
    """Numbers typed into string literals — the case an AST numeric scan misses
    entirely.

    This was found the hard way. The original guard walked numeric constants
    only, so injecting `f"<p>max breadth 0.3473 across 497 sessions</p>"` into
    the calibration section passed cleanly: to `ast` those digits are part of a
    string, not numbers. A figure typed into markup is the *primary* failure
    this guard exists to prevent, and the guard could not see it (R-31).
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    exempt: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == CSS_TEMPLATE for t in node.targets
        ):
            for sub in ast.walk(node.value):
                exempt.add(id(sub))

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in exempt:
            for hit in _FIGURE_IN_TEXT.findall(node.value):
                out.append((node.lineno, hit))
    return out


def test_the_lab_module_contains_no_data_constant():
    offenders = [(ln, v) for ln, v in _numeric_literals(SOURCE) if v not in ALLOWED]
    assert not offenders, (
        "numeric literals in the lab module — every figure must come from a "
        f"query or an artifact: {offenders}"
    )


def test_no_figure_is_typed_into_the_lab_markup():
    offenders = _figures_in_strings(SOURCE)
    assert not offenders, (
        "figures typed into string literals — these render as text and are "
        f"indistinguishable from measured values: {offenders}"
    )


def test_the_string_guard_fires_on_a_figure_typed_into_markup(tmp_path):
    """R-31. The exact injection that slipped past the numeric-only guard."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        '_PAGE = "body {{ margin: 0; padding: 24px; }}"\n'
        'def render():\n'
        '    return "<p>max breadth 0.3473 across 497 sessions</p>"\n'
    )
    found = _figures_in_strings(probe)
    assert ("0.3473" in [v for _, v in found]
            and "497" in [v for _, v in found]), found
    # And the CSS template is exempt, so pixel values do not trip it.
    assert not any(v == "24" for _, v in found)


def test_the_guard_would_actually_catch_a_typed_number(tmp_path):
    """A guard that cannot fail is decoration (R-27)."""
    probe = tmp_path / "probe.py"
    probe.write_text("def render():\n    return f'<td>{0.3473}</td>'\n")
    found = [v for _, v in _numeric_literals(probe) if v not in ALLOWED]
    assert found == [0.3473], "an inline figure slipped past the guard"


def test_a_named_module_constant_is_allowed_but_an_inline_one_is_not(tmp_path):
    """The distinction the guard turns on: named structure is reviewable, an
    inline figure in markup is not."""
    named = tmp_path / "named.py"
    named.write_text("MAX_ROWS = 7\n")
    assert [v for _, v in _numeric_literals(named) if v not in ALLOWED] == []

    inline = tmp_path / "inline.py"
    inline.write_text("def f():\n    return 'breadth ' + str(0.3473)\n")
    assert [v for _, v in _numeric_literals(inline) if v not in ALLOWED] == [0.3473]


def test_lab_renders_all_four_sections():
    r = client.get("/lab")
    assert r.status_code == 200
    body = r.text
    for section in ("Quality", "Reliability", "Calibration", "Evidence"):
        assert f">{section}</h2>" in body, f"missing section: {section}"


def test_lab_figures_come_from_the_artifacts_not_the_template():
    """If the benchmark artifact is present, its numbers must appear; if it is
    absent, the page says so rather than showing a stale figure."""
    import json
    r = client.get("/lab")
    metrics = lab_mod.RESULTS / "metrics.json"
    if not metrics.is_file():
        assert "No benchmark artifact" in r.text
        return
    m = json.loads(metrics.read_text())
    assert str(m["universe_size"]) in r.text
    assert str(m["held_out_sessions"]) in r.text


def test_lab_renders_the_risk_register_from_the_file():
    rows = lab_mod._risk_rows()
    if not rows:
        pytest.skip("risk register not readable in this checkout")
    r = client.get("/lab")
    assert rows[0][0] in r.text, "first risk id missing from the page"
    assert len(rows) >= 20, "register parsed suspiciously short"


def test_lab_degrades_rather_than_500s_without_a_database(monkeypatch):
    def boom():
        raise RuntimeError("no database")
    monkeypatch.setattr(lab_mod, "connect", boom)
    r = client.get("/lab")
    assert r.status_code == 200
    assert "Database unavailable" in r.text
    assert "Traceback" not in r.text


def test_the_digest_page_links_to_the_lab_from_the_footer():
    page = (Path(lab_mod.__file__).resolve().parents[1] / "static" / "index.html").read_text()
    assert 'href="/lab"' in page
    footer = page[page.index("<footer"):]
    assert 'href="/lab"' in footer, "the lab link belongs in the footer, not the nav"

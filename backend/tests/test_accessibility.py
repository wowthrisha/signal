"""A visual a screen reader cannot read is a visual half the check misses.

Same shape as the advice-language, hardcoded-number and claim guards: a
structural constraint over a rendered surface, enforced by the build rather
than by remembering.

**Honest scope note.** `test_every_svg_has_a_role_and_label` currently passes
vacuously — the product ships **zero** inline SVG. Per R-27 a guard that has
only ever passed is not evidence of anything, so the fires-test below runs the
same checker against synthetic markup and asserts it rejects an unlabelled
`<svg>`. The guard is preventative: it exists so the first chart or icon anyone
adds cannot arrive unlabelled.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
LAB = Path(__file__).resolve().parents[1] / "app" / "api" / "lab.py"
SURFACES = [STATIC, LAB]

_SVG_OPEN = re.compile(r"<svg\b([^>]*)>", re.I | re.S)


def svg_violations(markup: str) -> list[str]:
    """Every `<svg>` must declare `role="img"` and carry a descriptive
    `aria-label`, or be explicitly `aria-hidden` as decoration. There is no
    third option: an unlabelled graphic is either information a screen reader
    cannot reach, or decoration that should say so."""
    out = []
    for m in _SVG_OPEN.finditer(markup):
        attrs = m.group(1)
        if re.search(r'aria-hidden\s*=\s*"true"', attrs, re.I):
            continue
        problems = []
        if not re.search(r'role\s*=\s*"img"', attrs, re.I):
            problems.append('missing role="img"')
        label = re.search(r'aria-label\s*=\s*"([^"]*)"', attrs, re.I)
        if not label:
            problems.append("missing aria-label")
        elif len(label.group(1).strip()) < 4:
            problems.append(f"aria-label too short: {label.group(1)!r}")
        if problems:
            out.append(f"<svg …>: {'; '.join(problems)}")
    return out


@pytest.mark.parametrize("path", SURFACES, ids=lambda p: p.name)
def test_every_svg_has_a_role_and_label(path):
    assert not svg_violations(path.read_text())


def test_the_svg_guard_fires_on_an_unlabelled_graphic():
    """R-27. The product ships no SVG today, so without this the guard above
    is a test of nothing."""
    assert svg_violations('<svg viewBox="0 0 10 10"><path/></svg>')
    assert svg_violations('<svg role="img"><path/></svg>') == \
        ['<svg …>: missing aria-label']
    assert svg_violations('<svg aria-label="Breadth by session"><path/></svg>') == \
        ['<svg …>: missing role="img"']
    assert svg_violations('<svg role="img" aria-label="ok"><path/></svg>') == \
        ['<svg …>: aria-label too short: \'ok\'']
    # The two acceptable forms.
    assert svg_violations(
        '<svg role="img" aria-label="Breadth across 497 sessions"><path/></svg>') == []
    assert svg_violations('<svg aria-hidden="true"><path/></svg>') == []


def test_the_product_currently_ships_no_inline_svg():
    """Documents the vacuity rather than hiding it. If this starts failing,
    someone added a graphic and the guard above became load-bearing."""
    total = sum(len(_SVG_OPEN.findall(p.read_text())) for p in SURFACES)
    assert total == 0, (
        f"{total} inline <svg> now exist — the guard above is no longer "
        "vacuous, which is good; delete this test."
    )


# --- focus and names -------------------------------------------------------

def test_the_focus_ring_is_never_removed():
    css = STATIC.read_text()
    for banned in ("outline: none", "outline:none", "outline: 0", "outline:0"):
        assert banned not in css, f"focus ring suppressed: {banned!r}"
    assert ":focus-visible" in css and "outline: 2px solid var(--focus)" in css


def test_glyph_only_controls_carry_an_accessible_name():
    """A button whose entire content is `×` has no accessible name. `title`
    alone is not reliably announced, so these carry `aria-label` too."""
    markup = STATIC.read_text()
    assert 'aria-label="Remove ${w.symbol} from watchlist"' in markup
    assert 'aria-label="Add a symbol to your watchlist"' in markup


def test_decorative_glyphs_are_hidden_from_assistive_tech():
    """Chevrons and arrows are read aloud as punctuation noise otherwise."""
    markup = STATIC.read_text()
    for glyph in ("▸", "&#9656;", "&check;"):
        idx = markup.find(glyph)
        assert idx != -1, f"glyph vanished: {glyph}"
        window = markup[max(0, idx - 240):idx]
        assert "aria-hidden" in window, f"{glyph} is not hidden from screen readers"


def test_disclosures_report_their_expanded_state():
    """A rotating chevron communicates nothing to a screen reader."""
    markup = STATIC.read_text()
    assert 'aria-expanded="false"' in markup
    assert "aria-controls" in markup
    assert "setAttribute('aria-expanded'" in markup


def test_the_card_region_announces_updates():
    markup = STATIC.read_text()
    assert 'aria-live="polite"' in markup
    assert 'aria-busy' in markup

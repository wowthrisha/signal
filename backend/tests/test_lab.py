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
import json
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


# --- 6: the Model Card restructure -----------------------------------------
#
# The audience is a hiring engineer and the right reference is the Model Card
# format — intended use, quantitative analysis, limitations. The content was
# already model-card content; it was rendered as raw tables, which is a way of
# publishing a finding without stating it. Every table is still here, one
# disclosure down, and above each one is the shape it makes.


def _lab() -> str:
    r = client.get("/lab")
    assert r.status_code == 200, r.status_code
    return r.text


def test_the_masthead_is_not_implementation_documentation():
    """6a. "Every figure on this page is read at request time from a committed
    artifact or a live query — nothing is typed into the template" is a true
    and load-bearing claim, and it was the first sentence a reader met. It is
    a footnote now; the claim is not dropped."""
    body = _lab()
    sub = body[body.index('class="sub"'):body.index("</p>", body.index('class="sub"'))]
    assert "read at" not in sub and "typed into the template" not in sub, (
        f"the masthead is still documenting the implementation: {sub}"
    )
    assert "Back to the digest" in sub, "the masthead lost its only link"
    foot = body[body.index('class="foot"'):]
    assert "nothing is typed into the template" in foot, (
        "the claim was deleted rather than moved to a footnote"
    )


def test_the_reduction_is_bars_and_keeps_every_metric():
    """6b. 3251 to 166 is the whole argument for the pipeline, and a table of
    seven metric columns hides it among six other numbers."""
    m = _metrics_or_skip()
    body = _lab()
    names = [n for n in ("B0", "B1", "B2") if n in m["systems"]]
    assert len(names) == 3, f"the artifact is missing a system: {names}"
    bars = re.search(r'<div class=.bars.[^>]*>(.*?)</div>\s*<p', body, re.S)
    assert bars, "the reduction is not rendered as bars"
    top = max(m["systems"][n]["alerts"] for n in names)
    for n in names:
        v = m["systems"][n]["alerts"]
        assert f">{v}<" in body, f"{n}'s alert count is missing"
        # The width is the figure, at a length. A bar whose width does not
        # track its count is decoration.
        want = f"width:{v / top * lab_mod.BAR_TRACK_W:.1f}px"
        assert want in body, f"{n}'s bar is not proportional to its count"
    # Nothing was deleted: every metric column is one disclosure down.
    assert "Every metric, per system" in body
    for _, header in lab_mod._METRIC_COLS:
        assert f">{header}</th>" in body, f"the {header} column is gone"


def test_the_ablation_is_a_step_chart_with_every_delta_annotated():
    """6c. Seven rows describing one trajectory should look like one."""
    m = _metrics_or_skip()
    body = _lab()
    keys = [k for k in sorted(m["ablation"]) if "_" not in k]
    assert len(keys) > 2, f"too few ablation rows to chart: {keys}"
    vals = [m["ablation"][k]["alerts"] for k in keys]
    assert "<svg" in body and 'role="img"' in body
    for k, v in zip(keys, vals):
        assert f">{v}</text>" in body, f"ablation row {k}'s value is not on the chart"
        assert f">{k}</text>" in body, f"ablation row {k} is not labelled"
    for prev, cur in zip(vals, vals[1:]):
        d = cur - prev
        sign = "+" if d > 0 else ""
        assert f">{sign}{d}</text>" in body, f"the delta {sign}{d} is not annotated"
    # The challenger is not a step after F and must not be drawn as one.
    fuzzy = [k for k in m["ablation"] if "_" in k]
    assert fuzzy, "the artifact has no challenger row to exclude"
    chart = body[body.index("<svg"):body.index("</svg>")]
    for k in fuzzy:
        assert f">{k}</text>" not in chart, (
            f"{k} is drawn as a step, which asserts an ordering it does not have"
        )
    assert "Every metric, per ablation row" in body


def test_the_ablation_chart_does_not_pretend_the_decline_is_monotone():
    """The brief for this called the sequence a monotone decline. It is not:
    D adds the CUSUM drift detector and the alert count goes UP, because a
    second detector finds what the first one did not. The chart draws the
    trajectory the artifact actually contains, and this guard exists so that
    a later "tidy-up" cannot quietly sort it into a descent."""
    m = _metrics_or_skip()
    keys = [k for k in sorted(m["ablation"]) if "_" not in k]
    vals = [m["ablation"][k]["alerts"] for k in keys]
    rises = [(a, b) for a, b in zip(vals, vals[1:]) if b > a]
    if not rises:
        pytest.skip("this artifact's ablation happens to be monotone")
    body = _lab()
    for prev, cur in rises:
        assert f">+{cur - prev}</text>" in body, (
            f"the step that ADDS {cur - prev} alerts is not annotated as a rise"
        )
    assert "not monotone" in body, (
        "the chart's own description does not admit the sequence rises"
    )


def test_the_risk_register_surfaces_the_open_rows_and_hides_the_rest():
    """6d. Thirty-nine rows rendered by default is a data dump. What a reader
    wants is how much is still open and which of it would hurt.

    Scoped to product rows: the page renders those and counts over those, so
    the guard reasons about the same population the page does.
    """
    rows = lab_mod._product_risks()
    if not rows:
        pytest.skip("risk register not readable in this checkout")
    body = _lab()
    buckets = {}
    for r in rows:
        buckets.setdefault(lab_mod._risk_status(r), []).append(r)
    assert "OPEN" in buckets, "no open risks, so this guard proves nothing"
    for b, rs in buckets.items():
        assert f">{b}</span>" in body, f"the {b} bucket has no count"
        assert f">{len(rs)}</span>" in body, f"the {b} count is missing"
    # The surfaced rows are OPEN and high-impact, and there are few of them.
    surfaced = re.search(r"open risks that would hurt most</h3>(.*?)</table>",
                         body, re.S)
    assert surfaced, "no open risks are surfaced above the disclosure"
    ids = re.findall(r"<td[^>]*>(R-\d+)</td>", surfaced.group(1))
    assert 0 < len(ids) <= lab_mod.RISK_SURFACED, (
        f"expected at most {lab_mod.RISK_SURFACED} surfaced rows, got {ids}"
    )
    by_id = {r[0]: r for r in rows}
    for rid in ids:
        assert lab_mod._risk_status(by_id[rid]) == "OPEN", f"{rid} is not open"
    # And every row is still reachable.
    assert f"All {len(rows)} rows" in body
    for r in rows:
        assert f">{r[0]}</td>" in body, f"{r[0]} was dropped from the register"


def test_the_page_carries_no_process_scoped_risk():
    """The submission window, the judge and the gate numbers are this build's
    own timeline, not a property of the system a reader is evaluating. They
    stay in the register and stay off the page.

    Derived from the scope field on both sides — nothing here names an id, so
    a process row added later is filtered without editing this test.
    """
    everything = lab_mod._risk_rows()
    assert everything, "register not readable, so this guard proves nothing"
    process = [r for r in everything
               if lab_mod._risk_scope(r) != lab_mod.RISK_PRODUCT_SCOPE]
    assert process, "no process-scoped row exists, so this guard proves nothing"
    body = _lab()
    for r in process:
        assert f">{r[0]}</td>" not in body, f"{r[0]} is process-scoped and rendered"
    # And the filtering removed only those: the page still carries every
    # product row, which is what stops this becoming a way to hide a risk.
    for r in lab_mod._product_risks():
        assert f">{r[0]}</td>" in body, f"{r[0]} is product-scoped and missing"


def test_every_register_row_declares_a_scope():
    """A row with no scope, or a typo in one, must not silently vanish from
    the page — it would be indistinguishable from a risk someone hid."""
    rows = lab_mod._risk_rows()
    assert rows, "register not readable, so this guard proves nothing"
    for r in rows:
        assert lab_mod._risk_scope(r) in {"product", "process"}, (
            f"{r[0]} has scope {lab_mod._risk_scope(r)!r}")


def test_the_register_status_is_read_from_the_last_cell():
    """R-23's response contains `|z| > 2`, whose pipes split that row into more
    fields than every other one. Read at a fixed index its status is a
    fragment of the response and it lands in a bucket of its own."""
    rows = lab_mod._risk_rows()
    if not rows:
        pytest.skip("risk register not readable in this checkout")
    odd = [r for r in rows if len(r) != lab_mod.RISK_ROW_COLUMNS]
    assert odd, "no ragged row in the register, so this guard proves nothing"
    for r in odd:
        assert lab_mod._risk_status(r) != "OTHER", (
            f"{r[0]} has {len(r)} cells and its status was not recognised"
        )
        # Scope sits immediately before status and is read the same way, so a
        # ragged row must not lose it either — read from the left it would be
        # a fragment of the response, and the row would drop off the page.
        assert lab_mod._risk_scope(r) in {"product", "process"}, (
            f"{r[0]} has {len(r)} cells and its scope was not recognised"
        )


def test_the_fault_matrix_is_a_grid_stating_digest_equality():
    """6e. Eight 12-character hashes are not comparable by eye. The fact they
    encode is a yes/no, and the tile says it."""
    faults = sorted(lab_mod._artifact_root("results").glob("replay_*/metrics.json"))
    if not faults:
        pytest.skip("no replay artifact in this checkout")
    scenarios = json.loads(faults[-1].read_text())["scenarios"]
    assert "clean" in scenarios, "no baseline to compare against"
    body = _lab()
    for name in scenarios:
        assert f">{name}</p>" in body, f"the {name} scenario has no tile"
    clean = scenarios["clean"]["ledger_digest"]

    # The invariant, derived the same way the page derives it: a scenario that
    # perturbs delivery only must land a byte-identical ledger.
    delivery = [n for n, s in scenarios.items()
                if n != "clean"
                and s["observations"] == scenarios["clean"]["observations"]
                and s["events_emitted"] == scenarios["clean"]["events_emitted"]
                and not s["suppressed"] and not s["uncertain"]
                and not s["circuit_breaks"]]
    assert delivery, "no delivery-only scenario, so the invariant is untested"
    for name in delivery:
        assert scenarios[name]["ledger_digest"] == clean, (
            f"{name} perturbs delivery only and its ledger diverged from clean"
        )
    # Counted on the tiles, not on the page: the grid's aria-label repeats
    # every tile's state so a screen reader gets the same grid.
    tiles = re.findall(r"<p class='tile-s'>([^<]*)</p>", body)
    assert tiles, "the grid rendered no tiles"
    assert sum(t == "ledger identical to clean" for t in tiles) == len(delivery), (
        f"the grid does not claim the invariant for exactly the scenarios "
        f"that hold it: {tiles} vs {delivery}"
    )
    assert body.count("ledger identical to clean") == 2 * len(delivery), (
        "the grid's screen-reader label does not describe the same tiles"
    )
    # A different input producing a different ledger is correct, not a failure,
    # and the grid must not report it as one.
    assert "different input, different ledger" in body
    assert "LEDGER DIVERGED" not in body, (
        "a scenario that should be byte-identical to clean is not"
    )
    assert "Every counter, per scenario" in body


def _metrics_or_skip() -> dict:
    m = lab_mod._metrics()
    if not m:
        pytest.skip("no benchmark artifact in this checkout")
    return m

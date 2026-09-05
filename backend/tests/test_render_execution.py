"""Execute the render path. Every other UI guard only reads the markup.

**This is the gap that let a ReferenceError reach production while 375 tests
passed.** `test_readme_sections`, `test_accessibility`, `test_lab` and the rest
all *inspect* source text — they assert a string is present, a hex is absent, a
heading exists. Not one of them ever called `cardHTML`. A function can be
deleted outright and every one of those guards stays green, because the text
they look for lives in the callers that still reference it.

So this file does the one thing none of them did: it runs the page's script in
node against a real `/api/digest` payload and fails on any thrown exception.

Three separate deletions have now been caused by the same edit shape —
replacing a span of source whose contents were not read. `freshnessBadge`'s
regex took `freshnessText`; a README span-replacement took a whole section
(R-25); and the card rebuild replaced `cardHTML..filteredHTML`, a span that
contained `chainHTML` (R-33). Markup inspection cannot see any of them.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# A payload with the shapes that actually occur: cards with and without
# evidence, several temporal relations, a null-heavy card, and the caught-up
# state. Kept inline so the test does not depend on a running server.
PAYLOAD = {
    "since": "2026-09-02",
    "latest_session": "2026-09-03",
    "funnel": {"watched": 30, "moved": 22, "surfaced": 2},
    "filtered_count": 18,
    "filtered_reasons": {"explained_by_market": 4, "below_threshold": 14,
                         "low_confidence": 0},
    "evidence_chain": [
        {"stage": "moved", "count": 22, "label": "moved more than 1%"},
        {"stage": "explained_by_market", "count": 4, "label": "explained by market or sector"},
        {"stage": "stock_specific", "count": 18, "label": "stock-specific candidates"},
        {"stage": "confidence_passed", "count": 18, "label": "passed the confidence gate"},
        {"stage": "surfaced", "count": 2, "label": "surfaced"},
    ],
    "surfaced_below_display_threshold": 0,
    # The four cut points the plain-language labels are drawn from. They are
    # the detectors' and §7's own numbers; a card that renders a band without
    # them has invented it.
    "salience_config": {"ecdf_window": 250, "min_history": 60,
                        "d1_threshold": 3.0, "d2_threshold": 4.0,
                        "c_gate": 0.3, "c_gate_uncorroborated": 0.5},
    "freshness_policy": {"fresh_max_sessions_behind": 0, "delayed_max_sessions_behind": 2},
    "all_cards_lack_evidence": False,
    "cursor_head": 5961,
    "cursor": None,
    "cards": [
        {
            "symbol": "COALINDIA", "tier": "C", "total_return_pct": 3.97,
            "sector_return_pct": 0.37, "residual_pct": 3.67, "market_pct": 0.09,
            "sector_only_pct": 0.28, "gate": "C>=0.5 AND U>=0.99",
            "session_date": "2026-09-02", "u_score": 1.0, "i_score": 0,
            "confidence": 1.0, "event_type": "JUMP", "headline": "Single-session move",
            "freshness": "DELAYED", "sessions_behind": 1,
            "evidence": [
                {"temporal_relation": "PRECEDES", "temporal_label": "Published before the move",
                 "source_tier": 1, "source_name": "NSE Corporate Announcements",
                 "document_type": "Press Release", "title": "A filing",
                 "published_at": "2026-09-01T10:00:00+00:00",
                 "published_at_basis": "FILED_AT",
                 "retrieved_at": "2026-09-05T03:00:00+00:00",
                 "url": "https://nsearchives.nseindia.com/corporate/X.pdf",
                 "linkable": True, "checksum": "abc"},
                {"temporal_relation": "SAME_SESSION_UNORDERED",
                 "temporal_label": "Same session; ordering unknown",
                 "source_tier": 1, "source_name": "NSE Corporate Announcements",
                 "document_type": "General Updates", "title": "Another filing",
                 "published_at": "2026-09-02T11:00:00+00:00",
                 "published_at_basis": "FILED_AT",
                 "retrieved_at": "2026-09-05T03:00:00+00:00",
                 "url": None, "linkable": False, "checksum": "def"},
            ],
        },
        {
            # The fully-populated shape. The payload was deliberately
            # null-heavy, which meant the plain-language path — unusualness
            # bands, outcome vocabulary, forward horizons, the derived reasons
            # in "Why this?" — was never once executed by this file. A guard
            # that only ever sees nulls proves the null branch and nothing else.
            "symbol": "IFCI", "tier": "C", "total_return_pct": 11.93,
            "sector_return_pct": -0.87, "residual_pct": 12.39,
            "market_pct": -0.87, "sector_only_pct": 0.0,
            "gate": "C>=0.5 AND U>=0.99", "session_date": "2026-09-02",
            "u_score": 1.0, "i_score": 0, "confidence": 0.92,
            "event_type": "DRIFT", "z": 4.83,
            "headline": "Sustained one-directional drift over 3 sessions",
            "freshness": "DELAYED", "sessions_behind": 1, "evidence": [],
            "outcomes": {
                "horizons": [
                    {"h": 1, "residual_pct": -3.03, "outcome": "NORMALIZED"},
                    {"h": 3, "residual_pct": None, "outcome": "NOT_YET_OBSERVABLE"},
                    {"h": 5, "residual_pct": None, "outcome": "NOT_YET_OBSERVABLE"},
                ],
                "policy": {"material_fraction": 0.5, "horizons": [1, 3, 5]},
                "basis": "stock-specific residual, betas held fixed",
                "base_rates": {
                    "n": 263, "counts": {"CONTINUED": 64, "REVERSED": 56,
                                         "NORMALIZED": 143},
                    "percentages": {"CONTINUED": 24.3, "REVERSED": 21.3,
                                    "NORMALIZED": 54.4},
                    "min_n_for_percentages": 30, "horizon": 5,
                    "event_type": "DRIFT", "tier": "C",
                    "skipped_unobservable_or_unadjustable": 12,
                    "scope": "not the held-out window",
                    "cohort_sessions": 497, "cohort_events": 5962,
                    "note": "historical frequency, not a forecast",
                },
            },
        },
        {
            # Every optional field null: the shape that breaks naive templates.
            "symbol": "SPARSE", "tier": "C", "total_return_pct": None,
            "sector_return_pct": None, "residual_pct": None, "market_pct": None,
            "sector_only_pct": None, "gate": None, "session_date": None,
            "u_score": None, "i_score": None, "confidence": None,
            "event_type": None, "headline": "", "freshness": "UNKNOWN",
            "sessions_behind": None, "evidence": [],
        },
    ],
}

WATCHLIST = [
    {"isin": "INE1", "symbol": "COALINDIA", "name": "Coal India", "sector": "energy", "muted": False},
    {"isin": "INE2", "symbol": "MUTED", "name": "Muted Co", "sector": "energy", "muted": True},
]

HARNESS = textwrap.dedent("""
    const fs = require('fs');
    const noop = () => {};
    const store = {};
    const mk = (id) => ({
      addEventListener: noop, setAttribute: noop, removeAttribute: noop,
      classList: { toggle: noop, contains: () => true, add: noop, remove: noop },
      style: {}, dataset: {}, value: '', disabled: false,
      closest: () => null, querySelector: () => mk('q'),
      set textContent(v) { store[id + ':text'] = v },
      get textContent() { return store[id + ':text'] || '' },
      set innerHTML(v) { store[id + ':html'] = v },
      get innerHTML() { return store[id + ':html'] || '' },
      set hidden(v) { store[id + ':hidden'] = v },
      get hidden() { return store[id + ':hidden'] !== false },
    });
    global.document = {
      getElementById: mk, querySelector: () => mk('q'),
      querySelectorAll: () => [], addEventListener: noop,
      visibilityState: 'visible',
    };
    global.window = { addEventListener: noop };
    global.fetch = () => Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    global.setInterval = () => 0;
    global.clearInterval = noop;
    global.localStorage = { getItem: () => null, setItem: noop };
    global.crypto = { randomUUID: () => '11111111-1111-4111-8111-111111111111' };

    eval(fs.readFileSync(process.argv[2], 'utf8'));

    const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
    const watchlist = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

    // The full render path, exactly as load() drives it.
    render(payload);
    renderWatchlist(watchlist);
    render({ ...payload, cards: [], cursor: 12, all_cards_lack_evidence: false });
    render({ ...payload, latest_session: null });
    renderWatchlist([]);

    const html = store['cards:html'] || '';
    console.log(JSON.stringify({
      ok: true,
      cards_html_len: html.length,
      cards_html: html,
      funnel_lede: store['funnel-lede:html'] || '',
      head_summary: store['head-summary:text'] || '',
      help_body: store['help-body:html'] || '',
      chain_rendered: (store['filtered-body:html'] || '').includes('Evidence chain'),
    }));
""").strip()


def _script() -> str:
    return re.search(r"<script>(.*?)</script>", STATIC.read_text(), re.S).group(1)


def _run(tmp_path: Path, script: str) -> subprocess.CompletedProcess:
    js = tmp_path / "app.js"
    js.write_text(script)
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(PAYLOAD))
    watch = tmp_path / "watchlist.json"
    watch.write_text(json.dumps(WATCHLIST))
    return subprocess.run(
        [NODE, str(harness), str(js), str(payload), str(watch)],
        capture_output=True, text=True, timeout=60)


def test_the_render_path_executes_without_throwing(tmp_path):
    """The test that would have caught `chainHTML is not defined` before it
    shipped, and the two deletions before it."""
    r = _run(tmp_path, _script())
    assert r.returncode == 0, (
        "the page's render path threw:\n" + (r.stderr or "")[-1500:]
    )
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["ok"] is True
    assert out["cards_html_len"] > 0, "render produced no card markup"


def test_the_evidence_chain_actually_renders(tmp_path):
    """Not merely that `chainHTML` is defined — that its output reaches the
    drawer. A restored function wired to nothing is the same outage."""
    r = _run(tmp_path, _script())
    assert r.returncode == 0, (r.stderr or "")[-1500:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["chain_rendered"], "the evidence chain is not in the drawer"


def test_the_execution_guard_fires_when_a_declaration_is_deleted(tmp_path):
    """R-27, and R-33's whole point. Delete `chainHTML`'s declaration the way
    the span-replacement did, and this must fail — a markup-inspection guard
    would not notice, because the *call site* still mentions the name."""
    broken = re.sub(r"function chainHTML\(chain\) \{.*?\n\}\n", "",
                    _script(), flags=re.S)
    assert "chainHTML(" in broken, "the call site should survive the deletion"
    assert "function chainHTML" not in broken

    r = _run(tmp_path, broken)
    assert r.returncode != 0, "deleting a render function did not fail the guard"
    assert "chainHTML is not defined" in r.stderr


def test_every_render_helper_referenced_is_also_defined(tmp_path):
    """A cheap static backstop for the same class: any `fooHTML(` call must
    have a matching declaration."""
    script = _script()
    called = set(re.findall(r"\b(\w+HTML)\s*\(", script))
    defined = set(re.findall(r"function\s+(\w+HTML)\s*\(", script))
    missing = {c for c in called if c not in defined and not c.startswith("inner")}
    assert not missing, f"called but never defined: {sorted(missing)}"


# --- a render exception is not a backend outage ----------------------------

ERROR_HARNESS = textwrap.dedent("""
    const fs = require('fs');
    const noop = () => {};
    const store = {};
    const mk = (id) => ({
      addEventListener: noop, setAttribute: noop, removeAttribute: noop,
      classList: { toggle: noop, contains: () => true, add: noop, remove: noop },
      style: {}, dataset: {}, value: '', disabled: false,
      closest: () => null, querySelector: () => mk('q'),
      set textContent(v) { store[id + ':text'] = v },
      get textContent() { return store[id + ':text'] || '' },
      set innerHTML(v) { store[id + ':html'] = v },
      get innerHTML() { return store[id + ':html'] || '' },
      set hidden(v) { store[id + ':hidden'] = v },
      get hidden() { return store[id + ':hidden'] !== false },
    });
    global.document = {
      getElementById: mk, querySelector: () => mk('q'),
      querySelectorAll: () => [], addEventListener: noop,
      visibilityState: 'visible',
    };
    global.window = { addEventListener: noop };
    global.setInterval = () => 0;
    global.clearInterval = noop;
    global.localStorage = { getItem: () => null, setItem: noop };
    global.crypto = { randomUUID: () => '1' };

    const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
    const mode = process.argv[4];

    // MODE render : the API answers 200 and render() throws — the exact
    //               production shape, a ReferenceError after a good response.
    // MODE network: fetch itself rejects, which is a real outage.
    global.fetch = (url) => {
      if (mode === 'network') return Promise.reject(new TypeError('Failed to fetch'));
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    };

    eval(fs.readFileSync(process.argv[2], 'utf8'));

    if (mode === 'render') {
      // Break one renderer *after* the script has loaded, leaving fetch healthy.
      chainHTML = () => { throw new ReferenceError('boom is not defined'); };
    }

    load().then(() => {
      console.log(JSON.stringify({
        funnel: store['funnel:html'] || '',
        cards: store['cards:html'] || '',
        banner: store['api-banner:text'] || '',
      }));
    });
""").strip()

NETWORK_COPY = "The API is not responding"


def _run_failure(tmp_path: Path, mode: str) -> dict:
    js = tmp_path / "app.js"
    js.write_text(_script())
    harness = tmp_path / "err.js"
    harness.write_text(ERROR_HARNESS)
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(PAYLOAD))
    r = subprocess.run([NODE, str(harness), str(js), str(payload), mode],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.stderr or "")[-1500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_render_exception_is_not_reported_as_an_api_outage(tmp_path):
    """The production bug. `/api/digest` answered 200 and the funnel had
    already drawn "21 moved · 17 filtered", yet the page said the API was down.
    Sending someone to check a healthy server is worse than saying nothing."""
    out = _run_failure(tmp_path, "render")
    surface = out["funnel"] + out["cards"] + out["banner"]
    assert NETWORK_COPY not in surface, (
        "a render exception still produces the network-failure copy: " + surface[:400]
    )


def test_a_render_exception_names_itself_as_a_page_bug(tmp_path):
    out = _run_failure(tmp_path, "render")
    surface = out["funnel"] + out["cards"] + out["banner"]
    assert "failed to render" in surface.lower()
    assert "ReferenceError" in surface
    assert "not an outage" in surface.lower()


def test_a_real_network_failure_still_says_the_api_is_not_responding(tmp_path):
    """The other half: separating the two states must not silence the true
    outage message."""
    out = _run_failure(tmp_path, "network")
    surface = out["funnel"] + out["cards"] + out["banner"]
    assert NETWORK_COPY in surface, surface[:400]


# --- the plain-language layer, executed ------------------------------------
#
# Every guard below reads the *rendered output*, not the source. The whole
# reason this file exists is that a markup-inspection guard cannot tell a
# helper that runs from one that was deleted, and these labels are produced by
# helpers that a future span-replacement could take out whole.


def _cards(tmp_path) -> str:
    r = _run(tmp_path, _script())
    assert r.returncode == 0, (r.stderr or "")[-1500:]
    return json.loads(r.stdout.strip().splitlines()[-1])["cards_html"]


def test_confidence_renders_a_value_and_its_gate(tmp_path):
    """The regression this replaced: the gate bar shipped without the number,
    and four cards read `conf` beside an orange stub with no magnitude."""
    html = _cards(tmp_path)
    assert "data quality" in html
    # The numeric and the gate it is measured against, both, in the title.
    assert re.search(r"Data quality \d\.\d\d, against the gate of 0\.3", html), (
        "confidence rendered without its numeric value or without the gate"
    )
    # And a magnitude a sighted reader can see without hovering.
    assert "bar-accent" in html


def test_the_card_face_carries_words_not_sigma(tmp_path):
    """2a. The band's axis says normal/unusual; sigma moved one layer down but
    was not deleted, and the screen-reader label still carries it."""
    html = _cards(tmp_path)
    # The card that actually has a residual to band. A card with z=None
    # correctly renders no band at all, so asserting on it proves nothing.
    card = [a for a in html.split("<article") if "IFCI" in a][0]
    face = card.split("Technical details")[0]
    visible = re.sub(r"<[^>]+>", " ", face)
    assert "&#963;" not in visible and "\u03c3" not in visible, (
        f"a sigma value is still visible on the card face: {visible[:400]}"
    )
    for w in ("normal", "unusual"):
        assert w in visible
    assert re.search(r"Standardised residual [-\d.]+ sigma", card), (
        "the aria-label lost the standardised residual"
    )
    assert "standardised residual" in card.split("Technical details")[1], (
        "sigma was deleted rather than moved down a layer"
    )


def test_the_tier_letter_left_the_face_but_not_the_page(tmp_path):
    """2c. `TIER C` read as a low grade. It and its gate expression belong in
    Technical details — belong there, not nowhere."""
    html = _cards(tmp_path)
    face = html.split("Why this?")[0]
    assert "TIER C" not in face and "TIER B" not in face
    assert "unusual" in face, "the face lost the tier's meaning"
    tech = html.split("Technical details")[1]
    assert "Tier C" in tech, "the tier letter was deleted instead of moved"
    assert "C>=0.5" in tech, "the gate expression was deleted instead of moved"


def test_the_verdict_leads_in_plain_english(tmp_path):
    """2b. The technical term survives as a caption; it does not lead."""
    html = _cards(tmp_path)
    assert "This stock did." in html or "not the company." in html
    assert "stock-specific move" in html or "explained by" in html


def test_why_this_opens_on_plain_reasons(tmp_path):
    """3. Tier one is words; tier two is the record, nested beneath it."""
    html = _cards(tmp_path)
    assert "Why this?" in html
    # The populated card, not the null one: a reason that cannot be derived
    # is correctly absent, and asserting on the null card would prove nothing.
    why = html.split("Why this?")[2]
    plain = why.split("Technical details")[0]
    assert "It moved much more than this stock usually does." in plain
    assert "data-quality check" in plain
    # Nested, not adjacent: the technical block sits inside the drawer body.
    assert "<details" in why and why.index("<details") < why.index("Technical details")


def test_the_outcome_words_are_defined_from_the_policy(tmp_path):
    """2d. The legend is written from material_fraction, so it cannot drift
    from the classifier that produced the words."""
    html = _cards(tmp_path)
    for phrase in ("continued = kept going", "went the other way",
                   "faded back toward normal"):
        assert phrase in html, f"missing outcome definition: {phrase}"
    assert "50%" in html, "the legend did not derive its boundary from the policy"


def test_the_orientation_line_and_legend_are_derived(tmp_path):
    """5a and 5b. Both come from the payload; neither is a typed constant."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    lede, help_body = out["funnel_lede"], out["help_body"]
    f = PAYLOAD["funnel"]
    assert str(f["surfaced"]) in lede and str(f["watched"]) in lede, (
        f"the orientation line does not carry the funnel's own numbers: {lede}"
    )
    assert "worth a closer look" in lede
    assert str(f["watched"]) in out["head_summary"]
    missing = [t for t in ("Watchlist", "Moved", "Attention", "Evidence",
                           "After the move") if t not in help_body]
    assert not missing, f"the legend is missing {missing}"
    # Derived from the server's own chain label, not from a threshold retyped
    # into the template. The chain must be non-empty or the search below
    # proves nothing.
    assert PAYLOAD["evidence_chain"]
    moved = next(s for s in PAYLOAD["evidence_chain"] if s["stage"] == "moved")
    assert moved["label"] in help_body


def test_the_plain_language_guards_fire_when_the_helper_is_deleted(tmp_path):
    """R-27. Delete the confidence renderer the way a span-replacement would,
    and the page must fail loudly rather than quietly losing the label."""
    broken = re.sub(r"function confidenceGate\(c, ctx = \{\}\) \{.*?\n\}\n",
                    "", _script(), flags=re.S)
    assert "confidenceGate(" in broken and "function confidenceGate" not in broken
    r = _run(tmp_path, broken)
    assert r.returncode != 0, "deleting the confidence renderer did not fail the guard"
    assert "confidenceGate is not defined" in r.stderr

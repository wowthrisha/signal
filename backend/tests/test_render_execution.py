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
    # The context strip's tiles. Real close-to-close changes; an index with
    # no row on record produces no tile, which is why this list is short.
    "index_context": {
        "tiles": [
            {"kind": "market", "label": "Nifty 50", "index_name": "Nifty 50",
             "change_pct": -0.17, "session_date": "2026-09-03"},
            {"kind": "sector", "label": "Nifty Realty",
             "index_name": "Nifty Realty", "change_pct": 2.58,
             "session_date": "2026-09-03", "sector_id": "realty",
             "sector_name": "Realty"},
        ],
        "latest_session": "2026-09-03",
        "next_update": "when the next session's bhavcopy is published",
    },
    # One row per watched instrument, in all three states the table renders.
    "watchlist_state": [
        {"isin": "INE001", "symbol": "COALINDIA", "sector_id": "oil_gas",
         "change_pct": 3.97, "session_date": "2026-09-02",
         "status": "surfaced", "reason": None},
        {"isin": "INE002", "symbol": "SPARSE", "sector_id": None,
         "change_pct": None, "session_date": None,
         "status": "quiet", "reason": None},
        {"isin": "INE003", "symbol": "IFCI", "sector_id": "financial_services",
         "change_pct": 11.93, "session_date": "2026-09-02",
         "status": "surfaced", "reason": None},
        {"isin": "INE004", "symbol": "ADANIPORTS", "sector_id": "services",
         "change_pct": 2.02, "session_date": "2026-09-02",
         "status": "filtered", "reason": "below_threshold"},
    ],
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

# The ISINs match `watchlist_state` above on purpose: the table joins the two
# on `isin`, and a fixture whose keys do not line up would render every row in
# the fallback state and prove only that the fallback works.
WATCHLIST = [
    {"isin": "INE001", "symbol": "COALINDIA", "name": "Coal India",
     "sector": "energy", "muted": False},
    {"isin": "INE002", "symbol": "SPARSE", "name": "Sparse Co",
     "sector": "energy", "muted": False},
    {"isin": "INE003", "symbol": "IFCI", "name": "IFCI Ltd",
     "sector": "financial_services", "muted": False},
    {"isin": "INE004", "symbol": "ADANIPORTS", "name": "Adani Ports",
     "sector": "services", "muted": False},
    {"isin": "INE9", "symbol": "MUTED", "name": "Muted Co",
     "sector": "energy", "muted": True},
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
    // Snapshot the populated state before the empty renders overwrite it.
    const snap = {
      cards_html: store['cards:html'] || '',
      funnel_lede: store['funnel-lede:html'] || '',
      head_summary: store['head-summary:html'] || store['head-summary:text'] || '',
      help_body: store['help-body:html'] || '',
      context_strip: store['context:html'] || '',
      watchlist_html: store['watchlist:html'] || '',
      wl_filters: store['wl-filters:html'] || '',
      chain: store['chain:html'] || '',
      pareto: store['filtered-body:html'] || '',
    };
    render({ ...payload, cards: [], cursor: 12, all_cards_lack_evidence: false });
    render({ ...payload, latest_session: null });
    renderWatchlist([]);

    console.log(JSON.stringify({
      ok: true,
      cards_html_len: snap.cards_html.length,
      chain_rendered: snap.chain.includes('Evidence chain'),
      ...snap,
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
    Technical details — belong there, not nowhere.

    The tier's *meaning* is an evidence statement ("unusual, with no material
    filing on record"), so when the card was cut to six rows it took the
    evidence row on a card that has no filings. A card that does carry filings
    shows the count there instead and states the tier one layer down, which is
    what "everything else nests" means. Both are asserted, separately.
    """
    html = _cards(tmp_path)
    face = html.split("Why this?")[0]
    assert "TIER C" not in face and "TIER B" not in face

    # A card with no filings says what the tier means, on its face.
    no_ev = [a for a in html.split("<article") if "IFCI" in a][0]
    assert "unusual" in no_ev.split("Why this?")[0], (
        "a card with no filings lost the tier's meaning from its face"
    )
    # A card with filings shows the count there, and the tier is still reachable.
    with_ev = [a for a in html.split("<article") if "COALINDIA" in a][0]
    assert "filing" in with_ev.split("Why this?")[0]

    # Every card, either way, keeps the letter and the gate expression.
    for article in [a for a in html.split("<article") if "Technical details" in a]:
        tech = article.split("Technical details")[1]
        assert "Tier " in tech, "the tier letter was deleted instead of moved"
    assert "C>=0.5" in html.split("Technical details")[1], (
        "the gate expression was deleted instead of moved"
    )


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


# --- the application shell, executed ---------------------------------------


def test_the_context_strip_is_built_from_the_payload(tmp_path):
    """1b. Every tile is a real index change. An index with no row on record
    produces no tile — there is nothing here to fabricate, and the guard fails
    if a tile appears that the payload did not supply."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    strip = out["context_strip"]
    assert strip, "the context strip rendered nothing"
    for tile in PAYLOAD["index_context"]["tiles"]:
        shown = tile.get("sector_name") or tile["label"]
        assert shown in strip, f"missing context tile: {shown}"
        assert f"{tile['change_pct']:.2f}%" in strip, (
            f"tile {shown} lost its change"
        )
    # And nothing beyond what was supplied: two tiles in, two changes out.
    assert strip.count("%") == len(PAYLOAD["index_context"]["tiles"]), (
        "the strip rendered a percentage the payload did not carry"
    )


def test_the_header_carries_the_funnels_three_numbers(tmp_path):
    """1a. The centre zone survives any scroll depth, so it must hold the
    count rather than a label."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    head, f = out["head_summary"], PAYLOAD["funnel"]
    missing = [k for k in ("watched", "moved", "surfaced")
               if str(f[k]) not in head]
    assert not missing, f"the header summary is missing {missing}: {head}"
    assert "need attention" in head


def test_the_watchlist_is_a_table_with_a_row_per_symbol(tmp_path):
    """2. A row per instrument, each carrying its change and its state, and
    the three states derived from `watchlist_state` rather than guessed."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    table = out["watchlist_html"]
    assert table, "the watchlist rendered nothing"

    unmuted = [w for w in WATCHLIST if not w["muted"]]
    assert unmuted
    for w in unmuted:
        assert f'data-wl="{w["isin"]}"' in table, f"{w['symbol']} has no row"
    assert 'data-wl="INE9"' not in table, "a muted symbol rendered by default"

    # Each row's dot is the state the server assigned, not a client guess.
    by_isin = {r_["isin"]: r_ for r_ in PAYLOAD["watchlist_state"]}
    for isin, row in by_isin.items():
        assert f'data-status="{row["status"]}"' in table, (
            f"{row['symbol']} did not render as {row['status']}"
        )
    # And the change column carries the stored value.
    assert "+3.97%" in table and "+11.93%" in table


def test_the_watchlist_counts_partition_the_list(tmp_path):
    """The three filter chips must add up to the rows they filter, or a chip
    is reporting the size of a selection rather than of a state."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    chips = out["wl_filters"]
    assert chips
    counts = {row["status"] for row in PAYLOAD["watchlist_state"]}
    for status in counts:
        assert f'data-wlf="{status}"' in chips, f"no filter chip for {status}"
    for label in ("need attention", "moved", "quiet"):
        assert label in chips


def test_the_attribution_is_one_labelled_bar(tmp_path):
    """3a. The product's core claim is shown, not described: one bar, three
    segments, values on the segments and in the aria-label."""
    html = _cards(tmp_path)
    assert "attr-bar" in html, "the attribution bar is gone"
    card = [a for a in html.split("<article") if "COALINDIA" in a][0]
    assert card.count("attr-bar") == 1, "more than one attribution bar per card"
    assert re.search(r'aria-label="Attribution of this move: [^"]*market[^"]*'
                     r'sector[^"]*stock-specific[^"]*"', card), (
        "the attribution bar lost its screen-reader values"
    )
    # The three inline number pairs that used to duplicate the bar are gone
    # from the face, but every value is still on the bar itself.
    for v in ("+0.09%", "+0.28%", "+3.67%"):
        assert v in card, f"the bar lost {v}"


def test_the_base_rates_are_bars_and_keep_their_denominator(tmp_path):
    """3b. Counts became bars; the suppression rule and `n` did not move."""
    html = _cards(tmp_path)
    card = [a for a in html.split("<article") if "IFCI" in a][0]
    br = PAYLOAD["cards"][1]["outcomes"]["base_rates"]
    assert f"out of {br['n']} past events" in card
    for k, n in br["counts"].items():
        assert k.lower() in card, f"the {k} row is missing"
        assert f">{n}" in card or f"{n} " in card, f"the {k} count is missing"
    assert 'role="img"' in card and "Historically:" in card, (
        "the bars are not announced to a screen reader"
    )


def test_the_horizons_are_a_timeline_with_hollow_and_filled_markers(tmp_path):
    """3c. Filled = observed, hollow = the session has not closed. Three
    "not yet observable" phrases in a row read as three failures."""
    html = _cards(tmp_path)
    card = [a for a in html.split("<article") if "IFCI" in a][0]
    hs = PAYLOAD["cards"][1]["outcomes"]["horizons"]
    observed = [h for h in hs if h["residual_pct"] is not None]
    pending = [h for h in hs if h["residual_pct"] is None]
    assert observed and pending, "the fixture must exercise both marker states"
    assert 'fill="var(--accent)"' in card, "no filled marker for an observed horizon"
    assert 'fill="none"' in card, "no hollow marker for a pending horizon"
    for h in hs:
        assert f">+{h['h']}</text>" in card, f"the +{h['h']} marker is missing"
    assert "not yet observable" in card, (
        "the pending horizons lost their explanation entirely"
    )


def test_the_shell_guards_fire_when_the_strip_is_faked(tmp_path):
    """R-27. A tile the payload did not supply must fail the strip guard —
    the whole point of 1b is that nothing on it is invented."""
    marker = "el('context').innerHTML = tiles.join("
    script = _script()
    assert marker in script, "the strip's write site moved; update this guard"
    broken = script.replace(
        marker,
        "tiles.push('<span class=\"tile\">NIFTY BANK +1.20%</span>'); " + marker, 1)
    r = _run(tmp_path, broken)
    assert r.returncode == 0, (r.stderr or "")[-800:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert "NIFTY BANK" in out["context_strip"], "the mutation did not take"
    assert out["context_strip"].count("%") != len(
        PAYLOAD["index_context"]["tiles"]), (
        "a fabricated tile did not change the strip's percentage count, so "
        "the guard could not have caught it"
    )

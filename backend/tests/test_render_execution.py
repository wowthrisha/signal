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
    # How many exchange sessions this digest covers. The page used to type
    # this as a literal 2; it is the server's number.
    "window_sessions": 2,
    "funnel": {"watched": 30, "moved": 22, "surfaced": 2},
    # The reasons sum to the movers that did not surface, and every one of them
    # has a stage in the chain below. The fixture used to carry 4 + 14 against
    # 22 moved and 2 surfaced, which closes on nothing — the render guard never
    # looked at the arithmetic, and the page it was standing in for shipped the
    # same gap.
    "filtered_count": 20,
    "filtered_reasons": {"explained_by_market": 4, "below_threshold": 16,
                         "low_confidence": 0},
    "evidence_chain": [
        {"stage": "moved", "count": 22, "label": "moved more than 1%"},
        {"stage": "explained_by_market", "count": 4, "removed": True,
         "label": "explained by market or sector"},
        {"stage": "stock_specific", "count": 18, "label": "stock-specific candidates"},
        {"stage": "confidence_passed", "count": 18, "label": "passed the confidence gate"},
        {"stage": "below_threshold", "count": 16, "removed": True,
         "label": "moved, but inside their own normal range"},
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
        {"isin": "INE001", "symbol": "COALINDIA", "name": "COAL INDIA LTD",
         "sector_id": "oil_gas", "close": 417.85, "close_session": "2026-09-02",
         "spark": [415.25, 411.0, 412.6, 409.3, 410.5, 407.1, 408.45, 406.9, 400.0, 402.5,
                      405.2, 406.9, 404.0, 403.5, 400.0, 401.0, 401.75, 401.6, 417.85, 420.05],
         "change_pct": 3.97, "session_date": "2026-09-02",
         "status": "surfaced", "reason": None},
        # No name, no price and no trend line: the sparse shape a real row
        # takes when the instrument has neither a name on file nor a full
        # window of bars. The conditional in 3c must draw nothing here.
        {"isin": "INE002", "symbol": "SPARSE", "name": None, "sector_id": None,
         "close": None, "close_session": None, "spark": None,
         "change_pct": None, "session_date": None,
         "status": "quiet", "reason": None},
        {"isin": "INE003", "symbol": "IFCI", "name": "IFCI LTD",
         "sector_id": "financial_services",
         "close": 98.32, "close_session": "2026-09-02",
         "spark": [73.98, 76.02, 75.7, 73.51, 76.66, 76.94, 78.93, 81.36, 81.68, 81.76,
                      80.91, 82.48, 83.25, 85.44, 84.27, 89.96, 89.7, 87.26, 98.32, 95.91],
         "change_pct": 11.93, "session_date": "2026-09-02",
         "status": "surfaced", "reason": None},
        # The longest company name on the seeded watchlist, 25 characters
        # against a 22-character cap. It must ellipsise and must not move the
        # symbol above it.
        {"isin": "INE004", "symbol": "ADANIPORTS",
         "name": "BALRAMPUR CHINI MILLS LTD", "sector_id": "services",
         "close": 1706.5, "close_session": "2026-09-02",
         "spark": [1693.5, 1681.0, 1668.2, 1665.0, 1658.0, 1700.0, 1691.0, 1682.4, 1681.9, 1696.0,
                  1700.0, 1672.1, 1702.7, 1691.5, 1714.0, 1707.5, 1593.1, 1647.5, 1672.7, 1706.5],
         "change_pct": 2.02, "session_date": "2026-09-02",
         "status": "filtered", "reason": "below_threshold"},
    ],
    # 1d. The split the funnel actually screened on for the explained bucket:
    # two components, raw closes. Deliberately NOT a card's attribution — a
    # filtered mover has none, because attribution is computed inside the
    # detector and persisted only on an event.
    "filtered_attribution": {
        "reason": "explained_by_market", "n": 4,
        "market_pct": 2.35, "own_pct": 3.05,
        "basis": "total close-to-close move against the market's, summed over "
                 "the bucket — the screen the funnel ran, not a card's attribution",
    },
    "all_cards_lack_evidence": False,
    "cursor_head": 5961,
    "cursor": None,
    "cards": [
        {
            "symbol": "COALINDIA", "name": "COAL INDIA LTD",
            "close": 417.85, "close_session": "2026-09-02",
            "spark": [415.25, 411.0, 412.6, 409.3, 410.5, 407.1, 408.45, 406.9, 400.0, 402.5,
                      405.2, 406.9, 404.0, 403.5, 400.0, 401.0, 401.75, 401.6, 417.85, 420.05],
            "tier": "C", "total_return_pct": 3.97,
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
            "symbol": "IFCI", "name": "BALRAMPUR CHINI MILLS LTD",
            "close": 98.32, "close_session": "2026-09-02",
            "spark": [73.98, 76.02, 75.7, 73.51, 76.66, 76.94, 78.93, 81.36, 81.68, 81.76,
                      80.91, 82.48, 83.25, 85.44, 84.27, 89.96, 89.7, 87.26, 98.32, 95.91],
            "tier": "C", "total_return_pct": 11.93,
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
            "symbol": "SPARSE", "name": None, "close": None,
            "close_session": None, "spark": None,
            "tier": "C", "total_return_pct": None,
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
      // The page moves the rail's note node between containers, so the mock
      // has to model insertion. Noops: what is under test is that the render
      // path calls them and does not throw, not where the node lands.
      appendChild: (c) => { store[id + ':appended'] = true },
      removeChild: noop, after: noop, before: noop, scrollIntoView: noop,
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
      funnel: store['funnel:html'] || '',
      note_rehomed: !!store['wl-note-home:appended'],
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
      // The page moves the rail's note node between containers, so the mock
      // has to model insertion. Noops: what is under test is that the render
      // path calls them and does not throw, not where the node lands.
      appendChild: (c) => { store[id + ':appended'] = true },
      removeChild: noop, after: noop, before: noop, scrollIntoView: noop,
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
    # It left the collapsed face — it is identical on every card that can
    # surface — but it is a number, so it must still be on the page. This is
    # the assertion that caught it being dropped outright.
    assert "data quality" in html, "data quality is not rendered anywhere"
    assert "Technical details" in html
    # Only for cards that have a confidence value. The null-heavy card
    # correctly renders no row rather than a zero we did not measure, and
    # asserting on it would demand the opposite.
    assert PAYLOAD["cards"], "the fixture carries no cards"
    with_conf = [c["symbol"] for c in PAYLOAD["cards"]
                 if c.get("confidence") is not None]
    assert with_conf, "the fixture must carry at least one scored card"
    for symbol in with_conf:
        article = [a for a in html.split("<article") if symbol in a][0]
        assert "data quality" in article.split("Technical details")[1], (
            f"{symbol}: data quality is not inside its Technical details"
        )
    # The numeric and the gate it is measured against, both, in the title.
    assert re.search(r"Data quality \d\.\d\d, against the gate of 0\.3", html), (
        "confidence rendered without its numeric value or without the gate"
    )
    # And a magnitude a sighted reader can see without hovering.
    assert "bar-accent" in html


def test_the_card_face_carries_words_not_sigma(tmp_path):
    """2a, tightened. The card said "this move was unusual" four times: the
    band's two sigma tick labels, the axis ends, the verdict word beside the
    band, and a prose line under it. The redundancy effect says the same
    information in several simultaneous forms impairs comprehension rather
    than reinforcing it, so the face keeps the graphic and the words and
    nothing else.

    Sigma is not deleted. It is in the band's own aria-label and in the
    drawer's technical rows, and this asserts both — the previous version of
    this guard required the opposite, that the ticks be *on* the face, and it
    is inverted here deliberately rather than loosened."""
    html = _cards(tmp_path)
    # The card that actually has a residual to band. A card with z=None
    # correctly renders no band at all, so asserting on it proves nothing.
    card = [a for a in html.split("<article") if "IFCI" in a][0]
    face = card.split("Technical details")[0]
    visible = re.sub(r"<[^>]+>", " ", face)
    z = PAYLOAD["cards"][1]["z"]
    assert f"{z:.2f}" not in visible, (
        f"the card's own z value is still on its face: {visible[:400]}"
    )
    # No sigma anywhere on the face — neither the card's own value nor the
    # detector's bound. The unit is what a reader cannot use.
    assert "&#963;" not in face and "\u03c3" not in face, (
        "a sigma tick label is back on the card face"
    )
    h1 = PAYLOAD["salience_config"]["d1_threshold"]
    assert str(h1) not in visible, (
        f"the threshold {h1} is still printed on the face: {visible[:400]}"
    )
    # The axis still says which end is which, in words.
    for w in ("normal", "unusual"):
        assert w in visible, f"the band lost its {w!r} axis end"
    # And the graphic itself is unchanged: the interval is still drawn, with
    # both of its bounds ticked. Deleting the labels must not have deleted the
    # marks under them.
    band = re.search(r"<svg[^>]*aria-label=\"[^\"]*normal range[^\"]*\".*?</svg>",
                     card, re.S)
    assert band, "the extremity band is gone from the face"
    assert band.group(0).count("<line") >= 3, (
        "the band lost the ticks that marked its bounds"
    )
    # Nothing was deleted, only moved.
    assert re.search(r"Standardised residual [-\d.]+ sigma", card), (
        "the aria-label lost the standardised residual"
    )
    tech = card.split("Technical details")[1]
    assert "standardised residual" in tech, (
        "sigma was deleted rather than moved down a layer"
    )
    # JS renders 3.0 as "3", so the bound is matched as a number rather than
    # as the JSON literal's spelling.
    assert re.search(r"D1 fires at[^<]*<[^>]*>%g&#963;" % h1, tech) \
        or f">{h1:g}&#963;" in tech, (
        "the detector's threshold left the face and did not arrive in the "
        f"technical rows: {tech[:400]}"
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
    face = html.split("Investigate")[0]
    assert "TIER C" not in face and "TIER B" not in face

    # A card with no filings says what the tier means, on its face.
    no_ev = [a for a in html.split("<article") if "IFCI" in a][0]
    assert "unusual" in no_ev.split("Investigate")[0], (
        "a card with no filings lost the tier's meaning from its face"
    )
    # A card with filings shows the count there, and the tier is still reachable.
    with_ev = [a for a in html.split("<article") if "COALINDIA" in a][0]
    assert "filing" in with_ev.split("Investigate")[0]

    # Every card, either way, keeps the letter and the gate expression.
    for article in [a for a in html.split("<article") if "Technical details" in a]:
        tech = article.split("Technical details")[1]
        assert "Tier " in tech, "the tier letter was deleted instead of moved"
    assert "C>=0.5" in html.split("Technical details")[1], (
        "the gate expression was deleted instead of moved"
    )


def test_the_stock_specific_verdict_is_stated_exactly_once(tmp_path):
    """2c. It was on the card three times: the share sentence on the face
    ("68% of this move was the company, not the market."), a verdict paragraph
    in the drawer ("The market barely moved. This stock did. — stock-specific
    move"), and a numbered reason under it ("The market and its sector barely
    moved."). Three renderings of one fact.

    The one that survives is the share sentence, because it is the only one of
    the three that carries a number."""
    html = _cards(tmp_path)
    card = [a for a in html.split("<article") if "COALINDIA" in a][0]
    visible = re.sub(r"<[^>]+>", " ", card)
    assert visible.count("was the company, not the market") == 1, (
        "the stock-specific verdict is not stated exactly once"
    )
    for gone in ("This stock did.", "The market and its sector barely moved.",
                 "stock-specific move"):
        assert gone not in visible, f"a duplicate verdict is back: {gone!r}"
    # And the number it carries is still the measured share, not a word.
    c = PAYLOAD["cards"][0]
    total = sum(abs(c[k]) for k in ("market_pct", "sector_only_pct", "residual_pct"))
    assert f'{abs(c["residual_pct"]) / total * 100:.0f}%' in card


def test_the_template_headline_moved_rather_than_was_deleted(tmp_path):
    """2b. On a JUMP the headline read "Single-session move well outside this
    stock's own recent range" — the band, in words, three inches under the
    band. It could not simply be dropped: the DRIFT template carries the
    CUSUM's own bar count and the corporate-action headline is the exchange's
    own `purpose` line passed through verbatim, and neither exists anywhere
    else on the card. So it is a technical row now, not a second verdict."""
    html = _cards(tmp_path)
    with_headline = [c for c in PAYLOAD["cards"] if c.get("headline")]
    assert len(with_headline) > 0, (
        "the fixture carries no headline, so this guard proves nothing"
    )
    for c in with_headline:
        card = [a for a in html.split("<article") if c["symbol"] in a][0]
        assert c["headline"] in card, f"{c['symbol']}: the headline was deleted"
        tech = card.split("Technical details")[1]
        assert c["headline"] in tech, (
            f"{c['symbol']}: the headline is still outside the technical rows"
        )
        assert card.count(c["headline"]) == 1, (
            f"{c['symbol']}: the headline is rendered twice"
        )


def test_why_this_opens_on_plain_reasons(tmp_path):
    """3. Tier one is words; tier two is the record, nested beneath it."""
    html = _cards(tmp_path)
    assert "Investigate" in html, "the card has no disclosure control"
    # The populated card, not the null one: a reason that cannot be derived
    # is correctly absent, and asserting on the null card would prove nothing.
    # Split on the drawer body, not on the button's label: the first card in
    # the column opens on load and its control reads "Close", and which card
    # is first is now decided by the size of the move (4b).
    why = [x for x in html.split("<article") if "IFCI" in x][0].split("data-whybody")[1]
    plain = why.split("Technical details")[0]
    assert "It moved much more than this stock usually does." in plain
    assert "data-quality check" in plain
    # Nested, not adjacent: the technical block sits inside the drawer body.
    assert "<details" in why and why.index("<details") < why.index("Technical details")
    # One disclosure per card, not a toggle nested inside a toggle.
    card = [a for a in html.split("<article") if "IFCI" in a][0]
    assert card.count("data-whybody") == 1
    assert "data-evbody" not in card, "the evidence toggle is nested again"


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
    help_body = out["help_body"]
    f = PAYLOAD["funnel"]
    # The prose line above the cards is gone: it said the same three numbers
    # the fixed header carries at every scroll position, and the right rail
    # said them a third time. The header is now the single home, so THAT is
    # what has to carry them.
    assert not out["funnel_lede"], (
        "the orientation prose line is back; the funnel block already says this"
    )
    missing = [k for k in ("watched", "moved", "surfaced")
               if str(f[k]) not in out["funnel"]]
    assert not missing, f"the funnel block lost {missing}: {out['funnel'][:300]}"
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
    broken = re.sub(r"function extremityBand\(c, ctx\) \{.*?\n\}\n",
                    "", _script(), flags=re.S)
    assert "extremityBand(" in broken and "function extremityBand" not in broken
    r = _run(tmp_path, broken)
    assert r.returncode != 0, "deleting the band renderer did not fail the guard"
    assert "extremityBand is not defined" in r.stderr


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


def test_the_funnel_is_above_the_fold_at_the_size_of_its_claim(tmp_path):
    """4a. The funnel is the one thing none of the products examined presents
    — Groww, Kite, TradingView, Moneycontrol and Robinhood all show you what
    moved and none of them shows you what it filtered out. It was three words
    of 12px text in a fixed header while four research reports held the
    column. It is now the first block in the column, with a proportional bar
    per stage.

    This guard replaces `test_the_header_carries_the_funnels_three_numbers`,
    which asserted the opposite location. The header keeps one number — the
    one that says whether there is anything to do — and this asserts that
    split rather than allowing either surface to quietly drop a count."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    block, f = out["funnel"], PAYLOAD["funnel"]
    assert block, "the funnel block rendered nothing"
    missing = [k for k in ("watched", "moved", "surfaced")
               if f">{f[k]}<" not in block]
    assert not missing, f"the funnel block is missing {missing}: {block[:400]}"
    # Proportional bars, one per stage, widths taken from the counts.
    widths = [float(w) for w in re.findall(r"width:([\d.]+)%", block)]
    assert len(widths) == 3, f"expected three bars, found {len(widths)}"
    assert widths[0] == 100.0, "the first stage does not fill its track"
    expected = [round(f[k] / f["watched"] * 100, 1)
                for k in ("watched", "moved", "surfaced")]
    assert widths == expected, (
        f"the bars are not proportional to the counts: {widths} vs {expected}"
    )
    # And at the size of the claim: the counts are not body text.
    assert "funnel-n" in block, "the funnel's figures are not the block's hero"
    # The header keeps the actionable count and drops the other two, which
    # now live above it rather than beside it.
    head = out["head_summary"]
    assert str(f["surfaced"]) in head and "need attention" in head, (
        f"the header lost the count that survives a scroll: {head}"
    )


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


def test_the_watchlist_groups_head_the_rows_they_count(tmp_path):
    """The counts moved onto the group headings. They used to be a chip row
    above the table AND a heading over each group — the same three numbers
    twice, in a 280px rail. A heading is the filter now, and its count must be
    the number of rows beneath it or it is reporting something else."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    table = out["watchlist_html"]
    assert not out["wl_filters"], "the duplicate chip row is back"

    # What the fixture says each group should hold, for the rows the table
    # actually renders (muted rows are hidden by default).
    shown = {w["isin"] for w in WATCHLIST if not w["muted"]}
    assert shown
    expected = {}
    for row in PAYLOAD["watchlist_state"]:
        if row["isin"] in shown:
            expected[row["status"]] = expected.get(row["status"], 0) + 1
    assert expected, "the fixture produces no grouped rows"

    for status, n in expected.items():
        assert f'data-wlf="{status}"' in table, f"no heading for {status}"
        heading = table.split(f'data-wlf="{status}"')[1]
        head_text = heading[:heading.index("</button>")]
        assert f"({n})" in head_text, (
            f"the {status} heading does not count its own rows: "
            f"expected ({n}) in {head_text.strip()!r}"
        )
        # And the rows really are beneath it, before the next heading.
        body = heading.split("</button>", 1)[1].split("wl-headbtn")[0]
        assert body.count(f'data-status="{status}"') == n, (
            f"the {status} group does not contain the {n} rows it claims"
        )
    for label in ("Need attention", "Moved, not surfaced", "Quiet"):
        assert label in table


def test_the_attribution_is_demoted_but_keeps_every_value(tmp_path):
    """1b. Measured before it was changed: across all 932 tier C events in the
    database the stock-specific share sits inside a 5.4-point IQR, so the
    segments drew the same picture on every card. It is a 6px rule and a
    sentence now — and every number it used to carry is still on it."""
    html = _cards(tmp_path)
    card = [a for a in html.split("<article") if "COALINDIA" in a][0]
    assert "attr-slim" in card, "the attribution bar is gone entirely"
    assert "attr-bar" not in card, (
        "the full-size bar is back on the card; it belongs in the filtered "
        "drawer, which is the only place the proportion discriminates"
    )
    assert "seg-l" not in card and "seg-v" not in card, (
        "the demoted bar is carrying in-bar labels again"
    )
    # The share, stated rather than drawn.
    c = PAYLOAD["cards"][0]
    total = sum(abs(c[k]) for k in ("market_pct", "sector_only_pct", "residual_pct"))
    share = abs(c["residual_pct"]) / total * 100
    assert f"{share:.0f}%" in card, "the share is not stated as a number"
    assert "was the company, not the market" in card
    # Nothing was deleted: the three components survive in the aria-label and
    # the hover title.
    assert re.search(r'aria-label="Attribution of this move: [^"]*market[^"]*'
                     r'sector[^"]*stock-specific[^"]*"', card), (
        "the attribution bar lost its screen-reader values"
    )
    for v in ("+0.09%", "+0.28%", "+3.67%"):
        assert v in card, f"the bar lost {v}"


def test_the_extremity_band_is_the_hero(tmp_path):
    """1c. It is the object that actually varies across the slate — 3.11,
    4.33, 4.83, 3.36 sigma today — where the attribution share does not.

    It is no longer sized by a `width` attribute. A hard 420px that could not
    shrink forced 168px of horizontal overflow at a 390px viewport and
    truncated the ticker; the band is fluid now, so "is it the hero" is a
    question about its *coordinate space* and the width it is allowed to
    reach, not about a pixel attribute on the tag."""
    html = _cards(tmp_path)
    card = [a for a in html.split("<article") if "IFCI" in a][0]
    band = re.search(r'<svg[^>]*aria-label="[^"]*normal range[^"]*"[^>]*>',
                     card, re.S)
    assert band, "the extremity band is not on the card face"
    tag = band.group(0)
    assert "shrink-0" not in tag, (
        "the band is marked no-shrink again; that is what overflowed 390px"
    )
    assert 'width="' not in tag, (
        f"the band has a fixed width attribute again: {tag[:160]}"
    )
    vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', tag)
    assert vb and int(vb.group(1)) >= 300, (
        f"the band's coordinate space is {vb.group(1) if vb else None} wide; "
        "it is meant to be the hero"
    )
    # Wider than the demoted attribution rule, which is the whole point.
    assert "attr-slim" in card
    # And the plain verdict sits beside it, exactly once per card.
    visible = re.sub(r"<[^>]+>", " ", card)
    assert visible.count("far outside its normal range") == 1, (
        "the band's verdict is shown twice on the card face"
    )


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



def test_the_full_size_bar_lives_where_it_discriminates(tmp_path):
    """1d. Measured first (ACTION-LOG 1a): the stock-specific share does NOT
    separate surfaced from filtered — 90.6% of suppressed events exceed an 80%
    share against 66.7% of surfaceable ones, and tier C's IQR is 5.4 points.
    So the full-size bar left the card. The one bucket where the proportion is
    the *definition* is "explained by market or sector", and that is the only
    place it now appears — with its basis stated, because it is a
    two-component split of raw closes and a card's is three of an adjusted
    series."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    pareto, cards = out["pareto"], out["cards_html"]
    a = PAYLOAD["filtered_attribution"]

    assert "attr-bar" in pareto, "the full-size bar is not in the filtered drawer"
    assert "attr-bar" not in cards, "the full-size bar is back on the cards"
    # Both components, labelled, from the payload.
    total = abs(a["market_pct"]) + abs(a["own_pct"])
    assert f'{abs(a["market_pct"]) / total * 100:.0f}%' in pareto
    assert "market" in pareto and "its own" in pareto
    # The basis, so it can never be read as a card's decomposition.
    assert "not a card&#39;s attribution" in pareto or "not a card's attribution" in pareto, (
        "the filtered bar does not state which split it is showing"
    )


def test_the_filtered_bar_is_absent_when_the_payload_omits_it(tmp_path):
    """R-27 for 1d, and the honesty check: with no bucket there is nothing to
    draw, and the drawer must render no bar rather than an empty one."""
    payload = {**PAYLOAD, "filtered_attribution": None}
    js = tmp_path / "app.js"; js.write_text(_script())
    harness = tmp_path / "harness.js"; harness.write_text(HARNESS)
    pj = tmp_path / "p.json"; pj.write_text(json.dumps(payload))
    wj = tmp_path / "w.json"; wj.write_text(json.dumps(WATCHLIST))
    r = subprocess.run([NODE, str(harness), str(js), str(pj), str(wj)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.stderr or "")[-800:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert "attr-bar" not in out["pareto"], (
        "a bar rendered for a bucket the payload carried no split for"
    )
    # The Pareto itself still renders; only the bar is absent.
    assert "Below threshold" in out["pareto"] or "below" in out["pareto"].lower()


# --- the price anchor and the company name ---------------------------------
#
# Every competitor examined — Groww, Kite, TradingView, Moneycontrol,
# Robinhood — leads a watchlist row with an absolute price and a company name.
# Signal showed a percentage against a ticker, so a reader could not tell a
# ₹9.57 stock from a ₹21,500 one, and RELIANCE from Reliance Industries. Both
# data points were already in the database.


def _watchlist(tmp_path) -> str:
    r = _run(tmp_path, _script())
    assert r.returncode == 0, (r.stderr or "")[-1500:]
    return json.loads(r.stdout.strip().splitlines()[-1])["watchlist_html"]


def test_a_card_renders_its_price(tmp_path):
    """1a. The price is the anchor. Indian digit grouping, two decimals, and
    the rupee sign — a bare `1322.00` beside a percentage is a quantity with
    no unit."""
    html = _cards(tmp_path)
    assert len(PAYLOAD["cards"]) > 0, "the fixture carries no cards"
    priced = [c for c in PAYLOAD["cards"] if c.get("close") is not None]
    assert len(priced) > 0, "the fixture must carry at least one priced card"
    for c in priced:
        card = [a for a in html.split("<article") if c["symbol"] in a][0]
        assert "\u20b9" in card, f"{c['symbol']}: no rupee sign on the card"
        assert 'class="price' in card, f"{c['symbol']}: the price is not styled as a figure"
        # The value itself, grouped. 417.85 and 98.32 group to themselves;
        # the assertion is that the digits reached the page at all.
        digits = f"{c['close']:.2f}"
        assert digits.split(".")[1] in card, f"{c['symbol']}: {digits} is not rendered"
    # And a card with no close prints nothing rather than a zero or a dash
    # that would read as a price of nothing.
    sparse = [a for a in html.split("<article") if "SPARSE" in a][0]
    assert 'class="price' not in sparse, (
        "a card with no close on record still rendered a price element"
    )


def test_a_card_renders_a_company_name_distinct_from_its_symbol(tmp_path):
    """1b. `instrument.name` was populated for all 3,044 rows and reached the
    page only as a `title` attribute — invisible to everyone not hovering."""
    html = _cards(tmp_path)
    assert len(PAYLOAD["cards"]) > 0, "the fixture carries no cards"
    named = [c for c in PAYLOAD["cards"] if c.get("name")]
    assert len(named) > 0, "the fixture must carry at least one named card"
    for c in named:
        card = [a for a in html.split("<article") if c["symbol"] in a][0]
        assert 'class="co-name"' in card, f"{c['symbol']}: no company name element"
        assert c["name"] in card, f"{c['symbol']}: {c['name']!r} is not rendered"
        assert c["name"] != c["symbol"]
        # Rendered as stored. Title-casing is lossy on M&M, ITC LTD and BSE
        # LIMITED, and a mangled name is a worse error than an uppercase one.
        visible = re.search(r'class="co-name"[^>]*>([^<]*)<', card)
        assert visible and visible.group(1).strip() == c["name"], (
            f"{c['symbol']}: the name was transformed on the way to the page: "
            f"{visible.group(1) if visible else None!r}"
        )
    sparse = [a for a in html.split("<article") if "SPARSE" in a][0]
    assert "co-name" not in sparse, "a nameless card rendered an empty name element"


def test_a_long_company_name_cannot_overflow_or_shorten_the_symbol():
    """1c. BALRAMPUR CHINI MILLS LTD is 25 characters and the longest name on
    the seeded watchlist. The symbol is the one thing on a card that may never
    shorten — losing a ticker's last letters is losing which company the card
    is about — so the name is capped in CSS and truncates itself."""
    css = STATIC.read_text().split("</style>")[0]
    block = css.split(".co-name")[1].split("}")[0]
    assert "max-width" in block, "the company name has no width cap"
    assert "text-overflow: ellipsis" in block, "a long name will not ellipsise"
    assert "white-space: nowrap" in block, "a long name will wrap instead of truncating"
    # The rail's name is capped by its grid track rather than by ch, and must
    # ellipsise the same way.
    rail = css.split(".wl-name")[1].split("}")[0]
    assert "text-overflow: ellipsis" in rail and "overflow: hidden" in rail


def test_the_name_cap_is_actually_exercised_by_a_25_character_name(tmp_path):
    """R-27: a cap that no fixture ever reaches is a cap nobody has tested."""
    longest = max((c.get("name") or "" for c in PAYLOAD["cards"]), key=len)
    assert len(longest) >= 25, (
        f"the fixture's longest name is {len(longest)} characters; the cap is "
        "22 and this guard proves nothing below it"
    )
    html = _cards(tmp_path)
    assert longest in html, "the long name never reached the render"


def test_a_rail_row_carries_the_symbol_the_name_and_the_price(tmp_path):
    """1a/1b, the rail half. Three columns down the list: what it is, what it
    costs, what it did."""
    table = _watchlist(tmp_path)
    rows = [r for r in PAYLOAD["watchlist_state"] if r.get("close") is not None]
    assert rows, "the fixture must carry at least one priced row"
    for r in rows:
        assert "\u20b9" in table, f"{r['symbol']}: no price in the rail"
        assert r["name"] in table, f"{r['symbol']}: no company name in the rail"
    assert 'class="wl-name"' in table and "wl-price" in table
    # The quiet, nameless, priceless row still renders — as a row with empty
    # cells, not as a row with fabricated ones.
    assert "SPARSE" in table
    sparse = [x for x in table.split("<button") if "SPARSE" in x][0]
    assert "\u20b9" not in sparse, "a row with no close on record printed a price"


# --- the trend line --------------------------------------------------------


def test_every_row_with_a_full_window_gets_a_trend_line(tmp_path):
    """3a/3b. Tufte's sparkline: word-sized, axis-free, one polyline and a dot
    on the last point."""
    table = _watchlist(tmp_path)
    with_spark = [r for r in PAYLOAD["watchlist_state"] if r.get("spark")]
    assert with_spark, "the fixture must carry at least one full window"
    assert table.count("<polyline") == len(with_spark), (
        f"expected {len(with_spark)} trend lines in the rail, "
        f"found {table.count('<polyline')}"
    )
    assert 'stroke-width="1.5"' in table
    assert "var(--text-2)" in table, "the stroke is not on the audited token"
    # A dot on the final point, one per line.
    assert table.count("<circle") == len(with_spark)


def test_a_short_series_renders_no_trend_line_at_all(tmp_path):
    """3c, the conditional. A two-point polyline looks exactly like a twenty
    -point one and reads as a trend that was never measured. Where the window
    is short the row draws nothing."""
    table = _watchlist(tmp_path)
    sparse = [x for x in table.split("<button") if "SPARSE" in x][0]
    assert "<svg" not in sparse, (
        "a row with no full window still drew a trend line"
    )
    # And the client refuses a short series even if the server sends one.
    short = json.loads(json.dumps(PAYLOAD))
    for row in short["watchlist_state"]:
        if row.get("spark"):
            row["spark"] = row["spark"][:3]
    js = tmp_path / "app.js"
    js.write_text(_script())
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    payload = tmp_path / "short.json"
    payload.write_text(json.dumps(short))
    watch = tmp_path / "watchlist.json"
    watch.write_text(json.dumps(WATCHLIST))
    r = subprocess.run([NODE, str(harness), str(js), str(payload), str(watch)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.stderr or "")[-1500:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert "<polyline" not in out["watchlist_html"], (
        "a three-point series was drawn as a trend line"
    )


def test_the_trend_line_is_normalised_per_row_not_across_the_list(tmp_path):
    """3b. Absolute scaling across thirty differently-priced instruments —
    ₹9.57 beside ₹21,500 — renders every row but the most expensive flat. Each
    line must use its own extremes, which means each must reach both the top
    and the bottom of its own box."""
    table = _watchlist(tmp_path)
    lines = re.findall(r'<polyline points="([^"]+)"', table)
    assert lines, "no trend lines rendered"
    for pts in lines:
        ys = [float(p.split(",")[1]) for p in pts.split()]
        assert min(ys) < 3.0, f"the line never reaches its top: min y {min(ys)}"
        assert max(ys) > 17.0, f"the line never reaches its bottom: max y {max(ys)}"


def test_the_trend_line_is_announced_in_words_with_both_ends_of_its_range(tmp_path):
    """3d. Never an empty or unlabelled SVG. The label says how many sessions,
    what the range was in rupees, and which way it went."""
    table = _watchlist(tmp_path)
    labels = re.findall(r'<svg[^>]*aria-label="([^"]*)"', table)
    assert labels, "no labelled trend line in the rail"
    for lab in labels:
        assert re.search(r"\d+-session price trend", lab), lab
        assert lab.count("\u20b9") == 2, f"the range is not stated in rupees: {lab}"
        assert any(w in lab for w in ("rising", "falling", "flat")), lab
    # IFCI ran 73.51 to 98.32 over the window and closed above where it opened.
    ifci = [l for l in labels if l.startswith("IFCI")]
    assert ifci, "the IFCI row lost its label"
    assert "rising" in ifci[0] and "\u20b973.51" in ifci[0], ifci[0]


def test_the_card_carries_a_larger_trend_line_than_the_rail(tmp_path):
    """3b. 120x32 on the card, 64x20 in the rail — the same graphic at the
    size its surface affords."""
    html = _cards(tmp_path)
    card = [a for a in html.split("<article") if "IFCI" in a][0]
    m = re.search(r'<svg width="(\d+)" height="(\d+)"[^>]*class="spark"', card)
    assert m, "no trend line on the card"
    assert (int(m.group(1)), int(m.group(2))) == (120, 32)
    table = _watchlist(tmp_path)
    r = re.search(r'<svg width="(\d+)" height="(\d+)"[^>]*class="spark"', table)
    assert r and (int(r.group(1)), int(r.group(2))) == (64, 20)


# --- 2d: monospace is for numerals -----------------------------------------
#
# Groww and TradingView both set labels and prose proportionally and reserve
# mono for figures. Setting words in a typeface designed to align columns of
# digits is the single largest source of the "developer tool" read, and it is
# a type rule rather than a string, so the guard is structural: the CSS class
# that carries mono is checked against what it is actually applied to.


def _css() -> str:
    return STATIC.read_text().split("</style>")[0]


def test_only_the_figure_class_is_monospaced():
    """`.num` is the one rule that may name the mono stack. A label, a pill or
    a table heading that reaches for it is a word in a digit face."""
    css = _css()
    blocks = re.findall(r"([.#][\w-]+)\s*\{([^}]*)\}", css)
    mono = sorted({sel for sel, body in blocks if "var(--mono)" in body})
    # `.num` is the general figure face; `.price` and `.funnel-n` are two
    # figures with their own size and weight and nothing else. No rule that
    # sets words may appear here.
    assert mono == [".funnel-n", ".num", ".price"], (
        f"these rules set the monospace stack and should not: {mono}"
    )


def test_the_ticker_and_the_prose_are_not_set_in_a_digit_face(tmp_path):
    """The ticker is an identifier, not a figure, and the sentences around it
    are sentences. Both used to carry `.num`."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    card = [a for a in out["cards_html"].split("<article") if "IFCI" in a][0]
    h2 = re.search(r"<h2 class=\"([^\"]*)\"", card)
    assert h2 and "num" not in h2.group(1).split(), (
        f"the card's symbol is still monospaced: {h2.group(1) if h2 else None}"
    )
    matches = [x for x in out["watchlist_html"].split("<button") if "IFCI" in x]
    assert len(matches) == 1, f"expected exactly one IFCI row, got {len(matches)}"
    row = matches[0]
    sym = re.search(r'<span class="([^"]*)">IFCI</span>', row)
    assert sym and "num" not in sym.group(1).split(), (
        f"the rail's symbol is still monospaced: {sym.group(1) if sym else None}"
    )
    # The figures beside it are untouched — this is a substitution, not a
    # removal, and a page with no mono at all would have lost the alignment
    # the class exists for.
    assert 'class="num wl-price' in row, "the price lost its tabular figures"
    # The change column's cell carries the tabular face; the span inside it
    # carries only the colour, so the enclosing cell is what is checked.
    assert 'class="num text-right"' in row, "the change column lost its tabular figures"
    assert "11.93" in row


def test_the_figures_that_must_stay_monospaced_still_are(tmp_path):
    """The other half of 2d. Returns, prices, counts and dates keep the
    tabular face; a sweep that took mono off everything would pass the guard
    above and be just as wrong."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    articles = [a for a in out["cards_html"].split("<article") if "IFCI" in a]
    assert len(articles) == 1, f"expected one IFCI card, got {len(articles)}"
    card = articles[0]
    assert 'class="num figure-lg' in card, "the return is no longer tabular"
    assert 'class="price' in card, "the price is no longer a figure"
    chain = out["chain"]
    assert 'class="num' in chain, "the evidence chain's counts lost mono"


# --- 2e: engineering prose is not product surface --------------------------


def test_the_funnel_basis_prose_is_not_on_the_digest(tmp_path):
    """It was the longest string the product surface carried, and it is
    written in the vocabulary of whoever wrote the query. It is not deleted:
    /api/digest still ships it, it is still the hover title, and /lab#funnel
    sets both splits out side by side."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    pareto = out["pareto"]
    basis = PAYLOAD["filtered_attribution"]["basis"]
    visible = " ".join(re.sub(r"<[^>]+>", " ", pareto).split())
    assert "the screen the funnel ran" not in visible, (
        "the engineering basis line is still rendered as visible prose"
    )
    # Still reachable, and still says which split this is.
    assert basis in pareto, "the basis was deleted rather than moved to a title"
    assert "/lab#funnel" in pareto, "the digest does not point at the definition"
    assert "Measured on closing prices against the index" in visible, (
        "the bar lost its plain-language caption entirely"
    )


# --- 4b: one sort, applied to both surfaces --------------------------------


def test_the_cards_render_in_the_order_the_rail_lists_them(tmp_path):
    """The rail ranks by the size of the move; the slate ranks by tier then U,
    and with every card at tier C and U saturated at 1.0 that key is a tie
    broken by event_id. IFCI — the largest move on the list and the first row
    in the rail — came third in the column. Clicking the top row and finding
    its card third reads as a bug.

    The reorder applies to what the slate ALREADY SELECTED. Which cards
    survive is still the server's decision and is untouched."""
    r = _run(tmp_path, _script())
    assert r.returncode == 0, (r.stderr or "")[-1500:]
    out = json.loads(r.stdout.strip().splitlines()[-1])

    rendered = re.findall(r'data-symbol="([^"]+)"', out["cards_html"])
    assert len(rendered) == len(PAYLOAD["cards"]), (
        f"a card was lost or duplicated by the reorder: {rendered}"
    )
    assert set(rendered) == {c["symbol"] for c in PAYLOAD["cards"]}, (
        "the reorder changed which cards are on the slate"
    )

    # The rail's own order, for the rows that have cards.
    table = out["watchlist_html"]
    rail = re.findall(r'data-wl="([^"]+)"[^>]*\n?[^>]*data-status="surfaced"',
                      table)
    by_isin = {r["isin"]: r["symbol"] for r in PAYLOAD["watchlist_state"]}
    rail_symbols = [by_isin[i] for i in rail if i in by_isin]
    assert rail_symbols, "no surfaced rows in the rail to compare against"
    # Every surfaced symbol that has a card must appear in the same relative
    # order in both.
    common = [x for x in rail_symbols if x in rendered]
    assert common, "the rail and the column share no symbol"
    assert common == [x for x in rendered if x in common], (
        f"rail order {common} does not match card order {rendered}"
    )
    # And the sort really is by the size of the move, descending.
    mags = [abs(next(c["total_return_pct"] for c in PAYLOAD["cards"]
                     if c["symbol"] == sym) or 0) for sym in rendered]
    assert mags == sorted(mags, reverse=True), (
        f"the cards are not ordered by the size of the move: {list(zip(rendered, mags))}"
    )


def test_both_surfaces_break_a_tie_the_same_way_without_relying_on_the_api(tmp_path):
    """4b must not rest on the order /api/watchlist happens to return.

    The rail's tie-break was implicit: `Array.sort` is stable and the endpoint
    orders by symbol, so two equal moves came out alphabetically by accident of
    an ORDER BY three files away. This feeds the watchlist in reverse and
    asserts both surfaces still agree."""
    payload = json.loads(json.dumps(PAYLOAD))
    for c in payload["cards"]:
        c["total_return_pct"] = 5.0
    for r in payload["watchlist_state"]:
        if r["status"] == "surfaced":
            r["change_pct"] = 5.0
    js = tmp_path / "app.js"; js.write_text(_script())
    harness = tmp_path / "harness.js"; harness.write_text(HARNESS)
    pj = tmp_path / "p.json"; pj.write_text(json.dumps(payload))
    wj = tmp_path / "w.json"
    # Reversed on purpose: the page must not depend on the endpoint's order.
    wj.write_text(json.dumps(list(reversed(WATCHLIST))))
    r = subprocess.run([NODE, str(harness), str(js), str(pj), str(wj)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.stderr or "")[-1200:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    rendered = re.findall(r'data-symbol="([^"]+)"', out["cards_html"])
    assert len(rendered) > 1, "too few cards to tie-break"
    assert rendered == sorted(rendered), (
        f"the cards did not fall back to a symbol tie-break: {rendered}"
    )
    table = out["watchlist_html"]
    by_isin = {x["isin"]: x["symbol"] for x in PAYLOAD["watchlist_state"]}
    rail = [by_isin[i] for i in re.findall(r'data-wl="([^"]+)"', table)
            if i in by_isin]
    surfaced = [s for s in rail if s in rendered]
    assert len(surfaced) > 1, "too few surfaced rows to tie-break"
    assert surfaced == [s for s in rendered if s in surfaced], (
        f"with the watchlist reversed the rail order {surfaced} no longer "
        f"matches the card order {rendered}"
    )


def test_the_reorder_is_stable_for_two_equal_moves(tmp_path):
    """A total order, not a partial one. Two cards with the same magnitude
    must not swap between renders — a column that reshuffles on a background
    refresh is worse than one in the wrong order."""
    payload = json.loads(json.dumps(PAYLOAD))
    for c in payload["cards"]:
        c["total_return_pct"] = 5.0
    js = tmp_path / "app.js"; js.write_text(_script())
    harness = tmp_path / "harness.js"; harness.write_text(HARNESS)
    pj = tmp_path / "p.json"; pj.write_text(json.dumps(payload))
    wj = tmp_path / "w.json"; wj.write_text(json.dumps(WATCHLIST))
    seen = set()
    for _ in range(2):
        r = subprocess.run([NODE, str(harness), str(js), str(pj), str(wj)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (r.stderr or "")[-1200:]
        out = json.loads(r.stdout.strip().splitlines()[-1])
        seen.add(tuple(re.findall(r'data-symbol="([^"]+)"', out["cards_html"])))
    assert len(seen) == 1, f"the order is not deterministic: {seen}"
    assert list(seen)[0] == tuple(sorted(list(seen)[0])), (
        "equal moves do not fall back to a symbol tie-break"
    )


# --- 4c: the primary action is not at the bottom of a rail ------------------


def test_mark_all_as_seen_is_in_the_header_not_the_rail_footer():
    """It is the only control on this page that changes state the user owns,
    and it sat below three paragraphs at the bottom of the right rail — the
    least reachable position on the screen. It belongs beside the count it
    clears."""
    markup = STATIC.read_text()
    head = markup[markup.index("<header"):markup.index("</header>")]
    assert 'id="ack-btn"' in head, "the primary action is not in the header"
    rail = markup[markup.index("RIGHT RAIL"):markup.index("<!-- 5. Persistent footer")]
    assert 'id="ack-btn"' not in rail, "the button is still in the rail too"
    assert markup.count('id="ack-btn"') == 1, "there are two ack buttons"


def test_the_window_the_counts_cover_moved_with_the_counts(tmp_path):
    """`cursor-state` said which window the funnel was taken over, from the
    bottom of a rail three columns away from the funnel. It is under the
    counts now — and it is still derived from `cursor`, never typed."""
    r = _run(tmp_path, _script())
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'id="cursor-state"' in out["funnel"], (
        "the window line is not with the counts it describes"
    )
    markup = STATIC.read_text()
    rail = markup[markup.index("RIGHT RAIL"):markup.index("<!-- 5. Persistent footer")]
    assert 'id="cursor-state"' not in rail


# --- 5a: watchlist search --------------------------------------------------


# The page's state lives in `let` bindings inside the evaluated script, which a
# direct `eval` keeps out of the harness's own scope — so the filter cannot be
# poked from outside. That is a feature here: this harness records the real
# listeners and fires the real events, so what is under test is the wiring
# between the input and the table and not a predicate called directly.
SEARCH_HARNESS = textwrap.dedent("""
    const fs = require('fs');
    const noop = () => {};
    const store = {};
    const handlers = {};
    const mk = (id) => ({
      addEventListener: (evt, fn) => { (handlers[id + ':' + evt] ||= []).push(fn) },
      setAttribute: noop, removeAttribute: noop,
      appendChild: (c) => { store[id + ':appended'] = true },
      removeChild: noop, after: noop, before: noop, scrollIntoView: noop,
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

    render(JSON.parse(fs.readFileSync(process.argv[3], 'utf8')));
    renderWatchlist(JSON.parse(fs.readFileSync(process.argv[4], 'utf8')));

    const query = process.argv[5];
    const escape = process.argv[6] === 'escape';
    let stopped = false;
    const fire = (key, evt) => {
      for (const fn of handlers[key] || []) fn(evt);
    };
    fire('wl-search:input', { target: { value: query } });
    const filtered = store['watchlist:html'] || '';
    if (escape) {
      fire('wl-search:keydown', {
        key: 'Escape',
        stopPropagation: () => { stopped = true },
        target: { value: query },
      });
    }
    console.log(JSON.stringify({
      handlers: Object.keys(handlers),
      watchlist_html: filtered,
      after_escape: store['watchlist:html'] || '',
      wl_count: store['wl-count:text'] || '',
      stopped,
    }));
""").strip()


def _render_with_query(tmp_path, query: str, escape: bool = False) -> dict:
    js = tmp_path / "app.js"; js.write_text(_script())
    h = tmp_path / "search.js"; h.write_text(SEARCH_HARNESS)
    pj = tmp_path / "p.json"; pj.write_text(json.dumps(PAYLOAD))
    wj = tmp_path / "w.json"; wj.write_text(json.dumps(WATCHLIST))
    r = subprocess.run(
        [NODE, str(h), str(js), str(pj), str(wj), query,
         "escape" if escape else "no"],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.stderr or "")[-1500:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert "wl-search:input" in out["handlers"], (
        "nothing is listening to the filter input"
    )
    return out


def _rows(table: str) -> list[str]:
    return re.findall(r'data-wl="([^"]+)"', table)


def test_the_watchlist_filters_on_the_symbol():
    """Every product examined has one, and 31 rows is already past scannable."""
    markup = STATIC.read_text()
    assert 'id="wl-search"' in markup, "the watchlist has no filter"
    assert 'aria-label="Filter your watchlist by symbol or company name"' in markup


def test_the_filter_matches_a_symbol(tmp_path):
    out = _render_with_query(tmp_path, "ifci")
    assert _rows(out["watchlist_html"]) == ["INE003"], (
        f"expected only the IFCI row: {_rows(out['watchlist_html'])}"
    )


def test_the_filter_matches_a_company_name_the_symbol_does_not_contain(tmp_path):
    """The point of matching on the name: a reader who remembers Balrampur
    should not have to know it is BALRAMCHIN. The fixture's ADANIPORTS row
    carries BALRAMPUR CHINI MILLS LTD precisely so the two cannot be confused
    for each other by a substring."""
    out = _render_with_query(tmp_path, "balrampur")
    table = out["watchlist_html"]
    assert _rows(table) == ["INE004"], (
        f"the row was not found by its company name: {_rows(table)}"
    )
    assert "ADANIPORTS" in table, "the matched row lost its symbol"


def test_the_filter_is_case_insensitive(tmp_path):
    lower = _render_with_query(tmp_path, "coal india")["watchlist_html"]
    upper = _render_with_query(tmp_path, "COAL INDIA")["watchlist_html"]
    mixed = _render_with_query(tmp_path, "Coal India")["watchlist_html"]
    assert lower == upper == mixed
    # Matched on the company name in three casings, and only that row.
    assert _rows(lower) == ["INE001"]


def test_a_filter_that_matches_nothing_says_so_and_how_to_clear_it(tmp_path):
    """An empty rail with no explanation is indistinguishable from a watchlist
    that lost its rows."""
    out = _render_with_query(tmp_path, "zzzznotasymbol", escape=True)
    table = out["watchlist_html"]
    assert not _rows(table)
    assert "matches" in table and "Escape" in table, (
        f"the empty result does not explain itself: {table}"
    )
    # And Escape really does put every row back — driven through the page's
    # own keydown listener, not by resetting the variable.
    assert len(_rows(out["after_escape"])) == len(
        [w for w in WATCHLIST if not w["muted"]]), (
        "Escape did not restore the full list"
    )
    assert out["stopped"], (
        "Escape in the filter did not stop propagating, so it also closes the "
        "legend the reader did not ask to close"
    )


def test_the_count_reports_the_watchlist_not_the_selection(tmp_path):
    """A footer count that silently became the size of a filter result would
    report the filter rather than the list."""
    out = _render_with_query(tmp_path, "ifci")
    total = len([w for w in WATCHLIST if not w["muted"]])
    assert f"{total} watched" in out["wl_count"], (
        f"the count became the size of the selection: {out['wl_count']!r}"
    )
    assert "1 shown" in out["wl_count"], (
        f"the count does not name the selection: {out['wl_count']!r}"
    )


def test_a_key_that_is_not_escape_leaves_the_filter_alone(tmp_path):
    """The other half. A keydown handler that cleared on every key would make
    the filter impossible to type into."""
    out = _render_with_query(tmp_path, "ifci", escape=False)
    assert _rows(out["after_escape"]) == ["INE003"], (
        "the filter was cleared without an Escape"
    )


# --- the mobile viewport ---------------------------------------------------
#
# A layout guard, not a markup guard, so it needs a real layout engine. The
# node harness has no CSS box model, so this asserts the two structural
# properties that caused the overflow — a fixed width and a no-shrink rule on
# an element wider than a phone — against the stylesheet and the rendered tag.
# The measured check runs in the browser and its numbers are in the ACTION-LOG.

# The narrowest viewport the page claims to support, and the widest fixed
# element that can sit inside a card at that width.
MOBILE_VIEWPORT = 390


def test_no_card_element_is_wider_than_a_phone_can_show(tmp_path):
    """FIX 1. At 390px the shell is one column and a card's content box is
    279px. Any fixed width above that overflows the page, and the page has no
    horizontal scroll to absorb it — the ticker truncates instead.

    Scanned on the RENDERED markup, not on the source. `width="${W}"` is a
    template placeholder that no source-level regex for a literal will catch,
    and that is exactly the form the defect shipped in."""
    html = _cards(tmp_path)
    assert len(html) > 0, "no card markup to scan"
    offenders = []
    for m in re.finditer(r"<(svg|div|span|img|p)\b([^>]*)>", html, re.S):
        attrs = m.group(2)
        if 'aria-hidden="true"' in attrs:
            continue
        for w in re.findall(r'\bwidth="(\d+)"', attrs):
            if int(w) > MOBILE_VIEWPORT:
                offenders.append(f"<{m.group(1)} width={w}>")
        for w in re.findall(r"\bwidth:\s*(\d+)px", attrs):
            if int(w) > MOBILE_VIEWPORT:
                offenders.append(f"<{m.group(1)} style width:{w}px>")
    assert not offenders, (
        f"these rendered elements are wider than a {MOBILE_VIEWPORT}px "
        f"viewport and will force horizontal scroll: {offenders}"
    )
    # And nothing on a card refuses to shrink inside its flex row, which is
    # the other half of the same failure.
    band = re.search(r"<svg[^>]*aria-label=\"[^\"]*normal range[^\"]*\"[^>]*>",
                     html, re.S)
    assert band and "shrink-0" not in band.group(0)


def test_the_band_is_fluid_and_its_row_wraps():
    """The two rules that make it fit: the graphic scales, and the verdict
    beside it drops beneath instead of being pushed past the card's edge."""
    css = STATIC.read_text().split("</style>")[0]
    band = css.split(".band-svg")[1].split("}")[0]
    assert "width: 100%" in band, "the band is not fluid"
    assert "height: auto" in band, "the band will not keep its aspect ratio"
    row = css.split(".band-row")[1].split("}")[0]
    assert "flex-wrap: wrap" in row, (
        "the band's row does not wrap, so the verdict is pushed off-screen"
    )
    wrap = css.split(".band-wrap")[1].split("}")[0]
    assert "min-width: 0" in wrap, "the band cannot shrink inside a flex row"
    assert "flex: 0 1 420px" in wrap, (
        "the band grows or has no upper bound; it should take its design width "
        "where there is room and only ever give width back"
    )


def test_the_axis_labels_are_html_so_scaling_cannot_shrink_them(tmp_path):
    """Scaling an SVG scales the type inside it. At 66% — a 390px viewport —
    the 9px axis labels would render near 6px. They live outside the viewBox
    now, and this asserts they were moved rather than deleted."""
    html = _cards(tmp_path)
    card = [a for a in html.split("<article") if "IFCI" in a][0]
    band = re.search(r"<svg[^>]*aria-label=\"[^\"]*normal range[^\"]*\".*?</svg>",
                     card, re.S)
    assert band, "no band on the card"
    assert "<text" not in band.group(0), (
        "the axis labels are back inside the viewBox, where scaling shrinks them"
    )
    axis = re.search(r'<div class="band-axis"[^>]*>(.*?)</div>', card, re.S)
    assert axis, "the axis labels were deleted rather than moved out of the SVG"
    for w in ("normal", "unusual"):
        assert w in axis.group(1), f"the axis lost its {w!r} end"
    # And the geometry the labels described is still drawn.
    assert band.group(0).count("<line") >= 3, (
        "the band lost the ticks its labels point at"
    )


# --- FIX 2: the rail answers a click where the reader is looking -----------


def test_the_rail_note_is_inserted_beneath_the_row_that_was_clicked():
    """It rendered at the top of the rail's scroll container. The rail is
    ~1,740px of scroll in a ~765px window, so clicking a row two-thirds down
    updated a note 733px above the viewport — for most of the list the click
    appeared to do nothing.

    Placement is a layout property, so the measured proof is in the ACTION-LOG
    (last row, middle row, first row: attached, fully visible, 0px cut). What
    this pins is the three rules that produce it."""
    script = _script()
    handler = script.split("// A row points at its card.")[1]
    assert "row.after(note)" in handler, (
        "the note is no longer inserted next to the row that was clicked"
    )
    assert "wl-note-inline" in handler, "the note has no attached styling"
    # Only the minimum scroll, and only when the note would otherwise be cut.
    assert "block: 'nearest'" in handler, (
        "the note is scrolled with something other than `nearest`, which moves "
        "the list out from under the cursor"
    )
    # A surfaced row has a card to point at, so the note goes home and hides.
    surfaced = handler.split("if (st.status === 'surfaced')")[1]
    assert "note.hidden = true" in surfaced and "wl-note-home" in surfaced, (
        "clicking a surfaced row leaves a stale note stranded in the list"
    )


def test_a_rerender_returns_the_note_home_before_replacing_the_table(tmp_path):
    """The note is MOVED into the table on a click. `renderWatchlist` replaces
    that table's markup on every background refresh, so without re-homing the
    node first it is destroyed and every later click writes into nothing."""
    r = _run(tmp_path, _script())
    assert r.returncode == 0, (r.stderr or "")[-1200:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["note_rehomed"], (
        "renderWatchlist did not return the note to its home container"
    )
    script = _script()
    body = script.split("function renderWatchlist")[1]
    home = body.index("wl-note-home")
    table = body.index("el('watchlist').innerHTML")
    assert home < table, (
        "the note is re-homed after the table is replaced, which is too late — "
        "the node is already gone"
    )


def test_every_card_is_collapsed_on_load(tmp_path):
    """FIX 4. The first card used to open on load and ran to 997px against a
    901px viewport, so the card a judge sees first was cut through the middle
    of its outcome table. The funnel is the open, populated block at the top of
    this column now; the cards beneath it are a list to triage."""
    html = _cards(tmp_path)
    articles = [a for a in html.split("<article") if "data-symbol" in a]
    assert len(articles) > 1, "too few cards to check"
    open_drawers = [a for a in articles if re.search(r"data-whybody=\"\d+\"\s+id=", a)
                    and 'hidden' not in a.split("data-whybody")[1][:60]]
    assert not open_drawers, (
        f"{len(open_drawers)} card(s) render with the drawer already open"
    )
    for a in articles:
        sym = re.search(r'data-symbol="([^"]+)"', a).group(1)
        btn = a.split("data-why=")[1]
        assert 'aria-expanded="false"' in btn[:120], (
            f"{sym}: the disclosure claims to be expanded on load"
        )
        assert "Investigate" in a, f"{sym}: the control does not read Investigate"
        assert ">Close<" not in a, f"{sym}: the control reads Close on load"

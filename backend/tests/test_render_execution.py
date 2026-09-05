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
    "salience_config": {"ecdf_window": 250, "min_history": 60},
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

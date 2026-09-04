"""Corporate-action price adjustment — spec §4, applied BEFORE returns.

    "Adjustment applied **before** returns are computed, using an adjustment
     factor from the corporate-actions feed. An unadjusted split is the single
     largest source of false alarms in this system; it is handled at the
     normalizer, not the detector."  — spec §4

The arithmetic, stated once so the rest of the engine never has to think about
it. For a session `t`, let

    F_t = Π { factor(a) : a is a corporate action with ex_date > t }

and define `adjusted_close_t = raw_close_t · F_t`. Then

    r_t = ln(adjusted_close_t / adjusted_close_{t−1})
        = ln(raw_close_t / (raw_close_{t−1} · factor_on_t))

because `F_{t−1} = F_t · factor_on_t` exactly when an action goes ex on `t`.
Everything before the ex-date is restated; the ex-date return is computed
against the restated previous close. This is the whole mechanism.

Three states come out of this module, and the detector must respect all three:

| Status       | When                                        | Detection |
|--------------|---------------------------------------------|-----------|
| `OK`         | normal bar, or an action with a known factor | runs      |
| `CORP_ACTION_UNADJUSTED` | ex-date of an action the feed gave no ratio for | suppressed |
| `STALE`      | more than one consecutive missing bar (§4)   | suppressed |

`CORP_ACTION_UNADJUSTED` is the honest answer to a demerger: the exchange tells
us a corporate action happened but not by how much the price was restated. The
alternative — backing the factor out of the observed price drop — would make the
adjuster structurally incapable of ever reporting a real crash, since every
crash would be "explained" as an adjustment.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from app.normalize.corporate_actions import CorpAction, RIGHTS, rights_factor

# Spec §4: "Missing data. Forward-fill at most 1 bar. Beyond that: status =
# STALE, confidence -> 0, detection suppressed, banner shown."
MAX_FORWARD_FILL = 1

STATUS_OK = "OK"
STATUS_STALE = "STALE"
STATUS_CORP_ACTION_UNADJUSTED = "CORP_ACTION_UNADJUSTED"

# A return that spans a non-trading break. Routed to D1 only; excluded from the
# CUSUM accumulator (§4, "Overnight / weekend gaps"). One calendar day apart is
# a normal consecutive session; anything wider crossed a weekend or a holiday.
GAP_CALENDAR_DAYS = 1


@dataclass(frozen=True)
class AdjustedBar:
    """One session of one symbol after normalization."""

    isin: str
    session_date: date
    raw_close: float | None
    adj_close: float | None
    ret: float | None                 # log return on the adjusted series
    adj_factor_on_date: float = 1.0   # factor for actions going ex *today*
    status: str = STATUS_OK
    is_gap: bool = False              # spans a non-trading break -> D1 only
    filled: bool = False              # forward-filled from the previous session
    corp_actions: tuple[CorpAction, ...] = ()

    @property
    def detectable(self) -> bool:
        """Price detection is permitted on this bar."""
        return self.status == STATUS_OK and self.ret is not None

    @property
    def adjusted_for_corp_action(self) -> bool:
        return self.adj_factor_on_date != 1.0


def resolve_factor(action: CorpAction, cum_close: float | None) -> float | None:
    """The multiplicative factor for one action, or None if not derivable.

    `cum_close` is the last close before the ex-date, needed only by rights.
    """
    if not action.adjustable:
        return None
    if action.adj_factor is not None:
        return float(action.adj_factor)
    if action.ca_type == RIGHTS:
        if cum_close is None or cum_close <= 0 or not action.ratio_den:
            return None
        return rights_factor(
            float(action.ratio_num or 0.0),
            float(action.ratio_den),
            float(action.cash_amount or 0.0),
            cum_close,
        )
    return None


def factor_for_session(
    actions: Sequence[CorpAction], cum_close: float | None
) -> tuple[float, bool]:
    """Combine every action going ex on one session.

    Returns `(factor, derivable)`. Factors multiply: AHCL went ex on a 1:1 bonus
    *and* a 10 -> 2 split on 2026-04-24, and 0.5 × 0.2 = 0.1 is the only value
    that reproduces the observed close.

    `derivable` is False as soon as one action on the date has no factor. A
    partially-adjusted bar is worse than an unadjusted one: it looks plausible.
    """
    factor = 1.0
    derivable = True
    for a in actions:
        f = resolve_factor(a, cum_close)
        if f is None or not math.isfinite(f) or f <= 0:
            derivable = False
            continue
        factor *= f
    return factor, derivable


def _is_gap(prev_session: date, session: date) -> bool:
    return (session - prev_session) > timedelta(days=GAP_CALENDAR_DAYS)


def adjust_series(
    isin: str,
    closes: Sequence[tuple[date, float | None]],
    actions: Iterable[CorpAction] = (),
    *,
    calendar: Sequence[date] | None = None,
) -> list[AdjustedBar]:
    """Adjust one symbol's close series and compute its log returns.

    `closes` is `(session_date, close)` ascending. `calendar` is the exchange's
    full session list for the window; when given, sessions the symbol is missing
    from are forward-filled once and then marked STALE, per §4.

    The returned bars carry the *adjusted* close, so every downstream consumer —
    attribution, EWMA, CUSUM, the U-score's trailing window — sees one
    consistent series. Nothing downstream is allowed to see `raw_close`.
    """
    by_date = {d: c for d, c in closes}
    sessions = list(calendar) if calendar is not None else [d for d, _ in closes]
    sessions = sorted(sessions)

    # Pass 1: per-ex-date factors. Rights need the cum close, so this cannot be
    # folded into the cumulative product below without a second lookup anyway.
    by_ex: dict[date, list[CorpAction]] = {}
    for a in actions:
        if a.isin == isin:
            by_ex.setdefault(a.ex_date, []).append(a)
    for v in by_ex.values():
        v.sort(key=lambda a: a.purpose)

    factors: dict[date, float] = {}
    derivable: dict[date, bool] = {}
    for ex_date, acts in by_ex.items():
        cum = _last_close_before(sessions, by_date, ex_date)
        f, ok = factor_for_session(acts, cum)
        factors[ex_date] = f
        derivable[ex_date] = ok

    # Pass 2: the cumulative back-adjustment. Walking backwards accumulates
    # every *future* action, which is what restates history rather than the
    # present. Adjusting forwards would move today's price, which is the number
    # the user is looking at.
    cumulative: dict[date, float] = {}
    running = 1.0
    for d in reversed(sessions):
        cumulative[d] = running
        running *= factors.get(d, 1.0)

    out: list[AdjustedBar] = []
    prev_adj: float | None = None
    prev_session: date | None = None
    missing_run = 0
    # An unadjustable action can go ex on a session the symbol did not trade —
    # TRIVENI's demerger was ex 2026-07-22 while the symbol was suspended, and
    # it resumed on 2026-08-05 at half its last price. The corrupted quantity is
    # the first *return that spans* the ex-date, not the ex-date bar. Carrying
    # the flag forward is what catches that; keying it to the bar would not.
    pending_unadjustable = False

    for d in sessions:
        if not derivable.get(d, True):
            pending_unadjustable = True

        raw = by_date.get(d)
        filled = False
        if raw is None:
            missing_run += 1
            if missing_run <= MAX_FORWARD_FILL and prev_adj is not None:
                # Forward-fill: the price is carried, so the return is exactly
                # zero rather than undefined. Never interpolate (§4).
                raw = _raw_from_adjusted(prev_adj, cumulative[d])
                filled = True
            else:
                out.append(AdjustedBar(isin=isin, session_date=d, raw_close=None,
                                       adj_close=None, ret=None,
                                       status=STATUS_STALE))
                continue
        else:
            missing_run = 0

        adj = raw * cumulative[d]
        acts = tuple(by_ex.get(d, ()))
        factor_today = factors.get(d, 1.0)

        ret = None
        if prev_adj is not None and prev_adj > 0 and adj > 0:
            ret = math.log(adj / prev_adj)

        status = STATUS_OK
        if pending_unadjustable:
            # The exchange says a corporate action went ex and did not tell us
            # the ratio. This move is arithmetic, not news.
            status = STATUS_CORP_ACTION_UNADJUSTED
            ret = None
            # A forward-filled bar carries the *pre*-action price forward, so it
            # cannot be the bar that absorbs the action — clearing the flag there
            # would leave the next real return still spanning the ex-date.
            # TRIVENI: demerger ex 2026-07-22 with the symbol suspended, filled
            # on the 22nd, genuinely resumed on 2026-08-05 at half its price.
            pending_unadjustable = filled
        # A forward-filled bar is NOT stale. §4 allows exactly one fill as the
        # sanctioned repair and puts STALE *beyond* it — which the `continue`
        # above already emits. What the fill costs is freshness, and therefore
        # confidence: `scores.freshness(filled=True)` halves C, so a filled bar
        # can be scored but cannot on its own clear a tier gate.

        gap = prev_session is not None and _is_gap(prev_session, d)

        out.append(
            AdjustedBar(
                isin=isin, session_date=d, raw_close=raw, adj_close=adj, ret=ret,
                adj_factor_on_date=factor_today, status=status, is_gap=gap,
                filled=filled, corp_actions=acts,
            )
        )
        prev_adj = adj
        prev_session = d

    return out


def _raw_from_adjusted(prev_adj: float, cum: float) -> float:
    return prev_adj / cum if cum else prev_adj


def _last_close_before(
    sessions: Sequence[date], by_date: Mapping[date, float | None], ex_date: date
) -> float | None:
    """The cum close: the last observed close strictly before the ex-date."""
    best: float | None = None
    for d in sessions:
        if d >= ex_date:
            break
        c = by_date.get(d)
        if c is not None:
            best = c
    return best

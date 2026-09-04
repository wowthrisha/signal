"""Corporate-action parsing — spec §4 ("Corporate actions") and §9.

`CORP_ACTION` has a dual role (§9): it sets `I = 2` in the ontology *and*
supplies the price adjustment factor. This module owns the second half — turning
the exchange's free-text `subject` line into a number — and nothing here touches
the network or a database, so every ratio below is a unit test rather than a
guess.

The one rule that matters: **an action whose factor cannot be derived from the
feed is never assigned a factor.** It is marked `adjustable = False`, and the
detector suppresses price detection on that bar (§4 treats an untrustworthy
price the same way it treats a circuit-breaker halt). Inventing a factor — or,
worse, inferring one from the price drop itself — would make the adjuster
incapable of ever seeing a genuine crash, which is the failure this whole layer
exists to prevent.

Ratio conventions, all verified against real NSE ex-dates in the ingested
window (see ops/ACTION-LOG.md [S0.2]):

| Subject                          | Meaning                        | factor      |
|----------------------------------|--------------------------------|-------------|
| `Bonus a:b`                      | a new shares for every b held  | `b/(a+b)`   |
| `Face Value Split ... Rs X To Y` | face value X -> Y              | `Y/X`       |
| `Rights a:b @ Premium Rs P`      | a offered for every b held     | TERP/C *    |
| `Dividend - Rs D Per Share`      | cash, price series unadjusted  | `1.0`       |
| `Buy Back`                       | no ex-price adjustment         | `1.0`       |
| `Demerger`, `Scheme Of Arrangement` | ratio not in the feed       | **none**    |

\\* Rights are price-dependent: `TERP = (b·C + a·S)/(a+b)` with `S` the
subscription price, so the factor is computed at adjustment time from the cum
close and only the ratio is stored. See `rights_factor`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

SOURCE_NAME = "nse_corporate_actions_api"

# Types the normalizer knows how to price. Anything else is CORP_ACTION for the
# ontology but unadjustable for the price series.
SPLIT = "SPLIT"
BONUS = "BONUS"
RIGHTS = "RIGHTS"
DIVIDEND = "DIVIDEND"
BUYBACK = "BUYBACK"
DEMERGER = "DEMERGER"
OTHER = "OTHER"

# Every parsed action is a CORP_ACTION in the §9 ontology, so I = 2 regardless
# of whether the price series can be adjusted for it.
CORP_ACTION_IMPORTANCE = 2


@dataclass(frozen=True)
class CorpAction:
    """One parsed corporate action, ready for the `corp_action` table."""

    isin: str
    ex_date: date
    purpose: str  # verbatim subject line — the audit trail for the factor
    ca_type: str
    ratio_num: float | None = None
    ratio_den: float | None = None
    face_from: float | None = None
    face_to: float | None = None
    cash_amount: float | None = None
    adj_factor: float | None = None
    adjustable: bool = False
    symbol: str | None = None

    @property
    def price_dependent(self) -> bool:
        """True when the factor needs the cum close, so it cannot be stored."""
        return self.adjustable and self.adj_factor is None


# --------------------------------------------------------------------------
# subject-line parsing
# --------------------------------------------------------------------------

_RATIO = re.compile(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)")
# "From Rs 10/- Per Share To Re 1/- Per Share" — NSE writes "Re" for 1 and "Rs"
# otherwise, and the "/-" is sometimes absent.
_SPLIT_FACE = re.compile(
    r"from\s+r[es]\.?\s*(\d+(?:\.\d+)?)\s*/?-?\s*per\s+share\s+to\s+r[es]\.?\s*(\d+(?:\.\d+)?)",
    re.I,
)
_PREMIUM = re.compile(r"premium\s+r[es]\.?\s*(\d+(?:\.\d+)?)", re.I)
_CASH = re.compile(r"r[es]\.?\s*(\d+(?:\.\d+)?)\s*(?:/-)?\s*per\s+(?:share|unit)", re.I)

# Checked in order. "Scheme Of Arrangement - Bonus Ncrps 4:1" contains "bonus"
# but the 4:1 is preference shares, not equity — adjusting the equity close by
# it would be a fabricated 80 % move, so the scheme/demerger tests come first.
_UNADJUSTABLE_MARKERS = ("scheme of arrangement", "demerger", "spin off", "spin-off")


def classify(subject: str) -> str:
    """Map a subject line to a `ca_type`. Order is load-bearing (see above)."""
    s = (subject or "").strip().lower()
    if not s:
        return OTHER
    if any(m in s for m in _UNADJUSTABLE_MARKERS):
        return DEMERGER
    if "split" in s or "sub-division" in s or "sub division" in s or "subdivision" in s:
        return SPLIT
    if "bonus" in s:
        return BONUS
    if "rights" in s:
        return RIGHTS
    if "buy back" in s or "buyback" in s:
        return BUYBACK
    if "dividend" in s or "distribution" in s or "interest payment" in s:
        return DIVIDEND
    return OTHER


def bonus_factor(new: float, held: float) -> float:
    """`Bonus a:b` — a new shares for every b held, so b shares become a+b."""
    if new < 0 or held <= 0:
        raise ValueError(f"nonsensical bonus ratio {new}:{held}")
    return held / (new + held)


def split_factor(face_from: float, face_to: float) -> float:
    """Face value `face_from` -> `face_to`. A 10 -> 2 split scales price by 0.2."""
    if face_from <= 0 or face_to <= 0:
        raise ValueError(f"nonsensical face values {face_from} -> {face_to}")
    return face_to / face_from


def rights_factor(offered: float, held: float, subscription_price: float, cum_close: float) -> float:
    """Theoretical ex-rights price as a fraction of the cum close.

        TERP = (held·C + offered·S) / (held + offered)

    Price-dependent, which is why it is computed here at adjustment time rather
    than stored on the row.
    """
    if offered < 0 or held <= 0 or cum_close <= 0:
        raise ValueError(f"nonsensical rights ratio {offered}:{held} at close {cum_close}")
    subscription_price = max(subscription_price, 0.0)
    terp = (held * cum_close + offered * subscription_price) / (held + offered)
    return terp / cum_close


def parse_subject(subject: str, face_value: float | None = None) -> dict[str, Any]:
    """Extract the priceable fields from one subject line.

    Returns a dict of the `CorpAction` fields this line determines. Never raises
    on a subject it does not understand — it returns `adjustable = False`, which
    is the safe state.
    """
    ca_type = classify(subject)
    out: dict[str, Any] = {"ca_type": ca_type, "adjustable": False, "adj_factor": None}

    if ca_type == SPLIT:
        m = _SPLIT_FACE.search(subject)
        if m:
            f_from, f_to = float(m.group(1)), float(m.group(2))
            out.update(face_from=f_from, face_to=f_to,
                       adj_factor=split_factor(f_from, f_to), adjustable=True)
        elif face_value:
            # A split with no "From ... To ..." clause carries no derivable
            # ratio: faceVal alone is the post-split value, not the change.
            out.update(face_to=float(face_value))
        return out

    if ca_type == BONUS:
        m = _RATIO.search(subject)
        if m:
            new, held = float(m.group(1)), float(m.group(2))
            if held > 0:
                out.update(ratio_num=new, ratio_den=held,
                           adj_factor=bonus_factor(new, held), adjustable=True)
        return out

    if ca_type == RIGHTS:
        m = _RATIO.search(subject)
        if m:
            offered, held = float(m.group(1)), float(m.group(2))
            premium = _PREMIUM.search(subject)
            prem = float(premium.group(1)) if premium else 0.0
            # Subscription price = face value + premium. Without a face value
            # the price is unknown, so the action stays unadjustable.
            if held > 0 and face_value:
                out.update(ratio_num=offered, ratio_den=held,
                           cash_amount=float(face_value) + prem,
                           adj_factor=None, adjustable=True)
        return out

    if ca_type in (DIVIDEND, BUYBACK):
        # No ex-price adjustment. A price series is not a total-return series;
        # adjusting for ordinary dividends would restate every close in the
        # history. The event is still emitted with I = 2, so a large special
        # dividend lands in Tier B (corroborated) rather than Tier C.
        m = _CASH.search(subject)
        if m:
            out["cash_amount"] = float(m.group(1))
        out.update(adj_factor=1.0, adjustable=True)
        return out

    # DEMERGER / OTHER: the feed names the action but not its ratio.
    return out


def _to_date(raw: str | None) -> date | None:
    raw = (raw or "").strip()
    if not raw or raw == "-":
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _face_value(raw: Any) -> float | None:
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_feed(rows: list[dict[str, Any]], series: str = "EQ") -> list[CorpAction]:
    """Turn the NSE corporate-actions payload into `CorpAction` records.

    Rows without an ISIN or an ex-date are dropped: §9 forbids fuzzy-matching a
    company name when a structured ISIN exists, and an action with no ex-date
    cannot be applied to a session.
    """
    out: list[CorpAction] = []
    for r in rows:
        isin = (r.get("isin") or "").strip()
        ex = _to_date(r.get("exDate"))
        if not isin or ex is None:
            continue
        if series and (r.get("series") or "").strip() not in (series, "", "-"):
            continue
        subject = (r.get("subject") or "").strip()
        fields = parse_subject(subject, _face_value(r.get("faceVal")))
        out.append(
            CorpAction(
                isin=isin,
                ex_date=ex,
                purpose=subject,
                symbol=(r.get("symbol") or "").strip() or None,
                **fields,
            )
        )
    # Deterministic order; the ledger and the fixtures both depend on it.
    out.sort(key=lambda a: (a.ex_date, a.isin, a.purpose))
    return out

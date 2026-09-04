"""The detection pipeline — normalize -> attribute -> detect -> salience.

One pass over the session calendar, in order. Each session does exactly what
spec §3's FULL PIPELINE says, and nothing reaches back in time:

    adjusted returns (§4)  ->  r_sec⊥ per sector (§8, step 1)
                           ->  βm, βs per symbol (§8, step 2)
                           ->  ε, σ̂ (EWMA), z (§4)
                           ->  breadth, D1, D2 (§4, §8)
                           ->  U, I, C, R  ->  tier (§7)

**Every estimation window ends the session before the one being scored.** The
betas, the EWMA scale, and the U-score's reference distribution are all built
from strictly prior bars. This is one rule applied three times, and it is what
makes the numbers this file produces one-step-ahead statistics rather than
in-sample fits — the difference between a detector and a description.

There is no weighted sum anywhere in this module. `tiers.classify` is the only
place the four salience quantities meet, and it gates on them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Mapping, Sequence

import numpy as np

from app.core.clock import Clock
from app.engine.attribute import ols
from app.engine.detect import breadth as breadth_mod
from app.engine.detect import d1, d2
from app.engine.detect.ewma import (
    EwmaVol,
    cross_sectional_prior,
    sigma_floor,
    standardize,
)
from app.engine.salience import scores, tiers
from app.ledger.writer import Event
from app.normalize.adjust import AdjustedBar
from app.normalize.corporate_actions import CORP_ACTION_IMPORTANCE

log = logging.getLogger(__name__)

EVENT_CORP_ACTION = "CORP_ACTION"
EVENT_DATA_STATE = "DATA_STATE"

STATUS_WARMUP = "WARMUP"
STATUS_ACTIVE = "ACTIVE"
STATUS_STALE = "STALE"


@dataclass(frozen=True)
class Thresholds:
    """The calibrated operating point (§7, "Threshold calibration").

    Loaded from `configs/thresholds.json` so it can be changed without a code
    change, which is also what makes the "thresholds are not baked in" check
    observable rather than a claim.
    """

    h1: float = d1.H1_DEFAULT
    h2: float = d2.H2_DEFAULT
    k: float = d2.K_DEFAULT
    cooldown_bars: int = d2.COOLDOWN_BARS
    d1_only: bool = False
    u_unusual: float = tiers.U_UNUSUAL
    u_unusual_uncorroborated: float = tiers.U_UNUSUAL_UNCORROBORATED
    calibrated_on: str | None = None

    @classmethod
    def load(cls, path=None) -> "Thresholds":
        import json
        from pathlib import Path

        p = Path(path) if path else _default_threshold_path()
        if not p.is_file():
            return cls()
        raw = json.loads(p.read_text())
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in fields})

    def as_dict(self) -> dict:
        return {
            "h1": self.h1, "h2": self.h2, "k": self.k,
            "cooldown_bars": self.cooldown_bars, "d1_only": self.d1_only,
            "u_unusual": self.u_unusual,
            "u_unusual_uncorroborated": self.u_unusual_uncorroborated,
            "calibrated_on": self.calibrated_on,
        }


def _default_threshold_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[3] / "configs" / "thresholds.json"


@dataclass
class SymbolResult:
    """Everything one symbol produced on one session. The card's whole trace."""

    isin: str
    session_date: date
    ret: float | None = None
    residual: float | None = None
    sigma: float | None = None
    z: float | None = None
    n_obs: int = 0
    status: str = STATUS_WARMUP
    attribution: ols.Attribution | None = None
    jump: d1.JumpSignal | None = None
    drift: d2.DriftSignal | None = None
    u: float | None = None
    i: int = 0
    c: float = 0.0
    confidence: scores.Confidence | None = None
    verdict: tiers.Verdict | None = None
    event_types: tuple[str, ...] = ()
    suppressed_by_regime: bool = False


@dataclass
class SessionResult:
    session_date: date
    breadth: breadth_mod.Breadth
    sigma_floor: float
    results: list[SymbolResult] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    @property
    def zs(self) -> list[float]:
        return [r.z for r in self.results if r.z is not None]


@dataclass
class _State:
    """Per-symbol carry-over between sessions."""

    vol: EwmaVol
    cusum: d2.Cusum
    z_history: list[float] = field(default_factory=list)
    attribution: ols.Attribution | None = None
    n_obs: int = 0


class Pipeline:
    """Runs the detector over an aligned universe.

    Constructed from arrays rather than a database handle so the same object
    serves the replay harness, the tests, and the CLI. Clock injection is
    mandatory (CLAUDE.md): every `detected_at` comes from it.
    """

    def __init__(
        self,
        sessions: Sequence[date],
        universe: Mapping[str, Sequence[AdjustedBar]],
        market_returns: Mapping[date, float],
        sector_returns: Mapping[str, Mapping[date, float]],
        sector_of: Mapping[str, str | None],
        *,
        clock: Clock,
        thresholds: Thresholds | None = None,
        volumes: Mapping[tuple[str, date], int] | None = None,
        corp_actions: Mapping[tuple[str, date], list[str]] | None = None,
    ) -> None:
        self.sessions = list(sessions)
        self.index = {d: i for i, d in enumerate(self.sessions)}
        self.isins = sorted(universe)
        self.clock = clock
        self.th = thresholds or Thresholds()
        self.volumes = volumes or {}
        self.corp_actions = corp_actions or {}
        self.sector_of = dict(sector_of)

        T = len(self.sessions)
        self.ret = {isin: np.full(T, np.nan) for isin in self.isins}
        self.bars: dict[str, dict[date, AdjustedBar]] = {}
        for isin in self.isins:
            byd = {}
            for b in universe[isin]:
                byd[b.session_date] = b
                ti = self.index.get(b.session_date)
                if ti is not None and b.ret is not None:
                    self.ret[isin][ti] = b.ret
            self.bars[isin] = byd

        self.rm = np.array([market_returns.get(d, np.nan) for d in self.sessions])
        self.rs = {
            name: np.array([series.get(d, np.nan) for d in self.sessions])
            for name, series in sector_returns.items()
        }
        self.state = {
            isin: _State(
                vol=EwmaVol(),
                cusum=d2.Cusum(k=self.th.k, h2=self.th.h2,
                               cooldown_bars=self.th.cooldown_bars),
            )
            for isin in self.isins
        }
        self.by_sector: dict[str | None, list[str]] = {}
        for isin in self.isins:
            self.by_sector.setdefault(self.sector_of.get(isin), []).append(isin)

    # -- attribution -------------------------------------------------------

    def _window(self, ti: int) -> slice:
        """`[t−120, t−1]` — strictly prior, clipped at the start of history."""
        return slice(max(0, ti - ols.ESTIMATION_WINDOW), ti)

    def _sector_perp(self, ti: int) -> dict[str | None, tuple[np.ndarray, float] | None]:
        """Step 1 for every sector: the in-window `r_sec⊥` series, plus today's
        value formed by applying the fitted `(a, g)` to today's sector return."""
        w = self._window(ti)
        out: dict[str | None, tuple[np.ndarray, float] | None] = {}
        rm_w = self.rm[w]
        ok_w = np.isfinite(rm_w)
        for name, series in self.rs.items():
            s_w = series[w]
            mask = ok_w & np.isfinite(s_w)
            if mask.sum() < ols.MIN_OBS_FULL or not np.isfinite(self.rm[ti]):
                out[name] = None
                continue
            a, g, resid = ols.fit_sector(s_w[mask], rm_w[mask])
            # Scatter back onto the window so the row alignment with a symbol's
            # own returns survives; sessions the sector index was missing stay
            # NaN and are dropped from step 2's sample too.
            full = np.full(s_w.shape, np.nan)
            full[mask] = resid
            today = self.rs[name][ti]
            perp_today = (
                float(today - (a + g * self.rm[ti])) if np.isfinite(today) else np.nan
            )
            out[name] = (full, perp_today)
        return out

    def _attribute(self, ti: int, perp: dict) -> dict[str, ols.Attribution | None]:
        """Step 2 for every symbol, with §8's shrinkage toward the sector mean."""
        w = self._window(ti)
        rm_w = self.rm[w]
        rm_t = self.rm[ti]
        out: dict[str, ols.Attribution | None] = {}

        for sector, isins in self.by_sector.items():
            sec = perp.get(sector) if sector else None
            perp_w, perp_t = (sec if sec else (None, np.nan))

            raw: dict[str, ols.Attribution] = {}
            for isin in isins:
                y_w = self.ret[isin][w]
                base = np.isfinite(y_w) & np.isfinite(rm_w)
                with_sector = (
                    base & np.isfinite(perp_w) if perp_w is not None else None
                )
                # Use the sector factor only when it survives the mask with a
                # full 60-observation sample; otherwise fall back to the
                # market-only model rather than fitting three parameters on a
                # short one (§8, "Minimum history").
                use_sector = (
                    with_sector is not None
                    and int(with_sector.sum()) >= ols.MIN_OBS_FULL
                )
                use = with_sector if use_sector else base
                if int(use.sum()) < ols.MIN_OBS_ANY:
                    out[isin] = None
                    continue
                est = ols.estimate(
                    y_w[use], rm_w[use], perp_w[use] if use_sector else None
                )
                if est is None:
                    out[isin] = None
                    continue
                raw[isin] = est

            # The shrinkage target is the sector's *unshrunk* mean market beta,
            # computed once per session. Shrinking toward an already-shrunk mean
            # would pull the whole sector a little further toward it each
            # session, which over 127 sessions is a drift nobody asked for.
            mean_beta = (
                float(np.mean([a.beta_mkt for a in raw.values()])) if raw else 1.0
            )

            # β̂_shr = w·β̂ + (1−w)·β̄_sector, w = n/(n+60). Applied without
            # refitting: shrinkage is a post-estimation adjustment (§8).
            for isin, est in raw.items():
                if est.n_obs < ols.ESTIMATION_WINDOW:
                    from dataclasses import replace

                    out[isin] = replace(
                        est,
                        beta_mkt=ols.shrink(est.beta_mkt, mean_beta, est.n_obs),
                        shrunk=True,
                    )
                else:
                    out[isin] = est

            # Decompose today's return with the fitted loadings.
            for isin in isins:
                est = out.get(isin)
                if est is None:
                    continue
                r_t = self.ret[isin][ti]
                if not np.isfinite(r_t) or not np.isfinite(rm_t):
                    out[isin] = est
                    continue
                out[isin] = ols.apply(
                    est, float(r_t), float(rm_t),
                    float(perp_t) if (est.has_sector and np.isfinite(perp_t)) else 0.0,
                )
        return out

    # -- one session -------------------------------------------------------

    def step(self, ti: int) -> SessionResult:
        session = self.sessions[ti]
        perp = self._sector_perp(ti)
        attributions = self._attribute(ti, perp)

        # Pass 1: residuals and the σ̂ each symbol will be measured against.
        # σ̂ is built from residuals up to t−1, so it is known before any of
        # today's residuals are looked at — which is what lets the cross-
        # sectional floor be computed from it without circularity.
        residuals: dict[str, float | None] = {}
        own_sigma: dict[str, float | None] = {}
        for isin in self.isins:
            st = self.state[isin]
            att = attributions.get(isin)
            bar = self.bars[isin].get(session)
            resid = None
            if att is not None and bar is not None and bar.detectable:
                resid = att.residual
            residuals[isin] = resid
            own_sigma[isin] = st.vol.sigma()

        # §4 warm-up: a symbol without a seeded EWMA is measured against the
        # cross-sectional σ, never against a thin estimate of its own.
        prior = cross_sectional_prior(own_sigma.values())

        pre: list[tuple[str, ols.Attribution | None, float | None, float | None]] = []
        sigmas: list[float] = []
        for isin in self.isins:
            sigma = own_sigma[isin] if own_sigma[isin] is not None else prior
            if sigma is not None:
                sigmas.append(sigma)
            pre.append((isin, attributions.get(isin), residuals[isin], sigma))

        floor = sigma_floor(sigmas)

        # Pass 2: standardize, then measure breadth across the whole universe
        # before any per-symbol decision is taken (§8).
        zs: dict[str, float | None] = {}
        for isin, att, resid, sigma in pre:
            zs[isin] = standardize(resid, sigma, floor)
        breadth = breadth_mod.measure(zs.values())

        out = SessionResult(session_date=session, breadth=breadth, sigma_floor=floor)
        detected_at = self.clock.now()

        for isin, att, resid, sigma in pre:
            st = self.state[isin]
            bar = self.bars[isin].get(session)
            z = zs[isin]
            n_obs = att.n_obs if att else 0

            res = SymbolResult(
                isin=isin, session_date=session,
                ret=bar.ret if bar else None,
                residual=resid, sigma=sigma, z=z, n_obs=n_obs,
                attribution=att,
            )

            if bar is None or bar.status != "OK":
                res.status = STATUS_STALE
            elif n_obs < ols.MIN_OBS_FULL:
                res.status = STATUS_WARMUP
            else:
                res.status = STATUS_ACTIVE

            # §4 warm-up: below 20 observations there is no detection at all.
            detectable = (
                bar is not None and bar.detectable and z is not None
                and n_obs >= ols.MIN_OBS_ANY
            )

            if detectable:
                res.jump = d1.detect_jump(z, self.th.h1)
                # D2 is disabled during warm-up (§4) and by the Gate 4 cut.
                if not self.th.d1_only and n_obs >= ols.MIN_OBS_FULL:
                    res.drift = st.cusum.observe(z, is_gap=bool(bar and bar.is_gap))
                elif bar is not None and bar.is_gap:
                    pass  # gap returns never enter the accumulator
            elif bar is not None and bar.status != "OK":
                # The price series is not trustworthy; accumulated drift
                # evidence was about a series we no longer believe.
                st.cusum.reset()

            # U against this symbol's own trailing |z| history (strictly prior).
            res.u = scores.u_score(z, st.z_history)

            ca_types = self.corp_actions.get((isin, session), [])
            res.event_types = tuple(ca_types)
            res.i = scores.i_score(ca_types)

            conf = scores.confidence(
                n_obs=n_obs,
                volume=self.volumes.get((isin, session)),
                staleness_sessions=0 if (bar and bar.status == "OK") else 2,
                filled=bool(bar and bar.filled),
                stale=bool(bar is None or bar.status != "OK"),
                has_sector=bool(att and att.has_sector),
            )
            res.confidence = conf
            res.c = conf.value
            res.verdict = tiers.classify(
                u=res.u, i=res.i, c=res.c,
                u_unusual=self.th.u_unusual,
                u_unusual_uncorroborated=self.th.u_unusual_uncorroborated,
            )

            if breadth.is_regime and res.jump is not None:
                # "suppress all individual JUMP events for that session" (§8).
                res.suppressed_by_regime = True
                res.jump = None

            out.results.append(res)
            out.events.extend(_events_for(res, detected_at, session))

            # Carry state forward. The EWMA update uses today's residual, which
            # becomes tomorrow's scale — never today's.
            if resid is not None and np.isfinite(resid):
                st.vol.update(resid)
            if z is not None:
                st.z_history.append(z)
                if len(st.z_history) > scores.U_WINDOW:
                    del st.z_history[0]
            st.n_obs = n_obs

        if breadth.is_regime:
            out.events.append(_regime_event(session, breadth, detected_at))

        # Deterministic order before the ledger sees them, so event_id
        # assignment never inherits dict iteration order.
        out.events.sort(key=lambda e: (e.session_date, e.isin or "", e.event_type))
        return out

    def run(self, start_index: int = 0) -> list[SessionResult]:
        return [self.step(ti) for ti in range(start_index, len(self.sessions))]


# --------------------------------------------------------------------------
# event construction
# --------------------------------------------------------------------------


def _payload(res: SymbolResult) -> dict:
    att = res.attribution
    payload: dict = {
        "z": _r(res.z),
        "return": _r(res.ret),
        "residual": _r(res.residual),
        "sigma": _r(res.sigma),
        "n_obs": res.n_obs,
        "status": res.status,
        "u": _r(res.u),
        "i": res.i,
        "c": _r(res.c),
        "tier": res.verdict.tier if res.verdict else tiers.TIER_SUPPRESSED,
        "gate": res.verdict.gate if res.verdict else tiers.GATE_SUPPRESSED,
    }
    if att is not None:
        payload["attribution"] = {
            "model": att.model,
            "alpha": _r(att.alpha),
            "beta_mkt": _r(att.beta_mkt),
            "beta_sec": _r(att.beta_sec),
            "shrunk": att.shrunk,
            "market_component": _r(att.market_component),
            "sector_component": _r(att.sector_component),
            "stock_specific": _r(att.residual),
        }
    if res.confidence is not None:
        payload["confidence"] = res.confidence.as_dict()
    return payload


def _r(x, places: int = 6):
    """Fixed precision. A replay artifact must not depend on `repr(float)`."""
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), places)


def _events_for(res: SymbolResult, detected_at: datetime, session: date) -> list[Event]:
    events: list[Event] = []
    occurred_at = datetime.combine(session, detected_at.timetz())
    verdict = res.verdict
    tier = verdict.tier if verdict else tiers.TIER_SUPPRESSED
    gate = verdict.gate if verdict else tiers.GATE_SUPPRESSED

    if res.jump is not None:
        p = _payload(res)
        p["direction"] = res.jump.direction
        p["threshold"] = res.jump.threshold
        events.append(Event(
            isin=res.isin, event_type=d1.EVENT_JUMP, session_date=session,
            occurred_at=occurred_at, detected_at=detected_at,
            confidence=res.c, payload=p, u_score=res.u, i_score=res.i,
            magnitude=res.ret or 0.0,
        ))
    if res.drift is not None:
        p = _payload(res)
        p["direction"] = res.drift.direction
        p["cusum"] = _r(res.drift.statistic)
        p["threshold"] = res.drift.threshold
        p["bars"] = res.drift.bars
        events.append(Event(
            isin=res.isin, event_type=d2.EVENT_DRIFT, session_date=session,
            occurred_at=occurred_at, detected_at=detected_at,
            confidence=res.c, payload=p, u_score=res.u, i_score=res.i,
            magnitude=res.drift.statistic / 100.0,
        ))
    for ca in res.event_types:
        p = _payload(res)
        p["ca_type"] = ca
        events.append(Event(
            isin=res.isin, event_type=EVENT_CORP_ACTION, session_date=session,
            occurred_at=occurred_at, detected_at=detected_at,
            confidence=res.c, payload=p,
            u_score=res.u, i_score=CORP_ACTION_IMPORTANCE,
            magnitude=res.ret or 0.0,
        ))
        break  # one CORP_ACTION per symbol-session; the ledger dedups anyway
    for e in events:
        e.payload["tier"] = tier
        e.payload["gate"] = gate
    return events


def _regime_event(session: date, b: breadth_mod.Breadth, detected_at: datetime) -> Event:
    """One notification, not fifty (§8)."""
    return Event(
        isin=None,
        event_type=breadth_mod.EVENT_MARKET_REGIME,
        session_date=session,
        occurred_at=datetime.combine(session, detected_at.timetz()),
        detected_at=detected_at,
        confidence=1.0,
        payload={
            "breadth": _r(b.fraction),
            "n_extreme": b.n_extreme,
            "n_universe": b.n_universe,
            "threshold": b.threshold,
            "card_cap": breadth_mod.REGIME_CARD_CAP,
            "tier": tiers.TIER_B,
            "gate": tiers.GATE_B,
        },
        u_score=None,
        i_score=2,
        magnitude=b.fraction,
    )

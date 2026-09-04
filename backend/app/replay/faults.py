"""Seeded fault-injection decorators — spec §13.

Each decorator wraps a BarProvider and perturbs the stream one way. All draws go
through `stable_rng(seed, fault_name, session_date, ...)`, so:

  * the same seed reproduces the same faults exactly;
  * a fault's decisions do not depend on how many sessions preceded it;
  * enabling one fault does not reshuffle another's draws.

That last property is what makes the ablation matrix (§15) meaningful — rows
differ because of the fault under test, not because the RNG stream shifted.

| Fault       | Config              | Expected system behaviour                  |
|-------------|---------------------|--------------------------------------------|
| Stale       | stale_after_bars: 3 | C -> 0, suppressed, banner                  |
| Delayed     | delay_bars: 2       | detection lags; detected_at != occurred_at  |
| Missing     | drop_prob: 0.05     | forward-fill 1, then STALE                  |
| Duplicate   | dup_prob: 0.02      | dedup_key collapses; no double alert        |
| Out-of-order| reorder_window: 3   | sequence check drops the stale bar          |
| Conflicting | source_b_delta: 0.02| UNCERTAIN; display a range, not a point     |
| API failure | fail_at_bar: 40     | circuit breaker -> cached snapshot -> replay|
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from app.replay.provider import Bar, BarProvider, stable_rng


class ProviderUnavailable(RuntimeError):
    """The injected API-failure fault. The pipeline must circuit-break, not crash."""


@dataclass
class _Fault:
    inner: BarProvider
    seed: int

    name = "fault"

    def sessions(self) -> list[date]:
        return self.inner.sessions()

    def bars_for(self, session_date: date) -> list[Bar]:  # pragma: no cover - overridden
        return self.inner.bars_for(session_date)

    def _rng(self, *parts: object):
        return stable_rng(self.seed, self.name, *parts)


@dataclass
class StaleFault(_Fault):
    """After N bars, freeze each symbol's close at its last fresh value.

    The data keeps arriving and keeps looking well-formed — that is what makes
    staleness dangerous and why confidence must collapse rather than the row
    being dropped.
    """

    stale_after_bars: int = 3
    name = "stale"

    def __post_init__(self) -> None:
        self._frozen: dict[str, float] = {}
        self._n = 0

    def bars_for(self, session_date: date) -> list[Bar]:
        bars = self.inner.bars_for(session_date)
        idx = self.inner.sessions().index(session_date)
        if idx < self.stale_after_bars:
            for b in bars:
                if b.c is not None:
                    self._frozen[b.isin] = b.c
            return bars
        return [
            b.with_close(self._frozen[b.isin], source=f"{b.source}:STALE")
            if b.isin in self._frozen else b
            for b in bars
        ]


@dataclass
class DelayedFault(_Fault):
    """Serve session t's bars at session t+delay. Detection lags reality."""

    delay_bars: int = 2
    name = "delayed"

    def bars_for(self, session_date: date) -> list[Bar]:
        sessions = self.inner.sessions()
        i = sessions.index(session_date)
        j = i - self.delay_bars
        if j < 0:
            return []
        # Bars carry their true session_date, so occurred_at stays the real
        # session while detected_at becomes the session we served them in.
        return self.inner.bars_for(sessions[j])


@dataclass
class MissingFault(_Fault):
    """Drop each bar independently with probability drop_prob."""

    drop_prob: float = 0.05
    name = "missing"

    def bars_for(self, session_date: date) -> list[Bar]:
        return [
            b for b in self.inner.bars_for(session_date)
            if self._rng(session_date, b.isin).random() >= self.drop_prob
        ]


@dataclass
class DuplicateFault(_Fault):
    """Re-emit a bar with probability dup_prob. dedup_key must collapse it."""

    dup_prob: float = 0.02
    name = "duplicate"

    def bars_for(self, session_date: date) -> list[Bar]:
        out: list[Bar] = []
        for b in self.inner.bars_for(session_date):
            out.append(b)
            if self._rng(session_date, b.isin).random() < self.dup_prob:
                out.append(b)
        return out


@dataclass
class OutOfOrderFault(_Fault):
    """Permute bars within a sliding window.

    Order within a session is not semantically meaningful, so this exercises the
    sequence check rather than the arithmetic: a consumer that depends on
    arrival order will diverge, one that keys on (isin, session_date) will not.
    """

    reorder_window: int = 3
    name = "out_of_order"

    def bars_for(self, session_date: date) -> list[Bar]:
        bars = self.inner.bars_for(session_date)
        w = max(int(self.reorder_window), 1)
        out: list[Bar] = []
        for start in range(0, len(bars), w):
            chunk = bars[start:start + w]
            rng = self._rng(session_date, start)
            rng.shuffle(chunk)
            out.extend(chunk)
        return out


@dataclass
class ConflictingSourceFault(_Fault):
    """A second source disagrees by source_b_delta.

    Emits the primary bar plus a source-B variant. The pipeline must report a
    range and mark the event UNCERTAIN rather than silently picking one.
    """

    source_b_delta: float = 0.02
    name = "conflicting"

    def bars_for(self, session_date: date) -> list[Bar]:
        out: list[Bar] = []
        for b in self.inner.bars_for(session_date):
            out.append(b)
            if b.c is not None:
                out.append(b.with_close(b.c * (1.0 + self.source_b_delta), source="source_b"))
        return out


@dataclass
class ApiFailureFault(_Fault):
    """Raise ProviderUnavailable at a fixed bar index.

    Deterministic by index rather than by probability: a circuit-breaker test
    that only sometimes trips is not a test.
    """

    fail_at_bar: int = 40
    name = "api_failure"

    def bars_for(self, session_date: date) -> list[Bar]:
        idx = self.inner.sessions().index(session_date)
        if idx == self.fail_at_bar:
            raise ProviderUnavailable(
                f"injected API failure at bar {idx} ({session_date})"
            )
        return self.inner.bars_for(session_date)


FAULTS: dict[str, tuple[type[_Fault], str]] = {
    "stale": (StaleFault, "stale_after_bars"),
    "delayed": (DelayedFault, "delay_bars"),
    "missing": (MissingFault, "drop_prob"),
    "duplicate": (DuplicateFault, "dup_prob"),
    "out_of_order": (OutOfOrderFault, "reorder_window"),
    "conflicting": (ConflictingSourceFault, "source_b_delta"),
    "api_failure": (ApiFailureFault, "fail_at_bar"),
}


def build_chain(inner: BarProvider, config: dict[str, Any], seed: int) -> BarProvider:
    """Wrap `inner` in the faults named in config, in a fixed order.

    Order is FAULTS' declaration order, not dict order, so two configs listing
    the same faults differently produce the same chain.
    """
    provider = inner
    for name, (cls, param) in FAULTS.items():
        if name not in config:
            continue
        value = config[name]
        if value is None or value is False:
            continue
        kwargs = {param: value} if not isinstance(value, dict) else value
        provider = cls(inner=provider, seed=seed, **kwargs)
    return provider

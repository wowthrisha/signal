"""D1 — jump detection on the standardized residual. Spec §4.

    fire if |z_t| >= h1        h1 starts at 3.0, then is calibrated to an
                               alert budget on held-out replay (§7)

That last clause is the whole design stance: `h1 = 3.0` is a *starting value*,
not a distributional claim. Nobody here is asserting that standardized equity
residuals are Gaussian, so "3 sigma is one-in-370" is not a sentence this system
says. The threshold is an operating point, reported as one.
"""
from __future__ import annotations

from dataclasses import dataclass

H1_DEFAULT = 3.0

EVENT_JUMP = "JUMP"
DIRECTION_UP = "UP"
DIRECTION_DOWN = "DOWN"


@dataclass(frozen=True)
class JumpSignal:
    z: float
    threshold: float
    direction: str

    @property
    def magnitude(self) -> float:
        return abs(self.z)


def detect_jump(z: float | None, h1: float = H1_DEFAULT) -> JumpSignal | None:
    """Two-sided, inclusive at the threshold.

    Inclusive because the boundary has to land somewhere and `>=` is what the
    checklist's boundary test pins: 3.0 fires, 2.99 does not.
    """
    if z is None:
        return None
    if abs(z) < h1:
        return None
    return JumpSignal(
        z=float(z), threshold=float(h1),
        direction=DIRECTION_UP if z > 0 else DIRECTION_DOWN,
    )

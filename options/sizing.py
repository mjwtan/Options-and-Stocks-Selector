"""Delta-equivalent options sizing - system-spec.md S5.6.

Converts a target notional (from the equity weight computed in S4) into a
contract count on a delta-equivalent basis, so the portfolio's directional
exposure matches what the equity weights imply. Round down always; a short
put's capital commitment is the strike, not the spot, and must be tracked
separately (S5.6's "most likely way this system overcommits capital").
"""

from __future__ import annotations

import math
from typing import Optional


def contracts_for_long_call(target_notional: float, delta_value: Optional[float], spot: float) -> int:
    if not delta_value or delta_value <= 0 or spot <= 0:
        return 0
    return max(0, math.floor(target_notional / (delta_value * spot * 100)))


def contracts_for_short_put(target_notional: float, strike: float) -> int:
    if strike <= 0:
        return 0
    return max(0, math.floor(target_notional / (strike * 100)))


def capital_reserved_for_short_put(strike: float, contracts: int) -> float:
    return strike * 100 * contracts

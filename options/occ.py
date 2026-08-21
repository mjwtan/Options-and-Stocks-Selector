"""OCC option symbol parsing - e.g. "AAPL240315C00190000" ->
underlying=AAPL, expiry=2024-03-15, type=call, strike=190.00.

Needed because alpaca-py's Position model (used for option_positions())
carries only the OCC symbol, not strike/expiry/type as separate fields -
unlike get_option_contracts()/get_option_chain(), which return those
already parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_OCC_RE = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


@dataclass
class ParsedOcc:
    underlying: str
    expiry: date
    contract_type: str  # "call" or "put"
    strike: float


def parse_occ_symbol(symbol: str) -> ParsedOcc:
    m = _OCC_RE.match(symbol)
    if not m:
        raise ValueError(f"not a recognisable OCC option symbol: {symbol!r}")
    root, yy, mm, dd, cp, strike_raw = m.groups()
    return ParsedOcc(
        underlying=root,
        expiry=date(2000 + int(yy), int(mm), int(dd)),
        contract_type="call" if cp == "C" else "put",
        strike=int(strike_raw) / 1000.0,
    )

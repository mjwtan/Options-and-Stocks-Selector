"""Option chain quality filtering - system-spec.md S3.5.

Discards any contract failing the quality filters, and flags the options
layer to be skipped entirely for a name if too few contracts survive.
Nothing here talks to a provider directly - it consumes whatever
OptionsBroker.chain() returns, so it works the same regardless of which
adapter is behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from brokers.base import Contract, OptionsBroker

MAX_RELATIVE_SPREAD = 0.15
MIN_OPEN_INTEREST = 100
MIN_DTE = 21
MAX_DTE = 90
MIN_SURVIVING_CONTRACTS = 4  # not a specific number in system-spec.md S3.5 ("too few") - a name needs
                              # enough surviving strikes for S5.4/S5.5's expiry/delta search to have room


@dataclass
class ChainResult:
    contracts_by_expiry: dict[date, list[Contract]] = field(default_factory=dict)
    total_fetched: int = 0
    total_surviving: int = 0
    skip_reason: Optional[str] = None  # set -> S5.7 fallback to shares for this name

    @property
    def expiries(self) -> list[date]:
        return sorted(self.contracts_by_expiry.keys())


def fetch_filtered_chain(
    options_broker: OptionsBroker,
    symbol: str,
    valuation_date: date,
    min_dte: int = MIN_DTE,
    max_dte: int = MAX_DTE,
    max_relative_spread: float = MAX_RELATIVE_SPREAD,
    min_open_interest: int = MIN_OPEN_INTEREST,
    min_surviving_contracts: int = MIN_SURVIVING_CONTRACTS,
) -> ChainResult:
    expiry_range = (valuation_date + timedelta(days=min_dte), valuation_date + timedelta(days=max_dte))
    raw = options_broker.chain(symbol, expiry_range)

    survivors = []
    for c in raw:
        if c.bid <= 0 or c.ask <= 0:
            continue
        mid = c.mid
        if mid <= 0:
            continue
        if (c.ask - c.bid) / mid > max_relative_spread:
            continue
        if c.open_interest < min_open_interest:
            continue
        dte = (c.expiry - valuation_date).days
        if not (min_dte <= dte <= max_dte):
            continue
        survivors.append(c)

    by_expiry: dict[date, list[Contract]] = {}
    for c in survivors:
        by_expiry.setdefault(c.expiry, []).append(c)

    skip_reason = None
    if len(survivors) < min_surviving_contracts:
        skip_reason = (
            f"only {len(survivors)} contract(s) survived S3.5 quality filters "
            f"(need >= {min_surviving_contracts}) out of {len(raw)} fetched"
        )

    return ChainResult(
        contracts_by_expiry=by_expiry,
        total_fetched=len(raw),
        total_surviving=len(survivors),
        skip_reason=skip_reason,
    )

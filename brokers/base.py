"""Provider-agnostic protocols and value types - system-spec.md S1.1.

Three protocols, deliberately separate (equity broker, options broker, and
market data may be three different vendors - S1.1). Nothing downstream of
these should import a provider SDK (alpaca-py, etc.) directly; it should
receive an object conforming to one of these Protocols instead.

Value types here are intentionally minimal - just the fields the pipeline
actually consumes today (position_sizing.py, trade_from_csv.py) plus what
system-spec.md S5's options layer needs. They exist so a provider swap only
touches the adapter implementing these protocols, never the strategy code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

import pandas as pd


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class FillStatus(str, Enum):
    """Provider-agnostic terminal/non-terminal order states. Adapters map
    their SDK's native status enum onto this one."""
    NEW = "new"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    PENDING = "pending"

    @property
    def is_terminal(self) -> bool:
        return self in (
            FillStatus.FILLED,
            FillStatus.PARTIALLY_FILLED,
            FillStatus.CANCELED,
            FillStatus.EXPIRED,
            FillStatus.REJECTED,
        )

    @property
    def is_ok(self) -> bool:
        return self in (FillStatus.FILLED, FillStatus.PARTIALLY_FILLED)


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class Position:
    symbol: str
    qty: float
    market_value: float
    unrealized_plpc: Optional[float] = None  # fraction of cost basis, e.g. -0.15 = -15%; used by daily_monitor.py's stop-loss check


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float
    portfolio_value: float


@dataclass
class Order:
    id: str
    symbol: str
    status: FillStatus
    qty: Optional[float] = None
    notional: Optional[float] = None


@dataclass
class Contract:
    """One option contract surviving system-spec.md S3.5's quality filters."""
    occ_symbol: str
    underlying: str
    contract_type: str          # "call" or "put"
    strike: float
    expiry: date
    bid: float
    ask: float
    open_interest: int
    volume: int
    delta: Optional[float] = None       # provider-supplied greek, if available - cross-check only (S1.4.3)
    implied_volatility: Optional[float] = None  # provider-supplied IV, if available - cross-check only

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class OptionPosition:
    occ_symbol: str
    underlying: str
    contract_type: str
    strike: float
    expiry: date
    qty: int                    # negative = short
    market_value: float
    delta: Optional[float] = None


@runtime_checkable
class MarketData(Protocol):
    def daily_bars(self, symbols: list[str], start: datetime, end: datetime) -> pd.DataFrame:
        """Returns a DataFrame indexed by (symbol, timestamp) with at least
        'close' and 'volume' columns - the shape alpaca-py's
        `get_stock_bars(...).df` produces. bars_to_frames() in
        position_sizing.py depends on exactly this shape; a new provider's
        adapter is responsible for reshaping its own response to match."""
        ...

    def latest_quote(self, symbol: str) -> Quote: ...


@runtime_checkable
class EquityBroker(Protocol):
    def positions(self) -> list[Position]: ...
    def account(self) -> Account: ...

    def is_tradable(self, symbol: str) -> bool:
        """Not in system-spec.md S1.1's literal protocol text, but required
        by the S2.2 validation gate ('any ticker failing an Alpaca asset
        lookup or flagged non-tradable'). Raises if the lookup itself fails
        (network/auth error) so callers can distinguish "looked up, not
        tradable" from "couldn't look up at all"."""
        ...

    def submit_notional(self, symbol: str, notional: float, side: OrderSide) -> Order: ...
    def close_position(self, symbol: str) -> Order: ...
    def order_status(self, order_id: str) -> Order: ...

    def pending_corporate_action(self, symbol: str, lookahead_days: int = 90) -> Optional[str]:
        """Best-effort check for an announced merger/spinoff in the next
        lookahead_days (system-spec.md S5.8's "pending corporate action"
        skip gate). Returns a short description if one is found, else None
        - which means "none found", not "checked and confirmed safe" if the
        provider's coverage is incomplete. Also not in the spec's literal
        S1.1 protocol text, added for the same reason as is_tradable."""
        ...


@runtime_checkable
class OptionsBroker(Protocol):
    def chain(self, symbol: str, expiry_range: tuple[date, date]) -> list[Contract]: ...
    def submit_option(self, contract: Contract, qty: int, side: OrderSide) -> Order: ...
    def option_positions(self) -> list[OptionPosition]: ...
    def buying_power_reserved(self) -> Decimal: ...

    def latest_contract_quote(self, occ_symbol: str) -> Contract:
        """Not in system-spec.md S1.1's literal protocol text. Needed by
        trade_from_csv.py's execution path: position_sizing.py's output CSV
        carries strike/expiry/contracts but not a live bid/ask, and
        submitting against a quote that's hours or a week stale (from the
        sizing run) rather than fresh is the wrong default for real order
        submission. Raises if the symbol can't be quoted (delisted,
        expired, etc.) - callers must not fall back to a stale price."""
        ...

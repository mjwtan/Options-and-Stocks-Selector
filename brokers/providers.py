"""Provider factory - system-spec.md S1.1, S10 (EQUITY_BROKER, MARKET_DATA,
OPTIONS_BROKER config).

Alpaca is the only implementation wired up so far (it's the only provider
with credentials in .env); this module exists so adding Tradier/IBKR later
is a new adapter module plus one more branch here, not a rewrite of
position_sizing.py or trade_from_csv.py.

Critical property: when options_broker == "none" (the default), this module
never imports brokers.alpaca_options - equity-only mode has no options
dependency imported or credentialed at all (system-spec.md S1.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .base import EquityBroker, MarketData, OptionsBroker


@dataclass
class Providers:
    equity: EquityBroker
    market_data: MarketData
    options: Optional[OptionsBroker]


def resolve_providers(
    api_key: str,
    secret_key: str,
    equity_broker: str = "alpaca",
    market_data: str = "alpaca",
    options_broker: str = "none",
    paper: bool = True,
) -> Providers:
    if equity_broker == "alpaca":
        from .alpaca_equity import AlpacaEquityBroker
        equity = AlpacaEquityBroker(api_key, secret_key, paper=paper)
    else:
        raise ValueError(f"unknown EQUITY_BROKER {equity_broker!r}")

    if market_data == "alpaca":
        from data.alpaca_data import AlpacaMarketData
        data = AlpacaMarketData(api_key, secret_key)
    else:
        raise ValueError(f"unknown MARKET_DATA {market_data!r}")

    options: Optional[OptionsBroker] = None
    if options_broker == "alpaca":
        from .alpaca_options import AlpacaOptionsBroker
        options = AlpacaOptionsBroker(api_key, secret_key, paper=paper)
    elif options_broker != "none":
        raise ValueError(f"unknown OPTIONS_BROKER {options_broker!r}")

    return Providers(equity=equity, market_data=data, options=options)

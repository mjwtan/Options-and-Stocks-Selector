"""Alpaca implementation of the EquityBroker protocol - system-spec.md S1.2.

Extracted from what position_sizing.py's validate()/run_pipeline() and
trade_from_csv.py's execute_actions()/get_current_values()/get_basis_value()
called on alpaca-py's TradingClient directly. Behavior-preserving: same
calls, same paper=True, just behind the protocol in brokers/base.py so
strategy code no longer imports alpaca-py itself.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, OrderStatus as _AlpacaOrderStatus
from alpaca.trading.requests import GetCorporateAnnouncementsRequest, MarketOrderRequest
from alpaca.trading.enums import CorporateActionType, OrderSide as _AlpacaOrderSide, TimeInForce
from alpaca.common.exceptions import APIError

from .base import Account, EquityBroker, FillStatus, Order, OrderSide, Position

_STATUS_MAP = {
    _AlpacaOrderStatus.NEW: FillStatus.NEW,
    _AlpacaOrderStatus.FILLED: FillStatus.FILLED,
    _AlpacaOrderStatus.PARTIALLY_FILLED: FillStatus.PARTIALLY_FILLED,
    _AlpacaOrderStatus.CANCELED: FillStatus.CANCELED,
    _AlpacaOrderStatus.EXPIRED: FillStatus.EXPIRED,
    _AlpacaOrderStatus.REJECTED: FillStatus.REJECTED,
}


def _map_status(alpaca_status) -> FillStatus:
    return _STATUS_MAP.get(alpaca_status, FillStatus.PENDING)


def _to_order(alpaca_order) -> Order:
    return Order(
        id=str(alpaca_order.id),
        symbol=alpaca_order.symbol,
        status=_map_status(alpaca_order.status),
        qty=float(alpaca_order.qty) if alpaca_order.qty is not None else None,
        notional=float(alpaca_order.notional) if alpaca_order.notional is not None else None,
    )


class AlpacaEquityBroker(EquityBroker):
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self._client = TradingClient(api_key, secret_key, paper=paper)

    def positions(self) -> list[Position]:
        # get_all_positions() returns every asset class (equity AND
        # options) in one list - a real bug, found while building
        # current_positions.py: an open option position leaked into the
        # equity list here, which feeds trade_from_csv.py's "held but not
        # in this week's target -> close_position()" logic. Left
        # unfiltered, that could submit an equity close against an option
        # symbol. AlpacaOptionsBroker.option_positions() already filters
        # the other way for the same reason.
        return [
            Position(
                symbol=p.symbol, qty=float(p.qty), market_value=float(p.market_value),
                unrealized_plpc=float(p.unrealized_plpc) if p.unrealized_plpc is not None else None,
            )
            for p in self._client.get_all_positions()
            if p.asset_class == AssetClass.US_EQUITY
        ]

    def account(self) -> Account:
        a = self._client.get_account()
        return Account(
            equity=float(a.equity),
            cash=float(a.cash),
            buying_power=float(a.buying_power),
            portfolio_value=float(a.portfolio_value),
        )

    def is_tradable(self, symbol: str) -> bool:
        asset = self._client.get_asset(symbol)  # raises on lookup failure - caller's problem to catch
        return bool(asset.tradable)

    def submit_notional(self, symbol: str, notional: float, side: OrderSide) -> Order:
        alpaca_side = _AlpacaOrderSide.BUY if side == OrderSide.BUY else _AlpacaOrderSide.SELL
        order = self._client.submit_order(
            MarketOrderRequest(symbol=symbol, notional=notional, side=alpaca_side, time_in_force=TimeInForce.DAY)
        )
        return _to_order(order)

    def close_position(self, symbol: str) -> Order:
        order = self._client.close_position(symbol)
        return _to_order(order)

    def order_status(self, order_id: str) -> Order:
        order = self._client.get_order_by_id(order_id)
        return _to_order(order)

    def pending_corporate_action(self, symbol: str, lookahead_days: int = 90) -> Optional[str]:
        req = GetCorporateAnnouncementsRequest(
            ca_types=[CorporateActionType.MERGER, CorporateActionType.SPINOFF],
            since=date.today(),
            until=date.today() + timedelta(days=lookahead_days),
            symbol=symbol,
        )
        try:
            announcements = self._client.get_corporate_announcements(req)
        except Exception:
            return None  # provider lookup failed - treated as "unknown" by callers, never as "confirmed safe"
        if not announcements:
            return None
        a = announcements[0]
        return f"{a.ca_type.value} announced, ex-date {a.ex_date}"

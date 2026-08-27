"""Alpaca implementation of the OptionsBroker protocol - system-spec.md
S1.3, S3.5. Only imported when OPTIONS_ENABLED=True (see brokers/providers.py).

chain() joins two Alpaca calls, since neither alone carries everything a
Contract needs:
  - TradingClient.get_option_contracts(): strike/expiry/type/open_interest
    (contract metadata)
  - OptionHistoricalDataClient.get_option_chain(): live bid/ask/IV/greeks
    (market data) - keyed by the same OCC symbol, so joined locally.

Provider-supplied IV/delta are carried through on Contract as a cross-check
only (system-spec.md S1.4.3) - the actual instrument decision in
options/decision.py computes its own via QuantLib (options/pricing.py).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.enums import OrderSide as _AlpacaOrderSide, TimeInForce
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, OptionLatestQuoteRequest

from .base import Contract, OptionPosition, OptionsBroker, Order, OrderSide
from .alpaca_equity import _to_order
from options.occ import parse_occ_symbol


class AlpacaOptionsBroker(OptionsBroker):
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._data = OptionHistoricalDataClient(api_key, secret_key)
        self._checked_approval = False

    def _check_options_approval(self):
        """Fail loudly and clearly at first use if the paper account isn't
        approved for options, rather than let submit_option surface a
        confusing broker-level rejection later (system-spec.md S5)."""
        if self._checked_approval:
            return
        self._checked_approval = True
        account = self._trading.get_account()
        level = getattr(account, "options_approved_level", None) or getattr(account, "options_trading_level", None)
        if level is not None and int(level) < 1:
            raise RuntimeError(
                "Alpaca account is not approved for options trading (options approval level "
                f"{level}). Enable options trading on the paper account before setting "
                "OPTIONS_ENABLED=True."
            )

    def chain(self, symbol: str, expiry_range: tuple[date, date]) -> list[Contract]:
        self._check_options_approval()
        start, end = expiry_range

        contracts_req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.ACTIVE,
            expiration_date_gte=start,
            expiration_date_lte=end,
        )
        contract_meta = {}
        page_token = None
        while True:
            if page_token:
                contracts_req.page_token = page_token
            resp = self._trading.get_option_contracts(contracts_req)
            for c in resp.option_contracts:
                contract_meta[c.symbol] = c
            page_token = resp.next_page_token
            if not page_token:
                break

        if not contract_meta:
            return []

        snapshot = self._data.get_option_chain(OptionChainRequest(underlying_symbol=symbol))

        out = []
        for occ_symbol, meta in contract_meta.items():
            snap = snapshot.get(occ_symbol)
            if snap is None or snap.latest_quote is None:
                continue
            q = snap.latest_quote
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            out.append(Contract(
                occ_symbol=occ_symbol,
                underlying=symbol,
                contract_type=meta.type.value,
                strike=float(meta.strike_price),
                expiry=meta.expiration_date,
                bid=bid,
                ask=ask,
                open_interest=int(meta.open_interest or 0),
                volume=int(snap.latest_trade.size) if snap.latest_trade and snap.latest_trade.size else 0,
                delta=snap.greeks.delta if snap.greeks else None,
                implied_volatility=snap.implied_volatility,
            ))
        return out

    def latest_contract_quote(self, occ_symbol: str) -> Contract:
        parsed = parse_occ_symbol(occ_symbol)
        req = OptionLatestQuoteRequest(symbol_or_symbols=occ_symbol)
        quotes = self._data.get_option_latest_quote(req)
        q = quotes.get(occ_symbol)
        if q is None:
            raise RuntimeError(f"no live quote available for {occ_symbol}")
        return Contract(
            occ_symbol=occ_symbol,
            underlying=parsed.underlying,
            contract_type=parsed.contract_type,
            strike=parsed.strike,
            expiry=parsed.expiry,
            bid=float(q.bid_price or 0),
            ask=float(q.ask_price or 0),
            open_interest=0,  # not needed for order submission; use chain()/fetch_filtered_chain for OI
            volume=0,
        )

    def submit_option(self, contract: Contract, qty: int, side: OrderSide) -> Order:
        self._check_options_approval()
        alpaca_side = _AlpacaOrderSide.BUY if side == OrderSide.BUY else _AlpacaOrderSide.SELL
        # Limit priced to genuinely cross the spread, not a market order.
        # This went through two real bugs found live before landing here:
        #   1. Pricing at the mid is a resting price - a SELL at mid sits
        #      *above* the bid and a BUY sits *below* the ask, so neither
        #      crosses at all. Sat unfilled 45+ seconds on a liquid,
        #      actively-traded contract.
        #   2. Pricing exactly *at* the touch (BUY at ask, SELL at bid)
        #      still isn't reliably marketable in Alpaca's paper options
        #      simulation - two orders sat as OrderStatus.ACCEPTED for over
        #      a minute each, priced exactly at the then-current bid, with
        #      the quote never moving. Only a price that genuinely crosses
        #      *through* the touch (confirmed live: ask+$0.02 filled in
        #      ~3s) reliably triggers a fill.
        # So: cross by max(1 cent, 10% of the spread) beyond the touch.
        # Still a bounded limit order, not an open-ended market order - on
        # top of the S3.5 <=15% relative-spread filter already applied
        # upstream, worst case here is spread + a small fraction more, not
        # unlimited slippage.
        spread = max(0.0, contract.ask - contract.bid)
        cross = max(0.01, round(0.10 * spread, 2))
        limit_price = round((contract.ask + cross) if side == OrderSide.BUY else (contract.bid - cross), 2)
        limit_price = max(0.01, limit_price)  # options can't be priced <= 0
        order = self._trading.submit_order(
            LimitOrderRequest(
                symbol=contract.occ_symbol,
                qty=qty,
                side=alpaca_side,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
        )
        return _to_order(order)

    def option_positions(self) -> list[OptionPosition]:
        positions = self._trading.get_all_positions()
        out = []
        for p in positions:
            if p.asset_class != AssetClass.US_OPTION:
                continue
            try:
                parsed = parse_occ_symbol(p.symbol)
            except ValueError:
                continue
            out.append(OptionPosition(
                occ_symbol=p.symbol,
                underlying=parsed.underlying,
                contract_type=parsed.contract_type,
                strike=parsed.strike,
                expiry=parsed.expiry,
                qty=int(float(p.qty)),
                market_value=float(p.market_value),
            ))
        return out

    def buying_power_reserved(self) -> Decimal:
        """Alpaca has no explicit "reserved capital" figure. Derive it from
        currently-open short puts (strike x 100 x |qty|) - the capital S5.6
        says must not be double-committed. This covers positions carried
        over from prior runs; trade_from_csv.py additionally tracks an
        in-run ledger for orders submitted within the current run (see its
        module docstring) since this call can't see orders not yet filled."""
        total = Decimal("0")
        for pos in self.option_positions():
            if pos.contract_type == "put" and pos.qty < 0:
                total += Decimal(str(pos.strike)) * 100 * abs(pos.qty)
        return total

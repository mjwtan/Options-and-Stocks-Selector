"""Broker/data adapter abstraction - system-spec.md S1.

Nothing in the sizing, risk, or regime layers may import a provider module
(alpaca-py, etc.) directly - they receive an adapter instance conforming to
one of the Protocols in base.py. This is what makes equity-only mode
genuinely provider-independent: with OPTIONS_ENABLED=False, resolve_providers
never imports brokers.alpaca_options at all.
"""

from .base import MarketData, EquityBroker, OptionsBroker
from .providers import resolve_providers

__all__ = ["MarketData", "EquityBroker", "OptionsBroker", "resolve_providers"]

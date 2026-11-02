"""Typed error hierarchy for the market package.

Every exception derived from MarketError must carry a human-readable message
that NEVER contains API keys, tokens, or any field named `*_key` / `*_secret`.
"""
from __future__ import annotations


class MarketError(Exception):
    """Base for all market-package errors."""


class ConfigError(MarketError):
    """Configuration missing, malformed, or in violation of a safety rule."""


class ValidationError(MarketError):
    """Market data failed a sanity check (OHLC consistency, dupes, gaps)."""


class DataUnavailable(MarketError):
    """The requested market data could not be obtained from any source."""


class StaleData(MarketError):
    """The most recent available candle is older than the configured threshold."""


class ProviderError(MarketError):
    """A data provider returned an error, timed out, or returned garbage."""


class RateLimitError(ProviderError):
    """The provider explicitly rejected the request as over quota."""


class StrategyError(MarketError):
    """A strategy produced an unusable or inconsistent decision."""


class RiskRejection(MarketError):
    """The risk engine refused to accept a proposed trade setup."""

    def __init__(self, reason: str, code: str = ""):
        super().__init__(reason)
        self.code = code
        self.reason = reason

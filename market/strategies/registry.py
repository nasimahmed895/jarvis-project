"""Strategy registry — map name → latest version → class instance.

Strategies register themselves at import time by calling `register()`. The
registry preserves older versions when a new one is added so historical
decisions remain reproducible.
"""
from __future__ import annotations

from .base import Strategy

_REGISTRY: dict[tuple[str, str], Strategy] = {}


def register(strategy: Strategy) -> None:
    key = (strategy.name, strategy.version)
    if key in _REGISTRY:
        return
    _REGISTRY[key] = strategy


def get(name: str, version: str | None = None) -> Strategy:
    if version is not None:
        return _REGISTRY[(name, version)]
    # Latest version by lexicographic sort of the version string. Strategies
    # that use semver should keep the string zero-padded for correct ordering.
    matches = [k for k in _REGISTRY if k[0] == name]
    if not matches:
        raise KeyError(f"no strategy named {name!r}")
    latest = sorted(matches)[-1]
    return _REGISTRY[latest]


def list_strategies() -> list[tuple[str, str]]:
    return sorted(_REGISTRY.keys())

"""Final-decision policy regression tests.

Policy (see market/pipeline.py:compute_final_decision):

    strategy=NO TRADE               → NO TRADE
    multi_tf=NO TRADE                → NO TRADE
    strategy direction != multi_tf   → NO TRADE
    strategy direction == multi_tf   → that direction
"""
from __future__ import annotations

import pytest

from market.pipeline import compute_final_decision


@pytest.mark.parametrize(
    "strategy, bias, expected",
    [
        ("BUY BIAS", "BUY BIAS", "BUY BIAS"),
        ("SHORT BIAS", "SHORT BIAS", "SHORT BIAS"),
        ("BUY BIAS", "SHORT BIAS", "NO TRADE"),
        ("SHORT BIAS", "BUY BIAS", "NO TRADE"),
        ("BUY BIAS", "NO TRADE", "NO TRADE"),
        ("SHORT BIAS", "NO TRADE", "NO TRADE"),
        ("NO TRADE", "BUY BIAS", "NO TRADE"),
        ("NO TRADE", "SHORT BIAS", "NO TRADE"),
        ("NO TRADE", "NO TRADE", "NO TRADE"),
    ],
)
def test_policy(strategy: str, bias: str, expected: str) -> None:
    final, reason = compute_final_decision(strategy, bias)
    assert final == expected
    assert isinstance(reason, str) and reason


def test_conflict_reason_mentions_both_sides() -> None:
    _, reason = compute_final_decision("BUY BIAS", "SHORT BIAS")
    assert "strategy=BUY BIAS" in reason
    assert "multi-TF=SHORT BIAS" in reason


def test_strategy_no_trade_reason() -> None:
    _, reason = compute_final_decision("NO TRADE", "BUY BIAS")
    assert "strategy" in reason.lower()

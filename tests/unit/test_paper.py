"""Phase 9 — paper trading engine tests."""
from __future__ import annotations

from pathlib import Path

from market.paper.account import OpenOrder, PaperAccount
from market.paper.store import PaperStore


def _mk_account(tmp_path: Path, allow_short: bool = False) -> PaperAccount:
    store = PaperStore(tmp_path / "p.db")
    return PaperAccount(
        store, account_id="t1", initial_equity_usd=10_000.0, allow_short=allow_short
    )


def test_open_and_close_long(tmp_path: Path) -> None:
    acct = _mk_account(tmp_path)
    r = acct.open_order(
        OpenOrder(
            symbol="BTCUSDT",
            side="LONG",
            qty=0.01,
            entry_price=20_000.0,
            stop_price=19_000.0,
            target_price=22_000.0,
            strategy_name="ma_trend_momentum",
            strategy_version="1.0.0",
        )
    )
    assert r.accepted
    assert len(acct.open_positions) == 1
    coi = r.client_order_id
    r2 = acct.close_position(coi, exit_price=22_000.0, exit_reason="manual")
    assert r2.accepted
    assert len(acct.open_positions) == 0
    assert acct.state.realised_pnl_today_usd > 0


def test_short_disabled_by_default(tmp_path: Path) -> None:
    acct = _mk_account(tmp_path)
    r = acct.open_order(
        OpenOrder(
            symbol="BTCUSDT",
            side="SHORT",
            qty=0.01,
            entry_price=20_000.0,
            stop_price=21_000.0,
            target_price=18_000.0,
            strategy_name="x",
            strategy_version="1.0.0",
        )
    )
    assert not r.accepted
    assert "short" in r.reason


def test_insufficient_cash_rejected(tmp_path: Path) -> None:
    acct = _mk_account(tmp_path)
    r = acct.open_order(
        OpenOrder(
            symbol="BTCUSDT",
            side="LONG",
            qty=100,  # 100 * 20000 = 2M, way more than 10k
            entry_price=20_000.0,
            stop_price=19_000.0,
            target_price=22_000.0,
            strategy_name="x",
            strategy_version="1.0.0",
        )
    )
    assert not r.accepted
    assert "cash" in r.reason


def test_duplicate_client_order_id_rejected(tmp_path: Path) -> None:
    acct = _mk_account(tmp_path)
    o = OpenOrder(
        symbol="BTCUSDT",
        side="LONG",
        qty=0.01,
        entry_price=20_000.0,
        stop_price=19_000.0,
        target_price=22_000.0,
        strategy_name="x",
        strategy_version="1.0.0",
        client_order_id="dup-123",
    )
    r1 = acct.open_order(o)
    assert r1.accepted
    r2 = acct.open_order(o)
    assert not r2.accepted
    assert "duplicate" in r2.reason


def test_restart_recovery(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "p.db")
    acct = PaperAccount(store, account_id="t1", initial_equity_usd=10_000.0)
    acct.open_order(
        OpenOrder(
            symbol="BTCUSDT",
            side="LONG",
            qty=0.01,
            entry_price=20_000.0,
            stop_price=19_000.0,
            target_price=22_000.0,
            strategy_name="x",
            strategy_version="1.0.0",
            client_order_id="restart-1",
        )
    )
    equity_before = acct.state.equity_usd
    store.close()

    # Simulate restart.
    store2 = PaperStore(tmp_path / "p.db")
    acct2 = PaperAccount(store2, account_id="t1", initial_equity_usd=10_000.0)
    assert len(acct2.open_positions) == 1
    assert acct2.open_positions[0].client_order_id == "restart-1"
    assert abs(acct2.state.equity_usd - equity_before) < 1e-6


def test_mark_to_market_hits_stop(tmp_path: Path) -> None:
    acct = _mk_account(tmp_path)
    r = acct.open_order(
        OpenOrder(
            symbol="BTCUSDT",
            side="LONG",
            qty=0.01,
            entry_price=20_000.0,
            stop_price=19_000.0,
            target_price=22_000.0,
            strategy_name="x",
            strategy_version="1.0.0",
        )
    )
    assert r.accepted
    acct.mark_to_market({"BTCUSDT": 18_900.0})
    assert len(acct.open_positions) == 0
    # Realised P/L should be negative.
    assert acct.state.realised_pnl_today_usd < 0


def test_mark_to_market_hits_target(tmp_path: Path) -> None:
    acct = _mk_account(tmp_path)
    r = acct.open_order(
        OpenOrder(
            symbol="BTCUSDT",
            side="LONG",
            qty=0.01,
            entry_price=20_000.0,
            stop_price=19_000.0,
            target_price=22_000.0,
            strategy_name="x",
            strategy_version="1.0.0",
        )
    )
    assert r.accepted
    acct.mark_to_market({"BTCUSDT": 22_500.0})
    assert len(acct.open_positions) == 0
    assert acct.state.realised_pnl_today_usd > 0


def test_no_live_order_module_imported() -> None:
    """Paper subpackage must NOT import any symbol that could place a real order."""
    import market.paper.account as m
    forbidden = {"requests.post", "requests.put", "live_order", "place_order"}
    src = Path(m.__file__).read_text(encoding="utf-8")
    for f in forbidden:
        assert f not in src, f"forbidden token {f!r} found in paper module"

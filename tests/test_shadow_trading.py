from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from guardian.application.shadow import ShadowTradingLab
from guardian.domain.models import Candle
from guardian.infrastructure.repository import SqliteTradingRepository


def candle(at: datetime, open_price: str, close: str) -> Candle:
    low = min(Decimal(open_price), Decimal(close))
    high = max(Decimal(open_price), Decimal(close))
    return Candle(at, Decimal(open_price), high, low, Decimal(close), Decimal("1"))


def make_lab(repository: SqliteTradingRepository) -> ShadowTradingLab:
    return ShadowTradingLab(
        repository,
        starting_quote=Decimal("1000"),
        order_quote_amount=Decimal("10"),
        fee_rate=Decimal("0.001"),
        spread_bps=Decimal("1"),
        slippage_bps=Decimal("1"),
        stop_loss_pct=Decimal("90"),
        take_profit_pct=Decimal("100"),
        trailing_stop_pct=Decimal("90"),
        cohort_min_age_hours=168,
        minimum_completed_trades=30,
        minimum_profit_factor=Decimal("1.1"),
    )


def training() -> dict[str, object]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "family_leaders": [
            {
                "strategy": "sma_crossover",
                "name": "Cruce SMA",
                "interval": "1m",
                "parameters": {"fast_period": 2, "slow_period": 3},
            }
        ],
    }


def test_shadow_forward_fills_only_on_the_next_bar_and_persists():
    repository = SqliteTradingRepository(Path("data") / f"test-shadow-{uuid4().hex}.db")
    repository.initialize()
    lab = make_lab(repository)
    assert lab.sync_from_training(training()) == 1
    model = repository.list_shadow_models()[0]
    start = datetime(2026, 1, 1, tzinfo=UTC)

    # The last closed candle creates BUY intent, but cannot fill on the same candle.
    bars = [
        candle(start + timedelta(minutes=index), str(value), str(value))
        for index, value in enumerate((3, 2, 1, 2, 3))
    ]
    lab._advance_model(model, bars)
    after_signal = repository.list_shadow_models()[0]
    assert after_signal["pending_action"] == "BUY"
    assert repository.list_shadow_trades() == []
    assert len(repository.shadow_performance()[0]["equity_curve"]) == 1

    # It fills at the next bar's open and survives a fresh repository read.
    bars.append(candle(start + timedelta(minutes=5), "3", "4"))
    lab._advance_model(after_signal, bars)
    persisted = repository.list_shadow_models()[0]
    trades = repository.list_shadow_trades()
    assert trades[0]["side"] == "BUY"
    assert Decimal(str(trades[0]["reference_price"])) == Decimal("3")
    assert Decimal(str(persisted["base_quantity"])) > 0
    assert repository.get_state("paper_quote_balance", "unchanged") == "unchanged"
    assert len(repository.shadow_performance()[0]["equity_curve"]) == 2

    # Reprocessing the same REST window is idempotent.
    lab._advance_model(persisted, bars)
    assert len(repository.list_shadow_trades()) == 1


def test_shadow_cohort_is_not_reset_by_each_training_run():
    repository = SqliteTradingRepository(Path("data") / f"test-shadow-{uuid4().hex}.db")
    repository.initialize()
    lab = make_lab(repository)
    first = training()
    assert lab.sync_from_training(first) == 1
    second = training()
    second["family_leaders"][0]["parameters"] = {"fast_period": 3, "slow_period": 5}  # type: ignore[index]

    assert lab.sync_from_training(second) == 0
    models = repository.list_shadow_models()
    assert len(models) == 1
    assert models[0]["parameters"] == {"fast_period": 2, "slow_period": 3}


def test_shadow_slots_are_independent_by_family_and_interval():
    repository = SqliteTradingRepository(Path("data") / f"test-shadow-{uuid4().hex}.db")
    repository.initialize()
    lab = make_lab(repository)
    payload = training()
    base = payload.pop("family_leaders")[0]  # type: ignore[index]
    payload["shadow_leaders"] = [
        {**base, "interval": interval} for interval in ("1m", "5m", "15m", "1h")
    ]

    assert lab.sync_from_training(payload) == 4
    models = repository.list_shadow_models()
    assert {model["interval"] for model in models} == {"1m", "5m", "15m", "1h"}


def test_new_strategy_universe_archives_legacy_models_without_deleting_history():
    repository = SqliteTradingRepository(Path("data") / f"test-shadow-{uuid4().hex}.db")
    repository.initialize()
    lab = make_lab(repository)
    assert lab.sync_from_training(training()) == 1
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "assumptions": {"strategy_universe": "curated-multifamily-v1"},
        "shadow_leaders": [
            {
                "strategy": "regime_adaptive",
                "name": "Day trading adaptativo",
                "interval": "15m",
                "parameters": {
                    "trend_fast_period": 8,
                    "trend_slow_period": 32,
                    "trigger_period": 8,
                    "rsi_period": 14,
                    "atr_period": 14,
                    "entry_rsi": 48,
                    "trend_strength_pct": 0.1,
                    "max_atr_pct": 0.8,
                },
            }
        ],
    }

    assert lab.sync_from_training(payload) == 1
    assert [model["strategy"] for model in repository.list_shadow_models()] == [
        "regime_adaptive"
    ]
    assert len(repository.list_shadow_models(active_only=False)) == 2
    assert repository.list_events(1)[0]["event_type"] == "SHADOW_UNIVERSE_UPDATED"


def test_shadow_skips_offline_gap_without_inventing_fills():
    repository = SqliteTradingRepository(Path("data") / f"test-shadow-{uuid4().hex}.db")
    repository.initialize()
    lab = make_lab(repository)
    lab.sync_from_training(training())
    model = repository.list_shadow_models()[0]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    initial = [
        candle(start + timedelta(minutes=index), str(value), str(value))
        for index, value in enumerate((3, 2, 1, 2, 3))
    ]
    lab._advance_model(model, initial)
    assert repository.list_shadow_models()[0]["pending_action"] == "BUY"

    resumed = initial + [
        candle(start + timedelta(minutes=5), "3", "4"),
        candle(start + timedelta(minutes=6), "4", "5"),
        candle(start + timedelta(minutes=7), "5", "6"),
    ]
    lab._advance_model(repository.list_shadow_models()[0], resumed)

    current = repository.list_shadow_models()[0]
    assert current["pending_action"] is None
    assert current["last_candle"] == resumed[-1].open_time.isoformat()
    assert repository.list_shadow_trades() == []
    assert repository.list_events(1)[0]["event_type"] == "SHADOW_GAP_SKIPPED"


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,minutes", [("1m", 1), ("1h", 60)])
async def test_shadow_executes_buy_and_sell_before_next_bar_closes(interval, minutes):
    path = Path("data") / f"test-shadow-{uuid4().hex}.db"
    repository = SqliteTradingRepository(path)
    repository.initialize()
    lab = make_lab(repository)
    payload = training()
    payload["family_leaders"][0]["interval"] = interval
    lab.sync_from_training(payload)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        candle(start + timedelta(minutes=index * minutes), str(value), str(value))
        for index, value in enumerate((3, 2, 1, 2, 3))
    ]

    class Market:
        async def candles(self, symbol, requested_interval, limit):
            assert requested_interval == interval
            return bars

    # This incomplete bar's extreme close must not cause a signal or a stop.
    bars.append(candle(start + timedelta(minutes=5 * minutes), "3", "0.01"))
    await lab.run_cycle(Market(), "BTCUSDT")
    trades = repository.list_shadow_trades()
    assert len(trades) == 1
    assert trades[0]["side"] == "BUY"
    assert Decimal(trades[0]["reference_price"]) == Decimal("3")
    assert Decimal(repository.list_shadow_models()[0]["quote_balance"]) == Decimal("990")
    assert repository.list_shadow_models()[0]["pending_action"] is None

    # A restart while the same candle is open must not repeat the purchase.
    repository = SqliteTradingRepository(path)
    lab = make_lab(repository)
    await lab.run_cycle(Market(), "BTCUSDT")
    assert len(repository.list_shadow_trades()) == 1
    bars[-1] = candle(bars[-1].open_time, "3", "4")
    bars.append(candle(start + timedelta(minutes=6 * minutes), "4", "1"))
    await lab.run_cycle(Market(), "BTCUSDT")
    assert len(repository.list_shadow_trades()) == 1  # SELL bar is still open.

    bars.append(candle(start + timedelta(minutes=7 * minutes), "1.2", "1.2"))
    await lab.run_cycle(Market(), "BTCUSDT")
    trades = repository.list_shadow_trades()
    assert [trade["side"] for trade in trades] == ["SELL", "BUY"]
    assert Decimal(trades[0]["reference_price"]) == Decimal("1.2")
    model = repository.list_shadow_models()[0]
    assert Decimal(model["base_quantity"]) == 0
    assert Decimal(model["quote_balance"]) < Decimal("1000")
    assert repository.shadow_performance()[0]["completed_trades"] == 1
    await make_lab(SqliteTradingRepository(path)).run_cycle(Market(), "BTCUSDT")
    assert len(repository.list_shadow_trades()) == 2
    assert repository.get_state("paper_quote_balance", "unchanged") == "unchanged"


def test_shadow_trailing_peak_only_applies_to_following_bar():
    repository = SqliteTradingRepository(Path("data") / f"test-shadow-{uuid4().hex}.db")
    repository.initialize()
    lab = make_lab(repository)
    lab.trailing_stop_rate = Decimal("0.1")
    lab.sync_from_training(training())
    model_id = str(repository.list_shadow_models()[0]["model_id"])
    state = dict(quote_balance=Decimal("990"), base_quantity=Decimal("1"),
                 entry_cash=Decimal("3"), entry_price=Decimal("3"),
                 peak_price=Decimal("3.3"), pending_action=None)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bar = Candle(start, Decimal("3.3"), Decimal("4"), Decimal("3.2"),
                 Decimal("3.5"), Decimal("1"))
    lab._apply_protection(model_id, state, bar)
    assert repository.list_shadow_trades() == []
    assert state["peak_price"] == Decimal("4")
    next_bar = candle(start + timedelta(minutes=1), "4", "3.5")
    lab._apply_protection(model_id, state, next_bar)
    trade = repository.list_shadow_trades()[0]
    assert trade["reason"] == "Trailing stop"
    assert Decimal(trade["reference_price"]) == Decimal("3.6")

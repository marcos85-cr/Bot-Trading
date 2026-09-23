from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from guardian.application.engine import TradingEngine
from guardian.domain.models import Balance
from guardian.domain.risk import RiskLimits, RiskManager
from guardian.domain.strategy import SmaCrossoverStrategy
from guardian.infrastructure.paper import PaperExchange
from guardian.infrastructure.repository import SqliteTradingRepository


def build_engine():
    repository = SqliteTradingRepository(Path("data") / f"test-{uuid4().hex}.db")
    repository.initialize()
    engine = TradingEngine(
        exchange=None,  # type: ignore[arg-type]
        repository=repository,
        strategy=SmaCrossoverStrategy(7, 25),
        risk_manager=RiskManager(RiskLimits(Decimal("10"), Decimal("25"), Decimal("2"), 6, 300)),
        symbol="BTCUSDT",
        interval="1m",
        loop_seconds=20,
        mode="paper",
        environment="public-feed",
    )
    engine._balance = Balance(Decimal("0.1"), Decimal("0"))
    repository.set_state("position_entry_price", "100")
    repository.set_state("position_peak_price", "100")
    return engine


def test_stop_loss_triggers_below_threshold():
    assert "Stop-loss" in build_engine()._protective_exit_reason(Decimal("99"))


def test_take_profit_triggers_above_threshold():
    assert "Take-profit" in build_engine()._protective_exit_reason(Decimal("102"))


def test_trailing_stop_tracks_peak_and_triggers_reversal():
    engine = build_engine()
    assert engine._protective_exit_reason(Decimal("101")) is None
    assert "Trailing" in engine._protective_exit_reason(Decimal("100.3"))


def test_empty_peak_from_closed_position_is_safe_on_next_buy():
    engine = build_engine()
    engine.repository.set_state("position_peak_price", "")

    assert engine._protective_exit_reason(Decimal("101")) is None
    assert engine.repository.get_state("position_peak_price") == "101"


def test_corrupt_decimal_state_is_repaired_without_crashing_cycle():
    engine = build_engine()
    engine.repository.set_state("position_peak_price", "not-a-number")

    assert engine._protective_exit_reason(Decimal("101")) is None
    assert engine.repository.get_state("position_peak_price") == "101"
    assert engine.repository.list_events(1)[0]["event_type"] == "STATE_VALUE_REPAIRED"


@pytest.mark.parametrize("failure", ["unavailable", "empty"])
async def test_protection_executes_even_when_candle_feed_fails(failure):
    engine = build_engine()
    repository = engine.repository
    repository.set_state("paper_base_balance", "0.1")
    repository.set_state("paper_quote_balance", "0")
    repository.set_state("managed_base_quantity", "0.1")

    class Market:
        async def price(self, symbol):
            return Decimal("98")

        async def candles(self, symbol, interval, limit):
            if failure == "unavailable":
                raise ConnectionError("Feed de velas no disponible")
            return []

    engine.exchange = PaperExchange(Market(), Decimal("0"), Decimal("0.001"), repository)
    with pytest.raises((ConnectionError, ValueError)):
        await engine.run_cycle()
    orders = repository.list_orders()
    assert len(orders) == 1
    assert orders[0]["side"] == "SELL"
    assert engine._managed_base_quantity() == 0
    assert Decimal(repository.get_state("paper_base_balance")) == 0
    assert any(event["event_type"] == "PROTECTIVE_EXIT" for event in repository.list_events())


@pytest.mark.parametrize("quote", ["0", "-1", "NaN", "Infinity"])
async def test_invalid_quote_cannot_trigger_protection_or_order(quote):
    engine = build_engine()

    class Market:
        async def price(self, symbol):
            return Decimal(quote)

    engine.exchange = Market()
    with pytest.raises(ValueError, match="Precio de mercado inválido"):
        await engine.run_cycle()
    assert engine.repository.list_orders() == []
    assert engine.repository.get_state("position_peak_price") == "100"


async def test_emergency_blocks_cycle_before_accessing_exchange():
    engine = build_engine()
    engine.repository.set_state("emergency_stop", "true")
    # exchange is None: any access would fail, proving the check precedes I/O.
    with pytest.raises(RuntimeError, match="emergencia"):
        await engine.run_cycle()
    assert engine.repository.list_orders() == []

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from guardian.application.engine import TradingEngine
from guardian.application.trainer import ParameterTrainer
from guardian.domain.models import Candle, SignalAction
from guardian.domain.risk import RiskLimits, RiskManager
from guardian.domain.strategy import SmaCrossoverStrategy
from guardian.infrastructure.paper import PaperExchange
from guardian.infrastructure.repository import SqliteTradingRepository


async def test_full_paper_buy_restart_sell_with_research_gate_and_entry_limit():
    repository = SqliteTradingRepository(Path("data") / f"test-lifecycle-{uuid4().hex}.db")
    repository.initialize()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []

    def append(value):
        price = Decimal(str(value))
        bars.append(Candle(start + timedelta(minutes=len(bars)), price, price,
                           price, price, Decimal("1")))

    for value in (100, 90, 80, 90, 100, 110):
        append(value)

    class Market:
        async def candles(self, symbol, interval, limit):
            return bars

        async def price(self, symbol):
            return bars[-1].close

    def make_engine():
        exchange = PaperExchange(Market(), Decimal("1000"), Decimal("0.001"), repository)
        return TradingEngine(
            exchange=exchange, repository=repository, strategy=SmaCrossoverStrategy(2, 3),
            risk_manager=RiskManager(RiskLimits(Decimal("10"), Decimal("25"),
                                               Decimal("2"), 1, 300)),
            symbol="BTCUSDT", interval="1m", loop_seconds=20, mode="paper",
            environment="testnet", auto_training_enabled=False,
            stop_loss_pct=Decimal("90"), take_profit_pct=Decimal("100"),
            trailing_stop_pct=Decimal("90"),
        )

    # Fixture approval, not a claim that this synthetic strategy is profitable.
    repository.save_training_result({
        "generated_at": datetime.now(UTC).isoformat(), "status": "candidate",
        "recommended_fast": 2, "recommended_slow": 3,
        "assumptions": {"simulation_version": ParameterTrainer.SIMULATION_VERSION},
        "validation": {"return_pct": 1, "excess_return_pct": 0.5,
                       "profit_factor": 1.5, "trades": 8},
    })
    engine = make_engine()
    signal = await engine.run_cycle()
    assert signal.action is SignalAction.BUY
    assert len(repository.list_orders()) == 1
    assert engine._balance.quote_free == Decimal("990")
    assert engine._balance.base_free > 0
    assert engine._managed_base_quantity() == engine._balance.base_free
    assert repository.get_state("pending_order") == ""

    # Both the paper broker and engine are recreated from persisted state.
    engine = make_engine()
    await engine.run_cycle()
    assert len(repository.list_orders()) == 1
    append(50)
    await engine.run_cycle()  # Current incomplete candle cannot create SELL yet.
    assert len(repository.list_orders()) == 1
    append(55)
    signal = await engine.run_cycle()
    assert signal.action is SignalAction.SELL
    orders = repository.list_orders()
    assert len(orders) == 2
    assert {order["side"] for order in orders} == {"BUY", "SELL"}
    assert engine._balance.base_free == 0
    assert engine._managed_base_quantity() == 0
    assert Decimal("990") < engine._balance.quote_free < Decimal("1000")
    assert repository.get_state("pending_order") == ""
    await make_engine().run_cycle()
    assert len(repository.list_orders()) == 2
    saved_balance = repository.get_state("paper_quote_balance")
    bars.append(bars[-1])
    with pytest.raises(ValueError, match="duplicadas"):
        await make_engine().run_cycle()
    assert len(repository.list_orders()) == 2
    assert repository.get_state("paper_quote_balance") == saved_balance

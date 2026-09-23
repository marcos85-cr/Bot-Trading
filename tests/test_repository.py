from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from guardian.domain.models import Candle, OrderResult, Side, Signal, SignalAction
from guardian.infrastructure.repository import SqliteTradingRepository


def order(side: Side, quantity: str, quote: str, when: datetime) -> OrderResult:
    return OrderResult(
        order_id=uuid4().hex,
        client_order_id=uuid4().hex,
        symbol="BTCUSDT",
        side=side,
        status="FILLED",
        executed_quantity=Decimal(quantity),
        cumulative_quote_quantity=Decimal(quote),
        average_price=Decimal(quote) / Decimal(quantity),
        created_at=when,
        is_simulated=True,
    )


def test_repository_calculates_realized_loss_and_position():
    path = __import__("pathlib").Path("data") / f"test-{uuid4().hex}.db"
    repository = SqliteTradingRepository(path)
    repository.initialize()
    now = datetime.now(UTC)
    repository.record_order(order(Side.BUY, "1", "100", now - timedelta(minutes=1)))
    repository.record_order(order(Side.SELL, "1", "95", now))

    snapshot = repository.risk_snapshot(now.date(), Decimal("95"))

    assert snapshot.realized_pnl_today == Decimal("-5.0")
    assert snapshot.position_quote == Decimal("0.0")


def test_repository_deduplicates_training_observations():
    path = __import__("pathlib").Path("data") / f"test-{uuid4().hex}.db"
    repository = SqliteTradingRepository(path)
    repository.initialize()
    now = datetime.now(UTC)
    candle = Candle(now, Decimal("1"), Decimal("2"), Decimal("1"), Decimal("2"), Decimal("3"))
    signal = Signal(SignalAction.HOLD, "test", Decimal("1"), Decimal("1"))

    repository.record_observation("BTCUSDT", "1m", candle, signal)
    repository.record_observation("BTCUSDT", "1m", candle, signal)

    assert repository.observation_count() == 1


def test_daily_risk_uses_costa_rica_calendar_day():
    path = __import__("pathlib").Path("data") / f"test-{uuid4().hex}.db"
    repository = SqliteTradingRepository(path)
    repository.initialize()
    # 02:00 UTC on Sep 10 is still Sep 9 at 20:00 in Costa Rica.
    created = datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    repository.record_order(order(Side.BUY, "0.1", "10", created))

    local_day = repository.risk_snapshot(
        created.date() - timedelta(days=1), Decimal("100"), "America/Costa_Rica"
    )
    following_day = repository.risk_snapshot(created.date(), Decimal("100"), "America/Costa_Rica")

    assert local_day.trades_today == 1
    assert following_day.trades_today == 0


def test_order_and_position_state_are_idempotent_as_one_commit():
    path = __import__("pathlib").Path("data") / f"test-{uuid4().hex}.db"
    repository = SqliteTradingRepository(path)
    repository.initialize()
    fill = order(Side.BUY, "0.1", "10", datetime.now(UTC))
    assert repository.record_order_with_state(
        fill,
        {"managed_base_quantity": "0.1", "position_entry_price": "100"},
    )
    assert repository.get_state("managed_base_quantity") == "0.1"
    assert not repository.record_order_with_state(
        fill,
        {"managed_base_quantity": "0.2", "position_entry_price": "999"},
    )
    assert repository.get_state("managed_base_quantity") == "0.1"
    assert repository.get_state("position_entry_price") == "100"
    assert len(repository.list_orders()) == 1

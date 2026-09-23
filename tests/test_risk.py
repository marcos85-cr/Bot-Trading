from datetime import UTC, datetime, timedelta
from decimal import Decimal

from guardian.domain.models import RiskSnapshot, Side
from guardian.domain.risk import RiskLimits, RiskManager


def manager():
    return RiskManager(RiskLimits(Decimal("10"), Decimal("25"), Decimal("2"), 3, 60))


def snapshot(**overrides):
    values = dict(
        realized_pnl_today=Decimal("0"),
        trades_today=0,
        position_quote=Decimal("0"),
        emergency_stop=False,
        last_trade_at=None,
    )
    values.update(overrides)
    return RiskSnapshot(**values)


def test_emergency_stop_blocks_order():
    allowed, reason = manager().authorize(
        Side.BUY, snapshot(emergency_stop=True), datetime.now(UTC), Decimal("100"), Decimal("0")
    )
    assert not allowed
    assert "emergencia" in reason


def test_max_position_blocks_buy():
    allowed, _ = manager().authorize(
        Side.BUY,
        snapshot(position_quote=Decimal("20")),
        datetime.now(UTC),
        Decimal("100"),
        Decimal("0"),
    )
    assert not allowed


def test_cooldown_blocks_order():
    allowed, _ = manager().authorize(
        Side.BUY,
        snapshot(last_trade_at=datetime.now(UTC) - timedelta(seconds=10)),
        datetime.now(UTC),
        Decimal("100"),
        Decimal("0"),
    )
    assert not allowed


def test_safe_buy_is_authorized():
    allowed, _ = manager().authorize(
        Side.BUY, snapshot(), datetime.now(UTC), Decimal("100"), Decimal("0")
    )
    assert allowed


def test_daily_limit_counts_entries_but_never_blocks_exit():
    limited = snapshot(trades_today=6, entries_today=3)
    allowed, reason = manager().authorize(
        Side.BUY, limited, datetime.now(UTC), Decimal("100"), Decimal("0")
    )
    assert not allowed
    assert "entradas" in reason
    allowed, _ = manager().authorize(
        Side.SELL, limited, datetime.now(UTC), Decimal("0"), Decimal("10")
    )
    assert allowed

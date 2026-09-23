from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from guardian.domain.models import RiskSnapshot, Side
from guardian.domain.risk import (
    RiskLimits,
    RiskManager,
    compute_risk_limits_from_equity,
)


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


def test_equity_scaled_limits_grow_with_account_size():
    small = compute_risk_limits_from_equity(
        Decimal("1000"), Decimal("10"), Decimal("2.0"), Decimal("5.0"), 6, 300
    )
    large = compute_risk_limits_from_equity(
        Decimal("5000"), Decimal("10"), Decimal("2.0"), Decimal("5.0"), 6, 300
    )
    assert small.max_daily_loss_quote == Decimal("20")
    assert small.max_position_quote == Decimal("50")
    assert large.max_daily_loss_quote == Decimal("100")
    assert large.max_position_quote == Decimal("250")
    # Non-risk fields pass through untouched.
    assert small.max_trades_per_day == large.max_trades_per_day == 6
    assert small.cooldown_seconds == large.cooldown_seconds == 300


def test_equity_scaled_order_amount_never_exceeds_max_position():
    # A configured order size larger than what 5% of a small account allows
    # must be clamped down, never silently oversized.
    limits = compute_risk_limits_from_equity(
        Decimal("100"), Decimal("50"), Decimal("2.0"), Decimal("5.0"), 6, 300
    )
    assert limits.max_position_quote == Decimal("5")
    assert limits.order_quote_amount == Decimal("5")


def test_equity_scaled_limits_reject_non_positive_equity():
    with pytest.raises(ValueError):
        compute_risk_limits_from_equity(
            Decimal("0"), Decimal("10"), Decimal("2.0"), Decimal("5.0"), 6, 300
        )

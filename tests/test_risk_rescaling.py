from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from guardian.application.engine import TradingEngine
from guardian.domain.models import Balance, Candle
from guardian.domain.risk import RiskLimits, RiskManager
from guardian.domain.strategy import SmaCrossoverStrategy
from guardian.infrastructure.repository import SqliteTradingRepository


def _flat_candles(n: int = 30) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    price = Decimal("100")
    return [
        Candle(start + timedelta(minutes=i), price, price, price, price, Decimal("1"))
        for i in range(n)
    ]


class _FakeAccount:
    """Minimal ExchangePort stub: only what run_cycle() touches."""

    def __init__(self, base_free: Decimal, quote_free: Decimal, candles: list[Candle]):
        self._balance = Balance(base_free, quote_free)
        self._candles = candles

    async def price(self, symbol):
        return self._candles[-1].close

    async def candles(self, symbol, interval, limit):
        return self._candles

    async def balance(self, base_asset, quote_asset):
        return self._balance


def _build_engine(*, mode: str, quote_free: Decimal) -> TradingEngine:
    repository = SqliteTradingRepository(Path("data") / f"test-rescale-{uuid4().hex}.db")
    repository.initialize()
    candles = _flat_candles()
    engine = TradingEngine(
        exchange=_FakeAccount(Decimal("0"), quote_free, candles),
        repository=repository,
        strategy=SmaCrossoverStrategy(2, 3),
        risk_manager=RiskManager(RiskLimits(Decimal("10"), Decimal("25"), Decimal("2"), 6, 300)),
        symbol="BTCUSDT",
        interval="1m",
        loop_seconds=20,
        mode=mode,
        environment="testnet",
        auto_training_enabled=False,
        max_daily_loss_pct=Decimal("2.0"),
        max_position_pct=Decimal("5.0"),
    )
    return engine


async def test_testnet_mode_rescales_limits_to_real_equity():
    engine = _build_engine(mode="testnet", quote_free=Decimal("5000"))
    await engine.run_cycle()

    assert engine.risk.limits.max_daily_loss_quote == Decimal("100")   # 2% of 5000
    assert engine.risk.limits.max_position_quote == Decimal("250")     # 5% of 5000
    events = [e["event_type"] for e in engine.repository.list_events()]
    assert "RISK_LIMITS_RESCALED" in events


async def test_paper_mode_keeps_fixed_absolute_limits_regardless_of_balance():
    # Even though the fake account reports a large quote balance, paper mode
    # must never rescale: it always trades against the fixed simulated capital
    # the RiskLimits were originally sized for.
    engine = _build_engine(mode="paper", quote_free=Decimal("5000"))
    await engine.run_cycle()

    assert engine.risk.limits.max_daily_loss_quote == Decimal("2")
    assert engine.risk.limits.max_position_quote == Decimal("25")
    events = [e["event_type"] for e in engine.repository.list_events()]
    assert "RISK_LIMITS_RESCALED" not in events


async def test_rescale_is_a_noop_without_configured_percentages():
    engine = _build_engine(mode="testnet", quote_free=Decimal("5000"))
    engine.max_daily_loss_pct = None
    engine.max_position_pct = None
    await engine.run_cycle()

    assert engine.risk.limits.max_daily_loss_quote == Decimal("2")
    assert engine.risk.limits.max_position_quote == Decimal("25")

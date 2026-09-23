from datetime import UTC, datetime, timedelta
from decimal import Decimal

from guardian.domain.models import Candle, SignalAction
from guardian.domain.strategy import (
    SmaCrossoverStrategy,
    StrategySpec,
    generate_strategy_decisions,
    strategy_from_payload,
)


def candles(values: list[str]) -> list[Candle]:
    now = datetime.now(UTC)
    return [
        Candle(
            now + timedelta(minutes=i), Decimal(v), Decimal(v), Decimal(v), Decimal(v), Decimal("1")
        )
        for i, v in enumerate(values)
    ]


def test_sma_bullish_crossover():
    strategy = SmaCrossoverStrategy(2, 3)
    signal = strategy.evaluate(candles(["5", "4", "3", "4", "5"]))
    assert signal.action is SignalAction.BUY


def test_sma_holds_with_insufficient_data():
    signal = SmaCrossoverStrategy(2, 4).evaluate(candles(["1", "2", "3"]))
    assert signal.action is SignalAction.HOLD


def test_every_research_family_can_run_as_the_promoted_strategy():
    values = [str(100 + index * 0.1) for index in range(150)]
    configurations = [
        ("breakout", {"entry_period": 20, "exit_period": 5, "trend_period": 50}),
        (
            "momentum",
            {
                "fast_period": 9,
                "slow_period": 30,
                "rsi_period": 14,
                "entry_rsi": 50,
                "exit_rsi": 70,
            },
        ),
        (
            "mean_reversion",
            {
                "period": 20,
                "deviation": 2.0,
                "rsi_period": 14,
                "entry_rsi": 30,
                "exit_rsi": 50,
            },
        ),
        (
            "trend_pullback",
            {
                "trigger_period": 10,
                "trend_period": 50,
                "rsi_period": 14,
                "entry_rsi": 45,
                "exit_rsi": 72,
            },
        ),
    ]

    for kind, parameters in configurations:
        strategy = strategy_from_payload(kind, parameters)
        signal = strategy.evaluate(candles(values))
        assert signal.primary_label
        assert signal.secondary_label


def test_regime_adaptive_is_causal_and_uses_completed_hourly_bars():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    for index in range(240):
        value = Decimal(str(100 + index * 0.05 + (index % 9 - 4) * 0.08))
        bars.append(
            Candle(
                start + timedelta(minutes=index * 15),
                value,
                value + Decimal("0.1"),
                value - Decimal("0.1"),
                value,
                Decimal("1"),
            )
        )
    spec = StrategySpec(
        "regime_adaptive",
        "Day trading adaptativo",
        {
            "trend_fast_period": 8,
            "trend_slow_period": 32,
            "trigger_period": 8,
            "rsi_period": 14,
            "atr_period": 14,
            "entry_rsi": 48,
            "trend_strength_pct": 0.1,
            "max_atr_pct": 0.8,
        },
    )
    before = generate_strategy_decisions(bars, spec)
    changed_future = list(bars)
    last = changed_future[-1]
    changed_future[-1] = Candle(
        last.open_time,
        Decimal("10000"),
        Decimal("10000"),
        Decimal("1"),
        Decimal("10000"),
        last.volume,
    )
    after = generate_strategy_decisions(changed_future, spec)

    assert before[:-1] == after[:-1]
    strategy = strategy_from_payload("regime_adaptive", spec.parameters)
    assert strategy.fast_period == 8
    assert strategy.slow_period == 32

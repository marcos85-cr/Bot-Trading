import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from guardian.application.trainer import ParameterTrainer
from guardian.domain.models import Candle


def synthetic_candles(count: int = 500) -> list[Candle]:
    start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=count)
    result = []
    for index in range(count):
        value = Decimal(str(100 + index * 0.015 + math.sin(index / 12) * 3))
        result.append(
            Candle(start + timedelta(minutes=index), value, value, value, value, Decimal("1"))
        )
    return result


def test_trainer_generates_walk_forward_recommendation():
    trainer = ParameterTrainer(Decimal("0.001"), minimum_samples=300)
    result = trainer.train(synthetic_candles(10_000))

    assert result.samples == 10_000
    assert result.train_samples == 7_000
    assert result.validation_samples == 3_000
    assert result.recommended_fast < result.recommended_slow
    assert result.candidates_tested == 102
    assert result.recommended_interval == "15m"
    assert result.validation.max_drawdown_pct >= 0
    assert result.robustness_folds == 5
    assert len(result.robustness_returns_pct) == 5
    assert 0 <= result.positive_folds <= 5
    assert result.recommended_strategy in {
        "sma_crossover",
        "breakout",
        "momentum",
        "mean_reversion",
        "trend_pullback",
        "regime_adaptive",
    }
    assert result.recommended_parameters
    assert set(result.families_tested) == {
        "sma_crossover",
        "breakout",
        "momentum",
        "mean_reversion",
        "trend_pullback",
        "regime_adaptive",
    }
    assert result.candidates_tested == (
        len(trainer.candidate_specs()) * len(trainer.timeframe_multipliers)
        - len([spec for spec in trainer.candidate_specs() if spec.kind == "regime_adaptive"])
    )
    assert len(result.shadow_leaders) == 11
    assert {(leader["strategy"], leader["interval"]) for leader in result.shadow_leaders} == {
        *((family, "15m") for family in result.families_tested),
        *((family, "1h") for family in result.families_tested if family != "regime_adaptive"),
    }
    assert len(result.family_validation) == len(result.families_tested)
    assert sum(bool(item["selected"]) for item in result.family_validation) == 1
    assert all(item["interval"] in {"15m", "1h"} for item in result.family_validation)


def test_trainer_rejects_small_dataset():
    trainer = ParameterTrainer(Decimal("0.001"), minimum_samples=300)
    try:
        trainer.train(synthetic_candles(100))
    except ValueError as error:
        assert "300" in str(error)
    else:
        raise AssertionError("Small datasets must be rejected")


def test_backtest_fills_signal_at_next_bar_open_without_lookahead():
    start = datetime.now(UTC)
    values = [(3, 3), (2, 2), (1, 1), (2, 2), (3, 3), (10, 10)]
    bars = [
        Candle(
            start + timedelta(minutes=index),
            Decimal(str(open_price)),
            Decimal(str(open_price)),
            Decimal(str(open_price)),
            Decimal(str(close)),
            Decimal("1"),
        )
        for index, (open_price, close) in enumerate(values)
    ]
    trainer = ParameterTrainer(
        Decimal("0"),
        spread_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        stop_loss_pct=Decimal("100"),
        take_profit_pct=Decimal("100"),
        trailing_stop_pct=Decimal("100"),
    )

    result = trainer.backtest(bars, 2, 3)

    assert result.trades == 1
    assert result.return_pct == 0


def test_backtest_reports_transaction_costs_and_rejection_reasons():
    trainer = ParameterTrainer(Decimal("0.001"), minimum_samples=300)
    result = trainer.train(synthetic_candles(10_000))

    assert result.assumptions["fill_timing"] == "next_bar_open"
    assert result.assumptions["simulation_version"] == trainer.SIMULATION_VERSION
    assert result.assumptions["strategy_universe"] == "curated-multifamily-v1"
    assert result.assumptions["round_trip_cost_pct"] > 0
    assert result.purge_bars >= result.recommended_slow * 15
    assert result.validation.fees >= 0
    assert result.validation.slippage_cost >= 0
    if result.status != "candidate":
        assert result.rejection_reasons


def test_adaptive_candidates_require_volatility_above_round_trip_costs():
    trainer = ParameterTrainer(
        Decimal("0.001"),
        spread_bps=Decimal("1"),
        slippage_bps=Decimal("5"),
    )
    round_trip_cost_pct = (2 * 0.001 + 0.0001 + 2 * 0.0005) * 100
    adaptive = [spec for spec in trainer.candidate_specs() if spec.kind == "regime_adaptive"]

    assert adaptive
    assert all(float(spec.parameters["min_atr_pct"]) > round_trip_cost_pct for spec in adaptive)


def test_backtest_uses_same_fixed_order_size_as_execution():
    trainer = ParameterTrainer(
        Decimal("0.001"),
        order_quote_amount=Decimal("10"),
        stop_loss_pct=Decimal("100"),
        take_profit_pct=Decimal("100"),
        trailing_stop_pct=Decimal("100"),
    )
    result = trainer.backtest(synthetic_candles(800), 3, 15)

    assert result.trades > 0
    assert result.turnover < result.trades * 25


def test_trailing_stop_does_not_assume_current_high_precedes_current_low():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    values = (3, 2, 1, 2, 3, 3, 3.3)
    bars = [
        Candle(start + timedelta(minutes=i), *([Decimal(str(value))] * 4), Decimal("1"))
        for i, value in enumerate(values)
    ]
    bars.append(Candle(
        start + timedelta(minutes=7), Decimal("3.3"), Decimal("4"),
        Decimal("3.2"), Decimal("3.5"), Decimal("1"),
    ))
    trainer = ParameterTrainer(
        Decimal("0"), spread_bps=Decimal("0"), slippage_bps=Decimal("0"),
        stop_loss_pct=Decimal("90"), take_profit_pct=Decimal("100"),
        trailing_stop_pct=Decimal("10"),
    )
    result = trainer.backtest(bars, 2, 3)
    # Entry at 3; old peak 3.3 -> stop 2.97, so low 3.2 cannot trigger it.
    # The final liquidation is at 3.5, not an invented intrabar exit at 3.3.
    assert result.trades == 1
    assert result.final_equity == 1001.6667


def test_aggregation_is_aligned_to_utc_not_rest_window_length():
    start = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    bars = [
        Candle(start + timedelta(minutes=i), *([Decimal(i + 1)] * 4), Decimal("1"))
        for i in range(15)
    ]
    result = ParameterTrainer._aggregate(bars, 5)
    assert [bar.open_time.minute for bar in result] == [5, 10]
    assert result[0].open == Decimal("4")
    assert result[0].close == Decimal("8")
    assert result[0].volume == Decimal("5")
    # Appending an incomplete current bucket cannot move historical boundaries.
    assert ParameterTrainer._aggregate(bars[:-1], 5) == result


def test_aggregation_does_not_bridge_missing_or_duplicate_minutes():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        Candle(start + timedelta(minutes=i), *([Decimal("100")] * 4), Decimal("1"))
        for i in range(10)
    ]
    incomplete = bars[:2] + bars[3:]
    assert [bar.open_time.minute for bar in ParameterTrainer._aggregate(incomplete, 5)] == [5]
    duplicated = bars[:2] + [bars[1]] + bars[2:]
    assert [bar.open_time.minute for bar in ParameterTrainer._aggregate(duplicated, 5)] == [5]

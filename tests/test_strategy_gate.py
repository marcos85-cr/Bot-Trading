import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from guardian.application.engine import TradingEngine
from guardian.application.trainer import ParameterTrainer
from guardian.domain.models import (
    Balance,
    Candle,
    OrderRequest,
    OrderResult,
    Side,
    Signal,
    SignalAction,
)
from guardian.domain.ports import OrderExecutionUnknown
from guardian.domain.risk import RiskLimits, RiskManager
from guardian.domain.strategy import SmaCrossoverStrategy, strategy_from_payload
from guardian.infrastructure.repository import SqliteTradingRepository


def build_engine(
    mode: str = "testnet",
    paper_gate: bool = True,
    paper_experiment: bool = False,
) -> TradingEngine:
    repository = SqliteTradingRepository(Path("data") / f"test-{uuid4().hex}.db")
    repository.initialize()
    return TradingEngine(
        exchange=None,  # type: ignore[arg-type]
        repository=repository,
        strategy=SmaCrossoverStrategy(7, 25),
        risk_manager=RiskManager(RiskLimits(Decimal("10"), Decimal("25"), Decimal("2"), 6, 300)),
        symbol="BTCUSDT",
        interval="1m",
        loop_seconds=20,
        mode=mode,
        environment="testnet",
        paper_research_gate_enabled=paper_gate,
        paper_experimental_execution_enabled=paper_experiment,
    )


def training(status="candidate", fast=7, slow=25, return_pct=1.2, trades=8):
    return {
        "assumptions": {"simulation_version": ParameterTrainer.SIMULATION_VERSION},
        "generated_at": "2026-09-10T00:00:00+00:00",
        "status": status,
        "recommended_fast": fast,
        "recommended_slow": slow,
        "validation": {
            "return_pct": return_pct,
            "excess_return_pct": 0.5,
            "profit_factor": 1.5,
            "trades": trades,
        },
    }


def test_authenticated_buy_requires_approved_training():
    engine = build_engine()
    assert "validación" in engine._authenticated_strategy_error()
    engine.repository.save_training_result(training(status="rejected"))
    assert "validación" in engine._authenticated_strategy_error()


def test_authenticated_buy_requires_active_parameters_to_match():
    engine = build_engine()
    engine.repository.save_training_result(training(fast=5, slow=20))
    assert "no coinciden" in engine._authenticated_strategy_error()


def test_matching_candidate_is_authorized_and_paper_uses_research_gate():
    engine = build_engine()
    engine.repository.save_training_result(training())
    assert engine._authenticated_strategy_error() is None
    assert build_engine("paper")._authenticated_strategy_error() is not None
    assert build_engine("paper", paper_gate=False)._authenticated_strategy_error() is None


def test_candidate_promotion_is_explicit_and_persistent():
    engine = build_engine()
    engine.repository.save_training_result(training(fast=5, slow=20))

    promoted = engine.promote_latest_strategy()

    assert promoted["fast_period"] == 5
    assert engine.strategy.fast_period == 5
    assert '"fast_period":5' in engine.repository.get_state("promoted_strategy")
    assert engine._authenticated_strategy_error() is None


def test_legacy_training_cannot_authorize_or_be_promoted():
    engine = build_engine()
    legacy = training()
    legacy.pop("assumptions")
    engine.repository.save_training_result(legacy)
    assert "desactualizada" in engine._authenticated_strategy_error()
    with pytest.raises(RuntimeError, match="desactualizada"):
        engine.promote_latest_strategy()
    assert engine.repository.get_state("promoted_strategy") == ""


def test_promoted_approval_keeps_ruleset_and_rejects_changed_rules_after_restart():
    engine = build_engine()
    engine.repository.save_training_result(training())
    promoted = engine.promote_latest_strategy()
    assert promoted["assumptions"]["simulation_version"] == ParameterTrainer.SIMULATION_VERSION
    promoted["assumptions"]["simulation_version"] = "old-simulator"
    engine.repository.set_state("promoted_strategy", json.dumps(promoted))
    engine.repository.save_training_result(training(status="rejected"))
    assert engine._authenticated_strategy_error() is not None


async def test_legacy_candidate_is_not_auto_deployed():
    engine = build_engine("paper")
    legacy = training(fast=5, slow=20)
    legacy.pop("assumptions")
    await engine._auto_deploy_paper_model(legacy)
    assert engine.strategy.fast_period == 7
    assert engine.repository.get_state("promoted_strategy") == ""
    assert engine.repository.list_events()[0]["event_type"] == "TRAINING_STALE"


async def test_stale_simulation_rules_trigger_training_immediately():
    engine = build_engine("paper")
    engine.trainer = ParameterTrainer(Decimal("0.001"))
    engine.auto_training_enabled = True
    engine.training_interval_hours = 24
    stale = training()
    stale["generated_at"] = datetime.now(UTC).isoformat()
    stale["assumptions"] = {"simulation_version": "previous-version"}
    engine.repository.save_training_result(stale)
    completed = False

    async def mark_training_complete():
        nonlocal completed
        completed = True

    engine._run_background_training = mark_training_complete  # type: ignore[method-assign]

    await engine._maybe_train()
    assert engine._auto_training_task is not None
    await engine._auto_training_task
    assert completed


async def test_rejected_challenger_becomes_observer_but_cannot_trade():
    engine = build_engine("paper")
    payload = {
        "assumptions": {"simulation_version": ParameterTrainer.SIMULATION_VERSION},
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "rejected",
        "recommended_strategy": "mean_reversion",
        "recommended_interval": "15m",
        "recommended_parameters": {
            "period": 20,
            "deviation": 1.5,
            "rsi_period": 14,
            "entry_rsi": 30,
            "exit_rsi": 50,
        },
        "validation": {
            "return_pct": -0.1,
            "excess_return_pct": -0.2,
            "profit_factor": 0.8,
            "trades": 12,
        },
    }
    engine.repository.save_training_result(payload)

    await engine._adopt_research_observer(payload)

    assert engine.strategy.kind == "mean_reversion"
    assert engine.interval == "15m"
    assert engine.repository.get_state("promoted_strategy") == ""
    assert engine._authenticated_strategy_error() is not None
    assert engine.repository.list_events(1)[0]["event_type"] == "RESEARCH_OBSERVER_UPDATED"


async def test_rejected_challenger_can_trade_only_in_explicit_paper_experiment():
    engine = build_engine("paper", paper_experiment=True)
    payload = {
        "assumptions": {"simulation_version": ParameterTrainer.SIMULATION_VERSION},
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "rejected",
        "recommended_strategy": "mean_reversion",
        "recommended_interval": "15m",
        "recommended_parameters": {
            "period": 20,
            "deviation": 1.5,
            "rsi_period": 14,
            "entry_rsi": 30,
            "exit_rsi": 50,
        },
        "validation": {
            "return_pct": -0.1,
            "profit_factor": 0.8,
            "trades": 12,
        },
    }
    engine.repository.save_training_result(payload)
    await engine._adopt_research_observer(payload)

    assert engine._authenticated_strategy_error() is None
    assert engine._active_validation_level(None) == "research"

    authenticated = build_engine("testnet", paper_experiment=True)
    authenticated.repository.save_training_result(payload)
    authenticated.strategy = strategy_from_payload(
        "mean_reversion", dict(payload["recommended_parameters"])
    )
    authenticated.interval = "15m"
    assert authenticated._authenticated_strategy_error() is not None


def test_approved_champion_is_not_replaced_by_a_rejected_challenger():
    engine = build_engine()
    engine.repository.save_training_result(training())
    engine.promote_latest_strategy()
    engine.repository.save_training_result(training(status="rejected", fast=5, slow=20))

    assert engine._authenticated_strategy_error() is None


def test_forward_approval_authorizes_only_paper_execution():
    engine = build_engine("paper")
    approval = {
        "strategy": "sma_crossover",
        "parameters": {"fast_period": 7, "slow_period": 25},
        "interval": "1m",
        "approved_until": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        "validation_level": "forward_approved",
        "validation": {"forward_eligible": True},
    }
    engine.repository.set_state("promoted_strategy", json.dumps(approval))
    assert engine._authenticated_strategy_error() is None

    authenticated = build_engine("testnet")
    authenticated.repository.set_state("promoted_strategy", json.dumps(approval))
    assert authenticated._authenticated_strategy_error() is not None


async def test_engine_loads_enough_history_for_multitimeframe_strategy():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    for index in range(320):
        price = Decimal(str(100 + index * 0.05 + (index % 7 - 3) * 0.02))
        bars.append(
            Candle(
                start + timedelta(minutes=index * 15),
                price,
                price + Decimal("0.1"),
                price - Decimal("0.1"),
                price,
                Decimal("1"),
            )
        )

    class Market:
        def __init__(self):
            self.requested_limits = []

        async def price(self, _symbol):
            return bars[-1].close

        async def balance(self, _base_asset, _quote_asset):
            return Balance(Decimal("0"), Decimal("1000"))

        async def candles(self, _symbol, _interval, limit):
            self.requested_limits.append(limit)
            return bars[-limit:]

    strategy = strategy_from_payload(
        "regime_adaptive",
        {
            "trend_fast_period": 8,
            "trend_slow_period": 32,
            "trigger_period": 12,
            "rsi_period": 14,
            "atr_period": 14,
            "entry_rsi": 48,
            "trend_strength_pct": 0.2,
            "max_atr_pct": 0.8,
        },
    )
    market = Market()
    engine = build_engine("paper")
    engine.exchange = market
    engine.strategy = strategy
    engine.interval = "15m"

    signal = await engine.run_cycle()

    assert market.requested_limits == [300]
    assert market.requested_limits[0] >= strategy.spec.lookback + 10
    assert signal.reason != "Datos insuficientes"


async def test_unknown_order_activates_persistent_emergency_lock():
    class UnknownExchange:
        async def place_market_order(self, request):
            raise OrderExecutionUnknown(request.client_order_id, "estado desconocido")

    engine = build_engine("paper", paper_gate=False)
    engine.exchange = UnknownExchange()
    engine._balance = Balance(Decimal("0"), Decimal("100"))

    error = await engine._try_execute(Signal(SignalAction.BUY, "test"), Decimal("100"))

    assert error == "estado desconocido"
    assert engine.status.emergency_stop is True
    assert engine.repository.get_state("emergency_stop") == "true"
    assert engine.repository.list_events(1)[0]["event_type"] == "ORDER_STATUS_UNKNOWN"


async def test_engine_boundary_cannot_overwrite_emergency_state():
    engine = build_engine("paper")

    async def failing_cycle():
        error = OrderExecutionUnknown("g-race", "estado desconocido")
        engine._activate_uncertain_order_lock(error)
        raise error

    engine.run_cycle = failing_cycle  # type: ignore[method-assign]
    await engine._run()

    assert engine.status.running is False
    assert engine.status.emergency_stop is True


async def test_pending_order_is_recovered_exactly_once_after_restart():
    recovered = OrderResult(
        "77",
        "g-recovery",
        "BTCUSDT",
        Side.BUY,
        "FILLED",
        Decimal("0.1"),
        Decimal("10"),
        Decimal("100"),
        datetime.now(UTC),
        False,
    )

    class RecoveryExchange:
        async def find_order(self, _request):
            return recovered

    engine = build_engine("testnet")
    engine.exchange = RecoveryExchange()
    engine._save_pending_order(
        OrderRequest(
            "BTCUSDT",
            Side.BUY,
            quote_amount=Decimal("10"),
            client_order_id="g-recovery",
        )
    )

    await engine._recover_pending_order()
    await engine._recover_pending_order()

    assert engine._managed_base_quantity() == Decimal("0.1")
    assert engine.repository.get_state("pending_order") == ""
    assert len(engine.repository.list_orders()) == 1


async def test_positive_experimental_model_auto_deploys_only_in_paper():
    engine = build_engine("paper")
    payload = {
        "assumptions": {"simulation_version": ParameterTrainer.SIMULATION_VERSION},
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "experimental",
        "recommended_strategy": "mean_reversion",
        "recommended_name": "Reversión a la media",
        "recommended_interval": "1h",
        "recommended_parameters": {
            "period": 20,
            "deviation": 1.5,
            "rsi_period": 14,
            "entry_rsi": 30,
            "exit_rsi": 50,
        },
        "validation": {
            "return_pct": 0.02,
            "excess_return_pct": -0.01,
            "profit_factor": 1.05,
            "trades": 10,
        },
    }

    await engine._auto_deploy_paper_model(payload)

    assert engine.strategy.kind == "mean_reversion"
    assert engine.interval == "1h"
    assert engine._authenticated_strategy_error() is None
    assert engine.status_dict()["strategy"]["validation_level"] == "experimental"

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from guardian.application.engine import TradingEngine
from guardian.domain.models import Balance, BotStatus
from guardian.domain.risk import RiskLimits, RiskManager
from guardian.domain.strategy import SmaCrossoverStrategy
from guardian.infrastructure.repository import SqliteTradingRepository


def build_engine(mode="testnet"):
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
        mode=mode,
        environment="testnet",
    )
    engine._balance = Balance(Decimal("2.5"), Decimal("100"))
    return engine


def test_authenticated_mode_never_claims_preexisting_account_assets():
    engine = build_engine()
    assert engine._managed_base_quantity() == 0


def test_managed_position_reduction_does_not_touch_external_assets():
    engine = build_engine()
    engine.repository.set_state("managed_base_quantity", "0.01")
    engine._reduce_managed_position(Decimal("0.004"))
    assert engine._managed_base_quantity() == Decimal("0.006")
    assert engine._balance.base_free == Decimal("2.5")


def test_paper_wallet_migrates_existing_simulated_position():
    engine = build_engine("paper")
    assert engine._managed_base_quantity() == Decimal("2.5")


def test_status_exposes_auditable_levels_for_managed_position():
    engine = build_engine("paper")
    engine.repository.set_states({
        "managed_base_quantity": "0.01",
        "position_entry_price": "100",
        "position_peak_price": "110",
    })
    engine._balance = Balance(Decimal("0.01"), Decimal("100"))
    engine._status = BotStatus(
        False, "paper", "testnet", "BTCUSDT", Decimal("105"),
        None, None, None, False, None,
    )

    position = engine.status_dict()["position"]

    assert Decimal(position["entry_price"]) == Decimal("100")
    assert Decimal(position["stop_price"]) == Decimal("99.2")
    assert Decimal(position["take_profit_price"]) == Decimal("101.2")
    assert Decimal(position["trailing_stop_price"]) == Decimal("109.34")
    assert Decimal(position["unrealized_pnl_quote"]) == Decimal("0.05")

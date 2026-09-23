from __future__ import annotations

import json
import logging

import uvicorn

from guardian.application.engine import TradingEngine
from guardian.application.shadow import ShadowTradingLab
from guardian.application.trainer import ParameterTrainer
from guardian.domain.risk import RiskLimits, RiskManager
from guardian.domain.strategy import SmaCrossoverStrategy, strategy_from_payload
from guardian.infrastructure.binance import BinanceSpotClient
from guardian.infrastructure.paper import PaperExchange
from guardian.infrastructure.repository import SqliteTradingRepository
from guardian.infrastructure.settings import Settings
from guardian.presentation.api import create_app


def build_app(settings: Settings | None = None):
    settings = settings or Settings()
    repository = SqliteTradingRepository(settings.database_path)
    repository.initialize()
    trainer = ParameterTrainer(
        settings.paper_fee_rate,
        spread_bps=settings.simulated_spread_bps,
        slippage_bps=settings.simulated_slippage_bps,
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        trailing_stop_pct=settings.trailing_stop_pct,
        minimum_validation_trades=settings.minimum_validation_trades,
        source_interval=settings.training_source_interval,
        order_quote_amount=settings.order_quote_amount,
    )
    fast_period = settings.fast_sma_period
    slow_period = settings.slow_sma_period
    trading_interval = settings.trading_interval
    strategy = SmaCrossoverStrategy(fast_period, slow_period)
    promoted_raw = repository.get_state("promoted_strategy")
    if promoted_raw:
        try:
            promoted = json.loads(promoted_raw)
            candidate_interval = str(promoted.get("interval", trading_interval))
            if promoted.get("strategy") and promoted.get("parameters"):
                strategy = strategy_from_payload(
                    str(promoted["strategy"]), dict(promoted["parameters"])
                )
            else:
                candidate_fast = int(promoted["fast_period"])
                candidate_slow = int(promoted["slow_period"])
                strategy = SmaCrossoverStrategy(candidate_fast, candidate_slow)
            trading_interval = candidate_interval
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            repository.record_event(
                "PROMOTED_STRATEGY_INVALID",
                "La estrategia persistida es inválida; se usó la configuración segura",
                "ERROR",
                {"error_type": type(exc).__name__},
            )
    else:
        latest = repository.latest_training_result()
        assumptions = latest.get("assumptions") if latest else None
        parameters = latest.get("recommended_parameters") if latest else None
        if (
            latest
            and isinstance(assumptions, dict)
            and assumptions.get("simulation_version") == trainer.SIMULATION_VERSION
            and isinstance(parameters, dict)
        ):
            try:
                strategy = strategy_from_payload(
                    str(latest["recommended_strategy"]), dict(parameters)
                )
                trading_interval = str(latest.get("recommended_interval", trading_interval))
            except (KeyError, TypeError, ValueError) as exc:
                repository.record_event(
                    "RESEARCH_OBSERVER_INVALID",
                    "El observador de investigación es inválido; se usó la configuración segura",
                    "ERROR",
                    {"error_type": type(exc).__name__},
                )
    key = settings.binance_api_key.get_secret_value() if settings.binance_api_key else ""
    secret = settings.binance_api_secret.get_secret_value() if settings.binance_api_secret else ""
    if settings.trading_mode == "paper":
        market = BinanceSpotClient(settings.public_rest_base_url)
        research_market = BinanceSpotClient(settings.public_rest_base_url, timeout_seconds=20)
        exchange = PaperExchange(
            market,
            settings.paper_starting_quote,
            settings.paper_fee_rate,
            repository,
            settings.simulated_spread_bps,
            settings.simulated_slippage_bps,
            research_data=research_market,
        )
    else:
        exchange = BinanceSpotClient(settings.rest_base_url, key, secret, settings.recv_window_ms)
    shadow_lab = None
    if settings.trading_mode == "paper" and settings.shadow_trading_enabled:
        shadow_lab = ShadowTradingLab(
            repository,
            starting_quote=settings.shadow_starting_quote,
            order_quote_amount=settings.order_quote_amount,
            fee_rate=settings.paper_fee_rate,
            spread_bps=settings.simulated_spread_bps,
            slippage_bps=settings.simulated_slippage_bps,
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            trailing_stop_pct=settings.trailing_stop_pct,
            cohort_min_age_hours=settings.shadow_cohort_min_age_hours,
            minimum_completed_trades=settings.shadow_min_completed_trades,
            minimum_profit_factor=settings.shadow_min_profit_factor,
        )
        shadow_lab.sync_from_training(repository.latest_training_result())
    engine = TradingEngine(
        exchange=exchange,
        repository=repository,
        strategy=strategy,
        risk_manager=RiskManager(
            RiskLimits(
                settings.order_quote_amount,
                settings.max_position_quote,
                settings.max_daily_loss_quote,
                settings.max_trades_per_day,
                settings.trade_cooldown_seconds,
            )
        ),
        symbol=settings.trading_symbol,
        interval=trading_interval,
        loop_seconds=settings.loop_interval_seconds,
        mode=settings.trading_mode,
        environment="public-feed"
        if settings.trading_mode == "paper"
        else settings.binance_environment,
        trainer=trainer,
        auto_training_enabled=settings.auto_training_enabled,
        training_interval_hours=settings.training_interval_hours,
        training_candles=settings.training_candles,
        local_timezone=settings.local_timezone,
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        trailing_stop_pct=settings.trailing_stop_pct,
        paper_research_gate_enabled=settings.paper_research_gate_enabled,
        paper_auto_deploy_experimental=settings.paper_auto_deploy_experimental,
        paper_experimental_execution_enabled=settings.paper_experimental_execution_enabled,
        shadow_lab=shadow_lab,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        max_position_pct=settings.max_position_pct,
    )
    return create_app(engine, settings, exchange.close)


app = build_app()


def run() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()

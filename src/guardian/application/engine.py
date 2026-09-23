from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from zoneinfo import ZoneInfo

from guardian.application.shadow import ShadowTradingLab
from guardian.application.trainer import ParameterTrainer
from guardian.domain.market_data import validate_candles
from guardian.domain.models import (
    Balance,
    BotStatus,
    Candle,
    OrderRequest,
    OrderResult,
    Side,
    Signal,
    SignalAction,
)
from guardian.domain.ports import ExchangePort, OrderExecutionUnknown, TradingRepository
from guardian.domain.risk import RiskManager
from guardian.domain.strategy import AdaptiveLongStrategy, strategy_from_payload

logger = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        exchange: ExchangePort,
        repository: TradingRepository,
        strategy: AdaptiveLongStrategy,
        risk_manager: RiskManager,
        symbol: str,
        interval: str,
        loop_seconds: int,
        mode: str,
        environment: str,
        trainer: ParameterTrainer | None = None,
        auto_training_enabled: bool = True,
        training_interval_hours: int = 6,
        training_candles: int = 1000,
        local_timezone: str = "America/Costa_Rica",
        stop_loss_pct: Decimal = Decimal("0.8"),
        take_profit_pct: Decimal = Decimal("1.2"),
        trailing_stop_pct: Decimal = Decimal("0.6"),
        paper_research_gate_enabled: bool = True,
        paper_auto_deploy_experimental: bool = True,
        paper_experimental_execution_enabled: bool = False,
        shadow_lab: ShadowTradingLab | None = None,
    ) -> None:
        self.exchange = exchange
        self.repository = repository
        self.strategy = strategy
        self.risk = risk_manager
        self.symbol = symbol
        self.interval = interval
        self.loop_seconds = loop_seconds
        self.mode = mode
        self.environment = environment
        self.trainer = trainer
        self.auto_training_enabled = auto_training_enabled
        self.training_interval_hours = training_interval_hours
        self.training_candles = training_candles
        self.local_timezone = ZoneInfo(local_timezone)
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.paper_research_gate_enabled = paper_research_gate_enabled
        self.paper_auto_deploy_experimental = paper_auto_deploy_experimental
        self.paper_experimental_execution_enabled = paper_experimental_execution_enabled
        self.shadow_lab = shadow_lab
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._training_lock = asyncio.Lock()
        self._auto_training_task: asyncio.Task[None] | None = None
        self._market_candles: list[Candle] = []
        self._balance: Balance | None = None
        self._status = BotStatus(
            False,
            mode,
            environment,
            symbol,
            None,
            None,
            None,
            None,
            repository.get_state("emergency_stop", "false") == "true",
            None,
        )
        self.base_asset = symbol[:-
                                 4] if symbol.endswith("USDT") else symbol[:3]
        self.quote_asset = "USDT" if symbol.endswith(
            "USDT") else symbol[len(self.base_asset):]

    @property
    def status(self) -> BotStatus:
        return self._status

    @property
    def training_running(self) -> bool:
        return self._training_lock.locked() or bool(
            self._auto_training_task and not self._auto_training_task.done()
        )

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if self.repository.get_state("emergency_stop", "false") == "true":
            raise RuntimeError(
                "Desactive la parada de emergencia antes de iniciar")
        await self.exchange.ping()
        await self.exchange.server_time_ms()
        symbol_info = await self.exchange.symbol_info(self.symbol)
        self.base_asset = str(symbol_info["baseAsset"])
        self.quote_asset = str(symbol_info["quoteAsset"])
        await self._recover_pending_order()
        self._stop.clear()
        started = datetime.now(UTC)
        self._status = BotStatus(
            True,
            self.mode,
            self.environment,
            self.symbol,
            self._status.last_price,
            self._status.last_signal,
            self._status.last_cycle_at,
            None,
            False,
            started,
        )
        self._task = asyncio.create_task(self._run(), name="trading-engine")
        self.repository.record_event(
            "ENGINE_STARTED", "Motor de trading iniciado")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
            self._task = None
        self._status = BotStatus(
            False,
            self.mode,
            self.environment,
            self.symbol,
            self._status.last_price,
            self._status.last_signal,
            self._status.last_cycle_at,
            self._status.last_error,
            self._status.emergency_stop,
            self._status.started_at,
        )
        self.repository.record_event(
            "ENGINE_STOPPED", "Motor de trading detenido")

    async def emergency_stop(self) -> None:
        self.repository.set_state("emergency_stop", "true")
        self.repository.record_event(
            "EMERGENCY_STOP", "Parada de emergencia activada", "WARNING")
        await self.stop()
        self._status = BotStatus(
            False,
            self.mode,
            self.environment,
            self.symbol,
            self._status.last_price,
            self._status.last_signal,
            self._status.last_cycle_at,
            self._status.last_error,
            True,
            self._status.started_at,
        )

    def reset_emergency_stop(self) -> None:
        self.repository.set_state("emergency_stop", "false")
        self.repository.record_event(
            "EMERGENCY_RESET", "Sistema de emergencia rearmado")
        self._status = BotStatus(
            False,
            self.mode,
            self.environment,
            self.symbol,
            self._status.last_price,
            self._status.last_signal,
            self._status.last_cycle_at,
            self._status.last_error,
            False,
            self._status.started_at,
        )

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.run_cycle()
                    await self._maybe_train()
                except Exception as exc:
                    logger.exception("Trading cycle failed")
                    self.repository.record_event(
                        "CYCLE_ERROR", str(exc), "ERROR", {
                            "error_type": type(exc).__name__}
                    )
                    emergency = self.repository.get_state(
                        "emergency_stop", "false") == "true"
                    if emergency:
                        self._stop.set()
                    self._status = BotStatus(
                        not emergency,
                        self.mode,
                        self.environment,
                        self.symbol,
                        self._status.last_price,
                        self._status.last_signal,
                        datetime.now(UTC),
                        str(exc),
                        emergency,
                        self._status.started_at,
                    )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.loop_seconds)
                except TimeoutError:
                    pass
        finally:
            self._status = BotStatus(
                False,
                self.mode,
                self.environment,
                self.symbol,
                self._status.last_price,
                self._status.last_signal,
                self._status.last_cycle_at,
                self._status.last_error,
                self._status.emergency_stop,
                self._status.started_at,
            )

    async def run_cycle(self) -> Signal:
        async with self._lock:
            if self.repository.get_state("emergency_stop", "false") == "true":
                raise RuntimeError("Parada de emergencia activa")
            price = await self.exchange.price(self.symbol)
            if not price.is_finite() or price <= 0:
                raise ValueError(
                    "Precio de mercado inválido; no se enviaron órdenes")
            self._balance = await self.exchange.balance(self.base_asset, self.quote_asset)
            protective_reason = self._protective_exit_reason(price)
            if protective_reason:
                await self._execute_exit(price, protective_reason)
                self._balance = await self.exchange.balance(self.base_asset, self.quote_asset)
            # Keep live execution aligned with forward testing. Multi-timeframe
            # strategies (for example the 15m adaptive model using a 1h EMA)
            # need substantially more history than their nominal slow period.
            # The extra margin also guarantees complete hourly buckets when the
            # returned window starts part-way through an hour.
            candle_limit = max(300, self.strategy.spec.lookback + 10)
            candles = await self.exchange.candles(
                self.symbol, self.interval, candle_limit
            )
            self._market_candles = candles
            validate_candles(candles, self.interval)
            signal = self.strategy.evaluate(candles[:-1])
            self.repository.record_observation(
                self.symbol, self.interval, candles[-2], signal)
            error: str | None = None
            if not protective_reason and signal.action is not SignalAction.HOLD:
                signal_candle = candles[-2].open_time.isoformat()
                if self.repository.get_state("last_actionable_candle") == signal_candle:
                    error = None
                else:
                    self.repository.set_state(
                        "last_actionable_candle", signal_candle)
                    error = await self._try_execute(signal, price)
            self._balance = await self.exchange.balance(self.base_asset, self.quote_asset)
            equity = self._balance.quote_free + self._balance.base_free * price
            self.repository.record_equity(equity, price)
            snapshot = self.repository.risk_snapshot(
                datetime.now(self.local_timezone).date(
                ), price, self.local_timezone.key
            )
            if not error and snapshot.entries_today >= self.risk.limits.max_trades_per_day:
                error = "Límite diario de entradas alcanzado; las salidas continúan habilitadas"
            event_candle = candles[-2].open_time.isoformat()
            if self.repository.get_state("last_logged_candle") != event_candle:
                self.repository.set_state("last_logged_candle", event_candle)
                self.repository.record_event(
                    "MARKET_DECISION",
                    f"{signal.action.value}: {signal.reason}",
                    details={"price": str(price), "candle": event_candle},
                )
            self._status = BotStatus(
                self._status.running,
                self.mode,
                self.environment,
                self.symbol,
                price,
                signal,
                datetime.now(UTC),
                error,
                self._status.emergency_stop,
                self._status.started_at,
            )
            if self.shadow_lab:
                try:
                    self.shadow_lab.sync_from_training(
                        self.repository.latest_training_result())
                    await self.shadow_lab.run_cycle(self.exchange, self.symbol)
                except Exception as exc:
                    logger.exception("Shadow forward-testing cycle failed")
                    self.repository.record_event(
                        "SHADOW_CYCLE_ERROR",
                        "La prueba forward falló; el motor principal continuó protegido",
                        "ERROR",
                        {"error_type": type(exc).__name__, "error": str(exc)},
                    )
            return signal

    async def _try_execute(self, signal: Signal, price: Decimal) -> str | None:
        balance = self._balance or await self.exchange.balance(self.base_asset, self.quote_asset)
        snapshot = self.repository.risk_snapshot(
            datetime.now(self.local_timezone).date(
            ), price, self.local_timezone.key
        )
        side = Side(signal.action.value)
        if side is Side.SELL and self._managed_base_quantity() <= 0:
            self.repository.record_event(
                "SIGNAL_IGNORED",
                "SELL observada, sin posición administrada que cerrar",
                details={"price": str(price)},
            )
            return None
        if side is Side.BUY:
            validation_error = self._authenticated_strategy_error()
            if validation_error:
                self.repository.record_event(
                    "STRATEGY_BLOCKED", validation_error, "WARNING")
                return f"Orden bloqueada: {validation_error}"
        allowed, reason = self.risk.authorize(
            side, snapshot, datetime.now(
                UTC), balance.quote_free, balance.base_free * price
        )
        if not allowed:
            self.repository.record_event(
                "ORDER_BLOCKED",
                reason,
                "WARNING",
                {"side": side.value, "price": str(price)},
            )
            return f"Orden bloqueada: {reason}"
        request = OrderRequest(
            symbol=self.symbol,
            side=side,
            quote_amount=self.risk.limits.order_quote_amount if side is Side.BUY else None,
            base_quantity=self._managed_base_quantity() if side is Side.SELL else None,
            client_order_id=f"guardian-{uuid4().hex[:20]}",
        )
        if side is Side.SELL and not request.base_quantity:
            return "Orden bloqueada: Guardian no administra una posición para vender"
        self._save_pending_order(request)
        try:
            result = await self.exchange.place_market_order(request)
        except OrderExecutionUnknown as exc:
            self._activate_uncertain_order_lock(exc)
            return str(exc)
        except Exception:
            self.repository.set_state("pending_order", "")
            raise
        error = self._apply_order_result(result)
        self.repository.set_state("pending_order", "")

        # Colocación inmediata de Stop-Loss nativo en Binance para neutralizar flash crashes
        if not error and side is Side.BUY and result.executed_quantity > 0:
            try:
                stop_price = result.average_price * \
                    (Decimal("1") - self.stop_loss_pct / Decimal("100"))
                limit_price = stop_price * Decimal("0.995")
                await self.exchange.place_protective_stop(
                    self.symbol, result.executed_quantity, stop_price, limit_price
                )
            except Exception as stop_exc:
                logger.warning(
                    "No se pudo programar stop nativo: %s", stop_exc)
        return error

    def _apply_order_result(self, result: OrderResult) -> str | None:
        if result.executed_quantity <= 0:
            if not self.repository.record_order(result):
                return None
            message = f"Orden terminada sin ejecución ({result.status})"
            self.repository.record_event(
                "ORDER_NOT_FILLED",
                message,
                "WARNING",
                {"client_order_id": result.client_order_id, "status": result.status},
            )
            return message
        if result.side is Side.BUY:
            previous_quantity = self._managed_base_quantity()
            previous_entry = self._decimal_state(
                "position_entry_price", result.average_price)
            managed = previous_quantity + result.executed_quantity
            weighted_entry = (
                previous_quantity * previous_entry +
                result.executed_quantity * result.average_price
            ) / managed
            previous_peak = self._decimal_state(
                "position_peak_price", result.average_price)
            position_state = {
                "managed_base_quantity": str(managed),
                "position_entry_price": str(weighted_entry),
                "position_peak_price": str(max(previous_peak, result.average_price)),
            }
        else:
            remaining = max(
                Decimal("0"), self._managed_base_quantity() -
                result.executed_quantity
            )
            position_state = {"managed_base_quantity": str(remaining)}
            if remaining == 0:
                position_state.update(
                    position_entry_price="", position_peak_price="")
        if not self.repository.record_order_with_state(result, position_state):
            return None
        self.repository.record_event(
            "ORDER_FILLED",
            (
                f"{result.side.value} ejecutada por "
                f"{result.cumulative_quote_quantity:.4f} {self.quote_asset}"
            ),
            details={
                "price": str(result.average_price),
                "quantity": str(result.executed_quantity),
            },
        )
        logger.info("Order recorded: %s %s %s",
                    result.symbol, result.side, result.status)
        return None

    def _save_pending_order(self, request: OrderRequest) -> None:
        payload = {
            "symbol": request.symbol,
            "side": request.side.value,
            "quote_amount": str(request.quote_amount) if request.quote_amount is not None else None,
            "base_quantity": (
                str(request.base_quantity) if request.base_quantity is not None else None
            ),
            "client_order_id": request.client_order_id,
        }
        self.repository.set_state(
            "pending_order", json.dumps(payload, separators=(",", ":")))

    async def _recover_pending_order(self) -> None:
        raw = self.repository.get_state("pending_order")
        if not raw:
            return
        payload = json.loads(raw)
        request = OrderRequest(
            symbol=str(payload["symbol"]),
            side=Side(str(payload["side"])),
            quote_amount=(
                Decimal(str(payload["quote_amount"]))
                if payload.get("quote_amount") is not None
                else None
            ),
            base_quantity=(
                Decimal(str(payload["base_quantity"]))
                if payload.get("base_quantity") is not None
                else None
            ),
            client_order_id=str(payload["client_order_id"]),
        )
        try:
            result = await self.exchange.find_order(request)
        except OrderExecutionUnknown as exc:
            self._activate_uncertain_order_lock(exc)
            raise RuntimeError(str(exc)) from exc
        if result is None:
            self.repository.record_event(
                "ORDER_RECOVERY_NOT_FOUND",
                "La intención pendiente no existe en Binance y fue descartada",
                "WARNING",
                {"client_order_id": request.client_order_id},
            )
        else:
            self._apply_order_result(result)
            self.repository.record_event(
                "ORDER_RECOVERED",
                "Orden pendiente reconciliada al iniciar",
                details={"client_order_id": request.client_order_id,
                         "status": result.status},
            )
        self.repository.set_state("pending_order", "")

    def _managed_base_quantity(self) -> Decimal:
        raw = self.repository.get_state("managed_base_quantity")
        if raw:
            return max(Decimal("0"), self._decimal_state("managed_base_quantity", Decimal("0")))
        if self.mode == "paper" and self._balance and self._balance.base_free > 0:
            inferred = self._balance.base_free
            self.repository.set_state("managed_base_quantity", str(inferred))
            return inferred
        return Decimal("0")

    def _decimal_state(self, key: str, default: Decimal) -> Decimal:
        raw = self.repository.get_state(key)
        if not raw:
            return default
        try:
            value = Decimal(raw)
            if not value.is_finite():
                raise InvalidOperation
            return value
        except InvalidOperation:
            self.repository.set_state(key, str(default))
            self.repository.record_event(
                "STATE_VALUE_REPAIRED",
                f"Se reparó el valor numérico inválido de {key}",
                "WARNING",
                {"key": key},
            )
            return default

    def _reduce_managed_position(self, executed_quantity: Decimal) -> None:
        remaining = max(
            Decimal("0"), self._managed_base_quantity() - executed_quantity)
        self.repository.set_state("managed_base_quantity", str(remaining))
        if remaining == 0:
            self.repository.set_state("position_entry_price", "")
            self.repository.set_state("position_peak_price", "")

    @staticmethod
    def _current_training_rules(result: dict[str, object]) -> bool:
        assumptions = result.get("assumptions")
        return (
            isinstance(assumptions, dict)
            and assumptions.get("simulation_version") == ParameterTrainer.SIMULATION_VERSION
        )

    def _paper_experiment_is_ready(self, result: dict[str, object] | None) -> bool:
        """Allow an unapproved challenger to trade only inside the paper sandbox."""
        if (
            self.mode != "paper"
            or not self.paper_experimental_execution_enabled
            or not result
            or not self._current_training_rules(result)
        ):
            return False
        parameters = result.get("recommended_parameters")
        validation = result.get("validation")
        if not isinstance(parameters, dict) or not isinstance(validation, dict):
            return False
        minimum_trades = self.trainer.minimum_validation_trades if self.trainer else 8
        return (
            str(result.get("recommended_strategy", "")) == self.strategy.kind
            and dict(parameters) == self.strategy.parameters
            and str(result.get("recommended_interval", self.interval)) == self.interval
            and int(validation.get("trades", 0)) >= minimum_trades
        )

    def _authenticated_strategy_error(self) -> str | None:
        if self.mode == "paper" and not self.paper_research_gate_enabled:
            return None

        promoted_raw = self.repository.get_state("promoted_strategy")
        if promoted_raw:
            try:
                promoted = json.loads(promoted_raw)
                approval = promoted.get("validation") or {}

                # ─────────────────────────────────────────────────────────────
                # CORRECCIÓN: Verificar que la aprobación no haya expirado.
                # Las estrategias promovidas tienen vigencia de 7 días.
                # Si expiró, el bot bloquea compras hasta re-entrenar y promover.
                # ─────────────────────────────────────────────────────────────
                approved_until_raw = promoted.get("approved_until")
                if approved_until_raw:
                    try:
                        approved_until = datetime.fromisoformat(
                            str(approved_until_raw))
                        if datetime.now(UTC) > approved_until:
                            return (
                                f"Aprobación expiró el {approved_until.date().isoformat()}; "
                                "re-entrene y promueva la estrategia para continuar"
                            )
                    except ValueError:
                        return "Fecha de aprobación inválida en la estrategia promovida"
                # ─────────────────────────────────────────────────────────────

                matches_active = (
                    str(promoted.get("strategy", "")) == self.strategy.kind
                    and isinstance(promoted.get("parameters"), dict)
                    and dict(promoted["parameters"]) == self.strategy.parameters
                    and str(promoted.get("interval", self.interval)) == self.interval
                )
                validation_level = str(promoted.get(
                    "validation_level", "approved"))
                if matches_active:
                    if (
                        validation_level == "forward_approved"
                        and self.mode == "paper"
                        and bool(approval.get("forward_eligible"))
                    ):
                        return None
                    if (
                        validation_level == "experimental"
                        and self._current_training_rules(promoted)
                        and self.mode == "paper"
                        and self.paper_auto_deploy_experimental
                        and float(approval.get("return_pct", 0)) > 0
                        and float(approval.get("profit_factor", 0)) >= 1.0
                        and int(approval.get("trades", 0)) >= 8
                    ):
                        return None
                    if (
                        validation_level == "approved"
                        and self._current_training_rules(promoted)
                        and float(approval.get("return_pct", 0)) > 0
                        and float(approval.get("profit_factor", 0)) >= 1.10
                        and int(approval.get("trades", 0)) >= 8
                    ):
                        return None
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        latest = self.repository.latest_training_result()
        if self._paper_experiment_is_ready(latest):
            return None
        if not latest or latest.get("status") != "candidate":
            scope = "paper" if self.mode == "paper" else "autenticado"
            return f"modo {scope} de investigación: se requiere validación fuera de muestra"
        validation = latest.get("validation") or {}
        if not self._current_training_rules(latest):
            return "validación histórica desactualizada: pulse Entrenar ahora para recalcularla"
        if (
            float(validation.get("return_pct", 0)) <= 0
            or float(validation.get("profit_factor", 0)) < 1.10
            or int(validation.get("trades", 0)) < 8
        ):
            return "la validación de la estrategia no supera los mínimos"
        recommended_kind = str(latest.get(
            "recommended_strategy", "sma_crossover"))
        recommended_parameters = latest.get("recommended_parameters")
        if not isinstance(recommended_parameters, dict):
            recommended_parameters = {
                "fast_period": int(latest.get("recommended_fast", -1)),
                "slow_period": int(latest.get("recommended_slow", -1)),
            }
        if (
            recommended_kind != self.strategy.kind
            or recommended_parameters != self.strategy.parameters
            or str(latest.get("recommended_interval", self.interval)) != self.interval
        ):
            return "los parámetros activos no coinciden con el modelo validado"
        return None

    def _activate_uncertain_order_lock(self, exc: OrderExecutionUnknown) -> None:
        self.repository.set_state("emergency_stop", "true")
        self._stop.set()
        self.repository.record_event(
            "ORDER_STATUS_UNKNOWN",
            str(exc),
            "CRITICAL",
            {"client_order_id": exc.client_order_id},
        )
        self._status = BotStatus(
            False,
            self.mode,
            self.environment,
            self.symbol,
            self._status.last_price,
            self._status.last_signal,
            datetime.now(UTC),
            str(exc),
            True,
            self._status.started_at,
        )

    def _protective_exit_reason(self, price: Decimal) -> str | None:
        if not self._balance or self._managed_base_quantity() <= 0:
            return None
        entry_raw = self.repository.get_state("position_entry_price")
        if not entry_raw:
            self.repository.set_state("position_entry_price", str(price))
            self.repository.set_state("position_peak_price", str(price))
            return None
        entry = self._decimal_state("position_entry_price", price)
        peak = max(self._decimal_state("position_peak_price", entry), price)
        self.repository.set_state("position_peak_price", str(peak))
        if price <= entry * (Decimal("1") - self.stop_loss_pct / 100):
            return f"Stop-loss {self.stop_loss_pct}%"
        if price >= entry * (Decimal("1") + self.take_profit_pct / 100):
            return f"Take-profit {self.take_profit_pct}%"
        if peak > entry and price <= peak * (Decimal("1") - self.trailing_stop_pct / 100):
            return f"Trailing stop {self.trailing_stop_pct}%"
        return None

    async def _execute_exit(self, price: Decimal, reason: str) -> None:
        managed_quantity = self._managed_base_quantity()
        if not self._balance or managed_quantity <= 0:
            return
        request = OrderRequest(
            symbol=self.symbol,
            side=Side.SELL,
            base_quantity=min(managed_quantity, self._balance.base_free),
            client_order_id=f"guardian-exit-{uuid4().hex[:15]}",
        )
        self._save_pending_order(request)
        try:
            result = await self.exchange.place_market_order(request)
        except OrderExecutionUnknown as exc:
            self._activate_uncertain_order_lock(exc)
            raise
        except Exception:
            self.repository.set_state("pending_order", "")
            raise
        self._apply_order_result(result)
        self.repository.set_state("pending_order", "")
        self.repository.record_event(
            "PROTECTIVE_EXIT",
            f"Salida protectora ejecutada: {reason}",
            "WARNING",
            {"price": str(price), "quantity": str(result.executed_quantity)},
        )

    def status_dict(self) -> dict[str, object]:
        result = asdict(self._status)
        strategy_error = self._authenticated_strategy_error()
        if self._balance and self._status.last_price:
            managed_base = self._managed_base_quantity()
            base_value = self._balance.base_free * self._status.last_price
            result["portfolio"] = {
                "base_asset": self.base_asset,
                "quote_asset": self.quote_asset,
                "base_free": self._balance.base_free,
                "quote_free": self._balance.quote_free,
                "base_value_quote": base_value,
                "equity_quote": self._balance.quote_free + base_value,
                "managed_base": managed_base,
                "managed_value_quote": managed_base * self._status.last_price,
            }
            entry_price = self._decimal_state("position_entry_price", Decimal("0"))
            peak_price = self._decimal_state("position_peak_price", Decimal("0"))
            if managed_base > 0 and entry_price > 0:
                result["position"] = {
                    "quantity": managed_base,
                    "entry_price": entry_price,
                    "stop_price": entry_price
                    * (Decimal("1") - self.stop_loss_pct / Decimal("100")),
                    "take_profit_price": entry_price
                    * (Decimal("1") + self.take_profit_pct / Decimal("100")),
                    "trailing_stop_price": (
                        peak_price
                        * (Decimal("1") - self.trailing_stop_pct / Decimal("100"))
                        if peak_price > entry_price
                        else Decimal("0")
                    ),
                    "unrealized_pnl_quote": (
                        self._status.last_price - entry_price
                    )
                    * managed_base,
                }
            else:
                result["position"] = None
        else:
            result["portfolio"] = None
            result["position"] = None
        result["strategy"] = {
            "name": self.strategy.name,
            "kind": self.strategy.kind,
            "parameters": self.strategy.parameters,
            "fast_period": self.strategy.fast_period,
            "slow_period": self.strategy.slow_period,
            "interval": self.interval,
            "cycle_seconds": self.loop_seconds,
            "order_quote_amount": self.risk.limits.order_quote_amount,
            "max_position_quote": self.risk.limits.max_position_quote,
            "cooldown_seconds": self.risk.limits.cooldown_seconds,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "trailing_stop_pct": self.trailing_stop_pct,
            "primary_label": (
                self._status.last_signal.primary_label
                if self._status.last_signal
                else "Indicador A"
            ),
            "secondary_label": (
                self._status.last_signal.secondary_label
                if self._status.last_signal
                else "Indicador B"
            ),
            "execution_ready": strategy_error is None,
            "execution_blocker": strategy_error,
            "research_gate_enabled": self.mode != "paper" or self.paper_research_gate_enabled,
            "experimental_execution_enabled": (
                self.mode == "paper" and self.paper_experimental_execution_enabled
            ),
            "validation_level": self._active_validation_level(strategy_error),
        }
        now_local = datetime.now(self.local_timezone)
        next_reset = (now_local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        result["operations"] = {
            "timezone": self.local_timezone.key,
            "next_daily_reset": next_reset.isoformat(),
            "performance": self.repository.performance_summary(),
        }
        result["training"] = {
            "enabled": self.auto_training_enabled,
            "dataset_samples": self.repository.observation_count(),
            "latest": self.repository.latest_training_result(),
            "running": self.training_running,
            "interval_hours": self.training_interval_hours,
        }
        result["shadow"] = (
            self.shadow_lab.report(self._status.last_price)
            if self.shadow_lab
            else {"enabled": False, "models": [], "trades": []}
        )
        validation_level = str(result["strategy"]["validation_level"])
        if self._status.emergency_stop:
            operating_state = "blocked"
        elif not self._status.running:
            operating_state = "stopped"
        elif strategy_error:
            operating_state = "observing"
        elif validation_level in {"experimental", "research"}:
            operating_state = "experimenting"
        else:
            operating_state = "trading"
        cycle_age = (
            max(0.0, (datetime.now(UTC) - self._status.last_cycle_at).total_seconds())
            if self._status.last_cycle_at
            else None
        )
        result["runtime"] = {
            "operating_state": operating_state,
            "cycle_age_seconds": round(cycle_age, 1) if cycle_age is not None else None,
            "cycle_healthy": cycle_age is not None
            and cycle_age <= max(60, self.loop_seconds * 4)
            and not self._status.last_error,
            "observations": self.repository.observation_count(),
            "message": {
                "blocked": "Parada de emergencia activa",
                "stopped": "Motor detenido",
                "observing": "Analiza el mercado; compras bloqueadas por validación",
                "experimenting": "Ejecuta en paper un modelo de investigación no aprobado",
                "trading": "Analiza el mercado y puede ejecutar señales aprobadas",
            }[operating_state],
        }
        return _json_safe(result)

    async def train_now(self) -> dict[str, object]:
        if not self.trainer:
            raise RuntimeError(
                "El entrenador de parámetros no está configurado")
        async with self._training_lock:
            candles = await self.exchange.historical_candles(
                self.symbol, self.trainer.source_interval, self.training_candles
            )
            result = await asyncio.to_thread(self.trainer.train, candles[:-1])
            payload = result.to_dict()
            self.repository.save_training_result(payload)
            if self.shadow_lab:
                created = self.shadow_lab.sync_from_training(payload)
                if created:
                    self.repository.record_event(
                        "SHADOW_COHORT_CREATED",
                        f"Se iniciaron {created} carteras de prueba forward",
                        details={"models": created},
                    )
            await self._auto_deploy_paper_model(payload)
            await self._adopt_research_observer(payload)
            self.repository.record_event(
                "TRAINING_COMPLETED",
                (
                    f"Entrenamiento {result.status}: "
                    f"{result.recommended_name} · {result.recommended_interval}"
                ),
                details={"validation_return_pct": result.validation.return_pct},
            )
            logger.info(
                "Training completed: strategy=%s interval=%s validation_return=%s",
                result.recommended_strategy,
                result.recommended_interval,
                result.validation.return_pct,
            )
            return payload

    async def _adopt_research_observer(self, result: dict[str, object]) -> None:
        if (
            self.mode != "paper"
            or self.repository.get_state("promoted_strategy")
            or self._managed_base_quantity() > 0
            or not self._current_training_rules(result)
        ):
            return
        parameters = result.get("recommended_parameters")
        if not isinstance(parameters, dict):
            return
        kind = str(result.get("recommended_strategy", ""))
        interval = str(result.get("recommended_interval", self.interval))
        observer = strategy_from_payload(kind, parameters)
        async with self._lock:
            self.strategy = observer
            self.interval = interval
        self.repository.record_event(
            "RESEARCH_OBSERVER_UPDATED",
            f"{observer.name} analiza como challenger; ejecución sujeta a validación",
            details={"interval": interval, "status": str(
                result.get("status", ""))},
        )

    def promote_latest_strategy(self) -> dict[str, object]:
        if self._status.running:
            raise RuntimeError(
                "Detenga el motor antes de cambiar la estrategia")
        latest = self.repository.latest_training_result()
        if not latest or latest.get("status") != "candidate":
            raise RuntimeError("Sólo se puede promover un candidato aprobado")
        if not self._current_training_rules(latest):
            raise RuntimeError(
                "Validación histórica desactualizada: ejecute Entrenar ahora")
        kind = str(latest.get("recommended_strategy", "sma_crossover"))
        parameters = latest.get("recommended_parameters")
        if not isinstance(parameters, dict):
            parameters = {
                "fast_period": int(latest["recommended_fast"]),
                "slow_period": int(latest["recommended_slow"]),
            }
        interval = str(latest.get("recommended_interval", self.interval))
        promoted_strategy = strategy_from_payload(kind, parameters)
        promoted_at = datetime.now(UTC)
        promoted = {
            "strategy": kind,
            "name": promoted_strategy.name,
            "parameters": parameters,
            "fast_period": promoted_strategy.fast_period,
            "slow_period": promoted_strategy.slow_period,
            "interval": interval,
            "source_generated_at": str(latest["generated_at"]),
            "promoted_at": promoted_at.isoformat(),
            "approved_until": (promoted_at + timedelta(days=7)).isoformat(),
            "validation": latest.get("validation", {}),
            "assumptions": latest.get("assumptions", {}),
            "validation_level": "approved",
        }
        self.strategy = promoted_strategy
        self.interval = interval
        self.repository.set_state("promoted_strategy", json.dumps(
            promoted, separators=(",", ":")))
        self.repository.record_event(
            "STRATEGY_PROMOTED",
            f"{promoted_strategy.name} promovida manualmente",
            details=promoted,
        )
        return promoted

    def promote_shadow_model(self, model_id: str) -> dict[str, object]:
        if self.mode != "paper":
            raise RuntimeError(
                "La promoción forward sólo está habilitada en paper")
        if self._status.running:
            raise RuntimeError(
                "Detenga el motor antes de cambiar la estrategia")
        if not self.shadow_lab:
            raise RuntimeError("La prueba forward no está configurada")
        report = self.shadow_lab.report(self._status.last_price)
        selected = next(
            (model for model in report["models"]
             if str(model["model_id"]) == model_id),
            None,
        )
        if not selected or not selected.get("forward_eligible"):
            raise RuntimeError(
                "El modelo aún no cumple la política forward predefinida")
        parameters = selected.get("parameters")
        if not isinstance(parameters, dict):
            raise RuntimeError("El modelo forward tiene parámetros inválidos")
        kind = str(selected["strategy"])
        interval = str(selected["interval"])
        promoted_strategy = strategy_from_payload(kind, parameters)
        promoted_at = datetime.now(UTC)
        validation = {
            "forward_eligible": True,
            "completed_trades": int(selected["completed_trades"]),
            "profit_factor": str(selected["profit_factor"]),
            "realized_pnl": str(selected["realized_pnl"]),
            "age_hours": float(selected["age_hours"]),
        }
        promoted = {
            "strategy": kind,
            "name": promoted_strategy.name,
            "parameters": parameters,
            "fast_period": promoted_strategy.fast_period,
            "slow_period": promoted_strategy.slow_period,
            "interval": interval,
            "source_generated_at": str(selected["training_generated_at"]),
            "shadow_model_id": model_id,
            "promoted_at": promoted_at.isoformat(),
            "approved_until": (promoted_at + timedelta(days=7)).isoformat(),
            "validation": validation,
            "validation_level": "forward_approved",
        }
        self.strategy = promoted_strategy
        self.interval = interval
        self.repository.set_state("promoted_strategy", json.dumps(
            promoted, separators=(",", ":")))
        self.repository.record_event(
            "SHADOW_MODEL_PROMOTED",
            f"{promoted_strategy.name} promovida tras prueba forward",
            details=promoted,
        )
        return promoted

    async def _auto_deploy_paper_model(self, result: dict[str, object]) -> None:
        if self.mode != "paper" or not self.paper_auto_deploy_experimental:
            return
        if not self._current_training_rules(result):
            self.repository.record_event(
                "TRAINING_STALE", "Entrenamiento desactualizado: no se despliega automáticamente"
            )
            return
        status = str(result.get("status", "rejected"))
        if status not in {"candidate", "experimental"}:
            return
        if self._managed_base_quantity() > 0:
            self.repository.record_event(
                "PAPER_MODEL_PENDING",
                "El nuevo modelo paper espera a que la posición actual se cierre",
                details={"status": status},
            )
            return
        parameters = result.get("recommended_parameters")
        if not isinstance(parameters, dict):
            return
        kind = str(result.get("recommended_strategy", ""))
        interval = str(result.get("recommended_interval", self.interval))
        deployed = strategy_from_payload(kind, parameters)
        now = datetime.now(UTC)
        validation_level = "approved" if status == "candidate" else "experimental"
        async with self._lock:
            self.strategy = deployed
            self.interval = interval
            promoted = {
                "strategy": kind,
                "name": deployed.name,
                "parameters": parameters,
                "fast_period": deployed.fast_period,
                "slow_period": deployed.slow_period,
                "interval": interval,
                "source_generated_at": str(result["generated_at"]),
                "promoted_at": now.isoformat(),
                "approved_until": (
                    now + timedelta(days=7 if validation_level ==
                                    "approved" else 1)
                ).isoformat(),
                "validation": result.get("validation", {}),
                "assumptions": result.get("assumptions", {}),
                "validation_level": validation_level,
            }
            self.repository.set_state(
                "promoted_strategy", json.dumps(
                    promoted, separators=(",", ":"))
            )
        self.repository.record_event(
            "PAPER_MODEL_DEPLOYED",
            f"{deployed.name} desplegada como {validation_level} en paper",
            details={"interval": interval, "parameters": parameters},
        )

    def _active_validation_level(self, strategy_error: str | None) -> str:
        if strategy_error:
            return "blocked"
        raw = self.repository.get_state("promoted_strategy")
        if raw:
            try:
                return str(json.loads(raw).get("validation_level", "approved"))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if self._paper_experiment_is_ready(self.repository.latest_training_result()):
            return "research"
        return "approved"

    async def _maybe_train(self) -> None:
        if not self.auto_training_enabled or not self.trainer or self.training_running:
            return
        latest = self.repository.latest_training_result()
        if latest and self._current_training_rules(latest):
            generated = datetime.fromisoformat(str(latest["generated_at"]))
            elapsed_hours = (datetime.now(UTC) -
                             generated).total_seconds() / 3600
            if elapsed_hours < self.training_interval_hours:
                return
        self._auto_training_task = asyncio.create_task(
            self._run_background_training(), name="guardian-parameter-training"
        )

    async def _run_background_training(self) -> None:
        try:
            await self.train_now()
        except Exception as exc:
            logger.exception("Background training failed")
            self.repository.record_event(
                "TRAINING_FAILED",
                "El entrenamiento automático falló; la estrategia activa no cambió",
                "ERROR",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )

    def market_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "candles": [
                {
                    "time": candle.open_time.isoformat(),
                    "open": str(candle.open),
                    "high": str(candle.high),
                    "low": str(candle.low),
                    "close": str(candle.close),
                    "volume": str(candle.volume),
                }
                for candle in self._market_candles
            ],
        }

    async def market_interval_dict(self, interval: str, limit: int = 100) -> dict[str, object]:
        if interval == self.interval and self._market_candles:
            return self.market_dict()
        candles = await self.exchange.candles(self.symbol, interval, min(max(limit, 25), 500))
        return {
            "symbol": self.symbol,
            "interval": interval,
            "candles": [
                {
                    "time": candle.open_time.isoformat(),
                    "open": str(candle.open),
                    "high": str(candle.high),
                    "low": str(candle.low),
                    "close": str(candle.close),
                    "volume": str(candle.volume),
                }
                for candle in candles
            ],
        }


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value

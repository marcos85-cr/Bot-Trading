from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from guardian.domain.market_data import validate_candles
from guardian.domain.models import Candle, SignalAction
from guardian.domain.ports import ExchangePort, TradingRepository
from guardian.domain.strategy import StrategySpec, generate_strategy_decisions


class ShadowTradingLab:
    """Persistent forward-only paper portfolios, isolated from the execution wallet."""

    def __init__(
        self,
        repository: TradingRepository,
        *,
        starting_quote: Decimal,
        order_quote_amount: Decimal,
        fee_rate: Decimal,
        spread_bps: Decimal,
        slippage_bps: Decimal,
        stop_loss_pct: Decimal,
        take_profit_pct: Decimal,
        trailing_stop_pct: Decimal,
        cohort_min_age_hours: int,
        minimum_completed_trades: int,
        minimum_profit_factor: Decimal,
    ) -> None:
        self.repository = repository
        self.starting_quote = starting_quote
        self.order_quote_amount = order_quote_amount
        self.fee_rate = fee_rate
        self.adverse_rate = (spread_bps / Decimal("2") + slippage_bps) / Decimal("10000")
        self.stop_loss_rate = stop_loss_pct / Decimal("100")
        self.take_profit_rate = take_profit_pct / Decimal("100")
        self.trailing_stop_rate = trailing_stop_pct / Decimal("100")
        self.cohort_min_age_hours = cohort_min_age_hours
        self.minimum_completed_trades = minimum_completed_trades
        self.minimum_profit_factor = minimum_profit_factor

    @staticmethod
    def _model_id(strategy: str, interval: str, parameters: dict[str, object]) -> str:
        canonical = json.dumps(
            {"strategy": strategy, "interval": interval, "parameters": parameters},
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def sync_from_training(self, training: dict[str, object] | None) -> int:
        if not training:
            return 0
        assumptions = training.get("assumptions")
        strategy_universe = (
            str(assumptions.get("strategy_universe", ""))
            if isinstance(assumptions, dict)
            else ""
        )
        saved_universe = self.repository.get_state("shadow_strategy_universe")
        if strategy_universe and strategy_universe != saved_universe:
            obsolete = self.repository.list_shadow_models()
            for model in obsolete:
                self.repository.deactivate_shadow_slot(
                    str(model["strategy"]), str(model["interval"])
                )
            self.repository.set_state("shadow_strategy_universe", strategy_universe)
            if obsolete:
                self.repository.record_event(
                    "SHADOW_UNIVERSE_UPDATED",
                    "Modelos sombra anteriores archivados; inicia la estrategia adaptativa",
                    details={
                        "archived_models": len(obsolete),
                        "strategy_universe": strategy_universe,
                    },
                )
        leaders = training.get("shadow_leaders") or training.get("family_leaders")
        if not isinstance(leaders, list):
            return 0
        active_by_slot = {
            (str(model["strategy"]), str(model["interval"])): model
            for model in self.repository.list_shadow_models()
        }
        created = 0
        now = datetime.now(UTC)
        for raw in leaders:
            if not isinstance(raw, dict) or not isinstance(raw.get("parameters"), dict):
                continue
            strategy = str(raw.get("strategy", ""))
            interval = str(raw.get("interval", ""))
            parameters = dict(raw["parameters"])
            if not strategy or not interval:
                continue
            model_id = self._model_id(strategy, interval, parameters)
            current = active_by_slot.get((strategy, interval))
            if current and str(current["model_id"]) == model_id:
                continue
            if current:
                created_at = datetime.fromisoformat(str(current["created_at"]))
                age_hours = (now - created_at).total_seconds() / 3600
                if age_hours < self.cohort_min_age_hours:
                    continue
                self.repository.deactivate_shadow_slot(strategy, interval)
            model = {
                "model_id": model_id,
                "training_generated_at": str(training.get("generated_at", now.isoformat())),
                "strategy": strategy,
                "name": str(raw.get("name", strategy)),
                "interval": interval,
                "parameters": parameters,
            }
            if self.repository.create_shadow_model(model, self.starting_quote):
                created += 1
        return created

    async def run_cycle(self, exchange: ExchangePort, symbol: str) -> None:
        models = self.repository.list_shadow_models()
        groups: dict[str, list[dict[str, object]]] = {}
        for model in models:
            groups.setdefault(str(model["interval"]), []).append(model)
        for interval, interval_models in groups.items():
            lookback = max(self._spec(model).lookback for model in interval_models)
            candles = await exchange.candles(symbol, interval, max(300, lookback + 10))
            if len(candles) < lookback + 3:
                continue
            validate_candles(candles, interval)
            closed = candles[:-1]
            for model in interval_models:
                self._advance_model(model, closed)
            # The next bar is already open. Do not wait for it to close to fill
            # a confirmed signal, and never use its incomplete H/L/C as a signal.
            refreshed = {
                str(model["model_id"]): model for model in self.repository.list_shadow_models()
            }
            for model in interval_models:
                current = refreshed[str(model["model_id"])]
                if current.get("last_candle") == closed[-1].open_time.astimezone(UTC).isoformat():
                    self._fill_open_bar(current, candles[-1])

    def _fill_open_bar(self, model: dict[str, object], candle: Candle) -> None:
        if not model.get("pending_action"):
            return
        if candle.open_time <= datetime.fromisoformat(str(model["last_candle"])):
            return
        state = {
            key: model[key] for key in (
                "quote_balance", "base_quantity", "entry_cash", "entry_price",
                "peak_price", "pending_action",
            )
        }
        self._execute_pending(str(model["model_id"]), state, candle)
        self.repository.update_shadow_model(str(model["model_id"]), state)

    @staticmethod
    def _spec(model: dict[str, object]) -> StrategySpec:
        parameters = model.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("Parámetros inválidos en cartera sombra")
        return StrategySpec(str(model["strategy"]), str(model["name"]), dict(parameters))

    def _advance_model(self, model: dict[str, object], candles: list[Candle]) -> None:
        spec = self._spec(model)
        decisions = generate_strategy_decisions(candles, spec)
        last_raw = model.get("last_candle")
        if not last_raw:
            index = len(candles) - 1
            decision = decisions[index]
            pending = decision.action.value if decision.action is not SignalAction.HOLD else None
            self.repository.update_shadow_model(
                str(model["model_id"]),
                {
                    "last_candle": candles[index].open_time.astimezone(UTC).isoformat(),
                    "pending_action": pending,
                },
            )
            self.repository.record_shadow_equity(
                str(model["model_id"]),
                candles[index].open_time.astimezone(UTC).isoformat(),
                self.starting_quote,
                candles[index].close,
                False,
            )
            return

        last_time = datetime.fromisoformat(str(last_raw))
        unseen = [index for index, candle in enumerate(candles) if candle.open_time > last_time]
        if not unseen:
            return
        # Never reconstruct fills or stops while the process was offline. Even if the
        # previous checkpoint remains in the REST window, two or more unseen closed
        # bars mean the real next-open execution opportunity has already passed.
        if len(unseen) > 1 or not any(candle.open_time == last_time for candle in candles):
            index = unseen[-1]
            quantity = Decimal(str(model["base_quantity"]))
            equity = Decimal(str(model["quote_balance"])) + quantity * candles[index].close
            self.repository.update_shadow_model(
                str(model["model_id"]),
                {
                    "last_candle": candles[index].open_time.astimezone(UTC).isoformat(),
                    "pending_action": None,
                },
            )
            self.repository.record_shadow_equity(
                str(model["model_id"]),
                candles[index].open_time.astimezone(UTC).isoformat(),
                equity,
                candles[index].close,
                quantity > 0,
            )
            self.repository.record_event(
                "SHADOW_GAP_SKIPPED",
                "Intervalo sin ejecución omitido; no se inventaron operaciones",
                "WARNING",
                {
                    "model_id": str(model["model_id"]),
                    "missed_closed_bars": len(unseen),
                    "resumed_at": candles[index].open_time.astimezone(UTC).isoformat(),
                },
            )
            return

        state = {
            "quote_balance": Decimal(str(model["quote_balance"])),
            "base_quantity": Decimal(str(model["base_quantity"])),
            "entry_cash": Decimal(str(model["entry_cash"])),
            "entry_price": Decimal(str(model["entry_price"])),
            "peak_price": Decimal(str(model["peak_price"])),
            "pending_action": model.get("pending_action"),
        }
        for index in unseen:
            candle = candles[index]
            self._execute_pending(str(model["model_id"]), state, candle)
            self._apply_protection(str(model["model_id"]), state, candle)
            decision = decisions[index]
            if decision.action is SignalAction.BUY and state["base_quantity"] == 0:
                state["pending_action"] = SignalAction.BUY.value
            elif decision.action is SignalAction.SELL and state["base_quantity"] > 0:
                state["pending_action"] = SignalAction.SELL.value
            state["last_candle"] = candle.open_time.astimezone(UTC).isoformat()
            equity = (
                Decimal(str(state["quote_balance"]))
                + Decimal(str(state["base_quantity"])) * candle.close
            )
            self.repository.record_shadow_equity(
                str(model["model_id"]),
                str(state["last_candle"]),
                equity,
                candle.close,
                Decimal(str(state["base_quantity"])) > 0,
            )
        self.repository.update_shadow_model(str(model["model_id"]), state)

    def _execute_pending(self, model_id: str, state: dict[str, object], candle: Candle) -> None:
        action = state.get("pending_action")
        state["pending_action"] = None
        quote = Decimal(str(state["quote_balance"]))
        quantity = Decimal(str(state["base_quantity"]))
        if action == SignalAction.BUY.value and quantity == 0 and quote >= self.order_quote_amount:
            cash = min(self.order_quote_amount, quote)
            fill = candle.open * (Decimal("1") + self.adverse_rate)
            bought = cash / fill * (Decimal("1") - self.fee_rate)
            state.update(
                quote_balance=quote - cash,
                base_quantity=bought,
                entry_cash=cash,
                entry_price=fill,
                peak_price=fill,
            )
            self._record_fill(
                model_id,
                "BUY",
                bought,
                cash,
                candle.open,
                fill,
                Decimal("0"),
                "Señal: entrada en apertura siguiente",
                candle,
                state,
            )
        elif action == SignalAction.SELL.value and quantity > 0:
            self._close(model_id, state, candle.open, "Señal: salida en apertura siguiente", candle)

    def _apply_protection(self, model_id: str, state: dict[str, object], candle: Candle) -> None:
        quantity = Decimal(str(state["base_quantity"]))
        if quantity <= 0:
            return
        entry = Decimal(str(state["entry_price"]))
        peak = Decimal(str(state["peak_price"]))
        stop = entry * (Decimal("1") - self.stop_loss_rate)
        take = entry * (Decimal("1") + self.take_profit_rate)
        trailing_reference = max(peak, candle.high)
        # OHLC cannot tell whether this bar's high happened before its low.
        # Only a peak known before the bar can set its executable trailing stop.
        trailing = peak * (Decimal("1") - self.trailing_stop_rate)
        if candle.low <= stop:
            self._close(model_id, state, min(candle.open, stop), "Stop-loss", candle)
        elif peak > entry and candle.low <= trailing:
            self._close(model_id, state, min(candle.open, trailing), "Trailing stop", candle)
        elif candle.high >= take:
            self._close(model_id, state, max(candle.open, take), "Take-profit", candle)
        else:
            state["peak_price"] = trailing_reference

    def _close(
        self,
        model_id: str,
        state: dict[str, object],
        reference: Decimal,
        reason: str,
        candle: Candle,
    ) -> None:
        quantity = Decimal(str(state["base_quantity"]))
        fill = reference * (Decimal("1") - self.adverse_rate)
        gross = quantity * fill
        fee = gross * self.fee_rate
        proceeds = gross - fee
        realized = proceeds - Decimal(str(state["entry_cash"]))
        state.update(
            quote_balance=Decimal(str(state["quote_balance"])) + proceeds,
            base_quantity=Decimal("0"),
            entry_cash=Decimal("0"),
            entry_price=Decimal("0"),
            peak_price=Decimal("0"),
            pending_action=None,
        )
        self._record_fill(
            model_id, "SELL", quantity, proceeds, reference, fill, realized, reason, candle, state
        )

    def _record_fill(
        self,
        model_id: str,
        side: str,
        quantity: Decimal,
        quote_quantity: Decimal,
        reference: Decimal,
        fill: Decimal,
        realized_pnl: Decimal,
        reason: str,
        candle: Candle,
        state: dict[str, object],
    ) -> None:
        fee = quote_quantity * self.fee_rate if side == "BUY" else quantity * fill * self.fee_rate
        self.repository.record_shadow_trade(
            {
                "model_id": model_id,
                "side": side,
                "quantity": quantity,
                "quote_quantity": quote_quantity,
                "reference_price": reference,
                "fill_price": fill,
                "fee_quote": fee,
                "slippage_quote": quantity * abs(fill - reference),
                "realized_pnl": realized_pnl,
                "reason": reason,
                "candle_time": candle.open_time.astimezone(UTC).isoformat(),
            },
            state,
        )

    def report(self, current_price: Decimal | None = None) -> dict[str, object]:
        models = self.repository.shadow_performance()
        for model in models:
            quote = Decimal(str(model["quote_balance"]))
            base = Decimal(str(model["base_quantity"]))
            equity = quote + base * (current_price or Decimal(str(model["entry_price"])))
            completed = int(model["completed_trades"])
            profit_factor = Decimal(str(model["profit_factor"]))
            model["equity"] = str(equity)
            model["return_pct"] = str((equity / self.starting_quote - 1) * 100)
            model["unrealized_pnl"] = str(equity - quote - Decimal(str(model["entry_cash"])))
            model["forward_eligible"] = (
                bool(model["active"])
                and float(model["age_hours"]) >= self.cohort_min_age_hours
                and completed >= self.minimum_completed_trades
                and Decimal(str(model["realized_pnl"])) > 0
                and profit_factor >= self.minimum_profit_factor
            )
            model["progress"] = {
                "age_pct": min(100.0, float(model["age_hours"]) / self.cohort_min_age_hours * 100),
                "trades_pct": min(100.0, completed / self.minimum_completed_trades * 100),
            }
        return {
            "enabled": True,
            "method": "forward_only_next_bar",
            "models": models,
            "trades": self.repository.list_shadow_trades(100),
            "policy": {
                "starting_quote": str(self.starting_quote),
                "minimum_age_hours": self.cohort_min_age_hours,
                "minimum_completed_trades": self.minimum_completed_trades,
                "minimum_profit_factor": str(self.minimum_profit_factor),
                "requires_positive_realized_pnl": True,
            },
        }

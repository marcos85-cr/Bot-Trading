from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from statistics import mean, median, pstdev

from guardian.domain.market_data import validate_candles
from guardian.domain.models import Candle, SignalAction
from guardian.domain.strategy import StrategySpec, generate_strategy_decisions


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    return_pct: float
    max_drawdown_pct: float
    trades: int
    win_rate_pct: float
    final_equity: float
    gross_pnl: float = 0.0
    fees: float = 0.0
    slippage_cost: float = 0.0
    net_pnl: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    exposure_pct: float = 0.0
    turnover: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    buy_hold_return_pct: float = 0.0
    excess_return_pct: float = 0.0
    return_on_deployed_pct: float = 0.0
    regime_returns_pct: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TrainingResult:
    generated_at: str
    samples: int
    train_samples: int
    validation_samples: int
    candidates_tested: int
    recommended_fast: int
    recommended_slow: int
    training: BacktestMetrics
    validation: BacktestMetrics
    score: float
    status: str
    robustness_folds: int
    positive_folds: int
    robustness_returns_pct: list[float]
    validation_buy_hold_return_pct: float
    recommended_interval: str = "15m"
    purge_bars: int = 0
    selection_adjusted_confidence_pct: float = 0.0
    rejection_reasons: list[str] = field(default_factory=list)
    assumptions: dict[str, float | str] = field(default_factory=dict)
    recommended_strategy: str = "regime_adaptive"
    recommended_name: str = "Day trading adaptativo"
    recommended_parameters: dict[str, float |
                                 int] = field(default_factory=dict)
    families_tested: list[str] = field(default_factory=list)
    family_leaders: list[dict[str, object]] = field(default_factory=list)
    family_validation: list[dict[str, object]] = field(default_factory=list)
    shadow_leaders: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _CompletedTrade:
    pnl: float
    entry_cash: float
    regime: str


class ParameterTrainer:
    """Purged walk-forward research for a causal, long-only Spot strategy."""

    INITIAL_EQUITY = 1000.0
    SIMULATION_VERSION = "cost-aware-multitimeframe-v9"

    def __init__(
        self,
        fee_rate: Decimal,
        minimum_samples: int = 300,
        *,
        spread_bps: Decimal = Decimal("1"),
        slippage_bps: Decimal = Decimal("1"),
        stop_loss_pct: Decimal = Decimal("0.8"),
        take_profit_pct: Decimal = Decimal("1.2"),
        trailing_stop_pct: Decimal = Decimal("0.6"),
        minimum_validation_trades: int = 8,
        source_interval: str = "1m",
        order_quote_amount: Decimal = Decimal("10"),
    ) -> None:
        self.fee_rate = float(fee_rate)
        self.minimum_samples = minimum_samples
        self.spread_rate = float(spread_bps) / 10_000
        self.slippage_rate = float(slippage_bps) / 10_000
        self.stop_loss = float(stop_loss_pct) / 100
        self.take_profit = float(take_profit_pct) / 100
        self.trailing_stop = float(trailing_stop_pct) / 100
        self.minimum_validation_trades = minimum_validation_trades
        self.order_quote_amount = float(order_quote_amount)
        self.source_interval = source_interval
        self.timeframe_multipliers = self._research_multipliers(
            source_interval)

    def set_source_interval(self, interval: str) -> None:
        self.source_interval = interval
        self.timeframe_multipliers = self._research_multipliers(interval)

    @staticmethod
    def _research_multipliers(interval: str) -> tuple[int, ...]:
        """Compare intraday and hourly clocks without using incomplete buckets."""
        source_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(interval)
        if source_minutes is None or 15 % source_minutes:
            raise ValueError(
                "El entrenamiento adaptativo requiere velas fuente de 1m, 3m, 5m o 15m"
            )
        return (15 // source_minutes, 60 // source_minutes)

    @staticmethod
    def _aggregate(candles: list[Candle], size: int) -> list[Candle]:
        if size == 1:
            return candles
        aggregated: list[Candle] = []
        groups: dict[int, list[Candle]] = {}
        for candle in candles:
            bucket = int(candle.open_time.timestamp()) // (size * 60)
            groups.setdefault(bucket, []).append(candle)
        for bucket, group in groups.items():
            expected_start = bucket * size * 60
            if len(group) != size or any(
                item.open_time.timestamp() != expected_start + index * 60
                for index, item in enumerate(group)
            ):
                continue
            aggregated.append(
                Candle(
                    open_time=group[0].open_time,
                    open=group[0].open,
                    high=max(item.high for item in group),
                    low=min(item.low for item in group),
                    close=group[-1].close,
                    volume=sum((item.volume for item in group), Decimal("0")),
                )
            )
        return aggregated

    def _interval_label(self, multiplier: int) -> str:
        if self.source_interval.endswith("m"):
            minutes = int(self.source_interval[:-1]) * multiplier
            return "1h" if minutes == 60 else f"{minutes}m"
        return self.source_interval

    def candidate_specs(self) -> list[StrategySpec]:
        round_trip_cost_pct = (
            2 * self.fee_rate + self.spread_rate + 2 * self.slippage_rate
        ) * 100
        minimum_atr_levels = (
            round(max(0.25, round_trip_cost_pct * 1.25), 4),
            round(max(0.40, round_trip_cost_pct * 1.75), 4),
        )
        specs = [
            StrategySpec(
                "regime_adaptive",
                "Day trading adaptativo",
                {
                    "trend_fast_period": trend_fast,
                    "trend_slow_period": trend_slow,
                    "trigger_period": trigger,
                    "rsi_period": 14,
                    "atr_period": 14,
                    "entry_rsi": 48,
                    "trend_strength_pct": trend_strength,
                    "min_atr_pct": min_atr_pct,
                    "max_atr_pct": 0.8,
                },
            )
            for trend_fast in (8, 12)
            for trend_slow in (32, 48)
            for trigger in (8, 12)
            for trend_strength in (0.10, 0.20)
            for min_atr_pct in minimum_atr_levels
        ]
        specs.extend(
            StrategySpec(
                "sma_crossover", "Cruce SMA", {
                    "fast_period": fast, "slow_period": slow}
            )
            for fast, slow in ((8, 32), (12, 48), (20, 80))
        )
        specs.extend(
            StrategySpec(
                "breakout",
                "Ruptura de canal",
                {
                    "entry_period": entry,
                    "exit_period": exit_period,
                    "trend_period": trend,
                },
            )
            for entry in (20, 40)
            for exit_period in (10, 20)
            for trend in (50, 100)
        )
        specs.extend(
            StrategySpec(
                "momentum",
                "Momentum EMA + RSI",
                {
                    "fast_period": fast,
                    "slow_period": slow,
                    "rsi_period": 14,
                    "entry_rsi": entry_rsi,
                    "exit_rsi": 70,
                },
            )
            for fast in (8, 12)
            for slow in (32, 48)
            for entry_rsi in (50, 55)
        )
        specs.extend(
            StrategySpec(
                "mean_reversion",
                "Reversión a la media",
                {
                    "period": period,
                    "deviation": deviation,
                    "rsi_period": 14,
                    "entry_rsi": entry_rsi,
                    "exit_rsi": 50,
                },
            )
            for period in (20, 40)
            for deviation in (1.5, 2.0)
            for entry_rsi in (25, 30)
        )
        specs.extend(
            StrategySpec(
                "trend_pullback",
                "Retroceso en tendencia",
                {
                    "trigger_period": trigger,
                    "trend_period": trend,
                    "rsi_period": 14,
                    "entry_rsi": entry_rsi,
                    "exit_rsi": 72,
                },
            )
            for trigger in (8, 12)
            for trend in (50, 100)
            for entry_rsi in (45, 50)
        )
        return specs

    def _fill_price(self, reference: float, buy: bool) -> tuple[float, float]:
        adverse_rate = self.spread_rate / 2 + self.slippage_rate
        price = reference * (1 + adverse_rate if buy else 1 - adverse_rate)
        return price, abs(price - reference)

    @staticmethod
    def _risk_adjusted_returns(returns: list[float]) -> tuple[float, float]:
        if len(returns) < 2:
            return 0.0, 0.0
        deviation = pstdev(returns)
        downside = [min(item, 0.0) for item in returns]
        downside_deviation = math.sqrt(mean(item * item for item in downside))
        return (
            mean(returns) / deviation if deviation else 0.0,
            mean(returns) / downside_deviation if downside_deviation else 0.0,
        )

    def backtest(
        self,
        candles: list[Candle] | list[float],
        fast: int,
        slow: int,
        *,
        start_index: int = 0,
    ) -> BacktestMetrics:
        spec = StrategySpec(
            "sma_crossover", "Cruce SMA", {
                "fast_period": fast, "slow_period": slow}
        )
        return self.backtest_spec(candles, spec, start_index=start_index)

    def backtest_spec(
        self,
        candles: list[Candle] | list[float],
        spec: StrategySpec,
        *,
        start_index: int = 0,
    ) -> BacktestMetrics:
        if candles and not isinstance(candles[0], Candle):
            now = datetime.now(UTC)
            candles = [
                Candle(
                    now,
                    Decimal(str(value)),
                    Decimal(str(value)),
                    Decimal(str(value)),
                    Decimal(str(value)),
                    Decimal("0"),
                )
                for value in candles
            ]
        bars: list[Candle] = candles  # type: ignore[assignment]
        if len(bars) <= spec.lookback + 2:
            return BacktestMetrics(0, 0, 0, 0, self.INITIAL_EQUITY)

        decisions = generate_strategy_decisions(bars, spec)
        closes = [float(item.close) for item in bars]
        regime_average = self._simple_sma(
            closes, min(50, max(10, spec.lookback)))
        cash, quantity, entry_cash = self.INITIAL_EQUITY, 0.0, 0.0
        entry_price, peak_price, entry_regime = 0.0, 0.0, "lateral"
        peak_equity, max_drawdown = self.INITIAL_EQUITY, 0.0
        fees, slippage_cost, gross_pnl, turnover = 0.0, 0.0, 0.0, 0.0
        exposed_bars = 0
        completed: list[_CompletedTrade] = []
        pending: SignalAction | None = None

        def close_position(reference: float) -> None:
            nonlocal cash, quantity, fees, slippage_cost, gross_pnl, turnover
            sell_price, slip_per_unit = self._fill_price(reference, False)
            gross_proceeds = quantity * sell_price
            fee = gross_proceeds * self.fee_rate
            net_proceeds = gross_proceeds - fee
            gross_pnl += quantity * (reference - entry_price)
            fees += fee
            slippage_cost += quantity * slip_per_unit
            turnover += gross_proceeds
            completed.append(_CompletedTrade(
                net_proceeds - entry_cash, entry_cash, entry_regime))
            cash += net_proceeds
            quantity = 0.0

        begin = max(spec.lookback + 1, start_index)
        for index in range(begin, len(bars)):
            open_price = float(bars[index].open)
            high = float(bars[index].high)
            low = float(bars[index].low)
            if pending is SignalAction.BUY and quantity == 0 and cash >= self.order_quote_amount:
                fill, slip_per_unit = self._fill_price(open_price, True)
                entry_cash = min(self.order_quote_amount, cash)
                quantity = (entry_cash / fill) * (1 - self.fee_rate)
                fees += entry_cash * self.fee_rate
                slippage_cost += quantity * slip_per_unit
                turnover += entry_cash
                cash -= entry_cash
                entry_price = fill
                peak_price = fill
                older = max(0, index - 5)
                old_average = regime_average[older]
                new_average = regime_average[index - 1]
                slope = (new_average or closes[index - 1]
                         ) - (old_average or closes[older])
                entry_regime = "alcista" if slope > 0 else "bajista" if slope < 0 else "lateral"
            elif pending is SignalAction.SELL and quantity > 0:
                close_position(open_price)
            pending = None

            if quantity > 0:
                exposed_bars += 1
                stop_price = entry_price * (1 - self.stop_loss)
                take_price = entry_price * (1 + self.take_profit)
                trailing_reference = max(peak_price, high)
                trailing_price = peak_price * (1 - self.trailing_stop)
                if low <= stop_price:
                    close_position(min(open_price, stop_price))
                elif peak_price > entry_price and low <= trailing_price:
                    close_position(min(open_price, trailing_price))
                elif high >= take_price:
                    close_position(max(open_price, take_price))
                else:
                    peak_price = trailing_reference

            decision = decisions[index]
            if decision.action is SignalAction.BUY and quantity == 0:
                pending = SignalAction.BUY
            elif decision.action is SignalAction.SELL and quantity > 0:
                pending = SignalAction.SELL

            equity = cash + quantity * closes[index]
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(
                max_drawdown, (peak_equity - equity) / peak_equity * 100)

        if quantity > 0:
            close_position(closes[-1])
        final_equity = cash
        wins = [item for item in completed if item.pnl > 0]
        losses = [item for item in completed if item.pnl < 0]
        positive = sum(item.pnl for item in wins)
        negative = abs(sum(item.pnl for item in losses))
        profit_factor = positive / \
            negative if negative else (99.0 if positive else 0.0)
        buy_fill, _ = self._fill_price(closes[begin], True)
        hold_quantity = (self.order_quote_amount /
                         buy_fill) * (1 - self.fee_rate)
        sell_fill, _ = self._fill_price(closes[-1], False)
        hold_final = (
            self.INITIAL_EQUITY
            - self.order_quote_amount
            + hold_quantity * sell_fill * (1 - self.fee_rate)
        )
        benchmark = (hold_final / self.INITIAL_EQUITY - 1) * 100
        trade_returns = [
            item.pnl / item.entry_cash for item in completed if item.entry_cash]
        sharpe, sortino = self._risk_adjusted_returns(trade_returns)
        net_pnl = final_equity - self.INITIAL_EQUITY
        result_return = net_pnl / self.INITIAL_EQUITY * 100
        regime_pnl: dict[str, float] = {}
        for item in completed:
            regime_pnl[item.regime] = regime_pnl.get(
                item.regime, 0.0) + item.pnl
        return BacktestMetrics(
            return_pct=round(result_return, 4),
            max_drawdown_pct=round(max_drawdown, 4),
            trades=len(completed),
            win_rate_pct=round(len(wins) / len(completed) *
                               100, 2) if completed else 0.0,
            final_equity=round(final_equity, 4),
            gross_pnl=round(gross_pnl, 4),
            fees=round(fees, 4),
            slippage_cost=round(slippage_cost, 4),
            net_pnl=round(net_pnl, 4),
            profit_factor=round(profit_factor, 4),
            expectancy=round(net_pnl / len(completed),
                             4) if completed else 0.0,
            exposure_pct=round(
                exposed_bars / max(1, len(bars) - begin) * 100, 2),
            turnover=round(turnover, 4),
            sharpe=round(sharpe, 4),
            sortino=round(sortino, 4),
            buy_hold_return_pct=round(benchmark, 4),
            excess_return_pct=round(result_return - benchmark, 4),
            return_on_deployed_pct=round(
                net_pnl / self.order_quote_amount * 100, 4),
            regime_returns_pct={
                key: round(value / self.order_quote_amount * 100, 4)
                for key, value in regime_pnl.items()
            },
        )

    @staticmethod
    def _simple_sma(values: list[float], period: int) -> list[float | None]:
        result: list[float | None] = [None] * len(values)
        running = 0.0
        for index, value in enumerate(values):
            running += value
            if index >= period:
                running -= values[index - period]
            if index >= period - 1:
                result[index] = running / period
        return result

    @staticmethod
    def _selection_confidence(validation: BacktestMetrics, trials: int) -> float:
        if validation.trades < 2 or validation.sharpe <= 0:
            return 0.0
        hurdle = math.sqrt(2 * math.log(max(2, trials))) / \
            math.sqrt(validation.trades)
        z_score = (validation.sharpe - hurdle) * \
            math.sqrt(max(1, validation.trades - 1))
        return max(0.0, min(100.0, 50 * (1 + math.erf(z_score / math.sqrt(2)))))

    def train(self, candles: list[Candle]) -> TrainingResult:
        if len(candles) < self.minimum_samples:
            raise ValueError(
                f"Se requieren al menos {self.minimum_samples} velas cerradas")
        validate_candles(candles, self.source_interval, self.minimum_samples)
        specs = self.candidate_specs()
        ranked: list[tuple[float, int, StrategySpec,
                           BacktestMetrics, list[float]]] = []
        family_best: dict[str, tuple[float, int,
                                     StrategySpec, BacktestMetrics]] = {}
        slot_best: dict[tuple[str, int],
                        tuple[float, StrategySpec, BacktestMetrics]] = {}
        for multiplier in self.timeframe_multipliers:
            research_bars = self._aggregate(candles, multiplier)
            split = int(len(research_bars) * 0.7)
            development = research_bars[:split]
            for spec in specs:
                # The adaptive strategy explicitly derives 1h EMA values from
                # complete groups of four 15m bars; it is not a 1h strategy.
                if multiplier != self.timeframe_multipliers[0] and spec.kind == "regime_adaptive":
                    continue
                fold_returns: list[float] = []
                fold_drawdowns: list[float] = []
                for fold in range(5):
                    test_start = int(split * (0.35 + fold * 0.12))
                    test_end = min(split, int(split * (0.47 + fold * 0.12)))
                    context_start = max(0, test_start - spec.lookback - 2)
                    fold_result = self.backtest_spec(
                        development[context_start:test_end],
                        spec,
                        start_index=test_start - context_start,
                    )
                    fold_returns.append(fold_result.return_on_deployed_pct)
                    fold_drawdowns.append(fold_result.max_drawdown_pct)
                metrics = self.backtest_spec(development, spec)
                required_development_trades = math.ceil(
                    self.minimum_validation_trades * 0.7 / 0.3)
                activity_shortfall = max(
                    0, required_development_trades - metrics.trades)
                activity_penalty = activity_shortfall * 0.75
                stability_penalty = sum(
                    item <= 0 for item in fold_returns) * 0.75
                score = (
                    median(fold_returns)
                    + metrics.return_on_deployed_pct * 0.15
                    - max(fold_drawdowns) * 20
                    - activity_penalty
                    - stability_penalty
                )
                ranked.append((score, multiplier, spec, metrics, fold_returns))
                current = family_best.get(spec.kind)
                if current is None or score > current[0]:
                    family_best[spec.kind] = (score, multiplier, spec, metrics)
                slot_current = slot_best.get((spec.kind, multiplier))
                if slot_current is None or score > slot_current[0]:
                    slot_best[(spec.kind, multiplier)] = (score, spec, metrics)

        score, multiplier, spec, training_metrics, fold_returns = max(
            ranked, key=lambda item: item[0]
        )
        selected_bars = self._aggregate(candles, multiplier)
        split = int(len(selected_bars) * 0.7)
        context_start = max(0, split - spec.lookback - 2)
        validation_metrics = self.backtest_spec(
            selected_bars[context_start:], spec, start_index=split -
            context_start
        )
        positive_folds = sum(item > 0 for item in fold_returns)
        confidence = self._selection_confidence(
            validation_metrics, len(ranked))
        calmar = (
            validation_metrics.return_pct / validation_metrics.max_drawdown_pct
            if validation_metrics.max_drawdown_pct > 0
            else 1.0
        )
        reasons: list[str] = []
        if validation_metrics.trades < self.minimum_validation_trades:
            reasons.append(
                f"menos de {self.minimum_validation_trades} operaciones de validación")
        if validation_metrics.return_pct <= 0:
            reasons.append("retorno neto fuera de muestra no positivo")
        if validation_metrics.excess_return_pct <= 0:
            reasons.append("no supera comprar y mantener con la misma orden base")
        if validation_metrics.profit_factor < 1.10:
            reasons.append("profit factor inferior a 1,10")
        if calmar < 0.4:
            reasons.append("relación de riesgo Calmar insuficiente (< 0.4)")
        if positive_folds < 3:
            reasons.append("menos de tres ventanas walk-forward positivas")
        if confidence < 70:
            reasons.append("confianza ajustada por selección inferior a 70%")
        experimental = (
            validation_metrics.trades >= self.minimum_validation_trades
            and validation_metrics.return_pct > 0
            and validation_metrics.profit_factor >= 1.0
            and positive_folds >= 3
        )
        status = (
            "candidate"
            if not reasons
            else "experimental"
            if experimental
            else "insufficient_validation"
            if validation_metrics.trades < self.minimum_validation_trades
            else "rejected"
        )
        params = dict(spec.parameters)
        legacy_fast = int(
            params.get(
                "fast_period",
                params.get(
                    "trend_fast_period", params.get(
                        "rsi_period", params.get("exit_period", 7))
                ),
            )
        )
        legacy_slow = int(
            params.get(
                "slow_period",
                params.get(
                    "trend_slow_period",
                    params.get("period", params.get(
                        "entry_period", spec.lookback)),
                ),
            )
        )
        leaders = []
        family_validation = []
        for family, (family_score, family_multiplier, family_spec, family_metrics) in sorted(
            family_best.items()
        ):
            family_bars = self._aggregate(candles, family_multiplier)
            family_split = int(len(family_bars) * 0.7)
            family_context = max(0, family_split - family_spec.lookback - 2)
            family_oos = self.backtest_spec(
                family_bars[family_context:], family_spec,
                start_index=family_split - family_context,
            )
            family_validation.append({
                "strategy": family,
                "name": family_spec.name,
                "interval": self._interval_label(family_multiplier),
                "parameters": dict(family_spec.parameters),
                "return_pct": family_oos.return_pct,
                "buy_hold_return_pct": family_oos.buy_hold_return_pct,
                "excess_return_pct": family_oos.excess_return_pct,
                "trades": family_oos.trades,
                "profit_factor": family_oos.profit_factor,
                "max_drawdown_pct": family_oos.max_drawdown_pct,
                "net_pnl": family_oos.net_pnl,
                "fees": family_oos.fees,
                "slippage_cost": family_oos.slippage_cost,
                "selected": family == spec.kind and family_multiplier == multiplier,
            })
            leaders.append(
                {
                    "strategy": family,
                    "name": family_spec.name,
                    "interval": self._interval_label(family_multiplier),
                    "parameters": family_spec.parameters,
                    "development_score": round(family_score, 4),
                    "development_return_on_deployed_pct": family_metrics.return_on_deployed_pct,
                    "development_profit_factor": family_metrics.profit_factor,
                    "development_trades": family_metrics.trades,
                }
            )
        shadow_leaders = []
        for (family, family_multiplier), (
            family_score,
            family_spec,
            family_metrics,
        ) in sorted(slot_best.items()):
            shadow_leaders.append(
                {
                    "strategy": family,
                    "name": family_spec.name,
                    "interval": self._interval_label(family_multiplier),
                    "parameters": family_spec.parameters,
                    "development_score": round(family_score, 4),
                    "development_return_on_deployed_pct": (family_metrics.return_on_deployed_pct),
                    "development_profit_factor": family_metrics.profit_factor,
                    "development_trades": family_metrics.trades,
                }
            )
        return TrainingResult(
            generated_at=datetime.now(UTC).isoformat(),
            samples=len(candles),
            train_samples=int(len(candles) * 0.7),
            validation_samples=len(candles) - int(len(candles) * 0.7),
            candidates_tested=len(ranked),
            recommended_fast=legacy_fast,
            recommended_slow=legacy_slow,
            recommended_interval=self._interval_label(multiplier),
            recommended_strategy=spec.kind,
            recommended_name=spec.name,
            recommended_parameters=params,
            families_tested=sorted(family_best),
            family_leaders=leaders,
            family_validation=family_validation,
            shadow_leaders=shadow_leaders,
            training=training_metrics,
            validation=validation_metrics,
            score=round(score, 4),
            status=status,
            robustness_folds=len(fold_returns),
            positive_folds=positive_folds,
            robustness_returns_pct=[round(item, 4) for item in fold_returns],
            validation_buy_hold_return_pct=validation_metrics.buy_hold_return_pct,
            purge_bars=spec.lookback * multiplier,
            selection_adjusted_confidence_pct=round(confidence, 2),
            rejection_reasons=reasons,
            assumptions={
                "simulation_version": self.SIMULATION_VERSION,
                "strategy_universe": "curated-multifamily-v1",
                "decision_timeframe": self._interval_label(multiplier),
                "regime_timeframe": "1h",
                "bar_alignment": "utc_clock_complete_buckets",
                "fill_timing": "next_bar_open",
                "intrabar_ordering": "stop_first_conservative",
                "trailing_update": "closed_bar_peak_applies_to_next_bar",
                "fee_rate": self.fee_rate,
                "spread_bps": self.spread_rate * 10_000,
                "slippage_bps": self.slippage_rate * 10_000,
                "round_trip_cost_pct": (
                    2 * self.fee_rate + self.spread_rate + 2 * self.slippage_rate
                ) * 100,
                "stop_loss_pct": self.stop_loss * 100,
                "take_profit_pct": self.take_profit * 100,
                "trailing_stop_pct": self.trailing_stop * 100,
                "initial_equity": self.INITIAL_EQUITY,
                "order_quote_amount": self.order_quote_amount,
                "benchmark": "risk_adjusted_calmar_pf",
            },
        )

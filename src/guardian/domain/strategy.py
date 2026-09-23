from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from guardian.domain.models import Candle, Signal, SignalAction


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    action: SignalAction
    reason: str
    primary_value: float | None = None
    secondary_value: float | None = None
    primary_label: str = "Indicador A"
    secondary_label: str = "Indicador B"


@dataclass(frozen=True, slots=True)
class StrategySpec:
    kind: str
    name: str
    parameters: dict[str, float | int]

    @property
    def lookback(self) -> int:
        if self.kind == "regime_adaptive":
            return max(
                int(self.parameters["trend_slow_period"]) * 4 + 4,
                int(self.parameters["trigger_period"]) + 2,
                int(self.parameters["rsi_period"]) + 2,
                int(self.parameters["atr_period"]) + 2,
            )
        periods = [int(value)
                   for key, value in self.parameters.items() if "period" in key]
        return max([3, *periods])


def _sma(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            result[index] = running / period
    return result


def _rolling_std(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    running = 0.0
    running_sq = 0.0
    for index, value in enumerate(values):
        running += value
        running_sq += value * value
        if index >= period:
            removed = values[index - period]
            running -= removed
            running_sq -= removed * removed
        if index >= period - 1:
            variance = max(0.0, running_sq / period - (running / period) ** 2)
            result[index] = math.sqrt(variance)
    return result


def _ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        current = values[index] * alpha + current * (1 - alpha)
        result[index] = current
    return result


def _rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [max(0.0, values[index] - values[index - 1])
             for index in range(1, len(values))]
    losses = [max(0.0, values[index - 1] - values[index])
              for index in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def rsi_value() -> float:
        if avg_loss == 0:
            return 100.0 if avg_gain else 50.0
        return 100 - 100 / (1 + avg_gain / avg_loss)

    result[period] = rsi_value()
    for index in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[index - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index - 1]) / period
        result[index] = rsi_value()
    return result


def _atr(candles: list[Candle], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return result
    ranges = []
    for index, candle in enumerate(candles):
        previous = float(
            candles[index - 1].close) if index else float(candle.open)
        ranges.append(
            max(
                float(candle.high - candle.low),
                abs(float(candle.high) - previous),
                abs(float(candle.low) - previous),
            )
        )
    current = sum(ranges[1: period + 1]) / period
    result[period] = current
    for index in range(period + 1, len(candles)):
        current = (current * (period - 1) + ranges[index]) / period
        result[index] = current
    return result


def _hourly_ema_from_15m(
    candles: list[Candle], fast_period: int, slow_period: int
) -> tuple[list[float | None], list[float | None]]:
    """Causal hourly EMA values, exposed only after each UTC hour has closed."""
    hourly_closes: list[float] = []
    close_indices: list[int] = []
    groups: dict[int, list[tuple[int, Candle]]] = {}
    for index, candle in enumerate(candles):
        bucket = int(candle.open_time.timestamp()) // 3600
        groups.setdefault(bucket, []).append((index, candle))
    for bucket, group in groups.items():
        expected = bucket * 3600
        if len(group) != 4 or any(
            candle.open_time.timestamp() != expected + offset * 900
            for offset, (_, candle) in enumerate(group)
        ):
            continue
        close_indices.append(group[-1][0])
        hourly_closes.append(float(group[-1][1].close))
    fast_hourly = _ema(hourly_closes, fast_period)
    slow_hourly = _ema(hourly_closes, slow_period)
    fast: list[float | None] = [None] * len(candles)
    slow: list[float | None] = [None] * len(candles)
    pointer = -1
    for index in range(len(candles)):
        while pointer + 1 < len(close_indices) and close_indices[pointer + 1] <= index:
            pointer += 1
        if pointer >= 0:
            fast[index] = fast_hourly[pointer]
            slow[index] = slow_hourly[pointer]
    return fast, slow


def generate_strategy_decisions(
    candles: list[Candle], spec: StrategySpec
) -> list[StrategyDecision]:
    """Generate closed-candle decisions shared by execution and research."""
    count = len(candles)
    decisions = [StrategyDecision(
        SignalAction.HOLD, "Datos insuficientes") for _ in candles]
    if count < spec.lookback + 2:
        return decisions
    closes = [float(item.close) for item in candles]
    params = spec.parameters

    if spec.kind == "regime_adaptive":
        trend_fast_period = int(params["trend_fast_period"])
        trend_slow_period = int(params["trend_slow_period"])
        trigger_period = int(params["trigger_period"])
        rsi_period = int(params["rsi_period"])
        atr_period = int(params["atr_period"])
        entry_rsi = float(params["entry_rsi"])
        trend_strength_pct = float(params["trend_strength_pct"])
        max_atr_pct = float(params["max_atr_pct"])
        min_atr_pct = float(params.get("min_atr_pct", 0))
        trend_fast, trend_slow = _hourly_ema_from_15m(
            candles, trend_fast_period, trend_slow_period
        )
        trigger = _ema(closes, trigger_period)
        rsi = _rsi(closes, rsi_period)
        atr = _atr(candles, atr_period)
        begin = spec.lookback
        for index in range(begin, count):
            required = (
                trend_fast[index], trend_slow[index], trend_slow[index - 4],
                trigger[index], trigger[index - 1], rsi[index], atr[index],
            )
            if any(value is None for value in required):
                continue
            atr_pct = atr[index] / closes[index] * \
                100  # type: ignore[operator]
            trend_gap_pct = (
                # type: ignore[operator]
                (trend_fast[index] - trend_slow[index])
                / trend_slow[index]  # type: ignore[operator]
                * 100
            )
            bullish = (
                trend_fast[index] > trend_slow[index]  # type: ignore[operator]
                # type: ignore[operator]
                and trend_slow[index] > trend_slow[index - 4]
                and closes[index] > trend_slow[index]  # type: ignore[operator]
                and trend_gap_pct >= trend_strength_pct
            )
            recovered = (
                # type: ignore[operator]
                closes[index - 1] <= trigger[index - 1]
                and closes[index] > trigger[index]  # type: ignore[operator]
            )
            action, reason = SignalAction.HOLD, "Sin configuración adaptativa confirmada"
            entry_confirmed = (
                bullish
                and recovered
                and entry_rsi <= rsi[index] < 70  # type: ignore[operator]
                and atr_pct >= min_atr_pct
                and atr_pct <= max_atr_pct
            )
            if entry_confirmed:
                action, reason = SignalAction.BUY, "Retroceso recuperado en régimen horario alcista"
            elif (
                trend_fast[index] < trend_slow[index]  # type: ignore[operator]
                or closes[index] < trend_slow[index]  # type: ignore[operator]
            ):
                action, reason = SignalAction.SELL, "Régimen horario alcista perdido"
            decisions[index] = StrategyDecision(
                action,
                reason,
                trend_fast[index],
                trend_slow[index],
                f"EMA 1h {trend_fast_period}",
                f"EMA 1h {trend_slow_period}",
            )
        return decisions

    if spec.kind == "sma_crossover":
        fast_period = int(params["fast_period"])
        slow_period = int(params["slow_period"])
        fast = _sma(closes, fast_period)
        slow = _sma(closes, slow_period)
        volumes = [float(item.volume) for item in candles]
        vol_ma = _sma(volumes, min(20, slow_period))
        for index in range(slow_period, count):
            if None in (fast[index - 1], slow[index - 1], fast[index], slow[index]):
                continue
            volume_confirmed = (
                vol_ma[index] is None or vol_ma[index] == 0 or volumes[index] >= (
                    vol_ma[index] * 0.85)
            )
            action, reason = SignalAction.HOLD, "Sin cruce SMA nuevo"
            if fast[index - 1] <= slow[index - 1] and fast[index] > slow[index]:  # type: ignore[operator]
                if volume_confirmed:
                    action, reason = SignalAction.BUY, "Cruce alcista SMA confirmado"
                else:
                    action, reason = SignalAction.HOLD, "Cruce alcista descartado por volumen débil"
            # type: ignore[operator]
            elif fast[index - 1] >= slow[index - 1] and fast[index] < slow[index]:
                action, reason = SignalAction.SELL, "Cruce bajista SMA confirmado"
            decisions[index] = StrategyDecision(
                action, reason, fast[index], slow[index], f"SMA {fast_period}", f"SMA {slow_period}"
            )
        return decisions

    if spec.kind == "breakout":
        entry_period = int(params["entry_period"])
        exit_period = int(params["exit_period"])
        trend_period = int(params["trend_period"])
        trend = _ema(closes, trend_period)
        begin = max(entry_period, exit_period, trend_period) + 1
        for index in range(begin, count):
            upper = max(closes[index - entry_period: index])
            previous_upper = max(closes[index - entry_period - 1: index - 1])
            lower = min(closes[index - exit_period: index])
            previous_lower = min(closes[index - exit_period - 1: index - 1])
            action, reason = SignalAction.HOLD, "Dentro del canal de ruptura"
            if (
                closes[index] > upper
                and closes[index] > (trend[index] or closes[index])
                and closes[index - 1] <= previous_upper
            ):
                action, reason = (
                    SignalAction.BUY,
                    "Ruptura alcista confirmada con filtro de tendencia",
                )
            elif closes[index] < lower and closes[index - 1] >= previous_lower:
                action, reason = SignalAction.SELL, "Salida por ruptura del canal inferior"
            decisions[index] = StrategyDecision(
                action, reason, closes[index], upper, "Cierre", f"Canal {entry_period}"
            )
        return decisions

    if spec.kind == "momentum":
        fast_period = int(params["fast_period"])
        slow_period = int(params["slow_period"])
        rsi_period = int(params["rsi_period"])
        entry_rsi = float(params["entry_rsi"])
        exit_rsi = float(params["exit_rsi"])
        fast = _ema(closes, fast_period)
        slow = _ema(closes, slow_period)
        rsi = _rsi(closes, rsi_period)
        begin = max(slow_period, rsi_period) + 1
        for index in range(begin, count):
            if None in (fast[index], slow[index], rsi[index], rsi[index - 1]):
                continue
            action, reason = SignalAction.HOLD, "Momentum sin confirmación"
            # type: ignore[operator]
            if fast[index] > slow[index] and rsi[index - 1] <= entry_rsi < rsi[index]:
                action, reason = SignalAction.BUY, "Momentum alcista confirmado por EMA y RSI"
            # type: ignore[operator]
            elif fast[index] < slow[index] or rsi[index - 1] < exit_rsi <= rsi[index]:
                action, reason = SignalAction.SELL, "Momentum agotado"
            decisions[index] = StrategyDecision(
                action, reason, rsi[index], fast[index], f"RSI {rsi_period}", f"EMA {fast_period}"
            )
        return decisions

    if spec.kind == "mean_reversion":
        period = int(params["period"])
        deviation = float(params["deviation"])
        rsi_period = int(params["rsi_period"])
        entry_rsi = float(params["entry_rsi"])
        exit_rsi = float(params["exit_rsi"])
        average = _sma(closes, period)
        std = _rolling_std(closes, period)
        rsi = _rsi(closes, rsi_period)
        begin = max(period, rsi_period) + 1
        for index in range(begin, count):
            if None in (average[index], std[index], rsi[index], rsi[index - 1]):
                continue
            lower = average[index] - deviation * \
                std[index]  # type: ignore[operator]
            # type: ignore[operator]
            previous_lower = average[index - 1] - deviation * std[index - 1]
            # type: ignore[operator]
            entry = closes[index] < lower and rsi[index] <= entry_rsi
            # type: ignore[operator]
            previous_entry = closes[index -
                                    1] < previous_lower and rsi[index - 1] <= entry_rsi
            action, reason = SignalAction.HOLD, "Precio dentro de bandas de reversión"
            if entry and not previous_entry:
                action, reason = SignalAction.BUY, "Sobreventa confirmada por banda y RSI"
            elif closes[index] >= average[index] or rsi[index] >= exit_rsi:  # type: ignore[operator]
                # type: ignore[operator]
                if closes[index - 1] < average[index - 1] and rsi[index - 1] < exit_rsi:
                    action, reason = SignalAction.SELL, "Reversión completada hacia la media"
            decisions[index] = StrategyDecision(
                action, reason, rsi[index], lower, f"RSI {rsi_period}", "Banda inferior"
            )
        return decisions

    if spec.kind == "trend_pullback":
        trigger_period = int(params["trigger_period"])
        trend_period = int(params["trend_period"])
        rsi_period = int(params["rsi_period"])
        entry_rsi = float(params["entry_rsi"])
        exit_rsi = float(params["exit_rsi"])
        trigger = _ema(closes, trigger_period)
        trend = _ema(closes, trend_period)
        rsi = _rsi(closes, rsi_period)
        begin = max(trigger_period, trend_period, rsi_period) + 2
        for index in range(begin, count):
            if None in (
                trigger[index],
                trigger[index - 1],
                trend[index],
                trend[index - 5],
                rsi[index],
            ):
                continue
            # type: ignore[operator]
            trend_rising = trend[index] > trend[index - 5]
            # type: ignore[operator]
            recovered = closes[index - 1] <= trigger[index -
                                                     1] and closes[index] > trigger[index]
            action, reason = SignalAction.HOLD, "Esperando retroceso dentro de tendencia"
            if (
                trend_rising
                and closes[index] > trend[index]  # type: ignore[operator]
                and recovered
                # type: ignore[operator]
                and entry_rsi <= rsi[index] < exit_rsi
            ):
                action, reason = (
                    SignalAction.BUY,
                    "Retroceso recuperado dentro de tendencia alcista",
                )
            # type: ignore[operator]
            elif closes[index - 1] >= trigger[index - 1] and closes[index] < trigger[index]:
                action, reason = SignalAction.SELL, "Pérdida del impulso de tendencia"
            decisions[index] = StrategyDecision(
                action,
                reason,
                rsi[index],
                trend[index],
                f"RSI {rsi_period}",
                f"EMA tendencia {trend_period}",
            )
        return decisions

    raise ValueError(f"Tipo de estrategia no soportado: {spec.kind}")


class AdaptiveLongStrategy:
    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec
        self.fast_period = int(
            spec.parameters.get(
                "fast_period",
                spec.parameters.get("trend_fast_period",
                                    spec.parameters.get("rsi_period", 7)),
            )
        )
        self.slow_period = int(spec.parameters.get(
            "trend_slow_period", spec.lookback))
        self.name = spec.name
        self.kind = spec.kind
        self.parameters = dict(spec.parameters)

    def evaluate(self, candles: list[Candle]) -> Signal:
        if not candles:
            return Signal(SignalAction.HOLD, "Datos insuficientes")
        decision = generate_strategy_decisions(candles, self.spec)[-1]
        return Signal(
            decision.action,
            decision.reason,
            Decimal(str(decision.primary_value)
                    ) if decision.primary_value is not None else None,
            Decimal(str(decision.secondary_value))
            if decision.secondary_value is not None
            else None,
            decision.primary_label,
            decision.secondary_label,
        )


class SmaCrossoverStrategy(AdaptiveLongStrategy):
    """Backward-compatible constructor for the original strategy."""

    def __init__(self, fast_period: int, slow_period: int) -> None:
        if fast_period < 2 or slow_period <= fast_period:
            raise ValueError("SMA periods must satisfy 2 <= fast < slow")
        super().__init__(
            StrategySpec(
                "sma_crossover",
                "Cruce SMA",
                {"fast_period": fast_period, "slow_period": slow_period},
            )
        )


def strategy_from_payload(kind: str, parameters: dict[str, float | int]) -> AdaptiveLongStrategy:
    names = {
        "sma_crossover": "Cruce SMA",
        "breakout": "Ruptura de canal",
        "momentum": "Momentum EMA + RSI",
        "mean_reversion": "Reversión a la media",
        "trend_pullback": "Retroceso en tendencia",
        "regime_adaptive": "Day trading adaptativo",
    }
    if kind not in names:
        raise ValueError(f"Tipo de estrategia no soportado: {kind}")
    return AdaptiveLongStrategy(StrategySpec(kind, names[kind], parameters))

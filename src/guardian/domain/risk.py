from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from guardian.domain.models import RiskSnapshot, Side


@dataclass(frozen=True, slots=True)
class RiskLimits:
    order_quote_amount: Decimal
    max_position_quote: Decimal
    max_daily_loss_quote: Decimal
    max_trades_per_day: int
    cooldown_seconds: int


# ─────────────────────────────────────────────────────────────────────────────
# CORRECCIÓN: Función auxiliar para calcular límites como % del equity real.
#
# PROBLEMA ORIGINAL: max_daily_loss_quote era un valor absoluto fijo (ej: 2 USDT).
# Si alguien opera con 10,000 USDT reales, el límite seguía siendo 2 USDT (0.02%).
#
# USO: Llama a esta función al arrancar el motor (en main.py) pasando el equity
# real obtenido del exchange, y los porcentajes del .env:
#
#   equity = balance.quote_free + balance.base_free * precio_actual
#   limits = compute_risk_limits_from_equity(equity, settings)
#
# ─────────────────────────────────────────────────────────────────────────────
def compute_risk_limits_from_equity(
    equity: Decimal,
    order_quote_amount: Decimal,
    max_daily_loss_pct: Decimal,      # ej: Decimal("2.0")  → 2% del equity
    max_position_pct: Decimal,        # ej: Decimal("10.0") → 10% del equity
    max_trades_per_day: int,
    cooldown_seconds: int,
) -> RiskLimits:
    """
    Calcula RiskLimits escalando los límites de pérdida y posición
    como porcentaje del equity actual, en lugar de valores absolutos.

    Esto garantiza que al operar con capital real (ej: 5,000 USDT),
    los límites se ajusten automáticamente al capital disponible.
    """
    if equity <= Decimal("0"):
        raise ValueError(
            "El equity debe ser positivo para calcular límites dinámicos")

    max_daily_loss = equity * max_daily_loss_pct / Decimal("100")
    max_position = equity * max_position_pct / Decimal("100")

    # Sanidad: la orden individual no puede superar la posición máxima
    effective_order = min(order_quote_amount, max_position)

    return RiskLimits(
        order_quote_amount=effective_order,
        max_position_quote=max_position,
        max_daily_loss_quote=max_daily_loss,
        max_trades_per_day=max_trades_per_day,
        cooldown_seconds=cooldown_seconds,
    )


class RiskManager:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def authorize(
        self,
        side: Side,
        snapshot: RiskSnapshot,
        now: datetime,
        available_quote: Decimal,
        available_base_quote: Decimal,
    ) -> tuple[bool, str]:
        if snapshot.emergency_stop:
            return False, "Parada de emergencia activa"
        # A risk-reducing exit must remain possible after entry limits are reached.
        if side is Side.SELL and available_base_quote > Decimal("0"):
            return True, "Salida autorizada"
        if snapshot.realized_pnl_today <= -self.limits.max_daily_loss_quote:
            return False, "Límite de pérdida diaria alcanzado"
        if snapshot.entries_today >= self.limits.max_trades_per_day:
            return False, "Máximo de entradas diarias alcanzado"
        if snapshot.last_trade_at and now - snapshot.last_trade_at < timedelta(
            seconds=self.limits.cooldown_seconds
        ):
            return False, "Periodo de enfriamiento activo"
        if side is Side.BUY:
            if self.limits.order_quote_amount > available_quote:
                return False, "Saldo cotizado insuficiente"
            if (
                max(snapshot.position_quote, available_base_quote) +
                    self.limits.order_quote_amount
                > self.limits.max_position_quote
            ):
                return False, "La orden excedería la posición máxima"
        elif available_base_quote <= Decimal("0"):
            return False, "No existe posición disponible para vender"
        return True, "Autorizada"

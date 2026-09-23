from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class Candle:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class Signal:
    action: SignalAction
    reason: str
    fast_sma: Decimal | None = None
    slow_sma: Decimal | None = None
    primary_label: str = "Indicador A"
    secondary_label: str = "Indicador B"


@dataclass(frozen=True, slots=True)
class Balance:
    base_free: Decimal
    quote_free: Decimal


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: Side
    quote_amount: Decimal | None = None
    base_quantity: Decimal | None = None
    client_order_id: str = ""


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    status: str
    executed_quantity: Decimal
    cumulative_quote_quantity: Decimal
    average_price: Decimal
    created_at: datetime
    is_simulated: bool
    reference_price: Decimal | None = None
    fee_quote: Decimal = Decimal("0")
    slippage_quote: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    realized_pnl_today: Decimal
    trades_today: int
    position_quote: Decimal
    emergency_stop: bool
    last_trade_at: datetime | None = None
    entries_today: int = 0


@dataclass(frozen=True, slots=True)
class BotStatus:
    running: bool
    mode: str
    environment: str
    symbol: str
    last_price: Decimal | None
    last_signal: Signal | None
    last_cycle_at: datetime | None
    last_error: str | None
    emergency_stop: bool
    started_at: datetime | None


def utc_now() -> datetime:
    return datetime.now(UTC)

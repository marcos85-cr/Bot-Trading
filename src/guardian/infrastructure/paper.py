from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from guardian.domain.models import Balance, OrderRequest, OrderResult, Side
from guardian.domain.ports import TradingRepository
from guardian.infrastructure.binance import BinanceSpotClient


class PaperExchange:
    def __init__(
        self,
        market_data: BinanceSpotClient,
        starting_quote: Decimal,
        fee_rate: Decimal,
        repository: TradingRepository,
        spread_bps: Decimal = Decimal("1"),
        slippage_bps: Decimal = Decimal("1"),
        research_data: BinanceSpotClient | None = None,
    ) -> None:
        self.market_data = market_data
        self.research_data = research_data or market_data
        self.repository = repository
        self._order_lock = asyncio.Lock()
        self.quote_balance = Decimal(
            repository.get_state("paper_quote_balance", str(starting_quote))
        )
        self.base_balance = Decimal(
            repository.get_state("paper_base_balance", "0"))
        self.fee_rate = fee_rate
        self.adverse_fill_rate = (
            spread_bps / Decimal("2") + slippage_bps) / Decimal("10000")

    async def close(self) -> None:
        await self.market_data.close()
        if self.research_data is not self.market_data:
            await self.research_data.close()

    async def ping(self) -> None:
        await self.market_data.ping()

    async def server_time_ms(self) -> int:
        return await self.market_data.server_time_ms()

    async def candles(self, symbol: str, interval: str, limit: int):
        return await self.market_data.candles(symbol, interval, limit)

    async def historical_candles(self, symbol: str, interval: str, limit: int):
        return await self.research_data.historical_candles(symbol, interval, limit)

    async def price(self, symbol: str) -> Decimal:
        return await self.market_data.price(symbol)

    async def symbol_info(self, symbol: str):
        return await self.market_data.symbol_info(symbol)

    async def balance(self, base_asset: str, quote_asset: str) -> Balance:
        return Balance(self.base_balance, self.quote_balance)

    async def place_protective_stop(
        self, symbol: str, quantity: Decimal, stop_price: Decimal, limit_price: Decimal
    ) -> dict[str, Any] | None:
        return {
            "symbol": symbol,
            "orderId": f"paper-stop-{uuid4().hex[:12]}",
            "clientOrderId": f"paper-stop-{uuid4().hex[:12]}",
            "type": "STOP_LOSS_LIMIT",
            "side": "SELL",
            "status": "NEW",
            "origQty": str(quantity),
            "price": str(limit_price),
            "stopPrice": str(stop_price),
        }

    async def place_market_order(self, request: OrderRequest) -> OrderResult:
        async with self._order_lock:
            return await self._place_market_order(request)

    async def _place_market_order(self, request: OrderRequest) -> OrderResult:
        if not request.client_order_id:
            raise ValueError(
                "La orden paper requiere un identificador idempotente")
        existing = await self.find_order(request)
        if existing is not None:
            return existing
        market_price = await self.price(request.symbol)
        if not market_price.is_finite() or market_price <= 0:
            raise ValueError("Precio paper inválido")
        next_quote, next_base = self.quote_balance, self.base_balance
        price = market_price * (
            Decimal("1") + self.adverse_fill_rate
            if request.side is Side.BUY
            else Decimal("1") - self.adverse_fill_rate
        )
        if request.side is Side.BUY:
            quote = request.quote_amount or Decimal("0")
            if quote <= 0 or quote > self.quote_balance:
                raise ValueError("Saldo paper insuficiente")
            quantity = (quote / price) * (Decimal("1") - self.fee_rate)
            next_quote -= quote
            next_base += quantity
        else:
            quantity = min(
                request.base_quantity or self.base_balance, self.base_balance)
            if quantity <= 0:
                raise ValueError("Posición paper insuficiente")
            quote = quantity * price * (Decimal("1") - self.fee_rate)
            next_base -= quantity
            next_quote += quote
        result = OrderResult(
            order_id=f"paper-{uuid4().hex[:16]}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            status="FILLED",
            executed_quantity=quantity,
            cumulative_quote_quantity=quote,
            average_price=price,
            created_at=datetime.now(UTC),
            is_simulated=True,
            reference_price=market_price,
            fee_quote=(
                (request.quote_amount or Decimal("0")) * self.fee_rate
                if request.side is Side.BUY
                else quantity * price * self.fee_rate
            ),
            slippage_quote=quantity * abs(price - market_price),
        )
        receipt = {"request": self._request_signature(
            request), "result": asdict(result)}
        self.repository.set_states({
            "paper_quote_balance": str(next_quote),
            "paper_base_balance": str(next_base),
            f"paper_order:{request.client_order_id}": json.dumps(receipt, default=str),
        })
        self.quote_balance, self.base_balance = next_quote, next_base
        return result

    @staticmethod
    def _request_signature(request: OrderRequest) -> dict[str, str]:
        return {
            "symbol": request.symbol,
            "side": request.side.value,
            "quote_amount": str(request.quote_amount),
            "base_quantity": str(request.base_quantity),
        }

    async def find_order(self, request: OrderRequest) -> OrderResult | None:
        raw = self.repository.get_state(
            f"paper_order:{request.client_order_id}")
        if not raw:
            return None
        receipt = json.loads(raw)
        if receipt["request"] != self._request_signature(request):
            raise ValueError("Identificador paper reutilizado con otra orden")
        data = receipt["result"]
        for key in (
            "executed_quantity",
            "cumulative_quote_quantity",
            "average_price",
            "reference_price",
            "fee_quote",
            "slippage_quote",
        ):
            data[key] = Decimal(data[key]) if data[key] is not None else None
        data["side"] = Side(data["side"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        return OrderResult(**data)

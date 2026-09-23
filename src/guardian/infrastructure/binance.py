from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from guardian.domain.models import Balance, Candle, OrderRequest, OrderResult, Side
from guardian.domain.ports import OrderExecutionUnknown


class BinanceApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after


class BinanceSpotClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        api_secret: str = "",
        recv_window_ms: int = 5000,
        timeout_seconds: float = 10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._api_secret = api_secret.encode()
        self.recv_window_ms = recv_window_ms
        self._time_offset_ms = 0
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(max_connections=10,
                                max_keepalive_connections=5),
            headers={"User-Agent": "BinanceGuardian/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = await self._client.request(method, path, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                error = self._api_error(exc)
                if error.status_code == 429 and attempt + 1 < attempts:
                    await asyncio.sleep(min(error.retry_after or 1.0, 10.0))
                    continue
                raise error from exc
            except httpx.HTTPError as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25 * (2**attempt) + secrets.randbelow(151) / 1000)
                    continue
                raise BinanceApiError(
                    f"Error de red con Binance: {type(exc).__name__}") from exc
        raise BinanceApiError(
            "Binance no respondió después de varios intentos")

    @staticmethod
    def _api_error(exc: httpx.HTTPStatusError) -> BinanceApiError:
        code: int | None = None
        try:
            payload = exc.response.json()
            raw_code = payload.get("code")
            code = int(raw_code) if raw_code is not None else None
            message = f"Binance API {raw_code}: {payload.get('msg')}"
        except (ValueError, TypeError):
            message = f"Binance HTTP {exc.response.status_code}"
        retry_header = exc.response.headers.get("Retry-After")
        try:
            retry_after = float(retry_header) if retry_header else None
        except ValueError:
            retry_after = None
        return BinanceApiError(
            message,
            status_code=exc.response.status_code,
            code=code,
            retry_after=retry_after,
        )

    async def _signed_request(self, method: str, path: str, params: dict[str, Any]) -> Any:
        if not self.api_key or not self._api_secret:
            raise BinanceApiError("Credenciales de API no configuradas")
        signed = dict(params)
        signed["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        signed["recvWindow"] = self.recv_window_ms
        query = urlencode(signed)
        signed["signature"] = hmac.new(
            self._api_secret, query.encode(), hashlib.sha256).hexdigest()
        headers = {"X-MBX-APIKEY": self.api_key}
        try:
            response = await self._client.request(method, path, params=signed, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise self._api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise BinanceApiError(
                f"Error de red con Binance: {type(exc).__name__}") from exc

    async def query_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return await self._signed_request(
            "GET",
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )

    async def find_order(self, request: OrderRequest) -> OrderResult | None:
        try:
            payload = await self.query_order(request.symbol, request.client_order_id)
        except BinanceApiError as exc:
            if exc.code == -2013:
                return None
            raise
        if payload.get("status") in {"NEW", "PENDING_NEW", "PARTIALLY_FILLED"}:
            terminal = await self._await_terminal_order(request.symbol, request.client_order_id)
            if terminal is None:
                raise OrderExecutionUnknown(
                    request.client_order_id,
                    "La orden recuperada sigue sin un estado terminal verificable",
                )
            payload = terminal
        fallback_price = await self.price(request.symbol)
        info = await self.symbol_info(request.symbol)
        quote_asset = str(info.get("quoteAsset", ""))
        return self._order_result(payload, request, fallback_price, quote_asset)

    async def ping(self) -> None:
        await self._request("GET", "/api/v3/ping")

    async def server_time_ms(self) -> int:
        payload = await self._request("GET", "/api/v3/time")
        server_ms = int(payload["serverTime"])
        self._time_offset_ms = server_ms - int(time.time() * 1000)
        return server_ms

    async def candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        payload = await self._request(
            "GET", "/api/v3/klines", {"symbol": symbol,
                                      "interval": interval, "limit": limit}
        )
        return [
            Candle(
                open_time=datetime.fromtimestamp(row[0] / 1000, tz=UTC),
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                volume=Decimal(row[5]),
            )
            for row in payload
        ]

    async def historical_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        """Page backwards through Spot klines without exceeding the 1000-row API limit."""
        remaining = min(max(limit, 1), 10000)
        end_time: int | None = None
        collected: dict[datetime, Candle] = {}
        while remaining > 0:
            chunk_size = min(remaining, 1000)
            params: dict[str, Any] = {
                "symbol": symbol,
                "interval": interval,
                "limit": chunk_size,
            }
            if end_time is not None:
                params["endTime"] = end_time
            payload = await self._request("GET", "/api/v3/klines", params)
            if not payload:
                break
            chunk = [
                Candle(
                    open_time=datetime.fromtimestamp(row[0] / 1000, tz=UTC),
                    open=Decimal(row[1]),
                    high=Decimal(row[2]),
                    low=Decimal(row[3]),
                    close=Decimal(row[4]),
                    volume=Decimal(row[5]),
                )
                for row in payload
            ]
            collected.update({candle.open_time: candle for candle in chunk})
            remaining = limit - len(collected)
            earliest_ms = int(
                min(candle.open_time for candle in chunk).timestamp() * 1000)
            end_time = earliest_ms - 1
            if len(chunk) < chunk_size:
                break
        return sorted(collected.values(), key=lambda candle: candle.open_time)[-limit:]

    async def price(self, symbol: str) -> Decimal:
        payload = await self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return Decimal(payload["price"])

    async def symbol_info(self, symbol: str) -> dict[str, Any]:
        payload = await self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = payload.get("symbols", [])
        if not symbols:
            raise BinanceApiError(f"Símbolo no encontrado: {symbol}")
        return symbols[0]

    async def balance(self, base_asset: str, quote_asset: str) -> Balance:
        payload = await self._signed_request("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        balances = {item["asset"]: Decimal(
            item["free"]) for item in payload.get("balances", [])}
        return Balance(
            balances.get(base_asset, Decimal("0")), balances.get(
                quote_asset, Decimal("0"))
        )

    @staticmethod
    def _floor_step(value: Decimal, step: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    async def _validate_quantity(
        self, symbol: str, quantity: Decimal, price: Decimal
    ) -> tuple[Decimal, dict[str, Any]]:
        info = await self.symbol_info(symbol)
        if info.get("status") != "TRADING" or not info.get("isSpotTradingAllowed", False):
            raise BinanceApiError(f"{symbol} no está habilitado para Spot")
        if "MARKET" not in info.get("orderTypes", []):
            raise BinanceApiError(f"{symbol} no admite órdenes MARKET")
        filters = {item["filterType"]: item for item in info["filters"]}
        lot = filters.get("MARKET_LOT_SIZE") or filters["LOT_SIZE"]
        step = Decimal(lot["stepSize"])
        adjusted = self._floor_step(quantity, step) if step else quantity
        if adjusted < Decimal(lot["minQty"]) or adjusted > Decimal(lot["maxQty"]):
            raise BinanceApiError("Cantidad fuera de LOT_SIZE/MARKET_LOT_SIZE")
        notional = adjusted * price
        nf = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
        if nf:
            applies_min = nf.get("applyMinToMarket", nf.get(
                "applyToMarket", True)) is not False
            applies_max = nf.get("applyMaxToMarket", False) is True
            if applies_min and notional < Decimal(nf.get("minNotional", "0")):
                raise BinanceApiError(
                    "Orden inferior al mínimo nocional de Binance")
            max_notional = Decimal(nf.get("maxNotional", "0"))
            if applies_max and max_notional > 0 and notional > max_notional:
                raise BinanceApiError(
                    "Orden superior al máximo nocional de Binance")
        return adjusted, info

    async def place_market_order(self, request: OrderRequest) -> OrderResult:
        current_price = await self.price(request.symbol)
        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": "MARKET",
            "newClientOrderId": request.client_order_id,
            "newOrderRespType": "FULL",
        }
        if request.side is Side.BUY and request.quote_amount is not None:
            _, info = await self._validate_quantity(
                request.symbol, request.quote_amount / current_price, current_price
            )
            if not info.get("quoteOrderQtyMarketAllowed", False):
                raise BinanceApiError(
                    "El símbolo no admite quoteOrderQty en órdenes MARKET")
            params["quoteOrderQty"] = format(request.quote_amount, "f")
        elif request.base_quantity is not None:
            quantity, info = await self._validate_quantity(
                request.symbol, request.base_quantity, current_price
            )
            params["quantity"] = format(quantity, "f")
        else:
            raise BinanceApiError("La orden no contiene una cantidad válida")
        quote_asset = str(info.get("quoteAsset", ""))
        try:
            payload = await self._signed_request("POST", "/api/v3/order", params)
        except BinanceApiError as exc:
            uncertain = (
                exc.status_code is None
                or (exc.status_code is not None and exc.status_code >= 500)
                or exc.code == -1007
            )
            if not uncertain:
                raise
            payload = await self._reconcile_order(request.symbol, request.client_order_id)
            if payload is None:
                raise OrderExecutionUnknown(
                    request.client_order_id,
                    "Binance no pudo confirmar el estado de la orden; sistema bloqueado",
                ) from exc
        if payload.get("status") in {"NEW", "PENDING_NEW", "PARTIALLY_FILLED"}:
            terminal = await self._await_terminal_order(request.symbol, request.client_order_id)
            if terminal is None:
                raise OrderExecutionUnknown(
                    request.client_order_id,
                    (
                        "La orden sigue abierta y Binance no confirmó un estado terminal; "
                        "sistema bloqueado"
                    ),
                )
            payload = terminal
        return self._order_result(payload, request, current_price, quote_asset)

    async def place_protective_stop(
        self, symbol: str, quantity: Decimal, stop_price: Decimal, limit_price: Decimal
    ) -> dict[str, Any] | None:
        current_price = await self.price(symbol)
        adjusted_qty, info = await self._validate_quantity(symbol, quantity, current_price)
        filters = {item["filterType"]: item for item in info["filters"]}
        pf = filters.get("PRICE_FILTER")
        if pf and "tickSize" in pf:
            tick_size = Decimal(pf["tickSize"])
            stop_price = self._floor_step(stop_price, tick_size)
            limit_price = self._floor_step(limit_price, tick_size)

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP_LOSS_LIMIT",
            "quantity": format(adjusted_qty, "f"),
            "stopPrice": format(stop_price, "f"),
            "price": format(limit_price, "f"),
            "timeInForce": "GTC",
            "newOrderRespType": "ACK",
        }
        return await self._signed_request("POST", "/api/v3/order", params)

    async def _await_terminal_order(
        self, symbol: str, client_order_id: str
    ) -> dict[str, Any] | None:
        terminal_statuses = {"FILLED", "CANCELED",
                             "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}
        for delay in (0.25, 0.5, 1.0, 2.0):
            await asyncio.sleep(delay)
            try:
                payload = await self.query_order(symbol, client_order_id)
            except BinanceApiError as exc:
                if exc.code == -2013 or exc.status_code is None or (exc.status_code or 0) >= 500:
                    continue
                raise
            if payload.get("status") in terminal_statuses:
                return payload
        return None

    async def _reconcile_order(self, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        for delay in (0.5, 1.0, 2.0, 4.0):
            await asyncio.sleep(delay)
            try:
                return await self.query_order(symbol, client_order_id)
            except BinanceApiError as exc:
                if exc.code == -2013 or exc.status_code is None or (exc.status_code or 0) >= 500:
                    continue
                raise
        return None

    @staticmethod
    def _order_result(
        payload: dict[str, Any], request: OrderRequest, fallback_price: Decimal, quote_asset: str
    ) -> OrderResult:
        executed = Decimal(payload.get("executedQty", "0"))
        quote = Decimal(payload.get("cummulativeQuoteQty", "0"))
        average = quote / executed if executed else fallback_price
        fee_quote = sum(
            (
                Decimal(str(fill.get("commission", "0")))
                for fill in payload.get("fills", [])
                if fill.get("commissionAsset") == quote_asset
            ),
            Decimal("0"),
        )
        return OrderResult(
            order_id=str(payload["orderId"]),
            client_order_id=str(payload.get(
                "clientOrderId", request.client_order_id)),
            symbol=request.symbol,
            side=request.side,
            status=str(payload["status"]),
            executed_quantity=executed,
            cumulative_quote_quantity=quote,
            average_price=average,
            created_at=datetime.now(UTC),
            is_simulated=False,
            reference_price=fallback_price,
            fee_quote=fee_quote,
            slippage_quote=executed * abs(average - fallback_price),
        )

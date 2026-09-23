from decimal import Decimal

import pytest

from guardian.domain.models import OrderRequest, Side
from guardian.domain.ports import OrderExecutionUnknown
from guardian.infrastructure.binance import BinanceApiError, BinanceSpotClient


@pytest.fixture
def client():
    instance = BinanceSpotClient("https://example.invalid", "key", "secret")
    yield instance


async def test_uncertain_submission_is_reconciled_without_resubmitting(client, monkeypatch):
    calls: list[tuple[str, str]] = []

    async def price(_symbol):
        return Decimal("100")

    async def validate(_symbol, quantity, _price):
        return quantity, {"quoteOrderQtyMarketAllowed": True}

    async def signed(method, path, params):
        calls.append((method, path))
        if method == "POST":
            raise BinanceApiError("gateway timeout", status_code=504)
        return {
            "orderId": 42,
            "clientOrderId": params["origClientOrderId"],
            "status": "FILLED",
            "executedQty": "0.1",
            "cummulativeQuoteQty": "10",
        }

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(client, "price", price)
    monkeypatch.setattr(client, "_validate_quantity", validate)
    monkeypatch.setattr(client, "_signed_request", signed)
    monkeypatch.setattr("guardian.infrastructure.binance.asyncio.sleep", no_sleep)

    result = await client.place_market_order(
        OrderRequest("BTCUSDT", Side.BUY, quote_amount=Decimal("10"), client_order_id="g-1")
    )

    assert result.order_id == "42"
    assert calls.count(("POST", "/api/v3/order")) == 1
    assert calls.count(("GET", "/api/v3/order")) == 1
    await client.close()


async def test_unresolved_submission_raises_safety_exception(client, monkeypatch):
    post_count = 0

    async def price(_symbol):
        return Decimal("100")

    async def validate(_symbol, quantity, _price):
        return quantity, {"quoteOrderQtyMarketAllowed": True}

    async def signed(method, _path, _params):
        nonlocal post_count
        if method == "POST":
            post_count += 1
            raise BinanceApiError("timeout", code=-1007, status_code=504)
        raise BinanceApiError("unknown order", code=-2013, status_code=400)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(client, "price", price)
    monkeypatch.setattr(client, "_validate_quantity", validate)
    monkeypatch.setattr(client, "_signed_request", signed)
    monkeypatch.setattr("guardian.infrastructure.binance.asyncio.sleep", no_sleep)

    with pytest.raises(OrderExecutionUnknown) as error:
        await client.place_market_order(
            OrderRequest("BTCUSDT", Side.BUY, quote_amount=Decimal("10"), client_order_id="g-2")
        )
    assert error.value.client_order_id == "g-2"
    assert post_count == 1
    await client.close()


async def test_partial_market_response_is_polled_until_terminal(client, monkeypatch):
    post_count = 0
    query_count = 0

    async def price(_symbol):
        return Decimal("100")

    async def validate(_symbol, quantity, _price):
        return quantity, {"quoteOrderQtyMarketAllowed": True}

    async def signed(method, _path, _params):
        nonlocal post_count, query_count
        base = {
            "orderId": 7,
            "clientOrderId": "g-partial",
            "executedQty": "0.05",
            "cummulativeQuoteQty": "5",
        }
        if method == "POST":
            post_count += 1
            return {**base, "status": "PARTIALLY_FILLED"}
        query_count += 1
        return {**base, "status": "FILLED", "executedQty": "0.1", "cummulativeQuoteQty": "10"}

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(client, "price", price)
    monkeypatch.setattr(client, "_validate_quantity", validate)
    monkeypatch.setattr(client, "_signed_request", signed)
    monkeypatch.setattr("guardian.infrastructure.binance.asyncio.sleep", no_sleep)

    result = await client.place_market_order(
        OrderRequest(
            "BTCUSDT",
            Side.BUY,
            quote_amount=Decimal("10"),
            client_order_id="g-partial",
        )
    )

    assert result.status == "FILLED"
    assert result.executed_quantity == Decimal("0.1")
    assert post_count == 1
    assert query_count == 1
    await client.close()

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from guardian.domain.models import OrderRequest, Side
from guardian.infrastructure.paper import PaperExchange
from guardian.infrastructure.repository import SqliteTradingRepository


class Market:
    calls = 0

    async def price(self, symbol):
        self.calls += 1
        return Decimal("100")


def setup_exchange():
    repository = SqliteTradingRepository(Path("data") / f"test-paper-{uuid4().hex}.db")
    repository.initialize()
    market = Market()
    return repository, market, PaperExchange(
        market, Decimal("1000"), Decimal("0.001"), repository,
        spread_bps=Decimal("0"), slippage_bps=Decimal("0"),
    )


@pytest.mark.asyncio
async def test_same_paper_order_is_idempotent_across_restart():
    repository, market, exchange = setup_exchange()
    request = OrderRequest("BTCUSDT", Side.BUY, quote_amount=Decimal("10"),
                           client_order_id="paper-stable-id")
    first = await exchange.place_market_order(request)
    assert market.calls == 1
    assert exchange.quote_balance == Decimal("990")

    restarted = PaperExchange(market, Decimal("1000"), Decimal("0.001"), repository,
                              spread_bps=Decimal("0"), slippage_bps=Decimal("0"))
    second = await restarted.place_market_order(request)
    assert second == first
    assert market.calls == 1
    assert restarted.quote_balance == Decimal("990")
    assert restarted.base_balance == first.executed_quantity


@pytest.mark.asyncio
async def test_paper_order_id_cannot_be_reused_for_different_request():
    repository, _, exchange = setup_exchange()
    await exchange.place_market_order(OrderRequest(
        "BTCUSDT", Side.BUY, quote_amount=Decimal("10"), client_order_id="same-id"
    ))
    restarted = PaperExchange(exchange.market_data, Decimal("1000"), Decimal("0.001"),
                              repository)
    with pytest.raises(ValueError, match="reutilizado"):
        await restarted.place_market_order(OrderRequest(
            "BTCUSDT", Side.BUY, quote_amount=Decimal("11"), client_order_id="same-id"
        ))
    assert restarted.quote_balance == Decimal("990")


@pytest.mark.asyncio
async def test_paper_order_requires_id_and_rejects_bad_quote_before_state_change():
    repository, market, exchange = setup_exchange()
    with pytest.raises(ValueError, match="identificador"):
        await exchange.place_market_order(OrderRequest(
            "BTCUSDT", Side.BUY, quote_amount=Decimal("10")
        ))
    async def bad_price(symbol):
        return Decimal("NaN")

    market.price = bad_price  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="Precio paper inválido"):
        await exchange.place_market_order(OrderRequest(
            "BTCUSDT", Side.BUY, quote_amount=Decimal("10"), client_order_id="bad-price"
        ))
    assert repository.get_state("paper_quote_balance", "1000") == "1000"
    assert repository.get_state("paper_base_balance", "0") == "0"

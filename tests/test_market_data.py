from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from guardian.application.trainer import ParameterTrainer
from guardian.domain.market_data import validate_candles
from guardian.domain.models import Candle


def bars():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [Candle(start + timedelta(minutes=i), Decimal("100"), Decimal("101"),
                   Decimal("99"), Decimal("100"), Decimal("2")) for i in range(10)]


@pytest.mark.parametrize("case", ["missing", "duplicate", "unordered", "nan", "infinite",
                                  "negative", "ohlc", "volume", "naive", "unaligned"])
def test_invalid_market_data_is_rejected(case):
    data = bars()
    if case == "missing":
        del data[3]
    elif case == "duplicate":
        data[3] = data[2]
    elif case == "unordered":
        data[2], data[3] = data[3], data[2]
    else:
        changes = {
            "nan": {"close": Decimal("NaN")},
            "infinite": {"high": Decimal("Infinity")},
            "negative": {"open": Decimal("-1")},
            "ohlc": {"low": Decimal("110")},
            "volume": {"volume": Decimal("-1")},
            "naive": {"open_time": data[3].open_time.replace(tzinfo=None)},
            "unaligned": {"open_time": data[3].open_time + timedelta(seconds=1)},
        }
        data[3] = replace(data[3], **changes[case])
    with pytest.raises(ValueError):
        validate_candles(data, "1m")
    with pytest.raises(ValueError):
        ParameterTrainer(Decimal("0.001"), minimum_samples=5).train(data)


def test_valid_market_data_and_insufficient_data():
    validate_candles(bars(), "1m")
    with pytest.raises(ValueError, match="insuficientes"):
        validate_candles([], "1m")

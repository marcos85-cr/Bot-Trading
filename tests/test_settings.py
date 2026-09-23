import pytest
from pydantic import ValidationError

from guardian.infrastructure.settings import Settings


def test_paper_defaults_are_safe():
    settings = Settings(_env_file=None, database_path="data/test.db")
    assert settings.trading_mode == "paper"
    assert not settings.enable_live_trading
    assert settings.app_host == "127.0.0.1"
    assert settings.paper_experimental_execution_enabled
    assert settings.training_candles == 35040
    assert settings.training_interval_hours == 24


def test_live_requires_multiple_safety_switches():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            trading_mode="live",
            binance_environment="mainnet",
            binance_api_key="x",
            binance_api_secret="y",  # noqa: S106 - inert validation fixture
            enable_live_trading=False,
        )


def test_remote_dashboard_requires_long_token():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_host="0.0.0.0",  # noqa: S104 - deliberate unsafe input under test
            dashboard_token="short",  # noqa: S106 - inert validation fixture
        )

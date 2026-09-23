from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Binance Guardian"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"
    dashboard_token: SecretStr | None = None
    database_path: Path = Path("data/guardian.db")
    local_timezone: str = "America/Costa_Rica"

    trading_mode: Literal["paper", "testnet", "live"] = "paper"
    binance_environment: Literal["testnet", "mainnet"] = "testnet"
    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    enable_live_trading: bool = False
    recv_window_ms: int = Field(default=5000, ge=1000, le=5000)

    trading_symbol: str = "BTCUSDT"
    trading_interval: str = "1m"
    loop_interval_seconds: int = Field(default=20, ge=10, le=3600)
    fast_sma_period: int = Field(default=7, ge=2, le=200)
    slow_sma_period: int = Field(default=25, ge=3, le=500)
    order_quote_amount: Decimal = Field(default=Decimal("10"), gt=0)

    # ─────────────────────────────────────────────────────────────────────────
    # CORRECCIÓN: Límites de riesgo como PORCENTAJE del equity, no valores fijos.
    #
    # ANTES (problemático para live):
    #   max_position_quote: Decimal = Decimal("25")   ← fijo: 25 USDT siempre
    #   max_daily_loss_quote: Decimal = Decimal("2")  ← fijo: 2 USDT siempre
    #
    # AHORA (escala con el capital real):
    #   max_daily_loss_pct = 2.0  → con 1,000 USDT = 20 USDT máx. pérdida diaria
    #                              → con 5,000 USDT = 100 USDT máx. pérdida diaria
    #   max_position_pct   = 5.0  → con 1,000 USDT = 50 USDT posición máxima
    #                              → con 5,000 USDT = 250 USDT posición máxima
    #
    # Los valores absolutos (max_position_quote, max_daily_loss_quote) ya NO se
    # leen del .env — se calculan en main.py con compute_risk_limits_from_equity().
    # ─────────────────────────────────────────────────────────────────────────
    max_daily_loss_pct: Decimal = Field(
        default=Decimal("2.0"), gt=0, le=Decimal("20"))
    max_position_pct: Decimal = Field(
        default=Decimal("5.0"), gt=0, le=Decimal("50"))

    # Mantenemos estos para compatibilidad con paper (capital ficticio fijo):
    max_daily_loss_quote: Decimal = Field(default=Decimal("2"),  gt=0)
    max_position_quote:   Decimal = Field(default=Decimal("25"), gt=0)

    max_trades_per_day: int = Field(default=6, ge=1, le=100)
    trade_cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    paper_starting_quote: Decimal = Field(default=Decimal("1000"), gt=0)
    paper_fee_rate: Decimal = Field(
        default=Decimal("0.001"), ge=0, le=Decimal("0.01"))
    paper_research_gate_enabled: bool = True
    paper_auto_deploy_experimental: bool = True
    paper_experimental_execution_enabled: bool = True

    # ─────────────────────────────────────────────────────────────────────────
    # CORRECCIÓN: Slippage subido de 1bps a 5bps para simulación más conservadora.
    #
    # ANTES: simulated_slippage_bps = 1  → 0.01% por fill (muy optimista)
    # AHORA: simulated_slippage_bps = 5  → 0.05% por fill (más realista en BTC)
    #
    # En momentos de alta volatilidad (noticias macro, liquidaciones masivas),
    # el slippage real en BTC puede ser 5-10x mayor que en condiciones normales.
    # Usar 5bps da un margen de seguridad mínimo razonable para backtests/paper.
    # ─────────────────────────────────────────────────────────────────────────
    simulated_spread_bps:   Decimal = Field(
        default=Decimal("1"), ge=0, le=Decimal("100"))
    simulated_slippage_bps: Decimal = Field(
        default=Decimal("5"), ge=0, le=Decimal("100"))

    stop_loss_pct:     Decimal = Field(
        default=Decimal("1.5"), gt=0, le=Decimal("20"))
    take_profit_pct:   Decimal = Field(
        default=Decimal("3.0"), gt=0, le=Decimal("50"))
    trailing_stop_pct: Decimal = Field(
        default=Decimal("1.2"), gt=0, le=Decimal("20"))
    auto_training_enabled: bool = True
    training_interval_hours: int = Field(default=24, ge=1, le=168)
    training_source_interval: Literal["1m", "5m", "15m"] = "15m"
    training_candles: int = Field(default=35040, ge=300, le=300000)
    minimum_validation_trades: int = Field(default=8, ge=3, le=1000)
    shadow_trading_enabled: bool = True
    shadow_starting_quote: Decimal = Field(default=Decimal("1000"), gt=0)
    shadow_cohort_min_age_hours: int = Field(default=168, ge=1, le=2160)
    shadow_min_completed_trades: int = Field(default=30, ge=5, le=1000)
    shadow_min_profit_factor: Decimal = Field(
        default=Decimal("1.10"), ge=1, le=10)

    @field_validator("trading_symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.isalnum() or len(normalized) > 20:
            raise ValueError("TRADING_SYMBOL is invalid")
        return normalized

    @field_validator("local_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("LOCAL_TIMEZONE is invalid") from exc
        return value

    @model_validator(mode="after")
    def security_invariants(self) -> Settings:
        if self.fast_sma_period >= self.slow_sma_period:
            raise ValueError(
                "FAST_SMA_PERIOD must be less than SLOW_SMA_PERIOD")
        if self.order_quote_amount > self.max_position_quote:
            raise ValueError(
                "ORDER_QUOTE_AMOUNT cannot exceed MAX_POSITION_QUOTE")
        if self.trading_mode in {"testnet", "live"} and (
            not self.binance_api_key or not self.binance_api_secret
        ):
            raise ValueError(
                "Authenticated modes require BINANCE_API_KEY and BINANCE_API_SECRET")
        if self.trading_mode == "testnet" and self.binance_environment != "testnet":
            raise ValueError(
                "testnet mode must use BINANCE_ENVIRONMENT=testnet")
        if self.trading_mode == "live" and (
            self.binance_environment != "mainnet" or not self.enable_live_trading
        ):
            raise ValueError(
                "live mode requires mainnet and ENABLE_LIVE_TRADING=true")
        if self.trading_mode == "live":
            raise ValueError(
                "live mode is intentionally disabled until exchange-native protection "
                "is implemented"
            )
        if self.app_host not in {"127.0.0.1", "localhost", "::1"}:
            token = self.dashboard_token.get_secret_value() if self.dashboard_token else ""
            if len(token) < 32:
                raise ValueError(
                    "A dashboard token of at least 32 chars is required off loopback")
        return self

    @property
    def rest_base_url(self) -> str:
        if self.binance_environment == "testnet":
            return "https://testnet.binance.vision"
        return "https://api.binance.com"

    @property
    def public_rest_base_url(self) -> str:
        # Paper mode uses the liquid mainnet public feed; it never sends an order.
        return (
            "https://data-api.binance.vision"
            if self.trading_mode == "paper"
            else self.rest_base_url
        )

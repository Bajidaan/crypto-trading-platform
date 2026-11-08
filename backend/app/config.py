"""Application configuration and environment settings."""

from decimal import Decimal
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application configuration loaded from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application Metadata
    app_name: str = "Crypto Trading Platform"
    environment: str = Field(default="development", description="development | production | test")
    debug: bool = False

    # Database Configuration (Default: local SQLite WAL; switchable to PostgreSQL via DATABASE_URL)
    database_url: str = Field(
        default="sqlite+aiosqlite:///./trading_platform.db",
        description="SQLAlchemy async connection string",
    )

    # Security & Vault Configuration
    master_vault_secret: str = Field(
        default="dev-only-change-in-production-master-vault-secret-32b!",
        min_length=16,
        description="Master secret used to derive AES-256-GCM encryption keys for API credentials",
    )
    webhook_hmac_secret: str = Field(
        default="dev-only-change-in-production-webhook-hmac-secret!",
        min_length=16,
        description="Shared secret for verifying incoming TradingView/signal webhooks",
    )
    webhook_max_timestamp_skew_seconds: int = Field(
        default=300,
        ge=10,
        le=3600,
        description="Maximum allowed age (in seconds) for signed webhook requests",
    )

    # Default Risk & Circuit Breaker Settings (Phase 1 Locked Decisions)
    default_risk_per_trade_pct: Decimal = Field(default=Decimal("1.0"))
    default_max_concurrent_positions: int = Field(default=5, ge=1, le=50)
    default_max_daily_drawdown_pct: Decimal = Field(default=Decimal("5.0"))
    default_futures_leverage: int = Field(default=3, ge=1, le=20)
    default_max_leverage_cap: int = Field(default=10, ge=1, le=50)

    # Notification Channels (Telegram / Discord)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()

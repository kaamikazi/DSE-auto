from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore", case_sensitive=True)

    APP_ENV: Literal["development", "test", "production"] = "development"
    DATABASE_URL: str = "sqlite:///./data/dse_autotrader.db"
    REDIS_URL: str | None = None
    TRADING_MODE: Literal["paper", "live"] = "paper"
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None
    TELEGRAM_ALLOWED_CHAT_IDS: str = ""
    DATA_PRIMARY_PROVIDER: str = "mock"
    DATA_SECONDARY_PROVIDER: str = "csv"
    DATA_MAX_STALENESS_SECONDS: int = Field(default=30, ge=1)
    DATA_MAX_PROVIDER_DISAGREEMENT_PERCENT: float = Field(default=1.0, ge=0)
    PAPER_STARTING_CASH_BDT: float = Field(default=1_000_000, gt=0)
    API_SECRET_KEY: str = "development-only-secret-change-me"
    BROKER_ADAPTER: str = "disabled"
    LIVE_TRADING_ENABLED: bool = False
    ALLOWED_ORIGINS: str = "http://localhost:3000"
    CSV_DATA_DIR: Path = Path("./data/imports")
    LOG_LEVEL: str = "INFO"
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_STALE_AFTER_SECONDS: int = Field(default=3600, ge=60)
    PAPER_SESSION_STALE_AFTER_SECONDS: int = Field(default=300, ge=60)
    PAPER_FILL_MODEL: Literal["pessimistic", "balanced", "optimistic"] = "pessimistic"
    TEST_ONLY_MARKET_ORDERS_ENABLED: bool = False
    MARKET_CALENDAR_PATH: Path = Path("../config/dse_market_calendar.yaml")
    DSE_HOLIDAYS_PATH: Path = Path("../data/imports/dse_holidays.csv")
    BDSHARE_PRIMARY_ENDPOINT: str = "https://dsebd.org/"
    BDSHARE_SECONDARY_ENDPOINT: str = "https://dsebd.com.bd/"
    DSE_CUSTOM_CA_BUNDLE: Path | None = None

    @field_validator("API_SECRET_KEY")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if len(value) < 24:
            raise ValueError("API_SECRET_KEY must contain at least 24 characters")
        return value

    @model_validator(mode="after")
    def fail_closed_live_configuration(self) -> Settings:
        if self.TRADING_MODE == "live" or self.LIVE_TRADING_ENABLED:
            raise ValueError(
                "Live trading is disabled; use TRADING_MODE=paper and LIVE_TRADING_ENABLED=false"
            )
        if self.BROKER_ADAPTER not in {"disabled", "paper"}:
            raise ValueError("Only the disabled or paper broker adapter is permitted")
        forbidden = ("selenium", "playwright", "otp", "captcha", "broker_web", "unofficial")
        if any(marker in self.BROKER_ADAPTER.lower() for marker in forbidden):
            raise ValueError("Unofficial broker automation is forbidden")
        if self.TEST_ONLY_MARKET_ORDERS_ENABLED and self.APP_ENV != "test":
            raise ValueError("Market orders may only be enabled in the test environment")
        for endpoint in (self.BDSHARE_PRIMARY_ENDPOINT, self.BDSHARE_SECONDARY_ENDPOINT):
            if not endpoint.startswith("https://"):
                raise ValueError("DSE provider endpoints must use HTTPS")
        if self.DSE_CUSTOM_CA_BUNDLE and not self.DSE_CUSTOM_CA_BUNDLE.is_file():
            raise ValueError("DSE_CUSTOM_CA_BUNDLE must reference an existing CA bundle")
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.ALLOWED_ORIGINS.split(",") if item.strip()]

    @property
    def telegram_allowed_chat_ids(self) -> set[str]:
        configured = {
            item.strip() for item in self.TELEGRAM_ALLOWED_CHAT_IDS.split(",") if item.strip()
        }
        if self.TELEGRAM_CHAT_ID:
            configured.add(self.TELEGRAM_CHAT_ID.strip())
        return configured


@lru_cache
def get_settings() -> Settings:
    return Settings()

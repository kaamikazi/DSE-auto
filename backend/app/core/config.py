from __future__ import annotations

import hmac
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
    DATABASE_POOL_SIZE: int = Field(default=10, ge=1, le=100)
    DATABASE_MAX_OVERFLOW: int = Field(default=20, ge=0, le=200)
    DATABASE_POOL_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=300)
    DATABASE_POOL_RECYCLE_SECONDS: int = Field(default=1800, ge=60)
    DATABASE_TRANSACTION_RETRIES: int = Field(default=3, ge=0, le=10)
    DATABASE_RETRY_BACKOFF_SECONDS: float = Field(default=0.1, ge=0, le=10)
    DATABASE_ISOLATION_LEVEL: Literal["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"] = (
        "READ COMMITTED"
    )
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
    REVIEWER_API_SECRET_KEY: str = "development-reviewer-secret-change-me"
    SESSION_TTL_SECONDS: int = Field(default=3600, ge=60, le=86400)
    LOGIN_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=100)
    LOGIN_WINDOW_SECONDS: int = Field(default=300, ge=1, le=3600)
    APP_BIND_HOST: str = "127.0.0.1"
    BROKER_ADAPTER: str = "disabled"
    LIVE_TRADING_ENABLED: bool = False
    ALLOWED_ORIGINS: str = "http://localhost:3000"
    CSV_DATA_DIR: Path = Path("./data/imports")
    LOG_LEVEL: str = "INFO"
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_MODE: Literal["in_process", "external"] = "in_process"
    TASK_QUEUE_NAME: str = "dse-paper-tasks"
    TASK_LEASE_SECONDS: int = Field(default=120, ge=10, le=3600)
    TASK_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=20)
    WORKER_HEARTBEAT_SECONDS: int = Field(default=15, ge=1, le=300)
    WORKER_STALE_AFTER_SECONDS: int = Field(default=60, ge=10, le=3600)
    SCHEDULER_STALE_AFTER_SECONDS: int = Field(default=3600, ge=60)
    PAPER_SESSION_STALE_AFTER_SECONDS: int = Field(default=300, ge=60)
    PAPER_FILL_MODEL: Literal["pessimistic", "balanced", "optimistic"] = "pessimistic"
    TEST_ONLY_MARKET_ORDERS_ENABLED: bool = False
    MARKET_CALENDAR_PATH: Path = Path("../config/dse_market_calendar.yaml")
    DSE_HOLIDAYS_PATH: Path = Path("../data/imports/dse_holidays.csv")
    BDSHARE_PRIMARY_ENDPOINT: str = "https://dsebd.org/"
    BDSHARE_SECONDARY_ENDPOINT: str = "https://dsebd.com.bd/"
    DSE_CUSTOM_CA_BUNDLE: Path | None = None

    @field_validator("API_SECRET_KEY", "REVIEWER_API_SECRET_KEY")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if len(value) < 24:
            raise ValueError("API and reviewer secrets must contain at least 24 characters")
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
        if self.APP_ENV == "production":
            if not self.DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("Production requires PostgreSQL; SQLite is development/test only")
            if not self.REDIS_URL:
                raise ValueError("Production requires REDIS_URL for the external task queue")
            if self.SCHEDULER_MODE != "external" or self.SCHEDULER_ENABLED:
                raise ValueError("Production requires the external scheduler process")
            weak_markers = ("development", "change-me", "password", "secret")
            if any(marker in self.API_SECRET_KEY.lower() for marker in weak_markers):
                raise ValueError("Production refuses weak operator credentials")
            if any(marker in self.REVIEWER_API_SECRET_KEY.lower() for marker in weak_markers):
                raise ValueError("Production refuses weak reviewer credentials")
            if hmac.compare_digest(self.API_SECRET_KEY, self.REVIEWER_API_SECRET_KEY):
                raise ValueError("Operator and reviewer credentials must be distinct")
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

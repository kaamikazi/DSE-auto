import os
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DB = Path(__file__).parent / "test.db"
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{TEST_DB.as_posix()}",
        "TRADING_MODE": "paper",
        "LIVE_TRADING_ENABLED": "false",
        "DATA_PRIMARY_PROVIDER": "mock",
        "DATA_SECONDARY_PROVIDER": "csv",
        "API_SECRET_KEY": "test-secret-key-at-least-32-characters",
    }
)

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database() -> Generator[None, None, None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():  # type: ignore[no-untyped-def]
    with SessionLocal() as session:
        yield session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-secret-key-at-least-32-characters"}

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.services.task_queue import RedisBroker

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
REDIS_URL = os.getenv("TEST_REDIS_URL")


@pytest.mark.integration
@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_clean_migration_downgrade_and_reupgrade() -> None:
    assert POSTGRES_URL is not None
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", POSTGRES_URL)
    command.upgrade(config, "head")
    engine = create_engine(POSTGRES_URL)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
        assert connection.scalar(text("SELECT 1")) == 1
    command.downgrade(config, "0007")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
    engine.dispose()


@pytest.mark.integration
@pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL is not configured")
def test_redis_broker_round_trip_and_duplicate_delivery() -> None:
    assert REDIS_URL is not None
    broker = RedisBroker(REDIS_URL, "m7-integration-test")
    broker.client.delete("m7-integration-test")
    broker.push("task-one")
    broker.push("task-one")
    assert broker.pop() == "task-one"
    assert broker.pop() == "task-one"
    assert broker.health()["healthy"] is True
    broker.client.delete("m7-integration-test")

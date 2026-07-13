from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import Engine, MetaData, Table, create_engine, func, insert, select

from app.core.database import Base


def redact_database_url(url: str) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if parts.port:
        hostname = f"{hostname}:{parts.port}"
    username = parts.username or ""
    netloc = f"{username}:***@{hostname}" if username else hostname
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def table_fingerprint(engine: Engine, table: Table) -> tuple[int, str]:
    primary_keys = list(table.primary_key.columns)
    query = select(table)
    if primary_keys:
        query = query.order_by(*primary_keys)
    digest = hashlib.sha256()
    count = 0
    with engine.connect() as connection:
        for row in connection.execute(query).mappings():
            canonical = json.dumps(
                dict(row), sort_keys=True, separators=(",", ":"), default=_json_default
            )
            digest.update(canonical.encode())
            digest.update(b"\n")
            count += 1
    return count, digest.hexdigest()


def database_fingerprints(engine: Engine) -> tuple[dict[str, int], dict[str, str]]:
    counts: dict[str, int] = {}
    hashes: dict[str, str] = {}
    for table in Base.metadata.sorted_tables:
        try:
            counts[table.name], hashes[table.name] = table_fingerprint(engine, table)
        except Exception:
            continue
    return counts, hashes


def migrate_sqlite_to_postgresql(
    source_url: str,
    destination_url: str,
    *,
    dry_run: bool = True,
    allow_test_destination: bool = False,
) -> dict[str, Any]:
    if not source_url.startswith("sqlite:///"):
        raise ValueError("Migration source must be SQLite")
    if not allow_test_destination and not destination_url.startswith(
        ("postgresql://", "postgresql+psycopg://")
    ):
        raise ValueError("Migration destination must be PostgreSQL")
    source = create_engine(source_url)
    destination = create_engine(destination_url)
    source_counts, source_hashes = database_fingerprints(source)
    result: dict[str, Any] = {
        "source": redact_database_url(source_url),
        "destination": redact_database_url(destination_url),
        "dry_run": dry_run,
        "source_counts": source_counts,
        "source_hashes": source_hashes,
        "copied": False,
        "verified": False,
    }
    if dry_run:
        result["preflight_passed"] = bool(source_counts)
        return result
    Base.metadata.create_all(destination)
    destination_metadata = MetaData()
    destination_metadata.reflect(bind=destination)
    with destination.begin() as connection:
        for source_table in Base.metadata.sorted_tables:
            if source_table.name not in destination_metadata.tables:
                raise RuntimeError(f"Destination is missing table {source_table.name}")
            destination_table = destination_metadata.tables[source_table.name]
            existing = connection.scalar(select(func.count()).select_from(destination_table))
            if existing:
                raise RuntimeError(f"Destination table {source_table.name} is not empty")
            with source.connect() as source_connection:
                rows = [
                    dict(row) for row in source_connection.execute(select(source_table)).mappings()
                ]
            if rows:
                connection.execute(insert(destination_table), rows)
    destination_counts, destination_hashes = database_fingerprints(destination)
    result.update(
        {
            "copied": True,
            "destination_counts": destination_counts,
            "destination_hashes": destination_hashes,
            "count_match": source_counts == destination_counts,
            "hash_match": source_hashes == destination_hashes,
        }
    )
    result["verified"] = bool(result["count_match"] and result["hash_match"])
    return result

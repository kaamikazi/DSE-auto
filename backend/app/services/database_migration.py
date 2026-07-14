from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import Engine, MetaData, Table, create_engine, func, insert, inspect, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401  # Register all mapped tables in Base.metadata.
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
    if isinstance(value, datetime):
        normalized = value
        if value.tzinfo is not None:
            normalized = value.astimezone(UTC).replace(tzinfo=None)
        return normalized.isoformat(timespec="microseconds")
    if isinstance(value, date):
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


def database_constraint_signatures(engine: Engine) -> dict[str, Any]:
    """Return dialect-independent foreign-key and uniqueness semantics."""

    inspector = inspect(engine)
    expected_tables = sorted(table.name for table in Base.metadata.sorted_tables)
    actual_tables = sorted(
        table for table in inspector.get_table_names() if table != "alembic_version"
    )
    foreign_keys: dict[str, list[dict[str, Any]]] = {}
    unique_constraints: dict[str, list[list[str]]] = {}
    for table in expected_tables:
        foreign_keys[table] = sorted(
            (
                {
                    "columns": list(item.get("constrained_columns") or []),
                    "referred_table": item.get("referred_table"),
                    "referred_columns": list(item.get("referred_columns") or []),
                }
                for item in inspector.get_foreign_keys(table)
            ),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
        unique_columns = {
            tuple(
                sorted(
                    str(column) for column in (item.get("column_names") or []) if column is not None
                )
            )
            for item in inspector.get_unique_constraints(table)
        }
        unique_columns.update(
            tuple(
                sorted(
                    str(column) for column in (item.get("column_names") or []) if column is not None
                )
            )
            for item in inspector.get_indexes(table)
            if item.get("unique")
        )
        unique_constraints[table] = [list(columns) for columns in sorted(unique_columns)]
    return {
        "expected_tables": expected_tables,
        "actual_tables": actual_tables,
        "tables_match": expected_tables == actual_tables,
        "foreign_keys": foreign_keys,
        "unique_constraints": unique_constraints,
    }


def compare_database_fingerprints(source: Engine, destination: Engine) -> dict[str, Any]:
    source_counts, source_hashes = database_fingerprints(source)
    destination_counts, destination_hashes = database_fingerprints(destination)
    count_mismatches = {
        table: {"source": source_counts.get(table), "destination": destination_counts.get(table)}
        for table in sorted(set(source_counts) | set(destination_counts))
        if source_counts.get(table) != destination_counts.get(table)
    }
    hash_mismatches = {
        table: {"source": source_hashes.get(table), "destination": destination_hashes.get(table)}
        for table in sorted(set(source_hashes) | set(destination_hashes))
        if source_hashes.get(table) != destination_hashes.get(table)
    }
    return {
        "verified": not count_mismatches and not hash_mismatches,
        "count_mismatches": count_mismatches,
        "hash_mismatches": hash_mismatches,
        "fail_closed": bool(count_mismatches or hash_mismatches),
    }


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
    source_constraints = database_constraint_signatures(source)
    result: dict[str, Any] = {
        "source": redact_database_url(source_url),
        "destination": redact_database_url(destination_url),
        "dry_run": dry_run,
        "source_counts": source_counts,
        "source_hashes": source_hashes,
        "source_constraints": source_constraints,
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
    destination_constraints = database_constraint_signatures(destination)
    from app.services.audit import verify_audit_chain

    with Session(source) as source_session, Session(destination) as destination_session:
        source_audit_valid = verify_audit_chain(source_session)
        destination_audit_valid = verify_audit_chain(destination_session)
    critical_tables = (
        "audit_chains",
        "audit_events",
        "validation_campaigns",
        "campaign_days",
        "operational_incidents",
        "evidence_reviews",
        "paper_qualifications",
        "market_rule_sets",
        "fee_profiles",
        "strategy_registrations",
    )
    constraints_match = (
        source_constraints["foreign_keys"] == destination_constraints["foreign_keys"]
        and source_constraints["unique_constraints"]
        == destination_constraints["unique_constraints"]
        and source_constraints["tables_match"]
        and destination_constraints["tables_match"]
    )
    result.update(
        {
            "copied": True,
            "destination_counts": destination_counts,
            "destination_hashes": destination_hashes,
            "destination_constraints": destination_constraints,
            "count_match": source_counts == destination_counts,
            "hash_match": source_hashes == destination_hashes,
            "constraints_match": constraints_match,
            "source_audit_valid": source_audit_valid,
            "destination_audit_valid": destination_audit_valid,
            "critical_table_counts": {
                table: {
                    "source": source_counts.get(table, 0),
                    "destination": destination_counts.get(table, 0),
                }
                for table in critical_tables
            },
        }
    )
    result["verified"] = bool(
        result["count_match"]
        and result["hash_match"]
        and result["constraints_match"]
        and source_audit_valid
        and destination_audit_valid
    )
    return result

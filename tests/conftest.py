"""Shared fixtures.

Database fixtures live here rather than under tests/db/ because every service that writes
rows -- ingestion, policy, ERP -- needs them, not just the schema tests.

These talk to the real PostgreSQL from `make up`.

Every test runs inside a transaction that is rolled back afterwards, so tests neither see
each other's rows nor leave any behind. The exception is anything exercising the append-only
trigger, which needs its own connection because the failed statement aborts the transaction.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from sentinel.core.settings import Settings
from sentinel.db.base import build_engine

TABLES_IN_DEPENDENCY_ORDER = (
    "goods_receipt_lines",
    "goods_receipts",
    "purchase_order_lines",
    "purchase_orders",
    "vendor_bank_accounts",
    "vendor_contracts",
    "invoices",
    "dead_letters",
    "erp_transactions",
    "policy_versions",
    "vendors",
)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    settings = Settings(env="test", _env_file=None)  # type: ignore[call-arg]
    built = build_engine(settings)
    try:
        with built.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover -- environment, not logic
        pytest.skip(f"PostgreSQL is not reachable ({exc}); run `make up`")
    yield built
    built.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose work is always rolled back."""
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, expire_on_commit=False)
    try:
        yield db
    finally:
        db.close()
        # A test that provoked an IntegrityError has already had its transaction aborted,
        # so rolling back again would warn about a deassociated transaction.
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def raw_connection(engine: Engine) -> Iterator[Engine]:
    """An engine for statements expected to fail at the database level.

    A refused statement aborts its transaction, so these cannot share the rolled-back
    session. Rows they create are cleaned explicitly.
    """
    yield engine
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        )
        for table in (*TABLES_IN_DEPENDENCY_ORDER, "audit_events"):
            connection.execute(text(f"DELETE FROM {table}"))  # noqa: S608 -- fixed name list
        connection.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only"))

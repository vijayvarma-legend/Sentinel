"""Wiring. Constructs services and hands them a request-scoped session.

Nothing here decides anything (ADR-0008) -- it builds objects and manages lifetimes.

The engine, session factory, and document store are process-wide and built once; the session
is per-request and always closed. A session that leaks across requests would let one
invoice's uncommitted work become visible to another's, which in a financial ledger is the
kind of bug that is discovered in an audit rather than in a test.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from sentinel.core.errors import IngestionError
from sentinel.core.settings import Settings, get_settings
from sentinel.db.base import build_engine, session_factory
from sentinel.db.repositories import AuditRepository, InvoiceRepository
from sentinel.ingestion.service import IngestionService
from sentinel.storage.store import DocumentStore, S3DocumentStore

__all__ = [
    "SessionDep",
    "SettingsDep",
    "get_document_store",
    "get_ingestion_service",
    "get_session",
]


@lru_cache(maxsize=1)
def _engine() -> Engine:
    return build_engine(get_settings())


@lru_cache(maxsize=1)
def _sessions() -> sessionmaker[Session]:
    return session_factory(_engine())


@lru_cache(maxsize=1)
def get_document_store() -> DocumentStore:
    return S3DocumentStore.from_settings(get_settings())


def get_session() -> Iterator[Session]:
    """A request-scoped session. The transaction boundary is the request.

    Rolled back on any fault, so an endpoint that half-succeeds leaves nothing behind --
    the only safe default when a later stage may move money.

    **Except for an IngestionError**, which is committed. That exception is not a fault: it
    is ingestion reporting a decision it has already made and recorded. Spec §4.1 requires
    unsupported documents to be routed to a dead-letter path, and the 422 response hands the
    caller a correlation ID to quote. Rolling that back erases the record the response points
    at, turning a documented rejection into a silent drop -- which is exactly the behaviour
    the dead-letter path exists to prevent.

    The distinction is the one drawn in :mod:`sentinel.core.errors`: system faults abort,
    business outcomes persist.
    """
    session = _sessions()()
    try:
        yield session
        session.commit()
    except IngestionError:
        session.commit()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_ingestion_service(
    session: SessionDep,
    settings: SettingsDep,
    store: Annotated[DocumentStore, Depends(get_document_store)],
) -> IngestionService:
    return IngestionService(
        store=store,
        invoices=InvoiceRepository(session),
        audit=AuditRepository(session),
        max_bytes=settings.max_document_bytes,
    )

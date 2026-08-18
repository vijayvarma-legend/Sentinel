"""Engine, session, and the column conventions the schema is built from.

Money is stored as ``NUMERIC(18, 2)`` alongside a separate three-character currency column --
never as a float, and never as a bare number without its currency. The database is where data
outlives the process that wrote it, so the guarantees :class:`~sentinel.core.money.Money`
makes in memory have to be restated here or they end at the connection boundary.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated

from sqlalchemy import DateTime, MetaData, Numeric, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, mapped_column, sessionmaker

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import Engine

    from sentinel.core.settings import Settings

__all__ = [
    "Amount",
    "Base",
    "Currency",
    "Identifier",
    "Quantity",
    "Timestamp",
    "build_engine",
    "session_factory",
    "session_scope",
]

#: Explicit constraint naming so Alembic can autogenerate reversible migrations. Without it,
#: dropping an unnamed constraint means knowing the name PostgreSQL happened to invent.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# -- column types ------------------------------------------------------------------------

Amount = Annotated[Decimal, mapped_column(Numeric(18, 2))]
"""An amount in minor units.

NUMERIC, never DOUBLE PRECISION: PostgreSQL's float types carry the same representation error
as Python's, and a tolerance comparison against a drifted value is an approval decision made
on noise.
"""

Quantity = Annotated[Decimal, mapped_column(Numeric(18, 4))]
"""A quantity. Four decimal places, because invoices bill fractional units."""

Currency = Annotated[str, mapped_column(String(3))]
"""An ISO 4217 code, stored beside every amount so a bare number is never ambiguous."""

Identifier = Annotated[str, mapped_column(String(64))]
"""A prefixed identifier from :mod:`sentinel.core.ids`."""

Timestamp = Annotated[dt.datetime, mapped_column(DateTime(timezone=True))]
"""An instant, always timezone-aware.

A naive timestamp cannot be ordered against events from another region, and spec §12 requires
the audit trail to be orderable.
"""


# -- engine and session ------------------------------------------------------------------


def build_engine(settings: Settings, *, echo: bool = False) -> Engine:
    """A connection pool for `settings`.

    ``pool_pre_ping`` is on because an invoice can sit in a human review queue for hours. The
    connection that eventually posts it must not be a stale socket that fails *after* the ERP
    call has already been made.
    """
    return create_engine(
        str(settings.database_url),
        echo=echo,
        pool_pre_ping=True,
        future=True,
    )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """A transactional scope: commit on success, roll back on any exception.

    Explicit rather than autocommit. A partial write to a financial record is worse than no
    write at all, so every caller should be able to see exactly where its transaction
    boundary sits.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

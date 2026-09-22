"""Database session management with mandatory tenant scoping.

Every request-scoped session opens a transaction and pins `app.clinic_id` for
its lifetime. The row level security policies read that setting, so a query that
forgets its WHERE clause still cannot see another clinic's rows.

Two details that are easy to get wrong and expensive to get wrong:

* `set_config(..., is_local => true)` is the transaction-scoped form. The
  session-scoped form would survive the connection's return to the pool and
  leak the previous request's tenant into the next one.
* The application role must not own the tables, and the tables are additionally
  marked FORCE ROW LEVEL SECURITY. A table owner bypasses RLS by default, which
  makes policies look correct in tests while doing nothing in production.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    echo=False,
)

SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

SET_TENANT = text("SELECT set_config('app.clinic_id', :clinic_id, true)")


@asynccontextmanager
async def tenant_session(clinic_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """Open a transaction scoped to one clinic. Commits on success, rolls back on error."""
    async with SessionFactory() as session, session.begin():
        await session.execute(SET_TENANT, {"clinic_id": str(clinic_id)})
        yield session


@asynccontextmanager
async def unscoped_session() -> AsyncIterator[AsyncSession]:
    """For the few operations that legitimately precede tenant resolution.

    Only `clinics` is readable here; RLS still blocks every tenant-scoped table
    because `app.clinic_id` is unset, which the policies treat as "match nothing".
    """
    async with SessionFactory() as session, session.begin():
        yield session

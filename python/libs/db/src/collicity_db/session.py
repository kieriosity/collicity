"""Tenant-scoped transactions (SEC-09).

Every service reads and writes tenant data only inside ``tenant_session`` or
``tenant_connection``. Each sets the tenant and acting person with ``set_config(…, true)``, so the
setting lives exactly as long as the transaction: a pooled connection never carries one request's
tenant into the next. Row-level security reads the settings through ``current_tenant_id()`` and
``current_principal_id()``; with no setting, every tenant table reads as empty.
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from collicity_core import TenantContext

_SET_CONTEXT = text(
    "SELECT set_config('app.tenant_id', :tenant_id, true), "
    "set_config('app.principal_id', :principal_id, true)"
)


def _context_params(ctx: TenantContext) -> dict[str, str]:
    return {
        "tenant_id": str(ctx.tenant_id),
        "principal_id": "" if ctx.principal_id is None else str(ctx.principal_id),
    }


def make_engine(url: str, **kwargs: Any) -> Engine:
    """Create an engine for one service role. Pings pooled connections before use."""
    return create_engine(url, pool_pre_ping=True, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def tenant_session(factory: sessionmaker[Session], ctx: TenantContext) -> Generator[Session]:
    """An ORM session in one transaction scoped to ``ctx``; commits on success.

    Don't call ``session.commit()`` inside the block: the next transaction would start without a
    tenant, and reads would come back empty.
    """
    with factory.begin() as session:
        session.execute(_SET_CONTEXT, _context_params(ctx))
        yield session


@contextmanager
def tenant_connection(engine: Engine, ctx: TenantContext) -> Generator[Connection]:
    """A Core connection in one transaction scoped to ``ctx``; commits on success."""
    with engine.begin() as conn:
        conn.execute(_SET_CONTEXT, _context_params(ctx))
        yield conn

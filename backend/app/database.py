"""Database engine, session factory and shared column types (data access layer)."""
from datetime import datetime, timezone

from fastapi import Depends
from sqlalchemy import DateTime, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC timestamps on every dialect (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    settings = get_settings()
    url = url or settings.database_url
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    # Pooled connections: instances can be added behind a load balancer (NFR-08).
    return create_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def configure(url: str):
    """Rebind the engine (used by tests and scripts)."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)
    return engine


async def get_db():
    """One session per request.

    Deliberately an async generator: its clean-up then runs on the event loop
    as soon as the response is ready. As a plain generator the clean-up would
    queue for a worker thread behind waiting requests, and under a burst those
    requests would wait for connections that finished requests still held.
    The session opens its connection lazily, on the first query in the handler.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _session_limit() -> int:
    settings = get_settings()
    return max(2, settings.db_pool_size + settings.db_max_overflow - 5)


# The one dependency object every route shares. At most as many requests hold a session at once
# as there are pooled connections (less a few kept for background work); further requests wait
# their turn without occupying a thread or a connection. Without this bound, a burst can leave
# finished requests holding connections while they queue for a thread, and running requests
# holding threads while they queue for a connection.
DbSession = Depends(get_db, max_concurrency=_session_limit())

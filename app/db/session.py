"""Engine + session factory + FastAPI `get_db` dependency."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        # check_same_thread=False — required for FastAPI multi-request usage.
        # timeout=30 — lets writers wait up to 30s for the lock instead of
        #   immediately raising "database is locked" when the indexer cycle
        #   and an admin endpoint try to write concurrently.
        return {"connect_args": {"check_same_thread": False, "timeout": 30}}
    return {}


engine = create_engine(settings.database_url, future=True, **_engine_kwargs(settings.database_url))

# Enable SQLite WAL so readers don't block the single writer (indexer cycle)
# and admin endpoint writes can queue cleanly instead of immediately raising.
if settings.database_url.startswith("sqlite"):
    from sqlalchemy import event
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

"""Getting a database session.

Default is a SQLite file, so you can clone this repo and run it with no
database server at all. Point `RECON_DATABASE_URL` at Postgres and nothing else
changes; the models use no dialect-specific types.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from recon.config import get_settings
from recon.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(url: str | None = None) -> Engine:
    global _engine, _session_factory
    if url is not None:
        return _build_engine(url)
    if _engine is None:
        _engine = _build_engine(get_settings().database_url)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _build_engine(url: str) -> Engine:
    # check_same_thread is a SQLite-only quirk: FastAPI's threadpool hands the
    # connection to a different thread than the one that opened it.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, future=True, connect_args=connect_args)


def init_db(url: str | None = None) -> Engine:
    """Create the tables if they are not there yet."""
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that commits on success and rolls back on any error.

    There is deliberately no `except Exception: pass` in here. A money error
    should reach the caller with the transaction rolled back, not be swallowed
    and leave half a reconciliation written.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop the cached engine. Tests call this between databases."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None

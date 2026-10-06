from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from recon.models import Base


@pytest.fixture
def session() -> Iterator[Session]:
    """A throwaway in-memory database, fresh for every test."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def own_books(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The app's own database: empty, in a temporary file, demo switched off.

    A file rather than :memory: because the workspace and the pages each open
    their own session, and an in-memory database is not shared between them.
    """
    from recon import db as db_module
    from recon import state
    from recon.config import get_settings

    monkeypatch.setenv("RECON_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'books.db'}")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_books")
    monkeypatch.setenv("RECON_OFFLINE", "1")
    monkeypatch.delenv("RECON_DEMO", raising=False)
    get_settings.cache_clear()
    db_module.reset_engine()
    state.reset()
    db_module.init_db()

    yield

    state.reset()
    db_module.reset_engine()
    get_settings.cache_clear()

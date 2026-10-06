from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
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
    from recon import accounts, state
    from recon import db as db_module
    from recon.config import get_settings

    accounts.forget_failures()
    monkeypatch.delenv("RECON_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("RECON_PASSWORD", raising=False)
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


ADMIN_EMAIL = "boss@example.com"
ADMIN_PASSWORD = "correct horse battery"


@pytest.fixture
def admin_client(own_books: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app on its own books, with the admin made at startup and logged in."""
    from recon.app import app
    from recon.config import get_settings

    monkeypatch.setenv("RECON_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("RECON_PASSWORD", ADMIN_PASSWORD)
    get_settings.cache_clear()
    with TestClient(app) as client:
        response = client.post(
            "/login",
            data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        yield client

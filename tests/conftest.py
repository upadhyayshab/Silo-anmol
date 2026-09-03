"""
Shared pytest fixtures.

Every test that needs a database uses an isolated in-memory SQLite database via
the `client` fixture below - never the real Postgres database configured in .env.
FastAPI's dependency_overrides mechanism swaps out get_db() for the duration of
each test, so nothing here can ever read or write your production data.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

import models
from database import get_db

TEST_DASHBOARD_PASSWORD = "changeme"


@pytest.fixture()
def db_session():
    """A fresh in-memory SQLite database, created and torn down per test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session, monkeypatch):
    """A TestClient wired to the isolated test database instead of the real one,
    with a known password so login-based tests don't depend on your real .env.

    Deliberately does NOT run the app's real ASGI lifespan (no `with` block):
    main.py's lifespan() calls run_automation_cycle() synchronously at startup,
    which would otherwise make every single test trigger a real, many-minute
    scrape across YouTube/Facebook/Instagram/PlayStore/News. Individual routes
    still work fine without lifespan having run - nothing here reads app.state."""
    monkeypatch.setattr("auth.DASHBOARD_PASSWORD", TEST_DASHBOARD_PASSWORD)
    monkeypatch.setattr("auth.JWT_SECRET", "test-signing-secret")

    import main

    # Defense in depth: even though the fixture avoids triggering lifespan (see
    # docstring), make sure a real automation cycle can never fire from a test.
    monkeypatch.setattr(main, "run_automation_cycle", lambda: None)

    def override_get_db():
        yield db_session

    main.app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(main.app)
    yield test_client
    main.app.dependency_overrides.clear()


@pytest.fixture()
def auth_headers(client):
    """A valid Authorization header, obtained via a real login call."""
    res = client.post("/api/login", json={"password": TEST_DASHBOARD_PASSWORD})
    token = res.json()["token"]
    return {"Authorization": f"Bearer {token}"}

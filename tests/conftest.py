"""Shared pytest fixtures for SafeRoute tests.

Sets DATABASE_URL to SQLite in-memory before any import so the whole app
stack runs without PostgreSQL, network, or external services.
"""
import os
import pytest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret-key-long-enough-32chars!!"
os.environ["ADMIN_USERNAMES"] = "testadmin"
os.environ["EMAIL_ENABLED"] = "0"
os.environ["ROUTING_PRELOAD_ENABLED"] = "0"
os.environ["AUTO_CREATE_TABLES"] = "0"
os.environ["OLA_MAPS_KEY"] = ""

# Stub out OSMnx graph preload — would hit the network during lifespan startup
import backend.routing as _routing_mod
_routing_mod.preload_city_graphs = lambda *a, **kw: None

# Stub out email domain DNS check — avoids live DNS lookups in tests
import backend.auth as _auth_mod
_auth_mod._email_domain_exists = lambda email: True

import backend.models  # noqa: F401 — register ORM metadata
from backend.database import Base, engine, get_db, SessionLocal
from backend.main import app
from fastapi.testclient import TestClient


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _db_override():
    def _override():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Clear in-memory rate-limit buckets before each test so tests don't bleed into each other."""
    _auth_mod._rate_buckets.clear()
    yield
    _auth_mod._rate_buckets.clear()


@pytest.fixture(autouse=True)
def _raise_daily_issue_limit():
    """Set daily issue limit very high so tests don't hit spam blocks (test_daily_limit_enforced patches it back down)."""
    import backend.issues as _issues_mod
    original = _issues_mod._DAILY_LIMIT
    _issues_mod._DAILY_LIMIT = 1000
    yield
    _issues_mod._DAILY_LIMIT = original


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── Shared helpers ────────────────────────────────────────────────────────────

def _register(client, username, password="Pass123!", email=None):
    email = email or f"{username}@test.com"
    return client.post("/auth/register", json={
        "username": username, "email": email, "password": password,
    })


def _login(client, username, password="Pass123!"):
    return client.post("/auth/login", json={"username": username, "password": password})


def _token(client, username, password="Pass123!", email=None):
    _register(client, username, password, email)
    return _login(client, username, password).json()["access_token"]


@pytest.fixture
def user_token(client):
    return _token(client, "regularuser", email="regular@test.com")


@pytest.fixture
def admin_token(client):
    return _token(client, "testadmin", email="admin@test.com")


@pytest.fixture
def auth(user_token):
    return {"Authorization": f"Bearer {user_token}"}


@pytest.fixture
def admin_auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}

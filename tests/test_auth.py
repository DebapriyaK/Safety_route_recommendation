"""Integration tests for /auth endpoints."""
import pytest

REGISTER_URL = "/auth/register"
LOGIN_URL = "/auth/login"
ME_URL = "/auth/me"


def _reg(client, username="alice", password="Secure1!", email=None):
    email = email or f"{username}@example.com"
    return client.post(REGISTER_URL, json={
        "username": username, "email": email, "password": password,
    })


def _login(client, username="alice", password="Secure1!"):
    return client.post(LOGIN_URL, json={"username": username, "password": password})


def _token(client, username="alice", password="Secure1!", email=None):
    _reg(client, username, password, email)  # 409 is fine if user already exists
    return _login(client, username, password).json()["access_token"]


# ── Register ──────────────────────────────────────────────────────────────────

def test_register_success(client):
    res = _reg(client, "bob_auth", email="bob_auth@example.com")
    assert res.status_code == 201
    data = res.json()
    # Register returns TokenResponse: {access_token, token_type, user: {...}}
    assert data["user"]["username"] == "bob_auth"
    assert "password" not in data
    assert "password_hash" not in data
    assert "password" not in data["user"]


def test_register_returns_token(client):
    res = _reg(client, "carol_auth", email="carol_auth@example.com")
    assert res.status_code == 201
    assert "access_token" in res.json()


def test_register_duplicate_username(client):
    _reg(client, "dave_auth", email="dave_auth@example.com")
    res = _reg(client, "dave_auth", email="dave_auth2@example.com")
    assert res.status_code == 409


def test_register_duplicate_email(client):
    _reg(client, "eve_auth", email="shared_auth@example.com")
    res = _reg(client, "eve2_auth", email="shared_auth@example.com")
    assert res.status_code == 409


def test_register_invalid_email(client):
    res = client.post(REGISTER_URL, json={
        "username": "frank_auth", "email": "not-an-email", "password": "Secure1!",
    })
    assert res.status_code == 422


def test_register_short_password(client):
    res = client.post(REGISTER_URL, json={
        "username": "grace_auth", "email": "grace_auth@example.com", "password": "abc",
    })
    assert res.status_code == 400  # _validate_password returns 400


def test_register_missing_fields(client):
    res = client.post(REGISTER_URL, json={"username": "heidi_auth"})
    assert res.status_code == 422


# ── Login ─────────────────────────────────────────────────────────────────────

def test_login_success(client):
    _reg(client, "ivan_auth", email="ivan_auth@example.com")
    res = _login(client, "ivan_auth")
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_login_wrong_password(client):
    _reg(client, "judy_auth", email="judy_auth@example.com")
    res = _login(client, "judy_auth", "WrongPass1!")
    assert res.status_code == 401


def test_login_unknown_user(client):
    res = _login(client, "nobody_xyz", "anything")
    assert res.status_code == 401


def test_login_returns_user_info(client):
    _reg(client, "kate_auth", email="kate_auth@example.com")
    data = _login(client, "kate_auth").json()
    # Login returns TokenResponse: {access_token, token_type, user: {username, ...}}
    assert data["user"]["username"] == "kate_auth"


# ── /auth/me ──────────────────────────────────────────────────────────────────

def test_me_authenticated(client):
    token = _token(client, "leo_auth", email="leo_auth@example.com")
    res = client.get(ME_URL, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    # /me returns flat dict: {id, username, email, is_admin, ...}
    assert res.json()["username"] == "leo_auth"


def test_me_no_token(client):
    res = client.get(ME_URL)
    assert res.status_code == 401  # HTTPBearer returns 403, but get_current_user raises 401


def test_me_invalid_token(client):
    res = client.get(ME_URL, headers={"Authorization": "Bearer invalid.token.here"})
    assert res.status_code in (401, 403)


def test_me_is_admin_false_for_regular(client):
    token = _token(client, "mia_auth", email="mia_auth@example.com")
    data = client.get(ME_URL, headers={"Authorization": f"Bearer {token}"}).json()
    assert data["is_admin"] is False


def test_me_is_admin_true_for_admin(client, admin_token):
    data = client.get(ME_URL, headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert data["is_admin"] is True


# ── /auth/profile/stats ───────────────────────────────────────────────────────

def test_profile_stats_requires_auth(client):
    res = client.get("/auth/profile/stats")
    assert res.status_code in (401, 403)


def test_profile_stats_authenticated(client):
    token = _token(client, "nina_auth", email="nina_auth@example.com")
    res = client.get("/auth/profile/stats", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.json()
    # Response shape: {user: {...}, reported: {...}, validations: {...}}
    assert "reported" in data
    assert "validations" in data

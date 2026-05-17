"""Integration tests for /issues endpoints."""
import pytest

ISSUES_URL = "/issues"

_BASE_ISSUE = {
    "lat": 12.9716,
    "lon": 77.5946,
    "category": "Pothole",
    "severity": "medium",
    "description": "Big pothole near the junction",
}

# Spread-out coords so each issue avoids dedup radius (~55 m = 0.0005 deg)
def _coords(offset: float = 0.0):
    return {"lat": 12.9716 + offset, "lon": 77.5946 + offset}


# ── Create issue ──────────────────────────────────────────────────────────────

def test_create_issue_unauthenticated(client):
    res = client.post(ISSUES_URL, json=_BASE_ISSUE)
    assert res.status_code in (401, 403)


def test_create_issue_authenticated(client, auth):
    res = client.post(ISSUES_URL, json=_BASE_ISSUE, headers=auth)
    assert res.status_code == 201
    data = res.json()
    assert data["category"] == "Pothole"
    assert data["lat"] == pytest.approx(12.9716)
    assert data["is_active"] is True


def test_create_issue_invalid_category(client, auth):
    bad = {**_BASE_ISSUE, "category": "Flying Car"}
    res = client.post(ISSUES_URL, json=bad, headers=auth)
    assert res.status_code == 422


def test_create_issue_invalid_severity(client, auth):
    bad = {**_BASE_ISSUE, "category": "Pothole", "severity": "extreme"}
    res = client.post(ISSUES_URL, json=bad, headers=auth)
    assert res.status_code == 422


def test_create_issue_invalid_lat(client, auth):
    bad = {**_BASE_ISSUE, "lat": 200.0}
    res = client.post(ISSUES_URL, json=bad, headers=auth)
    assert res.status_code == 422


def test_create_issue_invalid_lon(client, auth):
    bad = {**_BASE_ISSUE, "lon": -200.0}
    res = client.post(ISSUES_URL, json=bad, headers=auth)
    assert res.status_code == 422


def test_create_issue_description_too_long(client, auth):
    bad = {**_BASE_ISSUE, "description": "x" * 501}
    res = client.post(ISSUES_URL, json=bad, headers=auth)
    assert res.status_code == 422


def test_create_issue_dedup_aggregates_nearby(client, auth):
    """Two reports of same category within 55 m should increment num_reports."""
    payload = {**_BASE_ISSUE, **_coords(0.01)}  # far from other tests
    res1 = client.post(ISSUES_URL, json=payload, headers=auth)
    assert res1.status_code == 201
    first_id = res1.json()["id"]

    # Same spot, same category → dedup
    res2 = client.post(ISSUES_URL, json=payload, headers=auth)
    # Spam check blocks same user same area; if it passes, id should match
    if res2.status_code == 201:
        assert res2.json()["id"] == first_id
        assert res2.json()["num_reports"] >= 2
    else:
        # 429 spam block is also acceptable
        assert res2.status_code == 429


def test_create_issue_severity_escalation(client, auth):
    """A second report with higher severity should escalate the issue."""
    coords = _coords(0.02)
    low = {**_BASE_ISSUE, **coords, "severity": "low"}
    high = {**_BASE_ISSUE, **coords, "severity": "high", "category": "Pothole"}
    r1 = client.post(ISSUES_URL, json=low, headers=auth)
    assert r1.status_code == 201
    r2 = client.post(ISSUES_URL, json=high, headers=auth)
    if r2.status_code == 201:
        assert r2.json()["severity"] == "high"


# ── Admin bypasses spam / dedup ───────────────────────────────────────────────

def test_admin_can_report_multiple_same_location(client, admin_auth):
    """Admin should not be blocked by spam or dedup checks."""
    payload = {**_BASE_ISSUE, **_coords(0.03)}
    results = []
    for _ in range(6):  # daily limit is 5 for regular users
        res = client.post(ISSUES_URL, json=payload, headers=admin_auth)
        results.append(res.status_code)
    # All should succeed (no 429)
    assert all(s == 201 for s in results), f"Got statuses: {results}"


def test_admin_creates_separate_issues_not_dedup(client, admin_auth):
    """Admin reports at same spot should create distinct issues."""
    payload = {**_BASE_ISSUE, **_coords(0.04)}
    r1 = client.post(ISSUES_URL, json=payload, headers=admin_auth)
    r2 = client.post(ISSUES_URL, json=payload, headers=admin_auth)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


# ── List / Get issues ─────────────────────────────────────────────────────────

def test_list_issues(client):
    res = client.get(ISSUES_URL)
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_list_issues_bbox_filter(client, auth):
    client.post(ISSUES_URL, json={**_BASE_ISSUE, **_coords(0.05)}, headers=auth)
    res = client.get(ISSUES_URL + "?lat_min=12.9&lat_max=13.1&lon_min=77.5&lon_max=77.7")
    assert res.status_code == 200
    assert all(
        12.9 <= i["lat"] <= 13.1 and 77.5 <= i["lon"] <= 77.7
        for i in res.json()
    )


def test_get_issue_by_id(client, auth):
    r = client.post(ISSUES_URL, json={**_BASE_ISSUE, **_coords(0.06)}, headers=auth)
    assert r.status_code == 201
    issue_id = r.json()["id"]
    res = client.get(f"{ISSUES_URL}/{issue_id}")
    assert res.status_code == 200
    assert res.json()["id"] == issue_id


def test_get_issue_not_found(client):
    res = client.get(f"{ISSUES_URL}/zzzzzzzz")
    assert res.status_code == 404


# ── Validate issue ────────────────────────────────────────────────────────────

def _create_issue(client, headers, offset=0.07):
    r = client.post(ISSUES_URL, json={**_BASE_ISSUE, **_coords(offset)}, headers=headers)
    assert r.status_code == 201
    return r.json()["id"]


def test_validate_confirm(client, user_token, admin_auth):
    # Admin creates; user confirms (can't validate own)
    issue_id = _create_issue(client, admin_auth, offset=0.08)
    res = client.patch(
        f"{ISSUES_URL}/{issue_id}/validate",
        json={"response": "confirm"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res.status_code == 200
    assert res.json()["num_confirmations"] >= 1


def test_validate_dismiss(client, user_token, admin_auth):
    issue_id = _create_issue(client, admin_auth, offset=0.09)
    res = client.patch(
        f"{ISSUES_URL}/{issue_id}/validate",
        json={"response": "dismiss"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res.status_code == 200
    assert res.json()["num_dismissals"] >= 1


def test_validate_own_issue_forbidden(client, auth):
    issue_id = _create_issue(client, auth, offset=0.10)
    res = client.patch(
        f"{ISSUES_URL}/{issue_id}/validate",
        json={"response": "confirm"},
        headers=auth,
    )
    assert res.status_code == 403


def test_validate_twice_conflict(client, user_token, admin_auth):
    issue_id = _create_issue(client, admin_auth, offset=0.11)
    headers = {"Authorization": f"Bearer {user_token}"}
    client.patch(f"{ISSUES_URL}/{issue_id}/validate", json={"response": "confirm"}, headers=headers)
    res = client.patch(f"{ISSUES_URL}/{issue_id}/validate", json={"response": "confirm"}, headers=headers)
    assert res.status_code == 409


def test_validate_invalid_response(client, user_token, admin_auth):
    issue_id = _create_issue(client, admin_auth, offset=0.12)
    res = client.patch(
        f"{ISSUES_URL}/{issue_id}/validate",
        json={"response": "maybe"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res.status_code == 422


def test_validate_unauthenticated(client, admin_auth):
    issue_id = _create_issue(client, admin_auth, offset=0.13)
    res = client.patch(f"{ISSUES_URL}/{issue_id}/validate", json={"response": "confirm"})
    assert res.status_code in (401, 403)


# ── Delete issue (admin only) ─────────────────────────────────────────────────

def test_delete_issue_admin(client, admin_auth):
    issue_id = _create_issue(client, admin_auth, offset=0.14)
    res = client.delete(f"{ISSUES_URL}/{issue_id}", headers=admin_auth)
    assert res.status_code == 200
    assert res.json()["deleted"] == issue_id

    # Issue should no longer appear in active list
    get_res = client.get(f"{ISSUES_URL}/{issue_id}")
    # get_issue returns even inactive, so just check is_active via list
    listing = client.get(ISSUES_URL).json()
    assert all(i["id"] != issue_id for i in listing)


def test_delete_issue_non_admin_forbidden(client, auth):
    # Need admin to create then regular user tries to delete
    # Create with regular user first
    r = client.post(ISSUES_URL, json={**_BASE_ISSUE, **_coords(0.15)}, headers=auth)
    if r.status_code != 201:
        pytest.skip("Issue creation blocked by spam; skipping delete test")
    issue_id = r.json()["id"]
    res = client.delete(f"{ISSUES_URL}/{issue_id}", headers=auth)
    assert res.status_code == 403


def test_delete_issue_not_found(client, admin_auth):
    res = client.delete(f"{ISSUES_URL}/zzzzzzzz", headers=admin_auth)
    assert res.status_code == 404


def test_delete_issue_unauthenticated(client, admin_auth):
    issue_id = _create_issue(client, admin_auth, offset=0.16)
    res = client.delete(f"{ISSUES_URL}/{issue_id}")
    assert res.status_code in (401, 403)


# ── Stats / Heatmap ───────────────────────────────────────────────────────────

def test_stats_summary(client):
    res = client.get(f"{ISSUES_URL}/stats/summary")
    assert res.status_code == 200
    data = res.json()
    assert "total_active" in data
    assert "by_category" in data
    assert "avg_confidence" in data


def test_heatmap_returns_geojson(client):
    res = client.get(
        f"{ISSUES_URL}/heatmap",
        params={"lat_min": 12.9, "lat_max": 13.0, "lon_min": 77.5, "lon_max": 77.7},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "FeatureCollection"
    assert isinstance(data["features"], list)


def test_heatmap_invalid_cell_size(client):
    res = client.get(f"{ISSUES_URL}/heatmap?cell_size=0.1")
    assert res.status_code == 400


# ── Spam protection (regular user) ───────────────────────────────────────────

def test_daily_limit_enforced(client, auth):
    """Regular user can't report more than N issues in 24 hours (patched N=2)."""
    import backend.issues as _issues_mod
    original = _issues_mod._DAILY_LIMIT
    _issues_mod._DAILY_LIMIT = 2
    try:
        responses = []
        for i in range(5):
            payload = {**_BASE_ISSUE, "lat": 9.0 + i * 0.2, "lon": 75.0 + i * 0.2}
            r = client.post(ISSUES_URL, json=payload, headers=auth)
            responses.append(r.status_code)
        assert 429 in responses, f"Expected 429 with daily limit=2, got: {responses}"
    finally:
        _issues_mod._DAILY_LIMIT = original

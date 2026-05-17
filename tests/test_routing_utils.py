"""Unit tests for pure routing helper functions (no network / OSMnx required)."""
import pytest

from backend.issues import _compute_confidence, _proximity_weight
from backend.routing import _decode_polyline
from backend.models import Issue
from datetime import datetime, timedelta, timezone


# ── _decode_polyline ──────────────────────────────────────────────────────────

def test_decode_polyline_known_value():
    # Google's encoded polyline for a simple two-point path
    # (12.97, 77.59) → (12.98, 77.60) encoded manually
    # Use a known encoded string from Google's docs: "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    # which decodes to: (-179.9832104, -179.9832104) — standard example
    encoded = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    result = _decode_polyline(encoded)
    assert len(result) == 3
    # First point should be close to (38.5, -120.2)
    assert result[0] == pytest.approx((38.5, -120.2), abs=0.00002)
    assert result[1] == pytest.approx((40.7, -120.95), abs=0.00002)
    assert result[2] == pytest.approx((43.252, -126.453), abs=0.00002)


def test_decode_polyline_returns_latlon_pairs():
    # Minimal valid encoded polyline (single point at 0,0 approx)
    encoded = "??"  # encodes (0, 0)
    result = _decode_polyline(encoded)
    assert isinstance(result, list)
    assert all(isinstance(p, tuple) and len(p) == 2 for p in result)


def test_decode_polyline_multi_point():
    # Encode/decode round-trip: Google's full example string decodes to 3 known points
    encoded = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    result = _decode_polyline(encoded)
    # Should decode to exactly 3 points without error
    assert len(result) == 3
    # All results should be (lat, lon) pairs with valid ranges
    assert all(-90 <= lat <= 90 and -180 <= lon <= 180 for lat, lon in result)


# ── _compute_confidence edge cases ────────────────────────────────────────────

def test_confidence_single_report_default_rep():
    score = _compute_confidence(1, 0, 0)
    # 50 + 20*log(2)*1.0 ≈ 63.8
    assert 60 < score < 70


def test_confidence_many_reports_diminishing_returns():
    s10 = _compute_confidence(10, 0, 0)
    s100 = _compute_confidence(100, 0, 0)
    s1000 = _compute_confidence(1000, 0, 0)
    # Each 10x increase in reports should add less than the previous
    delta1 = s100 - s10
    delta2 = s1000 - s100
    assert delta2 < delta1


def test_confidence_zero_reports_still_valid():
    # Edge case: 0 reports (shouldn't happen in practice but must not crash)
    score = _compute_confidence(0, 0, 0)
    assert 0.0 <= score <= 100.0


# ── _proximity_weight integration with validation logic ───────────────────────

def test_proximity_weight_exact_match():
    lat, lon = 12.9716, 77.5946
    assert _proximity_weight(lat, lon, lat, lon) == 2


def test_proximity_weight_boundary_inside():
    # 0.0009 degrees < 0.001 threshold → weight 2
    assert _proximity_weight(12.9716 + 0.0009, 77.5946, 12.9716, 77.5946) == 2


def test_proximity_weight_boundary_outside():
    # 0.0011 degrees > 0.001 threshold → weight 1
    assert _proximity_weight(12.9716 + 0.0011, 77.5946, 12.9716, 77.5946) == 1


def test_proximity_weight_diagonal_inside():
    # Both lat and lon within threshold
    assert _proximity_weight(12.9716 + 0.0005, 77.5946 + 0.0005, 12.9716, 77.5946) == 2


def test_proximity_weight_diagonal_outside():
    # Lat within but lon outside
    assert _proximity_weight(12.9716 + 0.0005, 77.5946 + 0.0015, 12.9716, 77.5946) == 1


# ── deactivate_stale_issues ───────────────────────────────────────────────────

def test_deactivate_stale_issues(db):
    from backend.issues import deactivate_stale_issues, _AUTO_EXPIRE_DAYS, _AUTO_EXPIRE_MIN_EFFECTIVE_CONF

    # Create a stale issue: old, no confirmations, low confidence
    old_date = datetime.now(timezone.utc) - timedelta(days=_AUTO_EXPIRE_DAYS + 1)
    stale = Issue(
        id="stale001",
        lat=12.9, lon=77.6,
        category="Pothole",
        confidence_score=10.0,  # below _AUTO_EXPIRE_MIN_EFFECTIVE_CONF
        num_confirmations=0,
        is_active=True,
        reported_at=old_date,
    )
    db.add(stale)

    # Create a recent issue that should NOT be deactivated
    fresh = Issue(
        id="fresh001",
        lat=12.91, lon=77.61,
        category="Pothole",
        confidence_score=80.0,
        num_confirmations=0,
        is_active=True,
        reported_at=datetime.now(timezone.utc),
    )
    db.add(fresh)
    db.commit()

    count = deactivate_stale_issues(db)
    assert count >= 1

    db.refresh(stale)
    db.refresh(fresh)
    assert stale.is_active is False
    assert fresh.is_active is True

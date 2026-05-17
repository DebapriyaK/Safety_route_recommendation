"""Unit tests for model computed properties and pure helper functions."""
import math
from datetime import datetime, timedelta, timezone

import pytest

from backend.models import Issue, STALE_DAYS
from backend.issues import _compute_confidence, _proximity_weight


# ── Issue.effective_confidence ────────────────────────────────────────────────

def _make_issue(confidence=70.0, age_days=0, category="Pothole", last_validated=None):
    now = datetime.now(timezone.utc)
    reported = now - timedelta(days=age_days)
    issue = Issue(
        id="test0001",
        lat=12.9, lon=77.6,
        category=category,
        confidence_score=confidence,
        reported_at=reported,
        last_validated=last_validated,
        is_active=True,
    )
    return issue


def test_effective_confidence_fresh_issue():
    issue = _make_issue(confidence=80.0, age_days=0)
    assert issue.effective_confidence == pytest.approx(80.0)


def test_effective_confidence_within_grace_period():
    issue = _make_issue(confidence=80.0, age_days=STALE_DAYS - 1)
    assert issue.effective_confidence == pytest.approx(80.0)


def test_effective_confidence_decays_after_grace():
    issue = _make_issue(confidence=80.0, age_days=STALE_DAYS + 5, category="Pothole")
    # Pothole decay rate = 1.0 pt/day; 5 days past grace → -5 pts
    assert issue.effective_confidence == pytest.approx(75.0, abs=0.1)


def test_effective_confidence_fast_decay_unsafe_area():
    issue = _make_issue(confidence=80.0, age_days=STALE_DAYS + 4, category="Unsafe Area")
    # Unsafe Area decay = 5.0 pt/day; 4 days → -20 pts → 60
    assert issue.effective_confidence == pytest.approx(60.0, abs=0.1)


def test_effective_confidence_clamps_to_zero():
    issue = _make_issue(confidence=10.0, age_days=STALE_DAYS + 100, category="Unsafe Area")
    assert issue.effective_confidence == 0.0


def test_effective_confidence_never_exceeds_100():
    issue = _make_issue(confidence=100.0, age_days=0)
    assert issue.effective_confidence <= 100.0


def test_effective_confidence_uses_last_validated_as_reference():
    now = datetime.now(timezone.utc)
    # Reported 20 days ago but validated 1 day ago → almost no decay
    last_validated = now - timedelta(days=1)
    issue = _make_issue(confidence=70.0, age_days=20, last_validated=last_validated)
    assert issue.effective_confidence == pytest.approx(70.0, abs=0.5)


# ── Issue.needs_revalidation ──────────────────────────────────────────────────

def test_needs_revalidation_fresh():
    issue = _make_issue(age_days=0)
    assert issue.needs_revalidation is False


def test_needs_revalidation_old():
    issue = _make_issue(age_days=STALE_DAYS + 1)
    assert issue.needs_revalidation is True


def test_needs_revalidation_recently_validated():
    now = datetime.now(timezone.utc)
    issue = _make_issue(age_days=30, last_validated=now - timedelta(days=1))
    assert issue.needs_revalidation is False


# ── _compute_confidence ───────────────────────────────────────────────────────

def test_compute_confidence_baseline():
    score = _compute_confidence(1, 0, 0, 1.0)
    assert 50 < score < 80  # should start around 63–65


def test_compute_confidence_increases_with_confirmations():
    low = _compute_confidence(1, 0, 0)
    high = _compute_confidence(1, 5, 0)
    assert high > low


def test_compute_confidence_decreases_with_dismissals():
    no_dismiss = _compute_confidence(1, 0, 0)
    with_dismiss = _compute_confidence(1, 0, 5)
    assert with_dismiss < no_dismiss


def test_compute_confidence_clamped_to_0_100():
    # Many dismissals should clamp to 0, not go negative
    score_low = _compute_confidence(1, 0, 1000)
    assert score_low == 0.0

    # Many confirmations should clamp to 100, not exceed
    score_high = _compute_confidence(1000, 1000, 0)
    assert score_high == 100.0


def test_compute_confidence_high_reputation_boosts():
    low_rep = _compute_confidence(1, 0, 0, reporter_reputation=0.5)
    high_rep = _compute_confidence(1, 0, 0, reporter_reputation=1.5)
    assert high_rep > low_rep


def test_compute_confidence_reputation_clamped():
    capped = _compute_confidence(1, 0, 0, reporter_reputation=99.0)
    normal = _compute_confidence(1, 0, 0, reporter_reputation=1.5)
    assert capped == pytest.approx(normal, abs=0.01)


# ── _proximity_weight ─────────────────────────────────────────────────────────

def test_proximity_weight_no_coords():
    assert _proximity_weight(None, None, 12.9, 77.6) == 1


def test_proximity_weight_nearby():
    # Same point → weight 2
    assert _proximity_weight(12.9, 77.6, 12.9, 77.6) == 2


def test_proximity_weight_within_100m():
    # 0.0005 deg ≈ 55 m — within 0.001 threshold
    assert _proximity_weight(12.9005, 77.6005, 12.9, 77.6) == 2


def test_proximity_weight_far():
    # 0.01 deg ≈ 1.1 km — outside threshold
    assert _proximity_weight(12.91, 77.61, 12.9, 77.6) == 1

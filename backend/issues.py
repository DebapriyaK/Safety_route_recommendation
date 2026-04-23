import math
import random
import string
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.database import get_db
from backend.models import Issue, User, Validation

router = APIRouter(prefix="/issues", tags=["issues"])

VALID_CATEGORIES = [
    "Broken Streetlight",
    "Pothole",
    "Narrow Lane",
    "Unsafe Area",
    "Other",
]

VALID_SEVERITIES = ("low", "medium", "high")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class IssueCreate(BaseModel):
    lat: float
    lon: float
    category: str
    description: Optional[str] = ""
    severity: Optional[str] = "medium"

    @field_validator("lat")
    @classmethod
    def validate_lat(cls, v: float) -> float:
        if not (-90 <= v <= 90):
            raise ValueError("lat must be between -90 and 90")
        return v

    @field_validator("lon")
    @classmethod
    def validate_lon(cls, v: float) -> float:
        if not (-180 <= v <= 180):
            raise ValueError("lon must be between -180 and 180")
        return v

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            raise ValueError(f"category must be one of {VALID_CATEGORIES}")
        return v

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        if v not in VALID_SEVERITIES:
            raise ValueError("severity must be 'low', 'medium', or 'high'")
        return v


class ValidateRequest(BaseModel):
    response: str           # "confirm" or "dismiss"
    user_lat: Optional[float] = None
    user_lon: Optional[float] = None
    comment: Optional[str] = None

    @field_validator("response")
    @classmethod
    def validate_response(cls, v: str) -> str:
        if v not in ("confirm", "dismiss"):
            raise ValueError("response must be 'confirm' or 'dismiss'")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_id(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _compute_confidence(
    num_reports: int,
    num_confirmations: int,
    num_dismissals: int,
    reporter_reputation: float = 1.0,
) -> float:
    """
    Logarithmic confidence with diminishing returns so a single spammer
    can't push a fake issue to 100 by filing 10 reports.
    reporter_reputation [0.5–1.5] scales the initial weight.
    """
    rep_clamp = min(1.5, max(0.5, reporter_reputation))
    score = (
        50
        + 20 * math.log1p(num_reports) * rep_clamp
        + 12 * math.log1p(num_confirmations)
        - 10 * math.log1p(num_dismissals)
    )
    return float(max(0.0, min(100.0, score)))


def _update_reporter_reputation(db: Session, reporter_id: int) -> None:
    """Recompute and persist a reporter's reputation from their full history."""
    reported = db.query(Issue).filter(Issue.reporter_id == reporter_id).all()
    if not reported:
        return
    confirmed = sum(1 for i in reported if i.num_confirmations > i.num_dismissals)
    accuracy = confirmed / len(reported)
    # Reputation in [0.5, 1.5] — never punish below 0.5 or reward above 1.5
    reporter = db.query(User).filter(User.id == reporter_id).first()
    if reporter:
        reporter.reputation_score = round(0.5 + accuracy, 3)


def _proximity_weight(user_lat: Optional[float], user_lon: Optional[float],
                      issue_lat: float, issue_lon: float) -> int:
    """
    Return 2 if the validating user is within ~100 m of the issue (they can
    actually see it), 1 otherwise.  Uses a simple degree-based bounding check.
    0.001 degrees ≈ 111 m at Bangalore latitude.
    """
    if user_lat is None or user_lon is None:
        return 1
    if abs(user_lat - issue_lat) < 0.001 and abs(user_lon - issue_lon) < 0.001:
        return 2
    return 1


_SPAM_RADIUS_DEG  = 0.0009   # ~100 m at Bangalore latitude
_SPAM_WINDOW_H    = 6        # same user, same area
_DAILY_LIMIT      = 5        # max new issues per user per 24 h
_AUTO_EXPIRE_DAYS = 30
_AUTO_EXPIRE_MIN_EFFECTIVE_CONF = 20.0
_DEDUP_RADIUS_DEG = 0.0005   # ~55 m — wider than before, more realistic dedup


def _check_spam(db: Session, user: "User", lat: float, lon: float) -> None:
    now = datetime.now(timezone.utc)
    cutoff_day = now - timedelta(hours=24)
    recent_total = (
        db.query(Issue)
        .filter(Issue.reporter_id == user.id, Issue.reported_at >= cutoff_day)
        .count()
    )
    if recent_total >= _DAILY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit reached. You can report at most {_DAILY_LIMIT} issues per 24 hours.",
        )

    cutoff_area = now - timedelta(hours=_SPAM_WINDOW_H)
    nearby = (
        db.query(Issue)
        .filter(
            Issue.reporter_id == user.id,
            Issue.reported_at >= cutoff_area,
            Issue.lat >= lat - _SPAM_RADIUS_DEG,
            Issue.lat <= lat + _SPAM_RADIUS_DEG,
            Issue.lon >= lon - _SPAM_RADIUS_DEG,
            Issue.lon <= lon + _SPAM_RADIUS_DEG,
        )
        .first()
    )
    if nearby:
        raise HTTPException(
            status_code=429,
            detail="You already reported an issue in this area recently. Wait 6 hours before reporting again.",
        )


def _issue_to_dict(issue: Issue) -> dict:
    return {
        "id": issue.id,
        "lat": issue.lat,
        "lon": issue.lon,
        "category": issue.category,
        "description": issue.description,
        "severity": issue.severity,
        "reporter_id": issue.reporter_id,
        "reporter_name": issue.reporter_name,
        "confidence_score": issue.confidence_score,
        "effective_confidence": issue.effective_confidence,
        "num_reports": issue.num_reports,
        "num_confirmations": issue.num_confirmations,
        "num_dismissals": issue.num_dismissals,
        "is_active": issue.is_active,
        "reported_at": issue.reported_at.isoformat() if issue.reported_at else None,
        "last_validated": issue.last_validated.isoformat() if issue.last_validated else None,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
        "needs_revalidation": issue.needs_revalidation,
    }


def deactivate_stale_issues(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=_AUTO_EXPIRE_DAYS)
    candidates = (
        db.query(Issue)
        .filter(
            Issue.is_active == True,
            Issue.num_confirmations == 0,
            Issue.reported_at <= cutoff,
        )
        .all()
    )
    changed = 0
    now = datetime.now(timezone.utc)
    for issue in candidates:
        if issue.effective_confidence < _AUTO_EXPIRE_MIN_EFFECTIVE_CONF:
            issue.is_active = False
            issue.resolved_at = now
            changed += 1
    if changed:
        db.commit()
    return changed


# ---------------------------------------------------------------------------
# Endpoints — ordering matters: specific paths before parameterised ones
# ---------------------------------------------------------------------------

@router.get("/stats/summary")
def get_stats_summary(db: Session = Depends(get_db)):
    total = db.query(Issue).filter(Issue.is_active == True).count()
    by_category: dict = {}
    for cat in VALID_CATEGORIES:
        by_category[cat] = db.query(Issue).filter(
            Issue.is_active == True, Issue.category == cat
        ).count()

    scores = db.query(Issue.confidence_score).filter(Issue.is_active == True).all()
    avg_conf = round(sum(r[0] for r in scores) / len(scores), 1) if scores else 0.0

    return {
        "total_active": total,
        "by_category": by_category,
        "avg_confidence": avg_conf,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_issue(
    body: IssueCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_spam(db, current_user, body.lat, body.lon)

    # Deduplication: aggregate nearby same-category reports instead of creating duplicates
    existing_nearby = (
        db.query(Issue)
        .filter(
            Issue.is_active == True,
            Issue.category == body.category,
            Issue.lat >= body.lat - _DEDUP_RADIUS_DEG,
            Issue.lat <= body.lat + _DEDUP_RADIUS_DEG,
            Issue.lon >= body.lon - _DEDUP_RADIUS_DEG,
            Issue.lon <= body.lon + _DEDUP_RADIUS_DEG,
        )
        .first()
    )
    if existing_nearby:
        existing_nearby.num_reports += 1
        # Escalate severity if new reporter marks it higher
        severity_rank = {'low': 0, 'medium': 1, 'high': 2}
        if severity_rank.get(body.severity, 1) > severity_rank.get(existing_nearby.severity, 1):
            existing_nearby.severity = body.severity
        existing_nearby.confidence_score = _compute_confidence(
            existing_nearby.num_reports,
            existing_nearby.num_confirmations,
            existing_nearby.num_dismissals,
            current_user.reputation_score,
        )
        db.commit()
        db.refresh(existing_nearby)
        return _issue_to_dict(existing_nearby)

    issue_id = _generate_id()
    while db.query(Issue).filter(Issue.id == issue_id).first():
        issue_id = _generate_id()

    confidence = _compute_confidence(1, 0, 0, current_user.reputation_score)
    issue = Issue(
        id=issue_id,
        lat=body.lat,
        lon=body.lon,
        category=body.category,
        description=body.description or "",
        severity=body.severity,
        reporter_id=current_user.id,
        reporter_name=current_user.username,
        confidence_score=confidence,
        num_reports=1,
        num_confirmations=0,
        num_dismissals=0,
        is_active=True,
        reported_at=datetime.now(timezone.utc),
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return _issue_to_dict(issue)


@router.get("/heatmap")
def get_issue_heatmap(
    lat_min: Optional[float] = None,
    lat_max: Optional[float] = None,
    lon_min: Optional[float] = None,
    lon_max: Optional[float] = None,
    cell_size: float = 0.005,
    db: Session = Depends(get_db),
):
    if cell_size <= 0 or cell_size > 0.05:
        raise HTTPException(status_code=400, detail="cell_size must be between 0 and 0.05")

    q = db.query(Issue).filter(Issue.is_active == True)
    if lat_min is not None:
        q = q.filter(Issue.lat >= lat_min)
    if lat_max is not None:
        q = q.filter(Issue.lat <= lat_max)
    if lon_min is not None:
        q = q.filter(Issue.lon >= lon_min)
    if lon_max is not None:
        q = q.filter(Issue.lon <= lon_max)

    issues = q.all()
    buckets: dict = {}
    for issue in issues:
        lat_idx = int(issue.lat // cell_size)
        lon_idx = int(issue.lon // cell_size)
        key = (lat_idx, lon_idx)
        if key not in buckets:
            buckets[key] = {"count": 0, "sum_conf": 0.0}
        buckets[key]["count"] += 1
        buckets[key]["sum_conf"] += issue.effective_confidence

    features = []
    for (lat_idx, lon_idx), agg in buckets.items():
        lat0 = lat_idx * cell_size
        lon0 = lon_idx * cell_size
        count = agg["count"]
        avg_conf = round(agg["sum_conf"] / count, 1)
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon0, lat0],
                    [lon0 + cell_size, lat0],
                    [lon0 + cell_size, lat0 + cell_size],
                    [lon0, lat0 + cell_size],
                    [lon0, lat0],
                ]],
            },
            "properties": {
                "issue_count": count,
                "avg_effective_confidence": avg_conf,
                "intensity": round(min(1.0, count / 8.0), 3),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {"cell_size": cell_size, "cells": len(features)},
    }


@router.get("")
def list_issues(
    lat_min: Optional[float] = None,
    lat_max: Optional[float] = None,
    lon_min: Optional[float] = None,
    lon_max: Optional[float] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Issue).filter(Issue.is_active == True)
    if lat_min is not None:
        q = q.filter(Issue.lat >= lat_min)
    if lat_max is not None:
        q = q.filter(Issue.lat <= lat_max)
    if lon_min is not None:
        q = q.filter(Issue.lon >= lon_min)
    if lon_max is not None:
        q = q.filter(Issue.lon <= lon_max)
    if category and category in VALID_CATEGORIES:
        q = q.filter(Issue.category == category)
    return [_issue_to_dict(i) for i in q.order_by(Issue.reported_at.desc()).all()]


@router.get("/{issue_id}")
def get_issue(issue_id: str, db: Session = Depends(get_db)):
    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return _issue_to_dict(issue)


@router.patch("/{issue_id}/validate")
def validate_issue(
    issue_id: str,
    body: ValidateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    issue = db.query(Issue).filter(Issue.id == issue_id, Issue.is_active == True).first()
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found")

    if issue.reporter_id == current_user.id:
        raise HTTPException(status_code=403, detail="You cannot validate an issue you reported.")

    existing = (
        db.query(Validation)
        .filter(Validation.issue_id == issue_id, Validation.user_id == current_user.id)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="You have already validated this issue")

    # Proximity weight: standing next to the issue makes the validation 2× more credible
    weight = _proximity_weight(body.user_lat, body.user_lon, issue.lat, issue.lon)

    validation = Validation(
        issue_id=issue_id,
        user_id=current_user.id,
        response=body.response,
        validated_at=datetime.now(timezone.utc),
        user_lat=body.user_lat,
        user_lon=body.user_lon,
        comment=body.comment,
    )
    db.add(validation)

    if body.response == "confirm":
        issue.num_confirmations += weight
    else:
        issue.num_dismissals += weight

    issue.confidence_score = _compute_confidence(
        issue.num_reports, issue.num_confirmations, issue.num_dismissals
    )
    issue.last_validated = datetime.now(timezone.utc)

    overwhelming_dismissal = issue.num_dismissals >= 2 * max(1, issue.num_reports + issue.num_confirmations)
    if issue.confidence_score <= 20 or overwhelming_dismissal:
        issue.is_active = False
        issue.resolved_at = datetime.now(timezone.utc)

    db.commit()

    # Update the original reporter's reputation based on their full history
    if issue.reporter_id:
        _update_reporter_reputation(db, issue.reporter_id)
        db.commit()

    db.refresh(issue)
    return _issue_to_dict(issue)

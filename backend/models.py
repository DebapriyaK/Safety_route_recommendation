import math
from datetime import datetime, timezone
from backend.database import Base
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Index, Integer, String, Text
)
from sqlalchemy.orm import relationship

STALE_DAYS = 3

# Per-category staleness decay (points per day after STALE_DAYS grace period).
# Physical issues (potholes, narrow lanes) decay slowly — they persist for weeks/months.
# Subjective/time-sensitive issues (unsafe area, broken streetlight) decay faster.
CATEGORY_DECAY_RATES = {
    'Pothole':            1.0,
    'Narrow Lane':        0.5,   # permanent physical feature, barely decays
    'Broken Streetlight': 2.0,
    'Unsafe Area':        5.0,   # subjective, situation changes quickly
    'Other':              3.0,
}
_DEFAULT_DECAY = 3.0


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    email = Column(String(256), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True, nullable=False)
    reputation_score = Column(Float, default=1.0, nullable=False)
    preferred_mode = Column(String(16), default='walk', nullable=False)

    issues = relationship("Issue", back_populates="reporter", foreign_keys="Issue.reporter_id")
    validations = relationship("Validation", back_populates="user")
    saved_routes = relationship("SavedRoute", back_populates="user")


class Issue(Base):
    __tablename__ = "issues"

    id = Column(String(8), primary_key=True, index=True)
    lat = Column(Float, nullable=False, index=True)
    lon = Column(Float, nullable=False, index=True)
    category = Column(String(64), nullable=False, index=True)
    description = Column(Text, default="")
    severity = Column(String(16), default='medium', nullable=False)
    reporter_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reporter_name = Column(String(64), default="anonymous")
    confidence_score = Column(Float, default=65.0)
    num_reports = Column(Integer, default=1)
    num_confirmations = Column(Integer, default=0)
    num_dismissals = Column(Integer, default=0)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    reported_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    last_validated = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    reporter = relationship("User", back_populates="issues", foreign_keys=[reporter_id])
    validations = relationship("Validation", back_populates="issue")

    @property
    def decay_rate(self) -> float:
        return CATEGORY_DECAY_RATES.get(self.category, _DEFAULT_DECAY)

    @property
    def effective_confidence(self) -> float:
        reference = self.last_validated if self.last_validated is not None else self.reported_at
        if reference is None:
            return float(self.confidence_score)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - reference).total_seconds() / 86400.0
        decay = max(0.0, age_days - STALE_DAYS) * self.decay_rate
        return max(0.0, min(100.0, float(self.confidence_score) - decay))

    @property
    def needs_revalidation(self) -> bool:
        reference = self.last_validated if self.last_validated is not None else self.reported_at
        if reference is None:
            return False
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - reference).total_seconds() / 86400.0 > STALE_DAYS


class Validation(Base):
    __tablename__ = "validations"

    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(String(8), ForeignKey("issues.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    response = Column(String(16), nullable=False)   # "confirm" or "dismiss"
    validated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    user_lat = Column(Float, nullable=True)         # GPS position at time of validation
    user_lon = Column(Float, nullable=True)
    comment = Column(Text, nullable=True)           # optional reason

    issue = relationship("Issue", back_populates="validations")
    user = relationship("User", back_populates="validations")


class SavedRoute(Base):
    __tablename__ = "saved_routes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    label = Column(String(128), default="")
    origin_lat = Column(Float, nullable=False)
    origin_lon = Column(Float, nullable=False)
    dest_lat = Column(Float, nullable=False)
    dest_lon = Column(Float, nullable=False)
    origin_label = Column(String(256), default="")
    dest_label = Column(String(256), default="")
    mode = Column(String(16), default='walk', nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="saved_routes")


class RouteEvent(Base):
    __tablename__ = "route_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    mode = Column(String(16), nullable=False)
    origin_lat = Column(Float, nullable=False)
    origin_lon = Column(Float, nullable=False)
    dest_lat = Column(Float, nullable=False)
    dest_lon = Column(Float, nullable=False)
    safe_score = Column(Float, nullable=True)
    fast_score = Column(Float, nullable=True)
    issues_near_route = Column(Integer, nullable=True)
    computed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


Index("ix_issues_active_category_reported", Issue.is_active, Issue.category, Issue.reported_at)
Index("ix_issues_active_lat_lon", Issue.is_active, Issue.lat, Issue.lon)
Index("ix_validations_issue_user", Validation.issue_id, Validation.user_id, unique=False)
Index("ix_saved_routes_user", SavedRoute.user_id)
Index("ix_route_events_user_time", RouteEvent.user_id, RouteEvent.computed_at)

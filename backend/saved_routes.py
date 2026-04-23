"""saved_routes.py - Server-side saved route storage per user."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.database import get_db
from backend.models import SavedRoute, User

router = APIRouter(prefix="/saved-routes", tags=["saved_routes"])

_MAX_ROUTES_PER_USER = 20


class SavedRouteCreate(BaseModel):
    label: Optional[str] = ""
    origin_lat: float
    origin_lon: float
    dest_lat: float
    dest_lon: float
    origin_label: Optional[str] = ""
    dest_label: Optional[str] = ""
    mode: Optional[str] = "walk"


def _to_dict(r: SavedRoute) -> dict:
    return {
        "id": r.id,
        "label": r.label,
        "origin_lat": r.origin_lat,
        "origin_lon": r.origin_lon,
        "dest_lat": r.dest_lat,
        "dest_lon": r.dest_lon,
        "origin_label": r.origin_label,
        "dest_label": r.dest_label,
        "mode": r.mode,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("")
def list_saved_routes(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    routes = (
        db.query(SavedRoute)
        .filter(SavedRoute.user_id == current_user.id)
        .order_by(SavedRoute.created_at.desc())
        .all()
    )
    return [_to_dict(r) for r in routes]


@router.post("", status_code=status.HTTP_201_CREATED)
def save_route(
    body: SavedRouteCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    count = db.query(SavedRoute).filter(SavedRoute.user_id == current_user.id).count()
    if count >= _MAX_ROUTES_PER_USER:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {_MAX_ROUTES_PER_USER} saved routes reached. Delete some first.",
        )

    auto_label = body.label or f"{body.origin_label or 'Origin'} → {body.dest_label or 'Destination'}"
    route = SavedRoute(
        user_id=current_user.id,
        label=auto_label,
        origin_lat=body.origin_lat,
        origin_lon=body.origin_lon,
        dest_lat=body.dest_lat,
        dest_lon=body.dest_lon,
        origin_label=body.origin_label or "",
        dest_label=body.dest_label or "",
        mode=body.mode or "walk",
    )
    db.add(route)
    db.commit()
    db.refresh(route)
    return _to_dict(route)


@router.delete("/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_route(
    route_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    route = (
        db.query(SavedRoute)
        .filter(SavedRoute.id == route_id, SavedRoute.user_id == current_user.id)
        .first()
    )
    if route is None:
        raise HTTPException(status_code=404, detail="Saved route not found")
    db.delete(route)
    db.commit()

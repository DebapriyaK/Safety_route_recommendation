"""main.py - FastAPI application entry point for SafeRoute."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from backend.auth import router as auth_router
from backend.config import (
    ALLOW_ALL_CORS,
    AUTO_CREATE_TABLES,
    CORS_ORIGINS,
    DEFAULT_CITY_LAT,
    DEFAULT_CITY_LON,
    GEOAPIFY_KEY,
    OLA_MAPS_KEY,
    ISSUE_CLEANUP_INTERVAL_MINUTES,
    ROUTING_PRELOAD_BLOCKING,
    ROUTING_PRELOAD_ENABLED,
    ROUTE_RATE_LIMIT,
    validate_runtime_config,
)
from backend.database import SessionLocal, create_tables, get_db
from backend.issues import deactivate_stale_issues, router as issues_router
from backend.models import Issue, RouteEvent
from backend.routing import get_routes, preload_city_graphs
from backend.saved_routes import router as saved_routes_router

limiter = Limiter(key_func=get_remote_address)


async def _issue_cleanup_loop(interval_minutes: int) -> None:
    while True:
        await asyncio.sleep(max(60, interval_minutes * 60))
        db = SessionLocal()
        try:
            cleaned = deactivate_stale_issues(db)
            if cleaned:
                print(f"[issues] auto-deactivated {cleaned} stale issues in background")
        except Exception as exc:
            print(f"[issues] background cleanup failed: {exc}")
        finally:
            db.close()


async def _routing_preload_startup() -> None:
    try:
        await asyncio.to_thread(preload_city_graphs)
    except Exception as exc:
        print(f'[routing-preload] failed: {exc}')


@asynccontextmanager
async def lifespan(app: FastAPI):
    if AUTO_CREATE_TABLES:
        create_tables()
        print('[startup] AUTO_CREATE_TABLES enabled; schema auto-create executed')
    else:
        print('[startup] AUTO_CREATE_TABLES disabled; expecting Alembic-managed schema')

    warnings = validate_runtime_config()
    for w in warnings:
        print(f"[config-warning] {w}")

    db = SessionLocal()
    try:
        cleaned = deactivate_stale_issues(db)
        if cleaned:
            print(f"[issues] auto-deactivated {cleaned} stale issues on startup")
    finally:
        db.close()

    cleanup_task = asyncio.create_task(_issue_cleanup_loop(ISSUE_CLEANUP_INTERVAL_MINUTES))
    preload_task = None
    if ROUTING_PRELOAD_ENABLED:
        if ROUTING_PRELOAD_BLOCKING:
            print('[routing-preload] blocking startup warmup...')
            await _routing_preload_startup()
        else:
            print('[routing-preload] background warmup started')
            preload_task = asyncio.create_task(_routing_preload_startup())

    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        if preload_task:
            preload_task.cancel()
            with suppress(asyncio.CancelledError):
                await preload_task


app = FastAPI(
    title='SafeRoute Backend API',
    description='Safety-Aware Routing System',
    version='2.1.0',
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

allow_origins = ['*'] if ALLOW_ALL_CORS else CORS_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(auth_router)
app.include_router(issues_router)
app.include_router(saved_routes_router)


@app.get('/geocode')
def geocode_place(
    query: str,
    lat: Optional[float] = DEFAULT_CITY_LAT,
    lon: Optional[float] = DEFAULT_CITY_LON,
):
    if OLA_MAPS_KEY:
        try:
            resp = requests.get(
                'https://api.olamaps.io/places/v1/geocode',
                params={'address': query, 'api_key': OLA_MAPS_KEY},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get('geocodingResults', [])
                if results:
                    loc = results[0].get('geometry', {}).get('location', {})
                    name = results[0].get('formatted_address', query)
                    if loc.get('lat') and loc.get('lng'):
                        return {'lat': loc['lat'], 'lon': loc['lng'], 'name': name}
        except requests.RequestException:
            pass  # fall through to Geoapify

    # Fallback: Geoapify
    try:
        resp = requests.get(
            'https://api.geoapify.com/v1/geocode/search',
            params={
                'text': query,
                'bias': f'proximity:{lon},{lat}',
                'filter': 'rect:77.3,12.7,77.9,13.2',
                'limit': 1,
                'apiKey': GEOAPIFY_KEY,
            },
            timeout=5,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f'Geocode service error: {exc}')
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail='Geocode service unavailable')
    data = resp.json()
    if not data.get('features'):
        return {'error': 'Location not found'}
    props = data['features'][0]['properties']
    return {'lat': props['lat'], 'lon': props['lon'], 'name': props.get('formatted', query)}


@app.get('/geocode/autocomplete')
def geocode_autocomplete(
    query: str,
    lat: Optional[float] = DEFAULT_CITY_LAT,
    lon: Optional[float] = DEFAULT_CITY_LON,
):
    if len(query.strip()) < 2:
        return {'suggestions': []}

    if OLA_MAPS_KEY:
        try:
            resp = requests.get(
                'https://api.olamaps.io/places/v1/autocomplete',
                params={
                    'input': query,
                    'location': f'{lat},{lon}',
                    'api_key': OLA_MAPS_KEY,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                suggestions = []
                for p in data.get('predictions', []):
                    name = p.get('description', '')
                    loc = p.get('geometry', {}).get('location', {})
                    if name and loc.get('lat') and loc.get('lng'):
                        suggestions.append({'name': name, 'lat': loc['lat'], 'lon': loc['lng']})
                if suggestions:
                    return {'suggestions': suggestions}
        except requests.RequestException:
            pass  # fall through to Geoapify

    # Fallback: Geoapify
    try:
        resp = requests.get(
            'https://api.geoapify.com/v1/geocode/autocomplete',
            params={
                'text': query,
                'bias': f'proximity:{lon},{lat}',
                'filter': 'rect:77.3,12.7,77.9,13.2',
                'limit': 5,
                'apiKey': GEOAPIFY_KEY,
            },
            timeout=5,
        )
    except requests.RequestException:
        return {'suggestions': []}
    if resp.status_code != 200:
        return {'suggestions': []}
    data = resp.json()
    suggestions = []
    for feat in data.get('features', []):
        p = feat['properties']
        name = p.get('formatted', '')
        if name and p.get('lat') and p.get('lon'):
            suggestions.append({'name': name, 'lat': p['lat'], 'lon': p['lon']})
    return {'suggestions': suggestions}


class RouteRequest(BaseModel):
    origin_lat: float
    origin_lon: float
    dest_lat: float
    dest_lon: float
    mode: Optional[str] = 'walk'


@app.post('/route')
@limiter.limit(ROUTE_RATE_LIMIT)
def compute_route(
    request: Request,
    req: RouteRequest,
    db: Session = Depends(get_db),
):
    lat_min = min(req.origin_lat, req.dest_lat) - 0.05
    lat_max = max(req.origin_lat, req.dest_lat) + 0.05
    lon_min = min(req.origin_lon, req.dest_lon) - 0.05
    lon_max = max(req.origin_lon, req.dest_lon) + 0.05

    db_issues = (
        db.query(Issue)
        .filter(
            Issue.is_active == True,
            Issue.lat >= lat_min,
            Issue.lat <= lat_max,
            Issue.lon >= lon_min,
            Issue.lon <= lon_max,
        )
        .all()
    )

    issues_data = [
        {
            'id': issue.id,
            'lat': issue.lat,
            'lon': issue.lon,
            'category': issue.category,
            'description': issue.description or '',
            'severity': issue.severity,
            'confidence_score': issue.confidence_score,
            'effective_confidence': issue.effective_confidence,
            'num_reports': issue.num_reports,
            'num_confirmations': issue.num_confirmations,
            'num_dismissals': issue.num_dismissals,
        }
        for issue in db_issues
    ]

    started = datetime.now(timezone.utc)
    try:
        result = get_routes(
            origin_lat=req.origin_lat,
            origin_lon=req.origin_lon,
            dest_lat=req.dest_lat,
            dest_lon=req.dest_lon,
            mode=req.mode,
            issues_data=issues_data,
        )
    except Exception as exc:
        import traceback
        print(f'[route] ERROR: {exc}\n{traceback.format_exc()}')
        raise HTTPException(status_code=500, detail=f'Routing failed: {exc}')

    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    print(f"[route] mode={req.mode} issues={len(issues_data)} latency_ms={elapsed_ms}")

    # Log route event for analytics (non-blocking; failure must not break the response)
    try:
        features = result.get('features', [])
        safe_f = next((f for f in features if f.get('properties', {}).get('route_type') == 'safe'), None)
        fast_f = next((f for f in features if f.get('properties', {}).get('route_type') == 'fast'), None)
        event = RouteEvent(
            mode=req.mode,
            origin_lat=req.origin_lat,
            origin_lon=req.origin_lon,
            dest_lat=req.dest_lat,
            dest_lon=req.dest_lon,
            safe_score=safe_f['properties'].get('safety_score') if safe_f else None,
            fast_score=fast_f['properties'].get('safety_score') if fast_f else None,
            issues_near_route=safe_f['properties'].get('issues_on_path') if safe_f else None,
        )
        db.add(event)
        db.commit()
    except Exception:
        pass

    return result


@app.get('/health')
def health():
    return {'status': 'ok', 'timestamp': datetime.now(timezone.utc).isoformat()}


@app.get('/api')
def root():
    return {
        'project': 'SafeRoute - Safety-Aware Routing System',
        'version': '2.1.0',
        'endpoints': {
            'POST /route': 'Compute safe + fast routes',
            'GET  /geocode': 'Resolve place name -> lat/lon',
            'GET  /geocode/autocomplete': 'Place suggestions',
            'POST /auth/register': 'Create account',
            'POST /auth/login': 'Obtain JWT token',
            'GET  /auth/me': 'Current user info',
            'GET  /auth/profile/stats': 'User reporting + validation stats',
            'POST /issues': 'Report a road issue (auth required)',
            'GET  /issues': 'All active issues',
            'GET  /issues/heatmap': 'Area-wise issue density grid',
            'GET  /issues/stats/summary': 'Issue statistics',
            'GET  /issues/{id}': 'Single issue details',
            'PATCH /issues/{id}/validate': 'Confirm or dismiss (auth required)',
        },
        'status': 'Running',
    }


_frontend_dir = Path(__file__).parent.parent / 'frontend'
app.mount('/', StaticFiles(directory=str(_frontend_dir), html=True), name='frontend')

"""routing.py - OSMnx safe/fast route computation with issue-aware penalties."""

import math
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests as _requests

import networkx as nx
import osmnx as ox
from shapely.geometry import Point

from backend.config import (
    DEFAULT_CITY_LAT,
    DEFAULT_CITY_LON,
    GRAPH_CACHE_MAX_ITEMS,
    GRAPH_CACHE_TTL_MINUTES,
    OLA_MAPS_KEY,
    ORS_API_KEY,
    ROUTING_CACHE_CENTER_DECIMALS,
    ROUTING_GRAPH_MAX_DIST_M,
    ROUTING_GRAPH_MIN_DIST_M,
    ROUTING_PRELOAD_DIST_M,
    ROUTING_PRELOAD_MODES,
    ROUTING_SHORT_TRIP_UNSIMPLIFIED_MAX_M,
)

ox.settings.use_cache = True
ox.settings.log_console = False

_IST = timezone(timedelta(hours=5, minutes=30))

# OrderedDict for LRU cache: key -> (created_ts, graph)
_graph_cache: OrderedDict = OrderedDict()
_graph_cache_ttl_sec = max(60, GRAPH_CACHE_TTL_MINUTES * 60)
_graph_store_dir = Path(__file__).parent / 'graph_store'
_preloaded_graphs: Dict[Tuple[str, bool], object] = {}

MODE_CONFIG = {
    'walk': {
        'network_type': 'walk',
        'speed_kmh': 5,
        'description': 'Pedestrian paths, footways, residential roads',
    },
    'cycle': {
        'network_type': 'bike',
        'speed_kmh': 15,
        'description': 'Cycle lanes and bike-friendly roads',
    },
    'drive': {
        'network_type': 'drive',
        'speed_kmh': 30,
        'description': 'Drivable roads only',
    },
}

_PRIVATE_ACCESS = {'private', 'no', 'customers', 'delivery', 'permit', 'military', 'restricted'}
_BAD_SERVICE = {'driveway', 'parking_aisle'}

ISSUE_PENALTIES = {
    'Pothole': 30,
    'Unsafe Area': 35,
    'Broken Streetlight': 25,
    'Narrow Lane': 15,
    'Other': 10,
}

# credibility = max(0.15, 0.20 * reports + 0.20 * confirmations - 0.10 * dismissals)
# All issues contribute some penalty — low-credibility ones are scaled down but not ignored.
# 1 report → 0.20, 5 reports → 1.0 (full weight).

ISSUE_RADIUS = {
    'walk': 40,
    'cycle': 50,
    'drive': 75,
}

_WALK_UNSAFE_HIGHWAYS = {'motorway', 'motorway_link', 'trunk', 'trunk_link'}

# OSMnx's built-in 'walk' filter can exclude secondary/primary roads in areas where
# OSM data lacks explicit pedestrian tags (common in Indian cities).  This explicit
# filter fetches the same road types the bike graph uses, then _sanitize_mode_edges
# strips genuinely non-pedestrian edges, giving walk routing the correct road set.
_WALK_CUSTOM_FILTER = (
    '["highway"~"footway|path|pedestrian|steps|corridor|living_street|residential|'
    'tertiary|tertiary_link|secondary|secondary_link|primary|primary_link|unclassified|service|track"]'
    '["area"!~"yes"]'
)
_CYCLE_UNSAFE_HIGHWAYS = {'motorway', 'motorway_link', 'trunk', 'trunk_link'}
_DRIVE_UNSUITABLE = {'footway', 'pedestrian', 'path', 'cycleway', 'steps'}

# OSM tags used to identify restricted areas (military, airports, etc.)
# These polygons are fetched once per graph and used to penalise edges inside them.
_RESTRICTED_AREA_TAGS = {
    'landuse': ['military'],
    'boundary': ['military'],
    'aeroway': ['aerodrome'],
}


def _fetch_restricted_geom(center_lat: float, center_lon: float, dist_m: int):
    """Return a merged Shapely polygon of all restricted areas near the route, or None."""
    from shapely.ops import unary_union
    polys = []
    try:
        gdf = ox.features_from_point((center_lat, center_lon), tags=_RESTRICTED_AREA_TAGS, dist=dist_m)
        for geom in gdf.geometry:
            if hasattr(geom, 'geom_type') and geom.geom_type in ('Polygon', 'MultiPolygon'):
                polys.append(geom)
    except Exception as e:
        print(f'[restricted-zones] fetch failed (non-fatal): {e}')
    if not polys:
        return None
    try:
        return unary_union(polys)
    except Exception:
        return None


def _stamp_restricted_edges(g, restricted_wgs84) -> int:
    """Mark edges whose midpoint falls inside a restricted area polygon.

    Returns the count of stamped edges.  The stamp is stored as edge attribute
    ``in_restricted_zone=True`` so it persists in the cached graph and in saved
    graphml files — avoiding repeat Overpass calls on subsequent requests.
    """
    if restricted_wgs84 is None:
        return 0
    crs = g.graph.get('crs')
    if crs is None:
        return 0
    try:
        restricted_proj, _ = ox.projection.project_geometry(
            restricted_wgs84, crs='EPSG:4326', to_crs=crs
        )
    except Exception as e:
        print(f'[restricted-zones] projection failed (non-fatal): {e}')
        return 0

    count = 0
    for u, v, key, data in g.edges(keys=True, data=True):
        if data.get('in_restricted_zone'):
            count += 1
            continue
        try:
            mid = Point(
                (g.nodes[u]['x'] + g.nodes[v]['x']) / 2,
                (g.nodes[u]['y'] + g.nodes[v]['y']) / 2,
            )
            if restricted_proj.contains(mid):
                data['in_restricted_zone'] = True
                count += 1
        except Exception:
            pass
    return count


def _prune_cache() -> None:
    now = time.time()
    stale_keys = [k for k, (created, _g) in _graph_cache.items() if now - created > _graph_cache_ttl_sec]
    for k in stale_keys:
        _graph_cache.pop(k, None)

    while len(_graph_cache) > max(1, GRAPH_CACHE_MAX_ITEMS):
        _graph_cache.popitem(last=False)


def _cache_get(key):
    _prune_cache()
    payload = _graph_cache.get(key)
    if not payload:
        return None
    created, graph = payload
    if time.time() - created > _graph_cache_ttl_sec:
        _graph_cache.pop(key, None)
        return None
    _graph_cache.move_to_end(key)
    return graph


def _cache_put(key, graph):
    _graph_cache[key] = (time.time(), graph)
    _graph_cache.move_to_end(key)
    _prune_cache()


def _cache_center(lat: float, lon: float) -> Tuple[float, float]:
    decimals = max(0, ROUTING_CACHE_CENTER_DECIMALS)
    return round(lat, decimals), round(lon, decimals)


def _preload_graph_file(mode: str, dist_m: int, simplify_graph: bool) -> Path:
    simp = 'simp' if simplify_graph else 'raw'
    return _graph_store_dir / f'{mode}_{dist_m}_{simp}.graphml'


def _compute_graph_bounds_latlon(g) -> Optional[Tuple[float, float, float, float]]:
    if not g or not g.nodes:
        return None

    xs = []
    ys = []
    for _nid, node in g.nodes(data=True):
        x = node.get('x')
        y = node.get('y')
        if x is None or y is None:
            continue
        xs.append(float(x))
        ys.append(float(y))

    if not xs or not ys:
        return None

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    crs = g.graph.get('crs')
    if crs is None:
        return None

    try:
        corners = [
            (min_x, min_y),
            (min_x, max_y),
            (max_x, min_y),
            (max_x, max_y),
        ]
        lats = []
        lons = []
        for x, y in corners:
            pt, _ = ox.projection.project_geometry(Point(x, y), crs=crs, to_crs='EPSG:4326')
            lon, lat = pt.coords[0]
            lats.append(float(lat))
            lons.append(float(lon))
        return (min(lats), max(lats), min(lons), max(lons))
    except Exception:
        return None


def _ensure_graph_bounds_latlon(g) -> None:
    if g.graph.get('bounds_latlon') is None:
        bounds = _compute_graph_bounds_latlon(g)
        if bounds is not None:
            g.graph['bounds_latlon'] = bounds


def _point_in_graph_bounds(g, lat: float, lon: float, margin_deg: float = 0.003) -> bool:
    bounds = g.graph.get('bounds_latlon')
    if bounds is None:
        _ensure_graph_bounds_latlon(g)
        bounds = g.graph.get('bounds_latlon')
    if bounds is None:
        return False
    lat_min, lat_max, lon_min, lon_max = bounds
    return (
        lat_min - margin_deg <= lat <= lat_max + margin_deg
        and lon_min - margin_deg <= lon <= lon_max + margin_deg
    )


def _register_preloaded_graph(mode: str, simplify_graph: bool, g) -> None:
    _ensure_graph_bounds_latlon(g)
    _preloaded_graphs[(mode, simplify_graph)] = g



def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def _project_issues(issues_data: List[dict], crs) -> List:
    projected = []
    for issue in issues_data:
        try:
            geom, _ = ox.projection.project_geometry(
                Point(issue['lon'], issue['lat']),
                crs='EPSG:4326',
                to_crs=crs,
            )
            projected.append((geom.x, geom.y, issue))
        except Exception:
            pass
    return projected


def compute_safety_score(edge_data: dict, mode: str = 'walk') -> float:
    highway = edge_data.get('highway', 'residential')
    if isinstance(highway, list):
        highway = highway[0]

    if mode == 'walk':
        road_scores = {
            'footway': 1.0,
            'pedestrian': 1.0,
            'living_street': 0.9,
            'residential': 0.85,
            'tertiary': 0.78,
            'secondary': 0.72,
            'primary': 0.65,
            'unclassified': 0.55,
            'service': 0.45,
            'track': 0.3,
            'trunk': 0.2,
            'motorway': 0.1,
        }
    elif mode == 'cycle':
        road_scores = {
            'cycleway': 1.0,
            'tertiary': 0.85,
            'residential': 0.8,
            'living_street': 0.8,
            'secondary': 0.75,
            'unclassified': 0.6,
            'primary': 0.55,
            'service': 0.4,
            'footway': 0.35,
            'track': 0.3,
            'trunk': 0.15,
            'motorway': 0.05,
        }
    else:
        road_scores = {
            'motorway': 1.0,
            'trunk': 0.95,
            'primary': 0.9,
            'secondary': 0.85,
            'tertiary': 0.75,
            'residential': 0.6,
            'unclassified': 0.5,
            'service': 0.35,
            'living_street': 0.3,
            'track': 0.2,
            'footway': 0.1,
            'pedestrian': 0.05,
        }

    road_type_score = road_scores.get(highway, 0.5)

    lit = edge_data.get('lit', None)
    if lit in ('yes', '24/7'):
        lighting_score = 1.0
    elif lit == 'no':
        lighting_score = 0.1
    else:
        lighting_score = road_type_score * 0.85

    if mode == 'walk':
        # For pedestrians "activity" = human foot traffic, not vehicle density.
        # Dedicated pedestrian infrastructure has high foot traffic = more eyes = safer.
        activity_map = {
            'pedestrian': 0.85,
            'footway': 0.75,
            'living_street': 0.65,
            'residential': 0.55,
            'primary': 0.9,    # busy roads have many witnesses
            'secondary': 0.8,
            'tertiary': 0.7,
            'cycleway': 0.5,
            'unclassified': 0.45,
            'service': 0.25,
            'track': 0.15,
        }
    else:
        activity_map = {
            'primary': 0.9,
            'secondary': 0.8,
            'tertiary': 0.7,
            'residential': 0.55,
            'living_street': 0.5,
            'cycleway': 0.5,
            'unclassified': 0.45,
            'footway': 0.4,
            'service': 0.25,
            'track': 0.15,
        }
    activity_score = activity_map.get(highway, 0.45)

    raw = 0.4 * lighting_score + 0.3 * activity_score + 0.3 * road_type_score
    return round(raw * 100, 1)


def _stamp_base_scores(g, mode: str) -> None:
    for _u, _v, _key, data in g.edges(keys=True, data=True):
        data['safety_score'] = compute_safety_score(data, mode)


def _sanitize_mode_edges(g, mode: str) -> None:
    def _val(v):
        return v[0] if isinstance(v, list) else v

    bad_edges = []
    for u, v, k, d in g.edges(keys=True, data=True):
        access = _val(d.get('access'))
        service = _val(d.get('service'))

        # Remove edges explicitly blocked for all users
        if access in _PRIVATE_ACCESS:
            bad_edges.append((u, v, k))
            continue

        if mode == 'walk':
            # Only remove edges explicitly tagged no-pedestrian access
            # Do NOT remove service=driveway/parking_aisle — in Indian residential
            # areas these connect neighbourhoods and are routinely walked through
            if _val(d.get('foot')) in ('no', 'private'):
                bad_edges.append((u, v, k))
        elif mode == 'cycle':
            if service in _BAD_SERVICE:
                bad_edges.append((u, v, k))
        else:  # drive
            if service in _BAD_SERVICE:
                bad_edges.append((u, v, k))

    if bad_edges:
        g.remove_edges_from(bad_edges)


def _largest_connected(g, mode: str):
    # Use weakly connected for all modes — we do undirected pathfinding for all modes
    # to avoid one-way detours caused by inaccurate Indian OSM one-way tags.
    try:
        nodes = max(nx.weakly_connected_components(g), key=len)
        return g.subgraph(nodes).copy()
    except Exception:
        return g


def _build_graph(center_lat: float, center_lon: float, dist: int, mode: str, simplify_graph: bool):
    config = MODE_CONFIG[mode]
    if mode == 'walk':
        try:
            g = ox.graph_from_point(
                (center_lat, center_lon),
                dist=dist,
                custom_filter=_WALK_CUSTOM_FILTER,
                simplify=simplify_graph,
            )
        except Exception:
            g = ox.graph_from_point(
                (center_lat, center_lon),
                dist=dist,
                network_type='walk',
                simplify=simplify_graph,
            )
    else:
        g = ox.graph_from_point(
            (center_lat, center_lon),
            dist=dist,
            network_type=config['network_type'],
            simplify=simplify_graph,
        )
    g = ox.project_graph(g)
    _sanitize_mode_edges(g, mode)
    g = _largest_connected(g, mode)
    _stamp_base_scores(g, mode)
    _ensure_graph_bounds_latlon(g)
    try:
        restricted = _fetch_restricted_geom(center_lat, center_lon, dist)
        n = _stamp_restricted_edges(g, restricted)
        if n:
            print(f'[restricted-zones] stamped {n} edges in restricted areas')
    except Exception as e:
        print(f'[restricted-zones] skipped ({e})')
    return g


def get_graph(origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float, mode: str = 'drive'):
    if mode not in MODE_CONFIG:
        mode = 'walk'

    center_lat = (origin_lat + dest_lat) / 2
    center_lon = (origin_lon + dest_lon) / 2

    straight = _haversine_m(origin_lat, origin_lon, dest_lat, dest_lon)
    raw_dist = straight * 1.5 + 800
    dist = int(round(min(max(raw_dist, ROUTING_GRAPH_MIN_DIST_M), ROUTING_GRAPH_MAX_DIST_M) / 500) * 500)

    simplify_graph = False
    center_key = _cache_center(center_lat, center_lon)
    cache_key = (mode, center_key[0], center_key[1], dist, simplify_graph)

    cached = _cache_get(cache_key)
    if cached is not None:
        print(f'[routing] cache hit: {cache_key}')
        return cached

    preloaded = _preloaded_graphs.get((mode, simplify_graph))
    if preloaded and _point_in_graph_bounds(preloaded, origin_lat, origin_lon) and _point_in_graph_bounds(
        preloaded, dest_lat, dest_lon
    ):
        print(f'[routing] preload hit: mode={mode}, simplify={simplify_graph}, dist={dist}')
        _cache_put(cache_key, preloaded)
        return preloaded

    print(f'[routing] downloading {mode} graph, dist={dist} m, simplify={simplify_graph} ...')
    g = _build_graph(center_lat, center_lon, dist, mode, simplify_graph)
    _cache_put(cache_key, g)
    print(f'[routing] graph ready: {len(g.nodes)} nodes, {len(g.edges)} edges, cache={len(_graph_cache)}')
    return g


def preload_city_graphs() -> None:
    """Warm in-memory cache using persistent graph files around default city center.

    This accelerates first-user routing by avoiding runtime Overpass downloads
    for common city-area routes.
    """

    _graph_store_dir.mkdir(parents=True, exist_ok=True)
    dist = int(round(min(max(ROUTING_PRELOAD_DIST_M, ROUTING_GRAPH_MIN_DIST_M), ROUTING_GRAPH_MAX_DIST_M) / 500) * 500)
    simplify_graph = True

    for mode in ROUTING_PRELOAD_MODES:
        if mode not in MODE_CONFIG:
            continue

        path = _preload_graph_file(mode, dist, simplify_graph)
        center_key = _cache_center(DEFAULT_CITY_LAT, DEFAULT_CITY_LON)
        cache_key = (mode, center_key[0], center_key[1], dist, simplify_graph)

        graph = _cache_get(cache_key)
        if graph is not None:
            _register_preloaded_graph(mode, simplify_graph, graph)
            continue

        try:
            if path.exists():
                print(f'[routing-preload] loading graph file: {path.name}')
                graph = ox.load_graphml(path)
                _sanitize_mode_edges(graph, mode)
                graph = _largest_connected(graph, mode)
                _stamp_base_scores(graph, mode)
                _ensure_graph_bounds_latlon(graph)
                # Stamp restricted zones if not already present in the saved file
                needs_stamp = not any(
                    d.get('in_restricted_zone')
                    for _, _, d in graph.edges(data=True)
                )
                if needs_stamp:
                    try:
                        restricted = _fetch_restricted_geom(DEFAULT_CITY_LAT, DEFAULT_CITY_LON, dist)
                        n = _stamp_restricted_edges(graph, restricted)
                        if n:
                            print(f'[restricted-zones] preload stamped {n} edges')
                    except Exception as e:
                        print(f'[restricted-zones] preload skipped ({e})')
            else:
                print(f'[routing-preload] building graph for {mode}, dist={dist}m ...')
                graph = _build_graph(DEFAULT_CITY_LAT, DEFAULT_CITY_LON, dist, mode, simplify_graph)
                ox.save_graphml(graph, path)
                print(f'[routing-preload] saved graph file: {path.name}')

            _cache_put(cache_key, graph)
            _register_preloaded_graph(mode, simplify_graph, graph)
            print(f'[routing-preload] ready mode={mode}, nodes={len(graph.nodes)}, edges={len(graph.edges)}')
        except Exception as exc:
            print(f'[routing-preload] skipped mode={mode}: {exc}')



def _decode_polyline(encoded: str) -> List[Tuple[float, float]]:
    """Decode a Google-encoded polyline string into a list of (lat, lon) tuples."""
    coords, index, lat, lng = [], 0, 0, 0
    n = len(encoded)
    while index < n:
        result, shift = 0, 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lat += (~(result >> 1) if result & 1 else result >> 1)
        result, shift = 0, 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lng += (~(result >> 1) if result & 1 else result >> 1)
        coords.append((lat / 1e5, lng / 1e5))
    return coords


_OLA_MODE_MAP = {'drive': 'driving', 'walk': 'walking', 'cycle': 'bike'}

_ORS_PROFILE_MAP = {'walk': 'foot-walking', 'cycle': 'cycling-regular', 'drive': 'driving-car'}


def _fetch_ors_safe_route(
    origin_lat: float, origin_lon: float,
    dest_lat: float, dest_lon: float,
    mode: str, api_key: str,
) -> Optional[dict]:
    """Call OpenRouteService for a safety-preferred route.

    Uses preference='recommended' which avoids highways/motorways and prefers
    pedestrian/cycling infrastructure — better real-world geometry than OSMnx.
    Returns {coords, duration_min, dist_km, steps} or None on any failure.
    """
    if not api_key:
        return None
    profile = _ORS_PROFILE_MAP.get(mode, 'foot-walking')
    try:
        # shortest = most direct pedestrian path, less likely to route on wrong carriageway
        # recommended = prefer cycling infrastructure; fastest = direct driving path
        _pref = {'walk': 'recommended', 'cycle': 'recommended', 'drive': 'fastest'}.get(mode, 'fastest')
        body: dict = {
            'coordinates': [[origin_lon, origin_lat], [dest_lon, dest_lat]],
            'preference': _pref,
            'instructions': True,
            'geometry': True,
            'units': 'km',
        }
        if mode == 'walk':
            body['options'] = {'avoid_features': ['ferries']}
        elif mode == 'cycle':
            body['options'] = {'avoid_features': ['ferries']}

        resp = _requests.post(
            f'https://api.openrouteservice.org/v2/directions/{profile}/geojson',
            headers={
                'Authorization': api_key,
                'Content-Type': 'application/json',
            },
            json=body,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f'[ors] HTTP {resp.status_code}: {resp.text[:200]}')
            return None
        data = resp.json()

        features = data.get('features', [])
        if not features:
            print('[ors] no features in response')
            return None

        feature = features[0]
        geom_coords = feature.get('geometry', {}).get('coordinates', [])
        if len(geom_coords) < 2:
            print('[ors] too few coordinates')
            return None

        # ORS GeoJSON coords are already [lon, lat]
        coords = [[round(c[0], 6), round(c[1], 6)] for c in geom_coords]

        props = feature.get('properties', {})
        summary = props.get('summary', {})
        dist_km = round(summary.get('distance', 0.0), 2)
        duration_min = round(summary.get('duration', 0.0) / 60.0, 1)

        segments = props.get('segments', [])
        steps: List[dict] = []
        if segments:
            for seg in segments:
                for s in seg.get('steps', []):
                    instr = s.get('instruction', '')
                    if instr:
                        steps.append({
                            'instruction': instr,
                            'distance_m': int(round(s.get('distance', 0) * 1000)),
                            'street': s.get('name', '') or '',
                        })
        if steps:
            steps.append({'instruction': 'Arrive at your destination', 'distance_m': 0, 'street': ''})

        print(f'[ors] safe route: {dist_km} km, {duration_min} min, {len(coords)} pts')
        return {'coords': coords, 'duration_min': duration_min, 'dist_km': dist_km, 'steps': steps}

    except Exception as exc:
        print(f'[ors] fetch failed: {exc}')
        return None


def _fetch_ola_fast_route(
    origin_lat: float, origin_lon: float,
    dest_lat: float, dest_lon: float,
    mode: str, api_key: str,
) -> Optional[dict]:
    """Call OLA Maps Directions API for a traffic-aware fast route.

    Returns a dict {coords, duration_min, dist_km, steps} or None on any
    failure so the caller silently falls back to the OSMnx fast route.
    """
    if not api_key:
        return None
    try:
        resp = _requests.post(
            'https://api.olamaps.io/routing/v1/directions',
            params={
                'origin': f'{origin_lat},{origin_lon}',
                'destination': f'{dest_lat},{dest_lon}',
                'mode': _OLA_MODE_MAP.get(mode, 'driving'),
                'overview': 'full',
                'steps': 'true',
                'api_key': api_key,
            },
            timeout=8,
        )
        if resp.status_code != 200:
            print(f'[ola-directions] HTTP {resp.status_code}')
            return None
        data = resp.json()
        if data.get('status') != 'SUCCESS' or not data.get('routes'):
            print(f'[ola-directions] status={data.get("status")}')
            return None

        route = data['routes'][0]
        leg = route['legs'][0]
        encoded = route.get('overview_polyline', '')
        if not encoded:
            return None

        latlon_pairs = _decode_polyline(encoded)
        if len(latlon_pairs) < 2:
            return None

        coords = [[round(lon, 6), round(lat, 6)] for lat, lon in latlon_pairs]
        duration_min = round(leg['duration'] / 60.0, 1)
        dist_km = round(leg['distance'] / 1000.0, 2)

        raw_steps = leg.get('steps', [])
        steps = [
            {'instruction': s.get('instructions', ''), 'distance_m': int(s.get('distance', 0)), 'street': ''}
            for s in raw_steps if s.get('instructions')
        ]
        if steps:
            steps.append({'instruction': 'Arrive at your destination', 'distance_m': 0, 'street': ''})

        print(f'[ola-directions] traffic-aware fast route: {dist_km} km, {duration_min} min')
        return {'coords': coords, 'duration_min': duration_min, 'dist_km': dist_km, 'steps': steps}

    except Exception as exc:
        print(f'[ola-directions] failed: {exc}')
        return None


def _fetch_ola_all_routes(
    origin_lat: float, origin_lon: float,
    dest_lat: float, dest_lon: float,
    mode: str, api_key: str,
) -> List[dict]:
    """Call OLA Maps Directions API with alternatives=true for any travel mode.

    Returns a list of route dicts {coords, duration_min, dist_km, steps}.
    OLA supports driving (traffic-aware), walking, and bike modes.
    Returns empty list on any failure so the caller falls back to OSMnx.
    """
    if not api_key:
        return []
    try:
        resp = _requests.post(
            'https://api.olamaps.io/routing/v1/directions',
            params={
                'origin': f'{origin_lat},{origin_lon}',
                'destination': f'{dest_lat},{dest_lon}',
                'mode': _OLA_MODE_MAP.get(mode, 'driving'),
                'overview': 'full',
                'steps': 'true',
                'alternatives': 'true',
                'api_key': api_key,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print(f'[ola-directions] HTTP {resp.status_code}')
            return []
        data = resp.json()
        if data.get('status') != 'SUCCESS' or not data.get('routes'):
            print(f'[ola-directions] no routes, status={data.get("status")}')
            return []

        results = []
        for route in data['routes']:
            leg = route.get('legs', [{}])[0]
            encoded = route.get('overview_polyline', '')
            if not encoded:
                continue
            latlon_pairs = _decode_polyline(encoded)
            if len(latlon_pairs) < 2:
                continue
            coords = [[round(lon, 6), round(lat, 6)] for lat, lon in latlon_pairs]
            duration_min = round(leg.get('duration', 0) / 60.0, 1)
            dist_km = round(leg.get('distance', 0) / 1000.0, 2)
            raw_steps = leg.get('steps', [])
            steps = [
                {'instruction': s.get('instructions', ''), 'distance_m': int(s.get('distance', 0)), 'street': ''}
                for s in raw_steps if s.get('instructions')
            ]
            if steps:
                steps.append({'instruction': 'Arrive at your destination', 'distance_m': 0, 'street': ''})
            results.append({'coords': coords, 'duration_min': duration_min, 'dist_km': dist_km, 'steps': steps})

        print(f'[ola-directions] {len(results)} route(s) for mode={mode}')
        return results

    except Exception as exc:
        print(f'[ola-directions] all-routes fetch failed: {exc}')
        return []


def _score_ola_route(
    g_proj, coords_lonlat: List, proj_issues: List, kdtree, mode: str, hour: int,
    edge_kdtree=None, edge_score_list: List[float] = None,
    base_score_list: List[float] = None,
) -> Tuple[float, float, List[dict]]:
    """Compute safety score and nearby-issue list for an OLA Maps route.

    Returns (penalized_score, road_baseline, issues_detail).

    road_baseline uses base_score_list (raw road-type scores, no issue penalties)
    so the displayed score for ORS safe routes never drops just from issue reports
    unless the fallback threshold triggers and the route actually changes.
    """
    if not coords_lonlat:
        return 65.0, 65.0, []
    crs = g_proj.graph.get('crs')
    if not crs:
        return 65.0, 65.0, []

    # Road-type baseline: length-weighted average of nearest OSMnx edge scores.
    # Uses base_score_list (raw road-type safety_score, no issue penalties) so
    # issue reports cannot affect the displayed safe-route score unless the route
    # itself changes (i.e. ORS→OSMnx fallback triggers).
    _scores_for_baseline = base_score_list if base_score_list else edge_score_list
    road_baseline = 65.0
    if edge_kdtree is not None and _scores_for_baseline:
        proj_pts: list = []  # (x, y, score) in projected CRS
        step_e = max(1, len(coords_lonlat) // 60)
        for lonlat in coords_lonlat[::step_e]:
            try:
                pt, _ = ox.projection.project_geometry(
                    Point(lonlat[0], lonlat[1]), crs='EPSG:4326', to_crs=crs
                )
                idx = edge_kdtree.query([pt.x, pt.y])[1]
                proj_pts.append((pt.x, pt.y, _scores_for_baseline[idx]))
            except Exception:
                pass
        if len(proj_pts) == 1:
            road_baseline = round(proj_pts[0][2], 1)
        elif len(proj_pts) > 1:
            weighted_sum = 0.0
            total_w = 0.0
            for i in range(len(proj_pts) - 1):
                x0, y0, s0 = proj_pts[i]
                x1, y1, s1 = proj_pts[i + 1]
                seg = math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2)
                weighted_sum += ((s0 + s1) / 2.0) * seg
                total_w += seg
            if total_w > 0:
                road_baseline = round(weighted_sum / total_w, 1)

    if kdtree is None or not proj_issues:
        return road_baseline, road_baseline, []

    radius = ISSUE_RADIUS.get(mode, 50)
    hit_indices: set = set()
    step = max(1, len(coords_lonlat) // 80)
    for lonlat in coords_lonlat[::step]:
        try:
            pt, _ = ox.projection.project_geometry(
                Point(lonlat[0], lonlat[1]), crs='EPSG:4326', to_crs=crs
            )
            hit_indices.update(kdtree.query_ball_point([pt.x, pt.y], r=radius))
        except Exception:
            pass

    issues_detail: List[dict] = []
    total_penalty = 0.0
    for idx in hit_indices:
        _x, _y, issue = proj_issues[idx]
        cat = issue.get('category', 'Other')
        conf = float(issue.get('effective_confidence', issue.get('confidence_score', 65)))
        n_rep = issue.get('num_reports', 1)
        n_con = issue.get('num_confirmations', 0)
        n_dis = issue.get('num_dismissals', 0)
        credibility = min(1.0, max(0.15, 0.20 * n_rep + 0.20 * n_con - 0.10 * n_dis))
        severity_factor = {'low': 0.5, 'medium': 1.0, 'high': 1.5}.get(
            issue.get('severity', 'medium'), 1.0
        )
        total_penalty += (
            ISSUE_PENALTIES.get(cat, 10)
            * credibility * conf
            * _category_time_factor(cat, hour)
            * severity_factor
            / 100.0
        )
        issues_detail.append({
            'id': issue.get('id'),
            'lat': issue.get('lat'),
            'lon': issue.get('lon'),
            'category': cat,
            'severity': issue.get('severity', 'medium'),
            'description': issue.get('description', ''),
            'effective_confidence': conf,
            'num_reports': n_rep,
            'num_confirmations': n_con,
            'num_dismissals': n_dis,
        })

    issues_detail.sort(key=lambda x: (
        -float(x.get('effective_confidence', 0)),
        -(int(x.get('num_reports', 0)) + int(x.get('num_confirmations', 0))),
    ))
    safety_score = max(0.0, min(100.0, road_baseline - min(total_penalty, road_baseline * 0.8)))
    return round(safety_score, 1), round(road_baseline, 1), issues_detail


def _category_time_factor(category: str, current_hour: int) -> float:
    # Night: 6 pm (18) to 6 am — covers Bangalore's earliest sunset (~5:55 pm Dec)
    is_night = current_hour >= 18 or current_hour < 6
    if category == 'Broken Streetlight':
        return 1.8 if is_night else 1.0   # neutral during day, not helpful
    if category == 'Unsafe Area':
        return 1.4 if is_night else 1.0   # neutral during day
    return 1.0


def _mode_edge_penalty(edge_data: dict, mode: str) -> float:
    highway = edge_data.get('highway', 'residential')
    if isinstance(highway, list):
        highway = highway[0]

    bridge = edge_data.get('bridge')
    sidewalk = edge_data.get('sidewalk')
    cycleway = edge_data.get('cycleway')
    maxspeed = edge_data.get('maxspeed')

    penalty = 0.0

    if mode == 'walk':
        if highway in _WALK_UNSAFE_HIGHWAYS:
            penalty += 22.0
        if bridge and bridge != 'no':
            penalty += 8.0
        if sidewalk in ('no', 'none'):
            penalty += 10.0
        if edge_data.get('foot') in ('no', 'private'):
            penalty += 30.0

    elif mode == 'cycle':
        if highway in _CYCLE_UNSAFE_HIGHWAYS:
            penalty += 26.0
        if cycleway in (None, 'no', 'none') and highway in {'primary', 'secondary', 'tertiary'}:
            penalty += 12.0
        if bridge and bridge != 'no':
            penalty += 6.0
        if isinstance(maxspeed, str):
            digits = ''.join(ch for ch in maxspeed if ch.isdigit())
            if digits:
                try:
                    if int(digits) >= 60:
                        penalty += 6.0
                except ValueError:
                    pass

    else:
        if highway in _DRIVE_UNSUITABLE:
            penalty += 50.0
        if edge_data.get('motor_vehicle') in ('no', 'private'):
            penalty += 50.0

    return penalty


def _precompute_safe_weights(g, mode: str, issues_data: Optional[List[dict]], current_hour: Optional[int] = None):
    import numpy as np
    from scipy.spatial import cKDTree

    kdtree = None
    proj_issues: List = []
    radius = ISSUE_RADIUS.get(mode, 50)

    if issues_data:
        try:
            proj_issues = _project_issues(issues_data, g.graph['crs'])
            if proj_issues:
                coords = np.array([[x, y] for x, y, _ in proj_issues])
                kdtree = cKDTree(coords)
        except Exception as e:
            print(f'[routing] KDTree build failed ({type(e).__name__}: {e})')
            proj_issues = []
            kdtree = None

    safe_weights: dict = {}
    adj_scores: dict = {}
    hour = datetime.now(_IST).hour if current_hour is None else int(current_hour)

    for u, v, key, data in g.edges(keys=True, data=True):
        base_score = float(data.get('safety_score', 50.0))
        length = float(data.get('length', 1.0))

        issue_penalty = 0.0
        if kdtree is not None:
            u_node = g.nodes[u]
            v_node = g.nodes[v]
            mid_x = (u_node['x'] + v_node['x']) / 2
            mid_y = (u_node['y'] + v_node['y']) / 2

            nearby = kdtree.query_ball_point([mid_x, mid_y], r=radius)
            for idx in nearby:
                _, _, issue = proj_issues[idx]
                cat = issue.get('category', 'Other')
                conf = float(issue.get('effective_confidence', issue.get('confidence_score', 65)))
                n_rep = issue.get('num_reports', 1)
                n_con = issue.get('num_confirmations', 0)
                n_dis = issue.get('num_dismissals', 0)
                credibility = min(1.0, max(0.15, 0.20 * n_rep + 0.20 * n_con - 0.10 * n_dis))
                severity_factor = {'low': 0.5, 'medium': 1.0, 'high': 1.5}.get(
                    issue.get('severity', 'medium'), 1.0
                )
                issue_penalty += (
                    ISSUE_PENALTIES.get(cat, 10)
                    * credibility * conf
                    * _category_time_factor(cat, hour)
                    * severity_factor
                    / 100.0
                )

            issue_penalty = min(issue_penalty, 60.0)

        mode_penalty = _mode_edge_penalty(data, mode)

        # Ambient darkness penalty: at night, unlit/unknown-lit roads are inherently less safe.
        # Pedestrians are most vulnerable (no headlights), cyclists intermediate, drivers least.
        is_night = (hour >= 18 or hour < 6)
        if is_night:
            lit = data.get('lit')
            if lit in ('yes', '24/7'):
                dark_penalty = 0.0
            elif lit == 'no':   # confirmed dark road — scale by mode vulnerability
                dark_penalty = 25.0 if mode == 'walk' else (18.0 if mode == 'cycle' else 10.0)
            else:               # unknown lit tag — most Indian roads lack this
                dark_penalty = 12.0 if mode == 'walk' else (8.0 if mode == 'cycle' else 4.0)
        else:
            dark_penalty = 0.0

        restricted_penalty = 90.0 if data.get('in_restricted_zone') else 0.0
        adj_score = max(0.0, base_score - issue_penalty - mode_penalty - dark_penalty - restricted_penalty)
        adj_scores[(u, v, key)] = adj_score
        # Issue surcharge is applied separately so that issue-heavy edges are
        # meaningfully more expensive for Dijkstra — folding issue_penalty into
        # adj_score alone only shifts the multiplier by ~8%, not enough to reroute.
        issue_surcharge = min((issue_penalty / 8.0) * length, length * 2.5)
        safe_weights[(u, v, key)] = length * (2.0 - adj_score / 100.0) + issue_surcharge

    return safe_weights, adj_scores, proj_issues, kdtree


def _build_edge_kdtree(g, adj_scores: dict):
    """Build a KDTree of edge midpoints with two parallel score lists.

    adj_score_list  — penalised scores (issues, mode, darkness) used for the
                      full penalised score calculation.
    base_score_list — raw road-type safety_score before ANY penalties, used
                      for road_baseline so reported issues never alter the
                      displayed score when the route itself hasn't changed.
    """
    import numpy as np
    from scipy.spatial import cKDTree

    mids: list = []
    adj_score_list: list = []
    base_score_list: list = []
    for u, v, key, data in g.edges(keys=True, data=True):
        un, vn = g.nodes[u], g.nodes[v]
        mids.append([(un['x'] + vn['x']) / 2.0, (un['y'] + vn['y']) / 2.0])
        adj_score_list.append(adj_scores.get((u, v, key), float(data.get('safety_score', 50.0))))
        base_score_list.append(float(data.get('safety_score', 50.0)))

    if not mids:
        return None, [], []

    return cKDTree(np.array(mids)), adj_score_list, base_score_list


def _edge_payload(g_proj, u, v):
    if not g_proj.has_edge(u, v):
        return None, None
    edge_dict = g_proj[u][v]
    key = 0 if 0 in edge_dict else list(edge_dict.keys())[0]
    return key, edge_dict[key]


def _collect_issue_indices_on_path(g_proj, node_list: List, mode: str, projected_issues: List, kdtree):
    if kdtree is None or not projected_issues:
        return set()

    radius = ISSUE_RADIUS.get(mode, 50)
    hit_indices: set = set()
    for i in range(len(node_list) - 1):
        u, v = node_list[i], node_list[i + 1]
        if not g_proj.has_node(u) or not g_proj.has_node(v):
            continue
        u_node = g_proj.nodes[u]
        v_node = g_proj.nodes[v]
        mid_x = (u_node['x'] + v_node['x']) / 2
        mid_y = (u_node['y'] + v_node['y']) / 2
        hit_indices.update(kdtree.query_ball_point([mid_x, mid_y], r=radius))
    return hit_indices


def count_issues_on_route(g_proj, node_list: List, mode: str, projected_issues: List, kdtree) -> int:
    return len(_collect_issue_indices_on_path(g_proj, node_list, mode, projected_issues, kdtree))


def collect_issues_on_route(g_proj, node_list: List, mode: str, projected_issues: List, kdtree) -> List[dict]:
    details: List[dict] = []
    for idx in _collect_issue_indices_on_path(g_proj, node_list, mode, projected_issues, kdtree):
        _x, _y, issue = projected_issues[idx]
        details.append(
            {
                'id': issue.get('id'),
                'lat': issue.get('lat'),
                'lon': issue.get('lon'),
                'category': issue.get('category', 'Other'),
                'description': issue.get('description', ''),
                'effective_confidence': issue.get('effective_confidence', issue.get('confidence_score', 0)),
                'num_reports': issue.get('num_reports', 0),
                'num_confirmations': issue.get('num_confirmations', 0),
                'num_dismissals': issue.get('num_dismissals', 0),
            }
        )

    details.sort(
        key=lambda x: (
            -float(x.get('effective_confidence', 0)),
            -(int(x.get('num_reports', 0)) + int(x.get('num_confirmations', 0))),
        )
    )
    return details


def _parse_maxspeed_kmh(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v > 0 else None
    if not isinstance(value, str):
        return None

    token = value.strip().lower()
    digits = ''.join(ch for ch in token if ch.isdigit())
    if not digits:
        return None
    try:
        num = float(digits)
    except ValueError:
        return None
    if 'mph' in token:
        num *= 1.60934
    return num if num > 0 else None


def _edge_speed_kmh(edge: dict, mode: str) -> float:
    highway = edge.get('highway', 'residential')
    if isinstance(highway, list):
        highway = highway[0]

    if mode == 'walk':
        base = {
            'footway': 5.0,
            'pedestrian': 5.0,
            'residential': 4.8,
            'living_street': 4.8,
            'tertiary': 4.7,
            'secondary': 4.7,
            'primary': 4.6,
            'service': 4.5,
            'track': 4.0,
        }.get(highway, 4.6)
    elif mode == 'cycle':
        base = {
            'cycleway': 18.0,
            'residential': 15.5,
            'tertiary': 16.0,
            'secondary': 14.5,
            'primary': 13.5,
            'service': 13.0,
            'track': 11.0,
        }.get(highway, 14.0)
    else:
        base = {
            'motorway': 72.0,
            'trunk': 58.0,
            'primary': 45.0,
            'secondary': 36.0,
            'tertiary': 30.0,
            'residential': 24.0,
            'service': 18.0,
            'living_street': 15.0,
            'unclassified': 22.0,
        }.get(highway, 24.0)

    tagged = _parse_maxspeed_kmh(edge.get('maxspeed'))
    if mode == 'drive' and tagged is not None:
        # Blend tagged speed with conservative city factor.
        return max(10.0, min(85.0, tagged * 0.72))

    return base


def get_route_stats(g_proj, node_list: List, mode: str, adj_scores: dict = None):
    if len(node_list) < 2:
        return 0.0, 0.0, 0

    total_length = 0.0
    weighted_safety = 0.0
    total_minutes = 0.0

    for i in range(len(node_list) - 1):
        u, v = node_list[i], node_list[i + 1]
        if not g_proj.has_edge(u, v):
            continue

        key, edge = _edge_payload(g_proj, u, v)
        if edge is None:
            continue

        edge_len = float(edge.get('length', 0.0))
        total_length += edge_len

        if adj_scores is not None and (u, v, key) in adj_scores:
            score = adj_scores[(u, v, key)]
        else:
            score = float(edge.get('safety_score', 50.0))
        weighted_safety += score * edge_len

        speed_kmh = _edge_speed_kmh(edge, mode)
        if speed_kmh > 0:
            total_minutes += (edge_len / 1000.0) / speed_kmh * 60.0

    if total_length == 0:
        return 0.0, 0.0, 0

    avg_safety = round(weighted_safety / total_length, 1)
    dist_km = round(total_length / 1000, 2)
    if total_minutes <= 0:
        speed_kmh = MODE_CONFIG[mode]['speed_kmh']
        total_minutes = (dist_km / speed_kmh) * 60.0
    time_min = round(total_minutes, 1)
    return avg_safety, dist_km, time_min


def nodes_to_geojson_coords(g_proj, node_list: List) -> List:
    if not node_list:
        return []

    crs = g_proj.graph['crs']

    def _node_lonlat(node):
        nd = g_proj.nodes[node]
        pt, _ = ox.projection.project_geometry(Point(nd['x'], nd['y']), crs=crs, to_crs='EPSG:4326')
        lon, lat = pt.coords[0]
        return [round(lon, 6), round(lat, 6)]

    coords = []
    for i in range(len(node_list) - 1):
        u, v = node_list[i], node_list[i + 1]
        if not g_proj.has_edge(u, v):
            if not coords and u in g_proj.nodes:
                coords.append(_node_lonlat(u))
            continue

        _key, edge = _edge_payload(g_proj, u, v)
        geom = edge.get('geometry') if edge else None

        if geom is not None:
            try:
                lonlat_line, _ = ox.projection.project_geometry(geom, crs=crs, to_crs='EPSG:4326')
                pts = [[round(p[0], 6), round(p[1], 6)] for p in lonlat_line.coords]
                if not coords:
                    coords.extend(pts)
                else:
                    coords.extend(pts[1:])
                continue
            except Exception:
                pass

        if not coords:
            coords.append(_node_lonlat(u))
        coords.append(_node_lonlat(v))

    return coords


def _edge_name(edge_data: dict) -> str:
    name = edge_data.get('name')
    if isinstance(name, list):
        name = name[0] if name else None
    if not name:
        return 'unnamed road'
    return str(name)


def _bearing_deg(x1: float, y1: float, x2: float, y2: float) -> float:
    angle = math.degrees(math.atan2(x2 - x1, y2 - y1))
    return (angle + 360.0) % 360.0


def _compass_dir(bearing: float) -> str:
    dirs = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west']
    idx = int((bearing + 22.5) // 45) % 8
    return dirs[idx]


def _turn_label(delta: float) -> str:
    if delta > 180:
        delta -= 360
    if delta < -180:
        delta += 360

    if -25 <= delta <= 25:
        return 'Continue'
    if 25 < delta <= 70:
        return 'Slight right'
    if 70 < delta <= 140:
        return 'Turn right'
    if delta > 140:
        return 'Make a U-turn'
    if -70 <= delta < -25:
        return 'Slight left'
    if -140 <= delta < -70:
        return 'Turn left'
    return 'Make a U-turn'


def _human_meters(meters: float) -> int:
    if meters < 20:
        return 20
    return int(round(meters / 10.0) * 10)


def build_turn_steps(g_proj, node_list: List) -> List[dict]:
    if len(node_list) < 2:
        return []

    segments: List[dict] = []
    for i in range(len(node_list) - 1):
        u, v = node_list[i], node_list[i + 1]
        _key, edge = _edge_payload(g_proj, u, v)
        if not edge:
            continue

        name = _edge_name(edge)
        length = float(edge.get('length', 0.0))
        un = g_proj.nodes[u]
        vn = g_proj.nodes[v]
        bearing = _bearing_deg(un['x'], un['y'], vn['x'], vn['y'])

        if segments and segments[-1]['name'] == name:
            segments[-1]['length'] += length
            segments[-1]['bearing'] = bearing
        else:
            segments.append({'name': name, 'length': length, 'bearing': bearing})

    if not segments:
        return []

    steps: List[dict] = []
    first = segments[0]
    first_dist = _human_meters(first['length'])
    steps.append(
        {
            'instruction': f"Head {_compass_dir(first['bearing'])} on {first['name']} for {first_dist} m",
            'distance_m': first_dist,
            'street': first['name'],
        }
    )

    for i in range(1, len(segments)):
        prev = segments[i - 1]
        cur = segments[i]
        delta = cur['bearing'] - prev['bearing']
        label = _turn_label(delta)
        dist = _human_meters(cur['length'])

        if label == 'Continue':
            text = f"Continue on {cur['name']} for {dist} m"
        else:
            text = f"{label} onto {cur['name']} and continue for {dist} m"

        steps.append({'instruction': text, 'distance_m': dist, 'street': cur['name']})

    steps.append({'instruction': 'Arrive at your destination', 'distance_m': 0, 'street': ''})
    return steps


def make_geojson_feature(
    coords: List,
    route_type: str,
    safety_score: float,
    distance_km: float,
    duration_min: int,
    mode: str,
    issues_on_path: int = 0,
    route_issues: Optional[List[dict]] = None,
    steps: Optional[List[dict]] = None,
) -> dict:
    return {
        'type': 'Feature',
        'geometry': {'type': 'LineString', 'coordinates': coords},
        'properties': {
            'route_type': route_type,
            'safety_score': safety_score,
            'distance_km': distance_km,
            'duration_min': duration_min,
            'travel_mode': mode,
            'issues_on_path': issues_on_path,
            'route_issues': route_issues or [],
            'description': f'Safest {mode} route' if route_type == 'safe' else f'Shortest {mode} route',
            'steps': steps or [],
        },
    }


def _nearest_node_on_graph(g_proj, x: float, y: float) -> int:
    """Snap to the nearest edge first, then pick the closer endpoint.

    nearest_nodes() snaps to intersections only — on simplified graphs the
    nearest intersection can be hundreds of metres away in the wrong direction,
    causing the router to backtrack before heading toward the destination.
    Snapping via the nearest edge gives a much tighter, more intuitive start/end.
    """
    try:
        ne = ox.distance.nearest_edges(g_proj, X=x, Y=y)
        u, v = ne[0], ne[1]
        un, vn = g_proj.nodes[u], g_proj.nodes[v]
        d_u = (un['x'] - x) ** 2 + (un['y'] - y) ** 2
        d_v = (vn['x'] - x) ** 2 + (vn['y'] - y) ** 2
        return u if d_u <= d_v else v
    except Exception:
        return ox.distance.nearest_nodes(g_proj, X=x, Y=y)


def get_routes(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    mode: str = 'walk',
    issues_data: Optional[List[dict]] = None,
) -> dict:
    if mode not in MODE_CONFIG:
        mode = 'walk'

    config = MODE_CONFIG[mode]
    g_proj = get_graph(origin_lat, origin_lon, dest_lat, dest_lon, mode)

    safe_weights, adj_scores, proj_issues, kdtree = _precompute_safe_weights(g_proj, mode, issues_data)
    edge_kdtree, edge_score_list, base_score_list = _build_edge_kdtree(g_proj, adj_scores)

    _hour = datetime.now(_IST).hour

    safe_coords: Optional[List] = None
    fast_coords: Optional[List] = None
    safe_dist = fast_dist = 0.0
    safe_time = fast_time = 0.0
    safe_score = fast_score = 0.0
    safe_steps: List[dict] = []
    fast_steps: List[dict] = []
    safe_issue_details: List[dict] = []
    fast_issue_details: List[dict] = []
    fast_route_source = 'osmnx'
    _same_route = False
    # ── Fast route: OLA Maps (time-optimised, all modes) ─────────────────────
    if OLA_MAPS_KEY:
        ola_routes = _fetch_ola_all_routes(
            origin_lat, origin_lon, dest_lat, dest_lon, mode, OLA_MAPS_KEY
        )
        if ola_routes:
            scored = []
            for r in ola_routes:
                sc, _baseline, iss = _score_ola_route(
                    g_proj, r['coords'], proj_issues, kdtree, mode, _hour,
                    edge_kdtree, edge_score_list, base_score_list,
                )
                scored.append((sc, r, iss))
            fastest = min(scored, key=lambda x: x[1]['duration_min'])
            fast_score, fast_r, fast_issue_details = fastest
            fast_coords = fast_r['coords']
            fast_dist   = fast_r['dist_km']
            fast_time   = fast_r['duration_min']
            fast_steps  = fast_r['steps']
            fast_route_source = 'ola_maps'
            print(f'[routing] OLA: {len(ola_routes)} route(s), fast={fast_time} min')

    # ── OSMnx graph setup (needed for fallback and fast-fallback) ────────────
    g_path = g_proj.to_undirected()

    def _safe_w(u, v, d):
        return min(
            safe_weights.get((u, v, k),
                safe_weights.get((v, u, k),
                    d[k].get('length', 1.0) * 1.5))
            for k in d if d
        ) if d else 1.0

    def _fast_w(u, v, d):
        return min(
            float(d[k].get('length', 1.0)) / max(_edge_speed_kmh(d[k], mode), 1.0)
            for k in d if d
        ) if d else 1.0

    orig_geom, _ = ox.projection.project_geometry(
        Point(origin_lon, origin_lat), crs='EPSG:4326', to_crs=g_proj.graph['crs']
    )
    dest_geom, _ = ox.projection.project_geometry(
        Point(dest_lon, dest_lat), crs='EPSG:4326', to_crs=g_proj.graph['crs']
    )
    orig_x, orig_y = orig_geom.coords[0]
    dest_x, dest_y = dest_geom.coords[0]

    orig_node = _nearest_node_on_graph(g_proj, orig_x, orig_y)
    dest_node = _nearest_node_on_graph(g_proj, dest_x, dest_y)

    if orig_node == dest_node:
        return {'error': 'Origin and destination resolve to the same point on the map'}

    safe_route_source = 'osmnx'

    # ── Safe route: ORS (real road geometry, safety-preferred) ───────────────
    ors_result = _fetch_ors_safe_route(
        origin_lat, origin_lon, dest_lat, dest_lon, mode, ORS_API_KEY
    )
    if ors_result:
        safe_coords  = ors_result['coords']
        safe_dist    = ors_result['dist_km']
        safe_time    = ors_result['duration_min']
        safe_steps   = ors_result['steps']
        safe_route_source = 'ors'
        _safe_penalized, safe_score, safe_issue_details = _score_ola_route(
            g_proj, safe_coords, proj_issues, kdtree, mode, _hour,
            edge_kdtree, edge_score_list, base_score_list,
        )
        # safe_score = road baseline (displayed — issue penalties don't show until fallback triggers)
        # _safe_penalized = full score with issue penalties (stored internally for future use)
        # Compute total credibility-weighted penalty for issues on the ORS path.
        # A single low-credibility report contributes ~2-5 pts — not enough to reroute.
        # Only fall back to OSMnx (which penalises these edges in its weights) when
        # the combined penalty is meaningful. Threshold varies implicitly per category
        # since ISSUE_PENALTIES differ (Unsafe Area=35 vs Other=10), so no fixed
        # report count exists that works across all types.
        ors_issue_penalty = 0.0
        for iss in safe_issue_details:
            cat  = iss.get('category', 'Other')
            conf = float(iss.get('effective_confidence', iss.get('confidence_score', 65)))
            n_rep = iss.get('num_reports', 1)
            n_con = iss.get('num_confirmations', 0)
            n_dis = iss.get('num_dismissals', 0)
            cred  = min(1.0, max(0.15, 0.20 * n_rep + 0.20 * n_con - 0.10 * n_dis))
            sev   = {'low': 0.5, 'medium': 1.0, 'high': 1.5}.get(iss.get('severity', 'medium'), 1.0)
            ors_issue_penalty += ISSUE_PENALTIES.get(cat, 10) * cred * conf * sev * _category_time_factor(cat, _hour) / 100.0

        if ors_issue_penalty >= 19.0:
            # Meaningful issue load — OSMnx safety weights will route around these edges.
            print(f'[routing] ORS path issue penalty={ors_issue_penalty:.1f} ≥ 19 → falling back to OSMnx')
            safe_coords = None
            safe_route_source = 'osmnx'
        else:
            print(f'[routing] ORS safe: {safe_dist} km, {safe_time} min, score={safe_score}')

    # ── Safe route fallback: OSMnx safety-weighted pathfinding ───────────────
    if safe_coords is None:
        safe_nodes: List = []
        try:
            safe_nodes = nx.shortest_path(g_path, orig_node, dest_node, weight=_safe_w)
        except Exception as exc:
            print(f'[routing] safe path failed ({type(exc).__name__}: {exc})')

        if not safe_nodes:
            try:
                safe_nodes = nx.shortest_path(g_path, orig_node, dest_node, weight='length')
                print('[routing] safe route used length fallback')
            except Exception:
                pass

        if not safe_nodes:
            hint = ' Try switching to Walk mode — cycle/drive networks can be sparse in some areas.' if mode in ('cycle', 'drive') else ''
            return {'error': f'No route found between selected points.{hint}'}

        safe_score, safe_dist, safe_time = get_route_stats(g_proj, safe_nodes, mode, adj_scores)
        safe_coords = nodes_to_geojson_coords(g_proj, safe_nodes)
        safe_issue_details = collect_issues_on_route(g_proj, safe_nodes, mode, proj_issues, kdtree)
        safe_steps = build_turn_steps(g_proj, safe_nodes)
        print(f'[routing] OSMnx safe (fallback): {safe_dist} km, {safe_time} min, score={safe_score}')

    # ── Fast fallback: OSMnx time-weighted when OLA failed/missing ───────────
    if fast_coords is None:
        fast_nodes: List = []
        try:
            fast_nodes = nx.shortest_path(g_path, orig_node, dest_node, weight=_fast_w)
        except Exception as exc:
            print(f'[routing] fast path failed ({type(exc).__name__}: {exc})')
        if not fast_nodes:
            try:
                fast_nodes = nx.shortest_path(g_path, orig_node, dest_node, weight='length')
                print('[routing] fast route used length fallback')
            except Exception:
                pass
        if fast_nodes:
            fast_score, fast_dist, fast_time = get_route_stats(g_proj, fast_nodes, mode, adj_scores)
            fast_coords = nodes_to_geojson_coords(g_proj, fast_nodes)
            fast_issue_details = collect_issues_on_route(g_proj, fast_nodes, mode, proj_issues, kdtree)
            fast_steps = build_turn_steps(g_proj, fast_nodes)
            _same_route = (safe_route_source == 'osmnx' and safe_coords == fast_coords)

    # ── same_route check + traffic-aware time unification ────────────────────
    if fast_coords is not None and fast_route_source == 'ola_maps' and fast_time > 0:
        dist_diff = abs(safe_dist - fast_dist) / max(fast_dist, 0.01)
        _same_route = dist_diff < 0.05
        # ORS/OSMnx use static speed tables; OLA has real traffic data.
        # When both routes cover the same corridor (within 10% distance),
        # use OLA's time for the safe route so the user sees a realistic estimate.
        if dist_diff < 0.10:
            safe_time = fast_time

    if not safe_coords or not fast_coords:
        return {'error': 'No route found between selected points.'}

    safe_issues = len(safe_issue_details)
    fast_issues = len(fast_issue_details)

    same_route = _same_route

    return {
        'type': 'FeatureCollection',
        'features': [
            make_geojson_feature(
                safe_coords, 'safe', safe_score, safe_dist, safe_time,
                mode, safe_issues, safe_issue_details, safe_steps,
            ),
            make_geojson_feature(
                fast_coords, 'fast', fast_score, fast_dist, fast_time,
                mode, fast_issues, fast_issue_details, fast_steps,
            ),
            {
                'type': 'Feature',
                'geometry': {'type': 'Point', 'coordinates': [origin_lon, origin_lat]},
                'properties': {'label': 'origin'},
            },
            {
                'type': 'Feature',
                'geometry': {'type': 'Point', 'coordinates': [dest_lon, dest_lat]},
                'properties': {'label': 'destination'},
            },
        ],
        'metadata': {
            'mode': mode,
            'network_type': config['network_type'],
            'speed_kmh': config['speed_kmh'],
            'same_route': same_route,
            'fast_route_source': fast_route_source,
            'safe_route_source': safe_route_source,
        },
    }

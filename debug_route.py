"""debug_route.py — full safety score breakdown for BOTH safe and fast routes.

Safe route  = ORS (if key set) or OSMnx fallback  → scored via per-edge weighted avg
Fast route  = OLA Maps (if key set) or OSMnx time  → scored via KDTree coord snap

Run from the project root:
    python debug_route.py
"""

import sys
import os
import math
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

import networkx as nx
import osmnx as ox
from shapely.geometry import Point

from backend.routing import (
    get_graph,
    _precompute_safe_weights,
    _build_edge_kdtree,
    _nearest_node_on_graph,
    _edge_payload,
    _mode_edge_penalty,
    _fetch_ors_safe_route,
    _fetch_ola_all_routes,
    _score_ola_route,
    ISSUE_PENALTIES,
    ISSUE_RADIUS,
    _category_time_factor,
)
from backend.config import ORS_API_KEY, OLA_MAPS_KEY
from backend.database import SessionLocal
from backend.models import Issue

_IST = timezone(timedelta(hours=5, minutes=30))

# ── Change these ──────────────────────────────────────────────────────────────
ORIGIN_LAT = 12.9738
ORIGIN_LON = 77.6522
DEST_LAT   = 12.9729
DEST_LON   = 77.6473
MODE       = 'cycle'   # 'walk' | 'cycle' | 'drive'
# ─────────────────────────────────────────────────────────────────────────────


def fetch_issues(origin_lat, origin_lon, dest_lat, dest_lon):
    pad = 0.02
    try:
        db = SessionLocal()
        rows = db.query(Issue).filter(
            Issue.lat >= min(origin_lat, dest_lat) - pad,
            Issue.lat <= max(origin_lat, dest_lat) + pad,
            Issue.lon >= min(origin_lon, dest_lon) - pad,
            Issue.lon <= max(origin_lon, dest_lon) + pad,
        ).all()
        db.close()
        return [
            {
                'id': r.id, 'lat': r.lat, 'lon': r.lon,
                'category': r.category, 'description': r.description or '',
                'severity': r.severity,
                'confidence_score': r.confidence_score,
                'effective_confidence': r.effective_confidence,
                'num_reports': r.num_reports,
                'num_confirmations': r.num_confirmations,
                'num_dismissals': r.num_dismissals,
            }
            for r in rows
        ]
    except Exception as e:
        print(f'  [warn] DB fetch failed: {e}')
        return []


def section(title):
    print(f'\n{"─" * 70}')
    print(f'  {title}')
    print(f'{"─" * 70}')


# ── Safe route: OSMnx per-edge breakdown ──────────────────────────────────────
def print_osmnx_score(g_proj, node_list, adj_scores, mode, issues_data_raw,
                      proj_issues, kdtree, hour):
    is_night = (hour >= 18 or hour < 6)

    print(f'\n  {"Edge":<32} {"Highway":<16} {"Len":>6} {"AdjScr":>7} {"Contrib":>10}')
    print(f'  {"─" * 75}')

    total_len = 0.0
    weighted  = 0.0

    for i in range(len(node_list) - 1):
        u, v = node_list[i], node_list[i + 1]
        key, edge = _edge_payload(g_proj, u, v)
        if edge is None:
            key, edge = _edge_payload(g_proj, v, u)
        if edge is None:
            print(f'  {u} → {v}  [edge missing]')
            continue

        elen  = float(edge.get('length', 0.0))
        hw    = edge.get('highway', 'unknown')
        if isinstance(hw, list):
            hw = hw[0]

        adj = adj_scores.get((u, v, key), adj_scores.get((v, u, key), 50.0))
        contrib = adj * elen
        total_len += elen
        weighted  += contrib

        print(f'  {str(u)+" → "+str(v):<32} {hw:<16} {elen:>6.1f} {adj:>7.1f} {contrib:>10.1f}')

    print(f'  {"─" * 75}')

    # Issue penalty breakdown
    if kdtree is not None and proj_issues:
        crs = g_proj.graph['crs']
        radius = ISSUE_RADIUS.get(mode, 50)
        hit = set()
        for nd in node_list:
            n = g_proj.nodes[nd]
            hit.update(kdtree.query_ball_point([n['x'], n['y']], r=radius))

        total_issue_pen = 0.0
        if hit:
            print(f'\n  Issues on this path:')
            for idx in hit:
                _x, _y, issue = proj_issues[idx]
                cat   = issue.get('category', 'Other')
                conf  = float(issue.get('effective_confidence', issue.get('confidence_score', 65)))
                n_rep = issue.get('num_reports', 1)
                n_con = issue.get('num_confirmations', 0)
                n_dis = issue.get('num_dismissals', 0)
                cred  = min(1.0, max(0.15, 0.20 * n_rep + 0.20 * n_con - 0.10 * n_dis))
                sev   = {'low': 0.5, 'medium': 1.0, 'high': 1.5}.get(issue.get('severity', 'medium'), 1.0)
                pen   = ISSUE_PENALTIES.get(cat, 10) * cred * conf * _category_time_factor(cat, hour) * sev / 100.0
                total_issue_pen += pen
                print(f'    [{cat}] conf={conf} cred={cred:.2f} sev={sev} → penalty={pen:.2f}')

    if total_len > 0:
        road_baseline = round(weighted / total_len, 1)
        print(f'\n  Road baseline (weighted avg) : {weighted:.2f} / {total_len:.2f} = {road_baseline}/100')
        if kdtree is not None and proj_issues and hit:
            capped = min(total_issue_pen, road_baseline * 0.8)
            final  = max(0.0, round(road_baseline - capped, 1))
            print(f'  Issue penalty (capped)       : -{capped:.2f}')
            print(f'  ✓ Final score                : {final}/100')
        else:
            print(f'  No issues on path.')
            print(f'  ✓ Final score                : {road_baseline}/100')


# ── Fast route: OLA coord-based KDTree snap breakdown ─────────────────────────
def print_coord_score(g_proj, coords_lonlat, edge_kdtree, edge_score_list,
                      edge_highway_list, proj_issues, kdtree, mode, hour, label):
    crs = g_proj.graph['crs']

    step_e = max(1, len(coords_lonlat) // 60)
    sampled_coords = coords_lonlat[::step_e]

    print(f'\n  Sampling {len(sampled_coords)} of {len(coords_lonlat)} coords (step={step_e})')
    print(f'\n  {"#":>4}  {"Lon":>10}  {"Lat":>10}  {"Highway":<16}  {"Score":>6}  {"SegLen(m)":>10}  {"Contrib":>10}')
    print(f'  {"─" * 78}')

    proj_pts = []
    for lonlat in sampled_coords:
        try:
            pt, _ = ox.projection.project_geometry(
                Point(lonlat[0], lonlat[1]), crs='EPSG:4326', to_crs=crs
            )
            idx = edge_kdtree.query([pt.x, pt.y])[1]
            hw = edge_highway_list[idx] if edge_highway_list and idx < len(edge_highway_list) else '?'
            proj_pts.append((pt.x, pt.y, edge_score_list[idx], lonlat[0], lonlat[1], hw))
        except Exception:
            pass

    weighted_sum = 0.0
    total_w      = 0.0

    for i, (x, y, score, lon, lat, hw) in enumerate(proj_pts):
        if i < len(proj_pts) - 1:
            x1, y1 = proj_pts[i + 1][0], proj_pts[i + 1][1]
            seg = math.sqrt((x1 - x) ** 2 + (y1 - y) ** 2)
            contrib = ((score + proj_pts[i + 1][2]) / 2.0) * seg
            weighted_sum += contrib
            total_w      += seg
            print(f'  {i:>4}  {lon:>10.6f}  {lat:>10.6f}  {hw:<16}  {score:>6.1f}  {seg:>10.1f}  {contrib:>10.1f}')
        else:
            print(f'  {i:>4}  {lon:>10.6f}  {lat:>10.6f}  {hw:<16}  {score:>6.1f}  {"(last)":>10}  {"─":>10}')

    print(f'  {"─" * 68}')

    road_baseline = round(weighted_sum / total_w, 1) if total_w > 0 else 65.0
    print(f'\n  Road baseline (length-weighted) : {weighted_sum:.2f} / {total_w:.2f} = {road_baseline}/100')

    # Issue penalty
    if kdtree is not None and proj_issues:
        radius = ISSUE_RADIUS.get(mode, 50)
        hit = set()
        step = max(1, len(coords_lonlat) // 80)
        for lonlat in coords_lonlat[::step]:
            try:
                pt, _ = ox.projection.project_geometry(
                    Point(lonlat[0], lonlat[1]), crs='EPSG:4326', to_crs=crs
                )
                hit.update(kdtree.query_ball_point([pt.x, pt.y], r=radius))
            except Exception:
                pass

        total_pen = 0.0
        if hit:
            print(f'  Issues on this path:')
            for idx in hit:
                _x, _y, issue = proj_issues[idx]
                cat   = issue.get('category', 'Other')
                conf  = float(issue.get('effective_confidence', issue.get('confidence_score', 65)))
                n_rep = issue.get('num_reports', 1)
                n_con = issue.get('num_confirmations', 0)
                n_dis = issue.get('num_dismissals', 0)
                cred  = min(1.0, max(0.15, 0.20 * n_rep + 0.20 * n_con - 0.10 * n_dis))
                sev   = {'low': 0.5, 'medium': 1.0, 'high': 1.5}.get(issue.get('severity', 'medium'), 1.0)
                pen   = ISSUE_PENALTIES.get(cat, 10) * cred * conf * _category_time_factor(cat, hour) * sev / 100.0
                total_pen += pen
                print(f'    [{cat}] conf={conf} cred={cred:.2f} sev={sev} → penalty={pen:.2f}')
            capped = min(total_pen, road_baseline * 0.8)
            final  = max(0.0, round(road_baseline - capped, 1))
            print(f'  Issue penalty (capped)          : -{capped:.2f}')
            print(f'  ✓ Final score                   : {final}/100')
        else:
            print(f'  No issues on path.')
            print(f'  ✓ Final score                   : {road_baseline}/100')
    else:
        print(f'  ✓ Final score                   : {road_baseline}/100')


def main():
    hour = datetime.now(_IST).hour

    print(f'\n{"=" * 70}')
    print(f'  Origin : ({ORIGIN_LAT}, {ORIGIN_LON})')
    print(f'  Dest   : ({DEST_LAT}, {DEST_LON})')
    print(f'  Mode   : {MODE}   |   Hour: {hour}h IST  |  Night: {hour>=18 or hour<6}')
    print(f'{"=" * 70}')

    # 0. Issues
    section('0. Issues from DB')
    issues_data = fetch_issues(ORIGIN_LAT, ORIGIN_LON, DEST_LAT, DEST_LON)
    print(f'  {len(issues_data)} issue(s) in bounding box')
    for iss in issues_data:
        print(f'    [{iss["category"]}] ({iss["lat"]:.5f},{iss["lon"]:.5f}) conf={iss["effective_confidence"]} reports={iss["num_reports"]}')

    # 1. Graph
    section('1. Graph')
    g_proj = get_graph(ORIGIN_LAT, ORIGIN_LON, DEST_LAT, DEST_LON, MODE)
    print(f'  {len(g_proj.nodes)} nodes, {len(g_proj.edges)} edges')

    # 2. Weights
    section('2. Safety weights + KDTree')
    safe_weights, adj_scores, proj_issues, kdtree = _precompute_safe_weights(
        g_proj, MODE, issues_data, hour
    )
    edge_kdtree, edge_score_list, base_score_list = _build_edge_kdtree(g_proj, adj_scores)

    # Build parallel highway list in same edge iteration order as _build_edge_kdtree
    edge_highway_list = []
    for _u, _v, _k, _d in g_proj.edges(keys=True, data=True):
        hw = _d.get('highway', 'unknown')
        edge_highway_list.append(hw[0] if isinstance(hw, list) else hw)

    print(f'  adj_scores: {len(adj_scores)} edges  |  proj_issues: {len(proj_issues)}')

    # 3. Snap nodes
    section('3. Node snapping')
    crs = g_proj.graph['crs']
    orig_geom, _ = ox.projection.project_geometry(Point(ORIGIN_LON, ORIGIN_LAT), crs='EPSG:4326', to_crs=crs)
    dest_geom, _ = ox.projection.project_geometry(Point(DEST_LON, DEST_LAT),     crs='EPSG:4326', to_crs=crs)
    orig_node = _nearest_node_on_graph(g_proj, *orig_geom.coords[0])
    dest_node = _nearest_node_on_graph(g_proj, *dest_geom.coords[0])
    on = g_proj.nodes[orig_node]
    dn = g_proj.nodes[dest_node]
    print(f'  Origin node : {orig_node}  ({on.get("y", "?"):.6f}, {on.get("x", "?"):.6f})')
    print(f'  Dest node   : {dest_node}  ({dn.get("y", "?"):.6f}, {dn.get("x", "?"):.6f})')
    if orig_node == dest_node:
        print('  ERROR: same node — cannot route.')
        return

    # ── SAFE ROUTE ────────────────────────────────────────────────────────────
    section('4. SAFE ROUTE')

    # Try ORS first
    ors_result = None
    if ORS_API_KEY:
        print('  Trying ORS ...')
        ors_result = _fetch_ors_safe_route(ORIGIN_LAT, ORIGIN_LON, DEST_LAT, DEST_LON, MODE, ORS_API_KEY)

    if ors_result:
        print(f'  ORS succeeded: {ors_result["dist_km"]} km, {ors_result["duration_min"]} min, {len(ors_result["coords"])} pts')
        print(f'\n  Scoring method: KDTree coord-snap (same as OLA)')
        print_coord_score(g_proj, ors_result['coords'], edge_kdtree, base_score_list,
                          edge_highway_list, proj_issues, kdtree, MODE, hour, 'ORS Safe')
    else:
        print('  ORS failed/unavailable → OSMnx fallback')
        g_path = g_proj.to_undirected()
        def _safe_w(u, v, d):
            return min(
                safe_weights.get((u, v, k),
                    safe_weights.get((v, u, k), d[k].get('length', 1.0) * 1.5))
                for k in d if d
            ) if d else 1.0
        try:
            safe_nodes = nx.shortest_path(g_path, orig_node, dest_node, weight=_safe_w)
            print(f'  OSMnx safe path: {len(safe_nodes)} nodes, {len(safe_nodes)-1} edges')
            print(f'\n  Scoring method: exact per-edge length-weighted avg')
            print_osmnx_score(g_proj, safe_nodes, adj_scores, MODE,
                              issues_data, proj_issues, kdtree, hour)
        except Exception as e:
            print(f'  OSMnx safe path FAILED: {e}')

    # ── FAST ROUTE ────────────────────────────────────────────────────────────
    section('5. FAST ROUTE')

    ola_routes = []
    if OLA_MAPS_KEY:
        print('  Fetching OLA routes ...')
        ola_routes = _fetch_ola_all_routes(ORIGIN_LAT, ORIGIN_LON, DEST_LAT, DEST_LON, MODE, OLA_MAPS_KEY)

    if ola_routes:
        # Score all and pick fastest
        scored = []
        for r in ola_routes:
            sc, _baseline, iss = _score_ola_route(g_proj, r['coords'], proj_issues, kdtree,
                                                   MODE, hour, edge_kdtree, edge_score_list, base_score_list)
            scored.append((sc, r, iss))
        fastest = min(scored, key=lambda x: x[1]['duration_min'])
        fast_score, fast_r, _ = fastest

        print(f'  OLA returned {len(ola_routes)} route(s):')
        for i, (sc, r, _) in enumerate(scored):
            marker = ' ← fastest (used)' if r is fast_r else ''
            print(f'    [{i}] {r["dist_km"]} km, {r["duration_min"]} min, score={sc}{marker}')

        print(f'\n  Scoring method: KDTree coord-snap for fastest route')
        print_coord_score(g_proj, fast_r['coords'], edge_kdtree, base_score_list,
                          edge_highway_list, proj_issues, kdtree, MODE, hour, 'OLA Fast')
    else:
        print('  OLA unavailable → OSMnx time-weighted fallback')
        g_path = g_proj.to_undirected()
        def _fast_w(u, v, d):
            from backend.routing import _edge_speed_kmh
            return min(float(d[k].get('length', 1.0)) / max(_edge_speed_kmh(d[k], MODE), 1.0)
                       for k in d if d) if d else 1.0
        try:
            fast_nodes = nx.shortest_path(g_path, orig_node, dest_node, weight=_fast_w)
            print(f'  OSMnx fast path: {len(fast_nodes)} nodes, {len(fast_nodes)-1} edges')
            print_osmnx_score(g_proj, fast_nodes, adj_scores, MODE,
                              issues_data, proj_issues, kdtree, hour)
        except Exception as e:
            print(f'  OSMnx fast path FAILED: {e}')

    print()


if __name__ == '__main__':
    main()

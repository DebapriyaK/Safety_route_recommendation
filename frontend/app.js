// app.js - SafeRoute frontend application

const DEFAULT_LAT = 12.9716;
const DEFAULT_LON = 77.5946;
const map = L.map('map').setView([DEFAULT_LAT, DEFAULT_LON], 13);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const LS_LAST_SEARCH = 'last_search';
const LS_TRACKING_ENABLED = 'tracking_enabled';
const LS_SHOW_ISSUES = 'show_issues';
const LS_SEARCH_MEMORY = 'search_memory';

function _issuesLayerEnabled() {
  if (!currentUser) return false;
  return localStorage.getItem(lsKey(LS_SHOW_ISSUES)) === '1';
}

// All user-facing storage is namespaced by user ID so two users on the same
// browser never see each other's search history, saved routes, or preferences.
function lsKey(base) {
  const uid = currentUser?.id || 'guest';
  return `sr_${uid}_${base}`;
}
const SEARCH_RECENT_LIMIT = 6;
const SEARCH_COMMON_LIMIT = 6;
const LIVE_NAV_ARRIVAL_M = 30;

let issueMode = false;
let routeLayer = null;
let markerLayer = null;
let issueClusterLayer = null; // route-specific issue markers only
let currentUser = null;
let originCoords = null;
let destCoords = null;
let _originPin = null;
let _destPin = null;
let lastRoutePayload = null;
let trackingEnabled = false; // loaded from user-scoped storage in init() after user is known
const shownValidationSet = new Set();
let _watchId = null;
let _navWatchId = null;
let _liveMarker = null;
let _liveAccuracyCircle = null;
let _liveTrail = null;
let _liveRouteType = null;
let _liveRouteCoords = [];
let _liveRouteSteps = [];
let _liveRouteTotalM = 0;
let _liveRouteMode = 'walk';
let _activeSheetTab = 'safe';
let _issueDragMarker = null;
let _issueFormPopup = null;
let adminIssueLayer = null;
let globalIssueLayer = null;

let _loadingTyperTimer = null;
let _loadingTyperPhrase = 0;
let _loadingTyperChar = 0;
let _loadingTyperDeleting = false;
const _LOADING_PHRASES = [
  'Building street graph…',
  'Crunching the numbers…',
  'Finding the safest path…',
  'Almost there…',
  'Mapping your route…',
];

function _runLoadingTyper() {
  const el = document.getElementById('loading-type');
  if (!el) return;
  const phrase = _LOADING_PHRASES[_loadingTyperPhrase % _LOADING_PHRASES.length];
  if (!_loadingTyperDeleting) {
    _loadingTyperChar++;
    el.textContent = phrase.slice(0, _loadingTyperChar);
    if (_loadingTyperChar === phrase.length) {
      _loadingTyperDeleting = true;
      _loadingTyperTimer = setTimeout(_runLoadingTyper, 1400);
    } else {
      _loadingTyperTimer = setTimeout(_runLoadingTyper, 55);
    }
  } else {
    _loadingTyperChar--;
    el.textContent = phrase.slice(0, _loadingTyperChar);
    if (_loadingTyperChar === 0) {
      _loadingTyperDeleting = false;
      _loadingTyperPhrase++;
      _loadingTyperTimer = setTimeout(_runLoadingTyper, 220);
    } else {
      _loadingTyperTimer = setTimeout(_runLoadingTyper, 30);
    }
  }
}

function showLoading(msg) {
  const el = document.getElementById('loading-overlay');
  if (!el) return;
  el.style.display = 'flex';
  clearTimeout(_loadingTyperTimer);
  _loadingTyperChar = 0;
  _loadingTyperDeleting = false;
  _loadingTyperPhrase = 0;
  const typeEl = document.getElementById('loading-type');
  if (typeEl) typeEl.textContent = '';
  _runLoadingTyper();
}

function hideLoading() {
  const el = document.getElementById('loading-overlay');
  if (el) el.style.display = 'none';
  clearTimeout(_loadingTyperTimer);
}

function showToast(msg, duration = 3500) {
  const toast = document.createElement('div');
  toast.textContent = msg;
  Object.assign(toast.style, {
    position: 'fixed',
    bottom: '88px',
    right: '24px',
    background: '#2c3e50',
    color: '#fff',
    padding: '10px 18px',
    borderRadius: '8px',
    zIndex: '9999',
    fontSize: '14px',
    boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
    maxWidth: '380px',
    lineHeight: '1.4',
  });
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), duration);
}

function scoreColor(score) {
  if (score >= 70) return '#27ae60';
  if (score >= 50) return '#e67e22';
  return '#e74c3c';
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function updateNavbar(user) {
  const userInfo = document.getElementById('user-info');
  const mobileBtn = document.getElementById('mobile-preview-btn');
  if (!userInfo) return;
  if (user) {
    userInfo.innerHTML = `
      <a class="nav-link" href="profile.html" title="Profile" style="font-size:18px;line-height:1;">&#128100;</a>
      <span class="nav-username">Hi, ${escHtml(user.username)}</span>
      <button class="nav-btn" onclick="logout()">Logout</button>
    `;
    if (mobileBtn) mobileBtn.style.display = user.is_admin ? '' : 'none';
    if (user.is_admin) loadAdminIssueLayer();
    else { if (adminIssueLayer) { map.removeLayer(adminIssueLayer); adminIssueLayer = null; } }
  } else {
    userInfo.innerHTML = '<a class="nav-link" href="login.html">Login</a>';
    if (mobileBtn) mobileBtn.style.display = 'none';
    if (adminIssueLayer) { map.removeLayer(adminIssueLayer); adminIssueLayer = null; }
  }
}

function metersBetween(aLat, aLon, bLat, bLon) {
  const R = 6371000;
  const p1 = aLat * Math.PI / 180;
  const p2 = bLat * Math.PI / 180;
  const dp = (bLat - aLat) * Math.PI / 180;
  const dl = (bLon - aLon) * Math.PI / 180;
  const x = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}

function _emptySearchMemory() {
  return {
    origin: { recent: [], counts: {} },
    destination: { recent: [], counts: {} },
    lastMode: 'walk',
  };
}

function _normalizePlaceKey(text) {
  return String(text || '').trim().toLowerCase();
}

function getSearchMemory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(lsKey(LS_SEARCH_MEMORY)) || '{}');
    const base = _emptySearchMemory();
    return {
      origin: parsed.origin || base.origin,
      destination: parsed.destination || base.destination,
      lastMode: parsed.lastMode || base.lastMode,
    };
  } catch {
    return _emptySearchMemory();
  }
}

function setSearchMemory(memory) {
  localStorage.setItem(lsKey(LS_SEARCH_MEMORY), JSON.stringify(memory));
}

function rememberPlace(kind, label, coords = null) {
  const clean = String(label || '').trim();
  const key = _normalizePlaceKey(clean);
  if (!clean || !key || (kind !== 'origin' && kind !== 'destination')) return;

  const now = Date.now();
  const mem = getSearchMemory();
  const bucket = mem[kind];

  bucket.recent = (bucket.recent || []).filter((e) => _normalizePlaceKey(e?.label) !== key);
  bucket.recent.unshift({
    label: clean,
    lat: Number.isFinite(coords?.lat) ? Number(coords.lat) : null,
    lon: Number.isFinite(coords?.lon) ? Number(coords.lon) : null,
    last_used: now,
  });
  if (bucket.recent.length > 20) bucket.recent.length = 20;

  const prev = bucket.counts?.[key] || { label: clean, count: 0, last_used: 0, lat: null, lon: null };
  bucket.counts[key] = {
    label: clean,
    count: (prev.count || 0) + 1,
    last_used: now,
    lat: Number.isFinite(coords?.lat) ? Number(coords.lat) : prev.lat,
    lon: Number.isFinite(coords?.lon) ? Number(coords.lon) : prev.lon,
  };

  mem[kind] = bucket;
  setSearchMemory(mem);
}

function getPlaceSuggestions(kind, query = '') {
  if (kind !== 'origin' && kind !== 'destination') return [];

  const q = _normalizePlaceKey(query);
  const mem = getSearchMemory();
  const bucket = mem[kind] || { recent: [], counts: {} };
  const out = [];
  const seen = new Set();

  for (const item of bucket.recent || []) {
    const key = _normalizePlaceKey(item?.label);
    if (!key || seen.has(key)) continue;
    if (q && !key.includes(q)) continue;
    seen.add(key);
    out.push({
      name: item.label,
      lat: Number.isFinite(item.lat) ? Number(item.lat) : null,
      lon: Number.isFinite(item.lon) ? Number(item.lon) : null,
    });
    if (out.length >= SEARCH_RECENT_LIMIT) break;
  }

  const common = Object.values(bucket.counts || {})
    .filter((item) => {
      const key = _normalizePlaceKey(item?.label);
      return key && (!q || key.includes(q));
    })
    .sort((a, b) => (b.count || 0) - (a.count || 0) || (b.last_used || 0) - (a.last_used || 0));

  for (const item of common) {
    const key = _normalizePlaceKey(item?.label);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push({
      name: item.label,
      lat: Number.isFinite(item.lat) ? Number(item.lat) : null,
      lon: Number.isFinite(item.lon) ? Number(item.lon) : null,
    });
    if (out.length >= SEARCH_RECENT_LIMIT + SEARCH_COMMON_LIMIT) break;
  }

  return out;
}

function resolveRememberedPlace(kind, text) {
  const key = _normalizePlaceKey(text);
  if (!key || (kind !== 'origin' && kind !== 'destination')) return null;

  const mem = getSearchMemory();
  const bucket = mem[kind] || { recent: [], counts: {} };
  const recentExact = (bucket.recent || []).find((e) => _normalizePlaceKey(e?.label) === key);
  if (Number.isFinite(recentExact?.lat) && Number.isFinite(recentExact?.lon)) {
    return { lat: Number(recentExact.lat), lon: Number(recentExact.lon) };
  }
  const commonExact = bucket.counts?.[key];
  if (Number.isFinite(commonExact?.lat) && Number.isFinite(commonExact?.lon)) {
    return { lat: Number(commonExact.lat), lon: Number(commonExact.lon) };
  }
  return null;
}

function saveLastSearch(originText, destText, mode, originResolved = null, destResolved = null) {
  localStorage.setItem(lsKey(LS_LAST_SEARCH), JSON.stringify({ mode }));
  const mem = getSearchMemory();
  mem.lastMode = mode || 'walk';
  setSearchMemory(mem);
  rememberPlace('origin', originText, originResolved);
  rememberPlace('destination', destText, destResolved);
}

function restoreLastSearch() {
  try {
    const raw = localStorage.getItem(lsKey(LS_LAST_SEARCH));
    const mem = getSearchMemory();
    const obj = raw ? JSON.parse(raw) : {};
    document.getElementById('originInput').value = '';
    document.getElementById('destInput').value = '';
    originCoords = null;
    destCoords = null;
    selectMode(obj.mode || mem.lastMode || 'walk');
  } catch {}
}

function setShareParams(originLat, originLon, destLat, destLon, mode, originText, destText) {
  const qs = new URLSearchParams(window.location.search);
  qs.set('from', `${originLat.toFixed(6)},${originLon.toFixed(6)}`);
  qs.set('to', `${destLat.toFixed(6)},${destLon.toFixed(6)}`);
  qs.set('mode', mode || 'walk');
  if (originText) qs.set('from_label', originText);
  if (destText) qs.set('to_label', destText);
  history.replaceState({}, '', `${window.location.pathname}?${qs.toString()}`);
}

function parseCoords(text) {
  const parts = String(text || '').split(',').map((s) => parseFloat(s.trim()));
  if (parts.length !== 2 || Number.isNaN(parts[0]) || Number.isNaN(parts[1])) return null;
  return { lat: parts[0], lon: parts[1] };
}

function applyShareParamsIfAny() {
  const qs = new URLSearchParams(window.location.search);
  const from = parseCoords(qs.get('from'));
  const to = parseCoords(qs.get('to'));
  const mode = qs.get('mode');

  if (!from || !to) return false;

  originCoords = from;
  destCoords = to;
  document.getElementById('originInput').value = qs.get('from_label') || `${from.lat.toFixed(5)}, ${from.lon.toFixed(5)}`;
  document.getElementById('destInput').value = qs.get('to_label') || `${to.lat.toFixed(5)}, ${to.lon.toFixed(5)}`;
  if (mode && ['walk', 'cycle', 'drive'].includes(mode)) {
    selectMode(mode);
  }
  return true;
}

const _acTimers = {};
function setupAutocomplete(inputId, onCoordSelect) {
  const input = document.getElementById(inputId);
  const list = document.getElementById(inputId + '-list');
  if (!input || !list) return;
  const kind = inputId === 'originInput' ? 'origin' : 'destination';

  const showLocalSuggestions = (query = '') => {
    const local = getPlaceSuggestions(kind, query);
    renderSuggestions(list, local, (s) => {
      input.value = s.name;
      list.innerHTML = '';
      list.style.display = 'none';
      if (Number.isFinite(s?.lat) && Number.isFinite(s?.lon)) {
        onCoordSelect({ lat: Number(s.lat), lon: Number(s.lon) });
      } else {
        onCoordSelect(null);
      }
    });
  };

  input.addEventListener('input', () => {
    onCoordSelect(null);
    clearTimeout(_acTimers[inputId]);
    const q = input.value.trim();

    if (q.length === 0) {
      showLocalSuggestions('');
      return;
    }

    if (q.length < 2) {
      showLocalSuggestions(q);
      return;
    }

    _acTimers[inputId] = setTimeout(async () => {
      try {
        const c = map.getCenter();
        const url = `${API_BASE}/geocode/autocomplete?query=${encodeURIComponent(q)}&lat=${c.lat}&lon=${c.lng}`;
        const res = await fetch(url);
        const data = await res.json();
        const remote = data.suggestions || [];
        const local = getPlaceSuggestions(kind, q).slice(0, 3);
        const merged = [];
        const seen = new Set();
        [...local, ...remote].forEach((s) => {
          const key = _normalizePlaceKey(s?.name);
          if (!key || seen.has(key)) return;
          seen.add(key);
          merged.push(s);
        });

        renderSuggestions(list, merged, (s) => {
          input.value = s.name;
          list.innerHTML = '';
          list.style.display = 'none';
          if (Number.isFinite(s?.lat) && Number.isFinite(s?.lon)) {
            onCoordSelect({ lat: Number(s.lat), lon: Number(s.lon) });
          } else {
            onCoordSelect(null);
          }
        });
      } catch (err) {
        console.error('Autocomplete error:', err);
      }
    }, 300);
  });

  input.addEventListener('focus', () => {
    if (!input.value.trim()) showLocalSuggestions('');
  });

  document.addEventListener('click', (e) => {
    if (!input.contains(e.target) && !list.contains(e.target)) {
      list.innerHTML = '';
      list.style.display = 'none';
    }
  });
}

function renderSuggestions(container, suggestions, onClickFn) {
  container.innerHTML = '';
  if (!suggestions.length) {
    container.style.display = 'none';
    return;
  }
  suggestions.forEach((s) => {
    const item = document.createElement('div');
    item.className = 'autocomplete-item';
    item.textContent = s.name;
    item.addEventListener('mousedown', (e) => {
      e.preventDefault();
      onClickFn(s);
    });
    container.appendChild(item);
  });
  container.style.display = 'block';
}

function summarizeRouteIssues(routeIssues) {
  if (!Array.isArray(routeIssues) || !routeIssues.length) return 'No reported issues on this route.';
  const items = routeIssues.slice(0, 6).map((i) => {
    const conf = Math.round(i.effective_confidence || 0);
    const desc = i.description ? ` - ${escHtml(i.description).slice(0, 80)}` : '';
    return `<li>${escHtml(i.category)} (${conf}/100)${desc}</li>`;
  });
  const more = routeIssues.length > 6 ? `<li>+${routeIssues.length - 6} more...</li>` : '';
  return `<ul style="margin:4px 0 0 16px;padding:0;">${items.join('')}${more}</ul>`;
}

function drawRouteIssueMarkers(routeData) {
  if (issueClusterLayer) {
    map.removeLayer(issueClusterLayer);
    issueClusterLayer = null;
  }

  const features = routeData?.features || [];
  const routeFeatures = features.filter((f) => f.properties?.route_type);
  const byId = new Map();

  routeFeatures.forEach((f) => {
    const routeType = f.properties?.route_type;
    (f.properties?.route_issues || []).forEach((issue) => {
      if (!issue?.id || byId.has(issue.id)) return;
      byId.set(issue.id, { ...issue, _routeType: routeType });
    });
  });

  issueClusterLayer = L.layerGroup();
  for (const issue of byId.values()) {
    const stale = (issue.effective_confidence || 0) <= 55;
    const marker = L.circleMarker([issue.lat, issue.lon], {
      radius: 8,
      color: stale ? '#e67e22' : '#e74c3c',
      fillColor: stale ? '#f39c12' : '#e74c3c',
      fillOpacity: 0.8,
      weight: 2,
    });

    const adminDeleteHtml = currentUser?.is_admin
      ? `<br><button onclick="deleteIssue('${issue.id}')" style="margin-top:6px;padding:4px 10px;background:#7f1d1d;color:#fff;border:none;border-radius:5px;cursor:pointer;font-size:11px;">&#128465; Delete (Admin)</button>`
      : '';
    const validationHtml = currentUser
      ? `
        <br><br>
        <button onclick="validateIssue('${issue.id}','confirm')" style="margin-right:6px;padding:4px 10px;background:#27ae60;color:#fff;border:none;border-radius:5px;cursor:pointer;font-size:12px;">Still there</button>
        <button onclick="validateIssue('${issue.id}','dismiss')" style="padding:4px 10px;background:#e74c3c;color:#fff;border:none;border-radius:5px;cursor:pointer;font-size:12px;">Fixed/Gone</button>
        ${adminDeleteHtml}
      `
      : '<br><small style="color:#888">Login to validate</small>';

    marker.bindPopup(`
      <b>${escHtml(issue.category)}</b><br>
      ${issue.description ? escHtml(issue.description) + '<br>' : ''}
      Confidence: <b>${Math.round(issue.effective_confidence || 0)}</b><br>
      Reports: ${issue.num_reports || 0}, Confirm: ${issue.num_confirmations || 0}, Dismiss: ${issue.num_dismissals || 0}
      ${validationHtml}
    `);

    marker._issueId = issue.id;
    marker._issueMeta = issue;
    issueClusterLayer.addLayer(marker);
  }

  // Only show route issue dots to users when the issues actually forced a reroute (osmnx fallback).
  // Admin always sees them. Regular users see them only when safe_route_source === 'osmnx'.
  if (currentUser?.is_admin || routeData?.metadata?.safe_route_source === 'osmnx') {
    issueClusterLayer.addTo(map);
  }
}

async function getRoutesFromInput() {
  const originText = document.getElementById('originInput').value.trim();
  const destText = document.getElementById('destInput').value.trim();
  const mode = document.getElementById('mode').value;

  if (!originText || !destText) {
    showToast('Please enter both origin and destination.');
    return;
  }

  showLoading('Resolving locations...');

  try {
    const center = map.getCenter();
    let origin_lat, origin_lon, dest_lat, dest_lon;

    if (originCoords) {
      origin_lat = originCoords.lat;
      origin_lon = originCoords.lon;
    } else {
      const remembered = resolveRememberedPlace('origin', originText);
      if (remembered) {
        origin_lat = remembered.lat;
        origin_lon = remembered.lon;
      } else {
        const res = await fetch(`${API_BASE}/geocode?query=${encodeURIComponent(originText)}&lat=${center.lat}&lon=${center.lng}`);
        const data = await res.json();
        if (data.error) {
          hideLoading();
          showToast('Origin not found. Try a more specific name.');
          return;
        }
        origin_lat = data.lat;
        origin_lon = data.lon;
      }
    }

    if (destCoords) {
      dest_lat = destCoords.lat;
      dest_lon = destCoords.lon;
    } else {
      const remembered = resolveRememberedPlace('destination', destText);
      if (remembered) {
        dest_lat = remembered.lat;
        dest_lon = remembered.lon;
      } else {
        const res = await fetch(`${API_BASE}/geocode?query=${encodeURIComponent(destText)}&lat=${center.lat}&lon=${center.lng}`);
        const data = await res.json();
        if (data.error) {
          hideLoading();
          showToast('Destination not found. Try a more specific name.');
          return;
        }
        dest_lat = data.lat;
        dest_lon = data.lon;
      }
    }

    // Clear ALL stale layers before new request — prevents old data showing on error
    if (routeLayer)        { map.removeLayer(routeLayer);        routeLayer        = null; }
    if (markerLayer)       { map.removeLayer(markerLayer);       markerLayer       = null; }
    if (issueClusterLayer) { map.removeLayer(issueClusterLayer); issueClusterLayer = null; }
    if (_originPin)        { map.removeLayer(_originPin);        _originPin        = null; }
    if (_destPin)          { map.removeLayer(_destPin);          _destPin          = null; }
    if (
      Math.abs(origin_lat - dest_lat) < 0.0001 &&
      Math.abs(origin_lon - dest_lon) < 0.0001
    ) {
      hideLoading();
      showToast('Origin and destination appear to be the same place. Please choose different locations.');
      return;
    }

    const summaryEl = document.getElementById('route-summary');
    if (summaryEl) summaryEl.style.display = 'none';

    markerLayer = L.layerGroup([
      L.circleMarker([origin_lat, origin_lon], {
        radius: 10, color: '#fff', weight: 2.5, fillColor: '#22c55e', fillOpacity: 1,
      }).bindPopup('<b>Start</b>'),
      L.circleMarker([dest_lat, dest_lon], {
        radius: 10, color: '#fff', weight: 2.5, fillColor: '#3b82f6', fillOpacity: 1,
      }).bindPopup('<b>Destination</b>'),
    ]).addTo(map);

    showLoading('Building street graph and computing routes...');
    const res = await fetch(`${API_BASE}/route`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ origin_lat, origin_lon, dest_lat, dest_lon, mode }),
    });
    if (res.status === 401) {
      hideLoading();
      showToast('Session expired. Please log in again.');
      setTimeout(() => (window.location.href = 'login.html'), 1500);
      return;
    }
    if (!res.ok) {
      hideLoading();
      let detail = 'Routing failed on the server.';
      try { const e = await res.json(); detail = e.detail || detail; } catch {}
      showToast(detail);
      const summaryEl2 = document.getElementById('route-summary');
      if (summaryEl2) { summaryEl2.style.display = 'none'; summaryEl2.classList.remove('sheet-open', 'sheet-peek'); }
      document.body.classList.remove('sheet-active');
      return;
    }
    const data = await res.json();
    hideLoading();

    if (!data || data.error || data.type !== 'FeatureCollection') {
      showToast(data?.error || 'No route found. Try nearby locations.');
      const summaryEl2 = document.getElementById('route-summary');
      if (summaryEl2) {
        summaryEl2.style.display = 'none';
        summaryEl2.classList.remove('sheet-open', 'sheet-peek');
      }
      document.body.classList.remove('sheet-active');
      return;
    }

    saveLastSearch(
      originText,
      destText,
      mode,
      { lat: origin_lat, lon: origin_lon },
      { lat: dest_lat, lon: dest_lon }
    );
    setShareParams(origin_lat, origin_lon, dest_lat, dest_lon, mode, originText, destText);

    lastRoutePayload = {
      data,
      origin: { lat: origin_lat, lon: origin_lon, label: originText },
      destination: { lat: dest_lat, lon: dest_lon, label: destText },
      mode,
    };

    drawRoutes(data);
  } catch (err) {
    hideLoading();
    console.error('Route error:', err);
    showToast('Something went wrong. Is the backend running?');
    const summaryEl = document.getElementById('route-summary');
    if (summaryEl) {
      summaryEl.style.display = 'none';
      summaryEl.classList.remove('sheet-open', 'sheet-peek');
    }
    document.body.classList.remove('sheet-active');
  }
}

function routeCard(p, type, meta) {
  const mobile = window.innerWidth <= 768;
  const label = type === 'safe' ? 'Safe' : 'Fast';
  const border = type === 'safe' ? '#27ae60' : '#e67e22';
  const color = scoreColor(p.safety_score);
  const issues = p.issues_on_path ?? 0;
  let debugBadge = '';
  if (currentUser?.is_admin && meta) {
    const src = type === 'safe' ? meta.safe_route_source : meta.fast_route_source;
    const srcLabel = src === 'ola_maps' ? 'OLA Maps' : src === 'ors' ? 'ORS' : 'OSMnx';
    const srcColor = src === 'ola_maps' ? '#4ade80' : src === 'ors' ? '#60a5fa' : '#94a3b8';
    const latency = meta.latency_ms != null ? ` &bull; ${meta.latency_ms}ms` : '';
    debugBadge = `<div style="margin-top:6px;padding:4px 7px;background:#0f172a;border-radius:4px;font-size:10px;font-family:monospace;color:#94a3b8;">&#128295; <span style="color:${srcColor}">${srcLabel}</span>${latency}</div>`;
  }
  return `
    <div class="route-card" id="route-card-${type}" style="border-left:4px solid ${border}">
      <div class="rc-label">${label}</div>
      <div class="rc-row">Safety <span style="color:${color};font-weight:700">${p.safety_score}/100</span></div>
      <div class="rc-row">Distance <span>${p.distance_km} km</span></div>
      <div class="rc-row">Time <span>~${formatMinutes(p.duration_min)} min</span></div>
      <div class="rc-row">Issues <span>${issues}</span></div>
      <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap;">
        <button class="small-btn" onclick="showSteps('${type}')">${mobile ? 'Directions' : 'Steps'}</button>
        <button class="small-btn small-btn-primary" onclick="startLiveNavigation('${type}')">${mobile ? 'Start' : 'Live'}</button>
      </div>
      ${mobile ? '<div class="route-card-hint">Tap Directions to see route overview. Tap Start to begin live turn-by-turn.</div>' : ''}
      ${debugBadge}
    </div>
  `;
}

function formatMinutes(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return '--';
  return n.toFixed(1);
}

function drawRoutes(data) {
  stopLiveNavigation(true);
  shownValidationSet.clear();

  if (routeLayer) map.removeLayer(routeLayer);

  routeLayer = L.geoJSON(data, {
    style: (feature) => {
      if (feature.geometry.type !== 'LineString') return {};
      if (feature.properties.route_type === 'safe') {
        return { color: '#27ae60', weight: 6, opacity: 0.9 };
      }
      if (feature.properties.route_type === 'fast') {
        return { color: '#e67e22', weight: 5, opacity: 0.85, dashArray: '10, 8' };
      }
      return {};
    },
    pointToLayer: (feature, latlng) => {
      // Origin/destination already rendered by markerLayer — skip GeoJSON duplicates
      const lbl = feature.properties?.label;
      if (lbl === 'origin' || lbl === 'destination') return null;
      return L.circleMarker(latlng, { radius: 6, color: '#888', fillOpacity: 0.7, weight: 1 });
    },
    onEachFeature: (feature, layer) => {
      if (!layer) return;
      if (feature.geometry.type === 'Point') {
        layer.bindPopup(`<b>${feature.properties.label}</b>`);
      }
      if (feature.properties?.route_type) {
        const p = feature.properties;
        layer.bindPopup(`
          <b>${p.route_type === 'safe' ? 'Safe Route' : 'Fast Route'}</b><br>
          Safety: <span style="color:${scoreColor(p.safety_score)};font-weight:bold">${p.safety_score}/100</span><br>
          Distance: ${p.distance_km} km<br>
          Time: ~${formatMinutes(p.duration_min)} min<br>
          Issues on path: ${p.issues_on_path ?? 0}<br>
          <b>Reported issues on this route:</b>
          ${summarizeRouteIssues(p.route_issues || [])}
        `);
      }
    },
  }).addTo(map);

  const bounds = routeLayer.getBounds();
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [50, 50] });

  drawRouteIssueMarkers(data);

  const features = data.features || [];
  const safeProps = features.find((f) => f.properties?.route_type === 'safe')?.properties;
  const fastProps = features.find((f) => f.properties?.route_type === 'fast')?.properties;
  const sameRoute = Boolean(data?.metadata?.same_route);
  const mobile = window.innerWidth <= 768;

  const summaryEl = document.getElementById('route-summary');
  if (!summaryEl || !safeProps) return;

  summaryEl.style.display = 'block';
  summaryEl.classList.remove('sheet-open', 'sheet-peek');

  if (mobile) {
    const hasFast = !sameRoute && Boolean(fastProps);
    _activeSheetTab = 'safe';
    summaryEl.innerHTML = `
      <div id="sheet-handle"></div>
      <div class="sheet-tab-row">
        <button class="sheet-tab sheet-tab-safe active" id="tab-safe" onclick="selectSheetTab('safe')">🛡️ Safe</button>
        ${hasFast ? `<button class="sheet-tab sheet-tab-fast" id="tab-fast" onclick="selectSheetTab('fast')">⚡ Fast</button>` : ''}
        <div class="sheet-quick-btns">
          <button class="small-btn" onclick="showSteps(_activeSheetTab)">Directions</button>
          <button class="small-btn small-btn-primary" onclick="startLiveNavigation(_activeSheetTab)">Start</button>
        </div>
      </div>
      <div id="sheet-detail">
        ${sameRoute ? `<div style="margin-bottom:8px;padding:8px;border-radius:8px;background:#ecfdf3;color:#0f5132;font-size:12px;">Safest route is also the fastest here.</div>` : ''}
        <div class="route-cards">
          ${routeCard(safeProps, 'safe', data.metadata)}
          ${hasFast ? routeCard(fastProps, 'fast', data.metadata) : ''}
        </div>
      </div>
      <div id="live-nav-panel" style="margin-top:10px;"></div>
      <div id="steps-panel" style="margin-top:10px;"></div>
    `;
    requestAnimationFrame(() => {
      summaryEl.classList.add('sheet-peek');
      initSheetDrag(summaryEl);
      document.body.classList.add('sheet-active');
    });
  } else {
    if (sameRoute || !fastProps) {
      summaryEl.innerHTML = `
        <h4 style="margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#6b7280;">Route Result</h4>
        <div style="margin-bottom:8px;padding:8px;border-radius:8px;background:#ecfdf3;color:#0f5132;font-size:12px;">
          The safest route is also the fastest here.
        </div>
        <div class="route-cards">${routeCard(safeProps, 'safe', data.metadata)}</div>
        <div id="live-nav-panel" style="margin-top:10px;"></div>
        <div id="steps-panel" style="margin-top:10px;"></div>
      `;
    } else {
      summaryEl.innerHTML = `
        <h4 style="margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#6b7280;">Route Comparison</h4>
        <div class="route-cards">
          ${routeCard(safeProps, 'safe', data.metadata)}
          ${routeCard(fastProps, 'fast', data.metadata)}
        </div>
        <div id="live-nav-panel" style="margin-top:10px;"></div>
        <div id="steps-panel" style="margin-top:10px;"></div>
      `;
    }
  }
}

function showSteps(type) {
  const feature = getRouteFeature(type);
  if (!feature?.geometry?.coordinates?.length) return;

  document.querySelectorAll('.route-card').forEach((card) => card.classList.remove('route-card-active'));
  document.getElementById(`route-card-${type}`)?.classList.add('route-card-active');

  // Bird's-eye view: fit the full route instead of showing text steps
  const latLngs = feature.geometry.coordinates.map((c) => [c[1], c[0]]);
  map.fitBounds(L.latLngBounds(latLngs), { padding: [50, 50], animate: true });

  // Clear any leftover steps text
  const panel = document.getElementById('steps-panel');
  if (panel) panel.innerHTML = '';

  // On mobile, close the sidebar so the map is visible
  if (window.innerWidth <= 768) closeMobileSearch();
}

function getRouteFeature(type) {
  if (!lastRoutePayload?.data?.features) return null;
  return lastRoutePayload.data.features.find((f) => f.properties?.route_type === type) || null;
}

function distanceMetersBetweenCoords(a, b) {
  return metersBetween(a[1], a[0], b[1], b[0]);
}

function routeLengthMeters(coords) {
  if (!Array.isArray(coords) || coords.length < 2) return 0;
  let total = 0;
  for (let i = 0; i < coords.length - 1; i += 1) {
    total += distanceMetersBetweenCoords(coords[i], coords[i + 1]);
  }
  return total;
}

function nearestPointOnRoute(userLat, userLon, coords) {
  let bestIdx = 0;
  let bestDist = Number.POSITIVE_INFINITY;
  for (let i = 0; i < coords.length; i += 1) {
    const p = coords[i];
    const d = metersBetween(userLat, userLon, p[1], p[0]);
    if (d < bestDist) {
      bestDist = d;
      bestIdx = i;
    }
  }
  return { index: bestIdx, dist_m: bestDist };
}

function remainingDistanceMeters(userLat, userLon, coords, nearestIdx) {
  if (!coords.length) return 0;
  let remaining = metersBetween(userLat, userLon, coords[nearestIdx][1], coords[nearestIdx][0]);
  for (let i = nearestIdx; i < coords.length - 1; i += 1) {
    remaining += distanceMetersBetweenCoords(coords[i], coords[i + 1]);
  }
  return remaining;
}

function stepFromProgress(traveledM, steps) {
  if (!Array.isArray(steps) || !steps.length) return '';
  let cumulative = 0;
  for (let i = 0; i < steps.length; i += 1) {
    cumulative += Number(steps[i].distance_m || 0);
    if (traveledM <= cumulative + 20) return steps[i].instruction || '';
  }
  return steps[steps.length - 1].instruction || '';
}

function ensureLivePanel() {
  const summary = document.getElementById('route-summary');
  if (!summary) return null;
  let panel = document.getElementById('live-nav-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'live-nav-panel';
    panel.style.marginTop = '10px';
    summary.appendChild(panel);
  }
  return panel;
}

function renderLivePanel(html) {
  const panel = ensureLivePanel();
  if (!panel) return;
  panel.innerHTML = html || '';
}

function stopLiveNavigation(silent = false) {
  if (_navWatchId !== null && navigator.geolocation) {
    navigator.geolocation.clearWatch(_navWatchId);
    _navWatchId = null;
  }
  if (_liveMarker) {
    map.removeLayer(_liveMarker);
    _liveMarker = null;
  }
  if (_liveAccuracyCircle) {
    map.removeLayer(_liveAccuracyCircle);
    _liveAccuracyCircle = null;
  }
  if (_liveTrail) {
    map.removeLayer(_liveTrail);
    _liveTrail = null;
  }
  _liveRouteType = null;
  _liveRouteCoords = [];
  _liveRouteSteps = [];
  _liveRouteTotalM = 0;
  _lastNavLat = null; _lastNavLon = null; _liveHeading = 0;
  if (silent) {
    renderLivePanel('');
    return;
  }
  renderLivePanel('');
  showToast('Live navigation stopped.');
}

function updateLiveNavigation(position) {
  if (!_liveRouteCoords.length) return;

  const userLat = position.coords.latitude;
  const userLon = position.coords.longitude;
  const accuracy = Number(position.coords.accuracy || 0);
  const ll = [userLat, userLon];

  // Resolve heading: prefer GPS heading, fall back to computed bearing from last position
  let heading = position.coords.heading;
  if (heading === null || isNaN(heading)) {
    if (_lastNavLat !== null) {
      heading = _computeBearing(_lastNavLat, _lastNavLon, userLat, userLon);
    } else {
      heading = _liveHeading;
    }
  }
  _liveHeading = heading;
  _lastNavLat = userLat; _lastNavLon = userLon;

  const arrowIcon = _navArrowIcon(heading);
  if (!_liveMarker) {
    _liveMarker = L.marker(ll, { icon: arrowIcon, zIndexOffset: 1000 }).addTo(map);
  } else {
    _liveMarker.setLatLng(ll);
    _liveMarker.setIcon(arrowIcon);
  }

  // Keep map centred on user (smooth pan)
  map.panTo(ll, { animate: true, duration: 0.5 });

  if (!_liveAccuracyCircle) {
    _liveAccuracyCircle = L.circle(ll, {
      radius: Math.max(5, accuracy),
      color: '#60a5fa',
      fillColor: '#93c5fd',
      fillOpacity: 0.15,
      weight: 1,
    }).addTo(map);
  } else {
    _liveAccuracyCircle.setLatLng(ll);
    _liveAccuracyCircle.setRadius(Math.max(5, accuracy));
  }

  if (!_liveTrail) {
    _liveTrail = L.polyline([ll], { color: '#1d4ed8', weight: 3, opacity: 0.7 }).addTo(map);
  } else {
    _liveTrail.addLatLng(ll);
    // Keep trail to last 150 points to avoid memory/render issues on long trips
    const pts = _liveTrail.getLatLngs();
    if (pts.length > 150) _liveTrail.setLatLngs(pts.slice(pts.length - 150));
  }

  const nearest = nearestPointOnRoute(userLat, userLon, _liveRouteCoords);
  const remainingM = remainingDistanceMeters(userLat, userLon, _liveRouteCoords, nearest.index);
  const traveledM = Math.max(0, _liveRouteTotalM - remainingM);
  const speed = _liveRouteMode === 'drive' ? 24 : _liveRouteMode === 'cycle' ? 14 : 4.8;
  const etaMin = (remainingM / 1000) / speed * 60;
  const nextStep = stepFromProgress(traveledM, _liveRouteSteps);

  renderLivePanel(`
    <div style="padding:10px;border:1px solid #dbeafe;border-radius:8px;background:#eff6ff;">
      <div style="font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:#1e3a8a;margin-bottom:4px;">Live Navigation (${_liveRouteType})</div>
      <div style="font-size:12px;color:#1f2937;line-height:1.5;">
        Remaining: <b>${(remainingM / 1000).toFixed(2)} km</b><br>
        ETA: <b>~${formatMinutes(etaMin)} min</b><br>
        Accuracy: <b>${Math.round(accuracy)} m</b><br>
        Next: ${escHtml(nextStep || 'Follow the highlighted route')}
      </div>
      <div style="margin-top:8px;">
        <button class="small-btn" onclick="stopLiveNavigation()">Stop live</button>
      </div>
    </div>
  `);

  if (remainingM <= LIVE_NAV_ARRIVAL_M) {
    showToast('You have arrived at your destination.');
    stopLiveNavigation(true);
    renderLivePanel(`
      <div style="padding:10px;border:1px solid #d1fae5;border-radius:8px;background:#ecfdf5;color:#065f46;font-size:12px;">
        Arrival detected. Live navigation finished.
      </div>
    `);
  }
}

let _pendingNavType = null;
let _lastNavLat = null;
let _lastNavLon = null;
let _liveHeading = 0;

function _computeBearing(lat1, lon1, lat2, lon2) {
  const toRad = Math.PI / 180;
  const dLon = (lon2 - lon1) * toRad;
  const y = Math.sin(dLon) * Math.cos(lat2 * toRad);
  const x = Math.cos(lat1 * toRad) * Math.sin(lat2 * toRad) -
            Math.sin(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.cos(dLon);
  return ((Math.atan2(y, x) * 180 / Math.PI) + 360) % 360;
}

function _navArrowIcon(heading) {
  return L.divIcon({
    html: `<svg width="36" height="36" viewBox="0 0 36 36" style="transform:rotate(${heading}deg);display:block;">
      <polygon points="18,3 30,30 18,24 6,30" fill="#1d4ed8" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/>
    </svg>`,
    className: '',
    iconSize: [36, 36],
    iconAnchor: [18, 18],
  });
}

function showLocationConfirm(type) {
  _pendingNavType = type;
  const dialog = document.getElementById('location-dialog');
  if (dialog) { dialog.style.display = 'flex'; }
}

function dismissLocationDialog() {
  const dialog = document.getElementById('location-dialog');
  if (dialog) dialog.style.display = 'none';
  _pendingNavType = null;
}

function confirmLocationDialog() {
  const dialog = document.getElementById('location-dialog');
  if (dialog) dialog.style.display = 'none';
  if (_pendingNavType) beginLiveNavigation(_pendingNavType);
  _pendingNavType = null;
}

function startLiveNavigation(type = 'safe') {
  const feature = getRouteFeature(type);
  if (!feature?.geometry?.coordinates?.length) {
    showToast('No route available for live navigation.');
    return;
  }
  if (!navigator.geolocation) {
    showToast('Geolocation is not supported on this browser/device.');
    return;
  }
  showLocationConfirm(type);
}

function beginLiveNavigation(type) {
  const feature = getRouteFeature(type);
  if (!feature) return;

  const coords = feature.geometry.coordinates;
  _liveRouteType = type;
  _liveRouteCoords = coords;
  _liveRouteSteps = feature.properties?.steps || [];
  _liveRouteMode = feature.properties?.travel_mode || lastRoutePayload?.mode || 'walk';
  _liveRouteTotalM = routeLengthMeters(coords);

  if (_navWatchId !== null) stopLiveNavigation(true);
  _lastNavLat = null; _lastNavLon = null; _liveHeading = 0;

  navigator.geolocation.getCurrentPosition(
    (pos) => {
      // Zoom into user position like Google Maps navigation mode
      map.setView([pos.coords.latitude, pos.coords.longitude], 17, { animate: true });
      updateLiveNavigation(pos);
      _navWatchId = navigator.geolocation.watchPosition(
        updateLiveNavigation,
        () => {
          showToast('Could not read live location updates.');
          stopLiveNavigation(true);
        },
        { enableHighAccuracy: true, maximumAge: 5000, timeout: 12000 }
      );
      showToast(`Live navigation started for ${type} route.`);
    },
    () => {
      showToast('Location permission denied. Enable it in your browser settings to use live navigation.');
      stopLiveNavigation(true);
    },
    { enableHighAccuracy: true, timeout: 12000 }
  );
}



async function toggleIssueLayer(on) {
  if (!on) {
    if (globalIssueLayer) { map.removeLayer(globalIssueLayer); globalIssueLayer = null; }
    return;
  }
  try {
    await loadGlobalIssueLayer();
  } catch {
    showToast('Unable to load issue markers right now.');
  }
}

async function loadGlobalIssueLayer() {
  // Admin always sees the dedicated adminIssueLayer with full controls; skip toggle layer.
  if (currentUser?.is_admin) return;

  if (globalIssueLayer) { map.removeLayer(globalIssueLayer); globalIssueLayer = null; }

  const res = await fetch(`${API_BASE}/issues`);
  const issues = await res.json();
  if (!Array.isArray(issues)) return;

  // Grid-based clustering: one dot per ~200 m cell. Never expands into individual markers.
  const CELL = 0.002;
  const cells = new Map();
  for (const issue of issues) {
    const key = `${Math.floor(issue.lat / CELL)},${Math.floor(issue.lon / CELL)}`;
    if (!cells.has(key)) cells.set(key, []);
    cells.get(key).push(issue);
  }

  globalIssueLayer = L.layerGroup();
  for (const cellIssues of cells.values()) {
    const avgLat = cellIssues.reduce((s, i) => s + i.lat, 0) / cellIssues.length;
    const avgLon = cellIssues.reduce((s, i) => s + i.lon, 0) / cellIssues.length;
    const count = cellIssues.length;
    const r = count > 3 ? 13 : count > 1 ? 10 : 8;

    const dot = L.circleMarker([avgLat, avgLon], {
      radius: r,
      color: '#c0392b',
      fillColor: '#e74c3c',
      fillOpacity: 0.85,
      weight: 2,
    });

    // Group same-category issues within the cell, summing report counts
    const sevOrder = { high: 2, medium: 1, low: 0 };
    const sevColor = { low: '#f39c12', medium: '#e67e22', high: '#e74c3c' };
    const byCategory = new Map();
    for (const i of cellIssues) {
      const cat = i.category;
      if (!byCategory.has(cat)) byCategory.set(cat, { totalReports: 0, severity: i.severity });
      const g = byCategory.get(cat);
      g.totalReports += (i.num_reports || 1);
      if ((sevOrder[i.severity] || 0) > (sevOrder[g.severity] || 0)) g.severity = i.severity;
    }

    const rows = Array.from(byCategory.entries()).map(([cat, g]) => `
      <div style="padding:5px 0;border-bottom:1px solid #f0f0f0">
        <span style="font-weight:600;font-size:12px">${escHtml(cat)}</span>
        <span style="float:right;padding:1px 5px;background:${sevColor[g.severity] || '#e67e22'};color:#fff;border-radius:8px;font-size:10px">${g.severity}</span>
        <div style="clear:both"></div>
        <div style="font-size:11px;color:#888;margin-top:2px">${g.totalReports} report${g.totalReports !== 1 ? 's' : ''}</div>
      </div>
    `).join('');

    dot.bindPopup(`
      <div style="min-width:200px;max-height:260px;overflow-y:auto">
        <b style="font-size:13px">${count} issue${count !== 1 ? 's' : ''} in this area</b>
        <div style="margin-top:8px">${rows}</div>
      </div>
    `);

    globalIssueLayer.addLayer(dot);
  }

  globalIssueLayer.addTo(map);
}

function isAmbiguousIssue(issue) {
  if (!issue) return false;
  return (issue.effective_confidence ?? 0) <= 55;
}

function _issueFormHtml(lat, lon) {
  return `
    <div style="min-width:210px">
      <b style="font-size:14px">Report Issue</b><br><br>
      <label style="font-size:12px;color:#555">Category</label><br>
      <select id="issue-category" style="width:100%;padding:5px;margin-bottom:8px;border-radius:5px;border:1px solid #ddd">
        <option>Broken Streetlight</option>
        <option>Pothole</option>
        <option>Narrow Lane</option>
        <option>Unsafe Area</option>
        <option>Other</option>
      </select>
      <label style="font-size:12px;color:#555">Severity</label><br>
      <select id="issue-severity" style="width:100%;padding:5px;margin-bottom:8px;border-radius:5px;border:1px solid #ddd">
        <option value="low">Low — minor inconvenience</option>
        <option value="medium" selected>Medium — noticeable hazard</option>
        <option value="high">High — serious danger</option>
      </select>
      <label style="font-size:12px;color:#555">Description (optional)</label><br>
      <input id="issue-desc" placeholder="Brief description..." style="width:100%;padding:5px;margin-bottom:10px;border-radius:5px;border:1px solid #ddd;font-size:13px"/><br>
      <button onclick="submitIssue(${lat}, ${lon})" style="width:100%;padding:8px;background:#e74c3c;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px">Submit Report</button>
      <button onclick="cancelIssuePlacement()" style="width:100%;padding:6px;margin-top:6px;background:none;color:#888;border:1px solid #ddd;border-radius:6px;cursor:pointer;font-size:12px">Cancel</button>
    </div>
  `;
}

function cancelIssuePlacement() {
  if (_issueFormPopup) { map.closePopup(_issueFormPopup); _issueFormPopup = null; }
  if (_issueDragMarker) { map.removeLayer(_issueDragMarker); _issueDragMarker = null; }
}

function _startIssuePlacement(lat, lon) {
  if (_issueDragMarker) { map.removeLayer(_issueDragMarker); _issueDragMarker = null; }
  if (_issueFormPopup) { map.closePopup(_issueFormPopup); _issueFormPopup = null; }

  const icon = L.divIcon({
    className: '',
    html: '<div style="width:22px;height:22px;background:#e74c3c;border:3px solid #fff;border-radius:50%;box-shadow:0 2px 8px rgba(0,0,0,0.45);cursor:grab"></div>',
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });

  _issueDragMarker = L.marker([lat, lon], { draggable: true, icon }).addTo(map);

  const openForm = () => {
    const pos = _issueDragMarker.getLatLng();
    const newPopup = L.popup({ maxWidth: 260, offset: [0, -14] })
      .setLatLng(pos)
      .setContent(_issueFormHtml(pos.lat, pos.lng));
    _issueFormPopup = newPopup;
    newPopup.openOn(map);
  };

  openForm();
  showToast('Drag the red marker to the exact spot, then fill the form.', 4000);

  _issueDragMarker.on('dragend', () => {
    // Temporarily disable closePopupOnClick to survive the synthetic click
    // that mobile browsers fire after touchend on a draggable element.
    map.options.closePopupOnClick = false;
    openForm();
    setTimeout(() => { map.options.closePopupOnClick = true; }, 600);
  });

  // Tap the marker to reopen the form if it was dismissed
  _issueDragMarker.on('click', openForm);
}

function enableIssueMode() {
  if (!currentUser) {
    showToast('Please login to report issues.');
    setTimeout(() => (window.location.href = 'login.html'), 1200);
    return;
  }
  if (navigator.geolocation) {
    showToast('Getting your location…', 5000);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const { latitude, longitude } = pos.coords;
        map.flyTo([latitude, longitude], 17, { animate: true, duration: 0.9 });
        setTimeout(() => _startIssuePlacement(latitude, longitude), 950);
      },
      () => {
        issueMode = true;
        showToast('Location unavailable — tap the map to mark the issue.');
        map.getContainer().style.cursor = 'crosshair';
      },
      { enableHighAccuracy: true, timeout: 8000 }
    );
  } else {
    issueMode = true;
    showToast('Tap on the map to place the issue marker.');
    map.getContainer().style.cursor = 'crosshair';
  }
}

map.on('contextmenu', function (e) {
  if (issueMode) return;
  const { lat, lng } = e.latlng;
  const wrap = document.createElement('div');
  wrap.style.cssText = 'text-align:center;min-width:160px';
  const coords = document.createElement('div');
  coords.style.cssText = 'font-size:11px;color:#6b7280;margin-bottom:8px';
  coords.textContent = `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
  const btnO = document.createElement('button');
  btnO.textContent = '📍 Set as Origin';
  btnO.style.cssText = 'display:block;width:100%;margin-bottom:6px;padding:7px 10px;background:#22c55e;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px';
  const btnD = document.createElement('button');
  btnD.textContent = '🏁 Set as Destination';
  btnD.style.cssText = 'display:block;width:100%;padding:7px 10px;background:#e74c3c;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px';
  wrap.append(coords, btnO, btnD);
  const popup = L.popup().setLatLng(e.latlng).setContent(wrap).openOn(map);
  btnO.onclick = () => { setMapPoint('origin', lat, lng); map.closePopup(popup); };
  btnD.onclick = () => { setMapPoint('dest', lat, lng); map.closePopup(popup); };
});

map.on('click', function (e) {
  if (!issueMode) return;
  issueMode = false;
  map.getContainer().style.cursor = '';
  const { lat, lng } = e.latlng;
  L.popup({ maxWidth: 260 })
    .setLatLng(e.latlng)
    .setContent(_issueFormHtml(lat, lng))
    .openOn(map);
});

map.on('popupclose', function (e) {
  if (_issueFormPopup && e.popup === _issueFormPopup) {
    _issueFormPopup = null;
    // Marker stays alive — user can drag it or tap it to reopen the form.
    // It is only removed on Submit (submitIssue) or Cancel (cancelIssuePlacement).
  }
});

async function submitIssue(lat, lon) {
  const category = document.getElementById('issue-category')?.value || 'Other';
  const severity = document.getElementById('issue-severity')?.value || 'medium';
  const description = document.getElementById('issue-desc')?.value || '';

  try {
    const res = await fetch(`${API_BASE}/issues`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ lat, lon, category, severity, description }),
    });

    if (res.status === 401) {
      showToast('Session expired. Please login again.');
      setTimeout(() => (window.location.href = 'login.html'), 1200);
      return;
    }

    if (!res.ok) {
      const err = await res.json();
      showToast(err.detail || 'Failed to submit issue.');
      return;
    }

    if (_issueDragMarker) { map.removeLayer(_issueDragMarker); _issueDragMarker = null; }
    _issueFormPopup = null;
    map.closePopup();
    showToast('Issue reported successfully!');

    if (currentUser?.is_admin) {
      loadAdminIssueLayer();
    } else {
      // Always show a confirmation dot for the user's own report, toggle or not.
      const sevColor = { low: '#f39c12', medium: '#e67e22', high: '#e74c3c' }[severity] || '#e67e22';
      const dot = L.circleMarker([lat, lon], {
        radius: 8, color: sevColor, fillColor: sevColor, fillOpacity: 0.85, weight: 2,
      }).bindPopup(`<b>${escHtml(category)}</b><br><span style="font-size:11px;color:#888">Your report — pending review</span>`);
      dot.addTo(map);
      if (_issuesLayerEnabled()) loadGlobalIssueLayer();
    }
    if (lastRoutePayload) getRoutesFromInput();
  } catch {
    showToast('Failed to submit. Is the backend running?');
  }
}

async function validateIssue(issueId, response) {
  const label = response === 'confirm' ? 'Still there (confirm)' : 'Fixed / Gone (dismiss)';
  if (!confirm(`Mark this issue as: ${label}?`)) return;

  // Attach current GPS position so the backend can weight nearby validations more heavily
  let userLat = null, userLon = null;
  if (navigator.geolocation) {
    await new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (pos) => { userLat = pos.coords.latitude; userLon = pos.coords.longitude; resolve(); },
        () => resolve(),
        { enableHighAccuracy: false, timeout: 3000 }
      );
    });
  }

  try {
    const res = await fetch(`${API_BASE}/issues/${issueId}/validate`, {
      method: 'PATCH',
      headers: authHeaders(),
      body: JSON.stringify({ response, user_lat: userLat, user_lon: userLon }),
    });

    if (res.status === 401) {
      showToast('Session expired. Please login again.');
      setTimeout(() => (window.location.href = 'login.html'), 1200);
      return;
    }

    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Validation failed.');
      return;
    }

    map.closePopup();
    const label = response === 'confirm' ? 'Confirmed' : 'Dismissed';
    showToast(`${label}. New confidence: ${Math.round(data.confidence_score)}`);

    if (_issuesLayerEnabled()) loadGlobalIssueLayer();
    if (lastRoutePayload) getRoutesFromInput();
  } catch {
    showToast('Validation failed. Please try again.');
  }
}

async function loadAdminIssueLayer() {
  if (!currentUser?.is_admin) return;
  if (adminIssueLayer) { map.removeLayer(adminIssueLayer); adminIssueLayer = null; }
  try {
    const res = await fetch(`${API_BASE}/issues`);
    const issues = await res.json();
    if (!Array.isArray(issues)) return;
    adminIssueLayer = L.layerGroup();
    for (const issue of issues) {
      const marker = L.circleMarker([issue.lat, issue.lon], {
        radius: 9, color: '#7f1d1d', fillColor: '#ef4444', fillOpacity: 0.75, weight: 2,
      });
      marker.bindPopup(`
        <b style="font-size:13px">${escHtml(issue.category)}</b><br>
        ${issue.description ? escHtml(issue.description) + '<br>' : ''}
        <small>Reporter: ${escHtml(issue.reporter_name || 'unknown')} &bull; Conf: ${Math.round(issue.effective_confidence || 0)}</small>
        <br><button onclick="deleteIssue('${issue.id}')" style="margin-top:7px;padding:4px 12px;background:#7f1d1d;color:#fff;border:none;border-radius:5px;cursor:pointer;font-size:12px;">&#128465; Delete Issue</button>
      `);
      adminIssueLayer.addLayer(marker);
    }
    adminIssueLayer.addTo(map);
  } catch {}
}

async function deleteIssue(id) {
  if (!confirm('Delete this issue permanently?')) return;
  try {
    const res = await fetch(`${API_BASE}/issues/${id}`, {
      method: 'DELETE',
      headers: authHeaders(),
    });
    if (!res.ok) {
      if (res.status === 403) {
        currentUser = { ...currentUser, is_admin: false };
        setUser(currentUser);
        updateNavbar(currentUser);
        showToast('Admin access revoked. Please refresh.');
      } else {
        const err = await res.json();
        showToast(err.detail || 'Delete failed.');
      }
      return;
    }
    map.closePopup();
    showToast('Issue deleted.');
    if (issueClusterLayer) {
      issueClusterLayer.eachLayer(layer => {
        if (layer._issueId === id) issueClusterLayer.removeLayer(layer);
      });
    }
    loadAdminIssueLayer();
    if (_issuesLayerEnabled()) loadGlobalIssueLayer();
  } catch {
    showToast('Delete failed. Is the backend running?');
  }
}

function showValidationPopup(issueId, latlng) {
  if (!currentUser) return;

  const wrap = document.createElement('div');
  wrap.style.minWidth = '190px';

  const title = document.createElement('b');
  title.textContent = 'Nearby Issue';
  const br1 = document.createElement('br');
  const text = document.createElement('span');
  text.textContent = 'Can you verify this issue?';
  const br2 = document.createElement('br');
  const br3 = document.createElement('br');

  const btnConfirm = document.createElement('button');
  btnConfirm.textContent = 'Still there';
  Object.assign(btnConfirm.style, {
    marginRight: '8px', padding: '6px 12px',
    background: '#27ae60', color: '#fff',
    border: 'none', borderRadius: '5px',
    cursor: 'pointer', fontSize: '13px',
  });
  btnConfirm.addEventListener('click', () => validateIssue(issueId, 'confirm'));

  const btnDismiss = document.createElement('button');
  btnDismiss.textContent = 'Fixed / gone';
  Object.assign(btnDismiss.style, {
    padding: '6px 12px',
    background: '#e74c3c', color: '#fff',
    border: 'none', borderRadius: '5px',
    cursor: 'pointer', fontSize: '13px',
  });
  btnDismiss.addEventListener('click', () => validateIssue(issueId, 'dismiss'));

  wrap.append(title, br1, text, br2, br3, btnConfirm, btnDismiss);
  L.popup().setLatLng(latlng).setContent(wrap).openOn(map);
}

function _clearRouteDisplay() {
  if (routeLayer)        { map.removeLayer(routeLayer);        routeLayer        = null; }
  if (markerLayer)       { map.removeLayer(markerLayer);       markerLayer       = null; }
  if (issueClusterLayer) { map.removeLayer(issueClusterLayer); issueClusterLayer = null; }
  lastRoutePayload = null;
  const summaryEl = document.getElementById('route-summary');
  if (summaryEl) {
    summaryEl.style.display = 'none';
    summaryEl.classList.remove('sheet-open', 'sheet-peek');
  }
  document.body.classList.remove('sheet-active');
}

function clearOriginInput() {
  document.getElementById('originInput').value = '';
  originCoords = null;
  if (_originPin) { map.removeLayer(_originPin); _originPin = null; }
  document.getElementById('originInput-list').style.display = 'none';
  _clearRouteDisplay();
}

function clearDestInput() {
  document.getElementById('destInput').value = '';
  destCoords = null;
  if (_destPin) { map.removeLayer(_destPin); _destPin = null; }
  document.getElementById('destInput-list').style.display = 'none';
  _clearRouteDisplay();
}

function useMyLocation() {
  if (!navigator.geolocation) {
    showToast('Geolocation is not supported by your browser.');
    return;
  }
  showToast('Getting your location…', 5000);
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const { latitude, longitude } = pos.coords;
      originCoords = { lat: latitude, lon: longitude };
      document.getElementById('originInput').value = 'My Location';
      document.getElementById('originInput-list').style.display = 'none';
      _placePin('origin', latitude, longitude);
    },
    () => {
      showToast('Could not get your location. Check browser permissions.');
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
}

function stopPositionWatch() {
  if (_watchId !== null && navigator.geolocation) {
    navigator.geolocation.clearWatch(_watchId);
    _watchId = null;
  }
  stopLiveNavigation(true);
}

function startPositionWatch() {
  if (!navigator.geolocation || !trackingEnabled || !currentUser) return;

  stopPositionWatch();
  _watchId = navigator.geolocation.watchPosition(
    (pos) => {
      if (!currentUser || !trackingEnabled || !issueClusterLayer) return;
      const userLatLng = L.latLng(pos.coords.latitude, pos.coords.longitude);

      issueClusterLayer.eachLayer((layer) => {
        if (!layer?.getLatLng) return;
        const issueId = layer._issueId;
        const meta = layer._issueMeta;
        if (!issueId || !isAmbiguousIssue(meta)) return;
        if (meta.reporter_id && currentUser && meta.reporter_id === currentUser.id) return;

        const dist = userLatLng.distanceTo(layer.getLatLng());
        if (dist < 70 && !shownValidationSet.has(issueId)) {
          shownValidationSet.add(issueId);
          showValidationPopup(issueId, layer.getLatLng());
        }
      });
    },
    null,
    { enableHighAccuracy: true, maximumAge: 10000 }
  );
}

function syncTrackingUI() {
  if (!currentUser) { trackingEnabled = false; return; }
  trackingEnabled = localStorage.getItem(lsKey(LS_TRACKING_ENABLED)) === '1';
  if (trackingEnabled) startPositionWatch();
}

setInterval(async () => {
  if (!currentUser || !trackingEnabled || !navigator.geolocation) return;

  navigator.geolocation.getCurrentPosition(async (pos) => {
    const userLatLng = L.latLng(pos.coords.latitude, pos.coords.longitude);
    if (!issueClusterLayer) return;

    issueClusterLayer.eachLayer((layer) => {
      if (!layer?.getLatLng) return;
      const issueId = layer._issueId;
      const meta = layer._issueMeta;
      if (!issueId || !isAmbiguousIssue(meta)) return;
      if (meta.reporter_id && currentUser && meta.reporter_id === currentUser.id) return;
      const dist = userLatLng.distanceTo(layer.getLatLng());
      if (dist < 200 && !shownValidationSet.has(issueId)) {
        shownValidationSet.add(issueId);
        showValidationPopup(issueId, layer.getLatLng());
      }
    });
  });
}, 5 * 60 * 1000);

function logout() {
  // Stop all live tracking before clearing auth
  stopLiveNavigation(true);
  stopPositionWatch();
  // Clear in-memory state that must not bleed into the next user's session
  shownValidationSet.clear();
  issueMode = false;
  trackingEnabled = false;
  currentUser = null;
  // Clear auth tokens
  clearToken();
  clearUser();
  window.location.href = 'login.html';
}

function selectMode(mode, fromUser = false) {
  document.getElementById('mode').value = mode;
  document.querySelectorAll('.mode-pill').forEach((p) =>
    p.classList.toggle('active', p.dataset.mode === mode)
  );
  updateMobileModeBadge(mode);
  if (fromUser && currentUser) {
    fetch(`${API_BASE}/auth/profile/mode?mode=${encodeURIComponent(mode)}`, {
      method: 'PATCH', headers: authHeaders(false),
    }).catch(() => {});
  }
  if (lastRoutePayload) getRoutesFromInput();
}

function initSheetDrag(summary) {
  const handle = document.getElementById('sheet-handle');
  if (!handle || !summary) return;

  let startY = null;
  let startIsOpen = false;
  let sheetH = 0;
  let wasDrag = false;

  function dragStart(clientY) {
    startY = clientY;
    startIsOpen = summary.classList.contains('sheet-open');
    sheetH = summary.offsetHeight;
    wasDrag = false;
    summary.style.transition = 'none';
  }

  function dragMove(clientY) {
    if (startY === null) return;
    const deltaY = clientY - startY;
    if (Math.abs(deltaY) > 8) wasDrag = true;
    if (!wasDrag) return;
    const peekPx = sheetH - 100;
    const ty = startIsOpen
      ? Math.max(0, Math.min(peekPx, deltaY))
      : Math.max(0, Math.min(peekPx, peekPx + deltaY));
    summary.style.transform = `translateY(${ty}px)`;
  }

  function dragEnd(clientY) {
    if (startY === null) return;
    const deltaY = clientY - startY;
    summary.style.transition = '';
    summary.style.transform = '';

    if (wasDrag) {
      if (startIsOpen && deltaY > 60) {
        summary.classList.remove('sheet-open');
        summary.classList.add('sheet-peek');
      } else if (!startIsOpen && deltaY < -60) {
        summary.classList.remove('sheet-peek');
        summary.classList.add('sheet-open');
      }
    } else {
      toggleBottomSheet();
    }
    startY = null;
  }

  // Touch events (real phone)
  handle.addEventListener('touchstart', (e) => dragStart(e.touches[0].clientY), { passive: true });
  document.addEventListener('touchmove', (e) => dragMove(e.touches[0].clientY), { passive: true });
  document.addEventListener('touchend', (e) => dragEnd(e.changedTouches[0].clientY), { passive: true });

  // Mouse events (laptop browser / mobile preview iframe)
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    dragStart(e.clientY);
    function onMove(ev) { dragMove(ev.clientY); }
    function onUp(ev) {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      dragEnd(ev.clientY);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function closeBottomSheet() {
  const el = document.getElementById('route-summary');
  if (!el) return;
  el.classList.remove('sheet-open');
  el.classList.add('sheet-peek');
}

function toggleBottomSheet() {
  const summary = document.getElementById('route-summary');
  if (!summary) return;
  if (summary.classList.contains('sheet-peek')) {
    summary.classList.remove('sheet-peek');
    summary.classList.add('sheet-open');
  } else {
    summary.classList.remove('sheet-open');
    summary.classList.add('sheet-peek');
  }
}

function selectSheetTab(type) {
  if (!getRouteFeature(type)) return; // route type doesn't exist, ignore tap
  _activeSheetTab = type;
  document.querySelectorAll('.sheet-tab').forEach(t => t.classList.remove('active'));
  document.getElementById(`tab-${type}`)?.classList.add('active');
  if (document.getElementById('route-summary')?.classList.contains('sheet-open')) {
    showSteps(type);
  }
}

function wireControls() {
  const routeBtn = document.getElementById('btn-get-route');
  if (routeBtn) routeBtn.onclick = getRoutesFromInput;

  map.on('moveend', () => {
    if (_issuesLayerEnabled()) loadGlobalIssueLayer().catch(() => {});
  });
}

async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  try {
    await navigator.serviceWorker.register('/sw.js?v=6');
  } catch {}
}

function _placePin(type, lat, lon) {
  const isOrigin = type === 'origin';
  const fillColor = isOrigin ? '#22c55e' : '#3b82f6';
  const label = isOrigin ? 'Start' : 'Destination';
  if (isOrigin) {
    if (_originPin) map.removeLayer(_originPin);
    _originPin = L.circleMarker([lat, lon], {
      radius: 11, color: '#fff', weight: 3, fillColor, fillOpacity: 1,
    }).bindPopup(`<b>${label}</b>`).addTo(map);
  } else {
    if (_destPin) map.removeLayer(_destPin);
    _destPin = L.circleMarker([lat, lon], {
      radius: 11, color: '#fff', weight: 3, fillColor, fillOpacity: 1,
    }).bindPopup(`<b>${label}</b>`).addTo(map);
  }
  if (_originPin && _destPin) {
    map.fitBounds(
      L.latLngBounds([_originPin.getLatLng(), _destPin.getLatLng()]),
      { padding: [70, 70] }
    );
  } else {
    map.flyTo([lat, lon], 15, { animate: true, duration: 0.8 });
  }
}

function setMapPoint(type, lat, lon) {
  const label = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  if (type === 'origin') {
    originCoords = { lat, lon };
    document.getElementById('originInput').value = label;
    _placePin('origin', lat, lon);
  } else {
    destCoords = { lat, lon };
    document.getElementById('destInput').value = label;
    _placePin('dest', lat, lon);
  }
  if (window.innerWidth <= 768) openMobileSearch();
}

function openMobileSearch() {
  const trigger = document.getElementById('mobile-search-trigger');
  const sidebar = document.getElementById('sidebar');
  if (trigger) trigger.style.display = 'none';
  if (sidebar) sidebar.classList.add('mobile-open', 'expanded');
  const input = document.getElementById('originInput');
  if (input) setTimeout(() => input.focus(), 50);
}

function closeMobileSearch() {
  const trigger = document.getElementById('mobile-search-trigger');
  const sidebar = document.getElementById('sidebar');
  if (sidebar) sidebar.classList.remove('mobile-open', 'expanded');
  if (trigger) trigger.style.display = '';
}

function updateMobileModeBadge(mode) {
  const badge = document.getElementById('mobile-mode-badge');
  if (!badge) return;
  const icons = { walk: '🚶 Walk', cycle: '🚴 Cycle', drive: '🚗 Drive' };
  badge.textContent = icons[mode] || '🚶 Walk';
}

async function init() {
  currentUser = await verifyToken();
  updateNavbar(currentUser);

  // Restore preferred mode from server profile (before restoreLastSearch so it can override)
  if (currentUser?.preferred_mode) {
    selectMode(currentUser.preferred_mode);
  }

  wireControls();
  syncTrackingUI();

  restoreLastSearch();
  const fromShare = applyShareParamsIfAny();

  setupAutocomplete('originInput', (coords) => {
    originCoords = coords;
    if (coords) _placePin('origin', coords.lat, coords.lon);
    else if (_originPin) { map.removeLayer(_originPin); _originPin = null; }
  });
  setupAutocomplete('destInput', (coords) => {
    destCoords = coords;
    if (coords) _placePin('dest', coords.lat, coords.lon);
    else if (_destPin) { map.removeLayer(_destPin); _destPin = null; }
  });

  if (currentUser && trackingEnabled) startPositionWatch();

  registerServiceWorker();

  if (fromShare) getRoutesFromInput();
}

init();

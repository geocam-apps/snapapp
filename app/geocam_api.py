"""Thin client for the GeoCam manager-api.

Used to look up `cells` whose extent (PostGIS `cells.space`, geography
4326) contains the GPS positions of shots in a snapapp project — the
user then kicks off a training workflow on the matching cell via a
manager-UI URL.

The read-only API at https://manager-api-app.geocam.io exposes cells
indirectly through `collections`. To find cells containing a set of
points we try two strategies in order:

1. `GET /api/cells/search?points=lon1,lat1;lon2,lat2...`
   — a (hypothetical) bulk point-in-polygon endpoint. If the
   manager-api side grows one, snapapp starts using it automatically.

2. Enumerate projects → collections → collection detail; collect any
   cells with a `space` GeoJSON geometry in the response; filter
   client-side with a small point-in-polygon test.

Auth: bearer token via env var SNAPAPP_GEOCAM_API_TOKEN. Base URL
overridable via SNAPAPP_GEOCAM_API_URL (defaults to the prod host).
"""
import json
import os
import time
from urllib.parse import quote, urlencode

import urllib.request
import urllib.error


API_URL = os.environ.get("SNAPAPP_GEOCAM_API_URL",
                         "https://manager-api-app.geocam.io").rstrip("/")
TOKEN = os.environ.get("SNAPAPP_GEOCAM_API_TOKEN", "").strip()
TIMEOUT_S = 20


def _auth_headers():
    if not TOKEN:
        raise RuntimeError(
            "SNAPAPP_GEOCAM_API_TOKEN not set — can't query manager-api"
        )
    # Cloudflare's managed bot-detection (Error 1010) blocks urllib's
    # default User-Agent; send a plausible browser UA.
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) snapapp/1.0"
        ),
    }


def _get(path: str, params: dict = None) -> dict:
    url = f"{API_URL}{path}"
    if params:
        url = f"{url}?{urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"geocam-api {path} → HTTP {e.code}: {body[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"geocam-api {path}: {e.reason}") from None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"geocam-api {path}: non-JSON response: {body[:200]}")


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def search_cells_for_points(points: list) -> dict:
    """Find cells whose extent contains any of the given (lon, lat) points.

    Returns a dict with:
      ok:           True/False
      cells:        list of {slug, name, address, reference,
                              cell_map_slug, project_slug, matched_points}
      tried:        which strategies were attempted
      points:       echo of input
      error:        present iff ok is False
    """
    if not points:
        return {"ok": False, "error": "no points provided",
                "cells": [], "tried": []}

    tried = []

    # Strategy 1: bulk search endpoint (may not exist on the current API)
    tried.append("GET /api/cells/search")
    try:
        param = ";".join(f"{lon:.7f},{lat:.7f}" for lon, lat in points)
        resp = _get("/api/cells/search", {"points": param})
        cells = _normalize_cells(resp.get("items") or resp.get("cells") or [])
        if cells:
            return {"ok": True, "cells": cells, "tried": tried,
                    "points": points, "strategy": "cells_search"}
    except RuntimeError as e:
        tried[-1] += f" (failed: {e})"

    # Strategy 2: walk projects → collections, collect cells with geometries,
    # filter locally.
    tried.append("walk projects → collections → cells")
    try:
        cells = _walk_and_filter(points)
        return {"ok": True, "cells": cells, "tried": tried,
                "points": points, "strategy": "walk_collections"}
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "tried": tried,
                "points": points, "cells": []}


def _normalize_cells(raw_items: list) -> list:
    out = []
    for c in raw_items:
        if not isinstance(c, dict):
            continue
        out.append({
            "slug": c.get("slug"),
            "name": c.get("name"),
            "address": c.get("address"),
            "reference": c.get("reference"),
            "cell_map_slug": (c.get("cell_map") or {}).get("slug")
                              if isinstance(c.get("cell_map"), dict)
                              else c.get("cell_map_slug"),
            "project_slug": (c.get("project") or {}).get("slug")
                              if isinstance(c.get("project"), dict)
                              else c.get("project_slug"),
            "matched_points": c.get("matched_points"),
        })
    return [c for c in out if c["slug"]]


def _walk_and_filter(points: list) -> list:
    """Fallback: list all projects → all collections → examine cells.

    Requires the manager-api to return cell `space` GeoJSON on the
    collection detail response. If it doesn't, this returns [] without
    error — which is a hint that a proper search endpoint is needed.
    """
    # Projects the caller can see.
    projects_resp = _get("/api/projects")
    projects = projects_resp.get("items") or []

    matches_by_slug = {}
    total_examined = 0

    for proj in projects:
        proj_slug = proj.get("slug")
        if not proj_slug:
            continue
        try:
            colls_resp = _get(f"/api/projects/{quote(proj_slug)}/collections")
        except RuntimeError:
            continue
        colls = colls_resp.get("items") or []
        for coll in colls:
            coll_slug = coll.get("slug")
            if not coll_slug:
                continue
            try:
                coll_resp = _get(f"/api/collections/{quote(coll_slug)}")
            except RuntimeError:
                continue
            # The detail response might include a `cells` array on either
            # the top level or nested; be defensive.
            cells = (coll_resp.get("cells")
                     or (coll_resp.get("collection") or {}).get("cells")
                     or [])
            for cell in cells:
                total_examined += 1
                matched = _points_in_cell(points, cell)
                if not matched:
                    continue
                slug = cell.get("slug")
                if not slug:
                    continue
                if slug not in matches_by_slug:
                    matches_by_slug[slug] = {
                        "slug": slug,
                        "name": cell.get("name"),
                        "address": cell.get("address"),
                        "reference": cell.get("reference"),
                        "cell_map_slug": coll_slug,
                        "project_slug": proj_slug,
                        "matched_points": [],
                    }
                matches_by_slug[slug]["matched_points"].extend(matched)

    if not matches_by_slug and total_examined == 0:
        raise RuntimeError(
            "Manager-api returned no cell geometries on any /api/collections/:slug "
            "response — add a point-containment endpoint on the server side or "
            "include `cells[].space` GeoJSON on the collection detail."
        )
    return list(matches_by_slug.values())


def _points_in_cell(points: list, cell: dict) -> list:
    """Return the subset of points that fall inside `cell['space']`.

    Accepts either:
      cell['space']       = GeoJSON geometry {type: "Polygon"|"MultiPolygon", coordinates: […]}
      cell['space_geojson'] = same, under an alternate key
      cell['bbox']        = [min_lon, min_lat, max_lon, max_lat] (loose fallback)
    """
    geom = cell.get("space") or cell.get("space_geojson") or cell.get("geometry")
    if isinstance(geom, str):
        try:
            geom = json.loads(geom)
        except Exception:
            geom = None

    matched = []
    if isinstance(geom, dict):
        gt = (geom.get("type") or "").lower()
        coords = geom.get("coordinates")
        if gt == "polygon" and coords:
            rings = coords
            for lon, lat in points:
                if _point_in_poly_with_holes(lon, lat, rings):
                    matched.append([lon, lat])
        elif gt == "multipolygon" and coords:
            for lon, lat in points:
                for rings in coords:
                    if _point_in_poly_with_holes(lon, lat, rings):
                        matched.append([lon, lat]); break
        return matched

    bbox = cell.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        min_lon, min_lat, max_lon, max_lat = bbox
        for lon, lat in points:
            if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
                matched.append([lon, lat])
    return matched


def _point_in_poly_with_holes(lon, lat, rings):
    """Ray-casting PIP with hole support; rings[0] is outer, rest are holes."""
    if not rings:
        return False
    if not _point_in_ring(lon, lat, rings[0]):
        return False
    for hole in rings[1:]:
        if _point_in_ring(lon, lat, hole):
            return False
    return True


def _point_in_ring(lon, lat, ring):
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-18) + xi):
            inside = not inside
        j = i
    return inside

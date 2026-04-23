"""Registry of available MegaLoc reference datasets.

Each reference has a FAISS index, an `extent` (geographic bbox in WGS84),
and optionally a COLMAP model + image source. At query time snapapp
picks the reference whose extent contains (or is closest to) the
incoming phone shots' GPS points.

Registry lives at `data/ref_registry.json` by default, or at a path
given by SNAPAPP_REF_REGISTRY. Each entry:

  {
    "name": "costa_mesa_drive",
    "faiss_dir": "/home/dev/costa_mesa_reference_db",
    "model_dir": null,                          # optional
    "images_url": "file:///home/dev/costa_mesa_panos",  # optional
    "extent": {"lat_min": ..., "lat_max": ...,
               "lon_min": ..., "lon_max": ...},
    "meta": {"n_panos": 588, "n_subviews": 4704,
             "layout": "split_pano", "num_views": 8,
             ... freeform}
  }

The legacy single-dataset env vars (SNAPAPP_MEGALOC_DB etc.) still
work — if the registry is empty or no entry matches, we fall back to
that single configured reference.
"""
import json
import os
from pathlib import Path

from . import paths


_REGISTRY_PATH = Path(
    os.environ.get("SNAPAPP_REF_REGISTRY") or (paths.DATA_DIR / "ref_registry.json")
)


def load() -> list:
    if not _REGISTRY_PATH.exists():
        return []
    try:
        with open(_REGISTRY_PATH) as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("references") or []
    return [e for e in data if isinstance(e, dict) and e.get("faiss_dir")]


def save(entries: list):
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_REGISTRY_PATH, "w") as f:
        json.dump({"references": entries}, f, indent=2)


def upsert(entry: dict):
    """Insert or update a registry entry by `name`."""
    entries = load()
    name = entry.get("name")
    if not name:
        raise ValueError("registry entry needs a 'name'")
    out = [e for e in entries if e.get("name") != name]
    out.append(entry)
    save(out)
    return entry


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _contains(extent: dict, lat: float, lon: float) -> bool:
    if not extent:
        return False
    return (extent["lat_min"] <= lat <= extent["lat_max"]
            and extent["lon_min"] <= lon <= extent["lon_max"])


def _score(entry: dict, points) -> tuple:
    """Return (n_contained, priority, -distance_to_center, name) — higher
    is better. When multiple references contain the same points (e.g. a
    pano dataset and an undistorted dataset over the same area), the one
    with a higher `priority` wins. Default priority is 0.
    """
    ext = entry.get("extent") or {}
    if not ext:
        return (0, 0, 0, entry.get("name", ""))
    n_in = sum(1 for lat, lon in points if _contains(ext, lat, lon))
    cx = (ext["lat_min"] + ext["lat_max"]) / 2
    cy = (ext["lon_min"] + ext["lon_max"]) / 2
    dists = [(lat - cx) ** 2 + (lon - cy) ** 2 for lat, lon in points]
    mean_d2 = sum(dists) / len(dists) if dists else 1e9
    priority = int(entry.get("priority") or 0)
    return (n_in, priority, -mean_d2, entry.get("name", ""))


def select_for_points(points, nearby_deg: float = 0.005) -> dict | None:
    """Pick the reference whose extent matches the most points, breaking
    ties by proximity of the reference center to the points. Returns None
    when the registry is empty.

    If no entry strictly contains any point, we fall back to the closest
    entry whose center is within `nearby_deg` (default ~550 m at mid-lat)
    of the points' centroid — the COLMAP-derived extents are tight
    bounding boxes and a phone capture can easily land a few metres
    outside. Beyond that radius we return None (wrong dataset).

    `points` may be (lat, lon) or (lon, lat); we normalize by checking
    which value fits a latitude range.
    """
    entries = load()
    if not entries:
        return None
    norm = []
    for a, b in points:
        if -90 <= a <= 90 and -180 <= b <= 180:
            norm.append((a, b))
        else:
            norm.append((b, a))

    # 1. Strict containment
    best = max(entries, key=lambda e: _score(e, norm))
    if _score(best, norm)[0] > 0:
        return best

    # 2. Fallback: closest center within nearby_deg
    pts_lat = sum(p[0] for p in norm) / len(norm)
    pts_lon = sum(p[1] for p in norm) / len(norm)
    best_nearby = None
    best_dist = float("inf")
    for e in entries:
        ext = e.get("extent") or {}
        if not ext:
            continue
        cx = (ext["lat_min"] + ext["lat_max"]) / 2
        cy = (ext["lon_min"] + ext["lon_max"]) / 2
        d = ((pts_lat - cx) ** 2 + (pts_lon - cy) ** 2) ** 0.5
        if d < best_dist:
            best_dist = d
            best_nearby = e
    if best_nearby and best_dist <= nearby_deg:
        return best_nearby
    return None


def get_by_name(name: str) -> dict | None:
    for e in load():
        if e.get("name") == name:
            return e
    return None


def as_pipeline_paths(entry: dict) -> dict:
    """Translate a registry entry into the tuple of paths/URLs the pipeline
    and ref_cache expect. Falls back to env-configured defaults for any
    field the entry doesn't provide."""
    return {
        "megaloc_db": Path(entry.get("faiss_dir") or paths.MEGALOC_DB),
        "ref_model_dir": Path(entry["model_dir"]) if entry.get("model_dir")
                         else paths.REF_MODEL_DIR,
        "images_url": entry.get("images_url") or os.environ.get(
            "SNAPAPP_REF_IMAGES_URL") or str(paths.REF_IMAGES_DIR),
        "megaloc_gcdb_dir": Path(entry.get("gcdb_dir") or paths.MEGALOC_GCDB_DIR),
    }

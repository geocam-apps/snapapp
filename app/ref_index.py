"""Reference-shot geographic index.

Computes a `{shot_key → (lat, lon, alt)}` map from the COLMAP reference
model + `crs.json` so we can filter MegaLoc matches by the phone's reported
GPS. `shot_key` is the same `"capture_name/shot_id"` MegaLoc emits.

Built lazily on first use and cached on disk.
"""
import json
import math
import re
import sys
from pathlib import Path

from . import paths


def _cache_path(model_dir=None) -> Path:
    import hashlib
    target = Path(model_dir) if model_dir else paths.REF_MODEL_DIR
    h = hashlib.sha1(str(target).encode()).hexdigest()[:12]
    return paths.DATA_DIR / f"ref_index.{h}.json"


# Memoized per model_dir so switching references doesn't thrash.
_MEM_CACHES: dict = {}


def _crs_info():
    crs_json = paths.REF_MODEL_DIR.parent / "crs.json"
    if not crs_json.exists():
        return None
    with open(crs_json) as f:
        j = json.load(f)
    # crsPose is a row-major rigid transform; the last 3 entries are the
    # translation/offset in projected-CRS units (UTM for this dataset).
    pose = j.get("crsPose") or []
    if len(pose) < 12:
        return None
    offset = tuple(pose[-3:])
    # The WKT has several nested AUTHORITY blocks (spheroid, datum, geog CS…).
    # We want the outermost one, which is the projected CRS itself — it's
    # the LAST `AUTHORITY["EPSG","N"]]` in the string (right before WKT
    # close). Default to UTM 11N if we can't find one.
    matches = re.findall(r'AUTHORITY\["EPSG","(\d+)"\]', j.get("crs", ""))
    epsg = int(matches[-1]) if matches else 32611
    return {"epsg": epsg, "offset": offset}


def _crs_info_for(model_dir: Path):
    crs_json = Path(model_dir).parent / "crs.json"
    if not crs_json.exists():
        return None
    with open(crs_json) as f:
        j = json.load(f)
    pose = j.get("crsPose") or []
    if len(pose) < 12:
        return None
    offset = tuple(pose[-3:])
    matches = re.findall(r'AUTHORITY\["EPSG","(\d+)"\]', j.get("crs", ""))
    epsg = int(matches[-1]) if matches else 32611
    return {"epsg": epsg, "offset": offset}


def _build_index(model_dir=None):
    """Walk a COLMAP reference model → `{shot_key: {lat, lon, alt}}`."""
    try:
        import pyproj
    except ImportError:
        return {}
    target = Path(model_dir) if model_dir else paths.REF_MODEL_DIR
    if not target.exists():
        return {}
    crs = _crs_info_for(target)
    if crs is None:
        return {}

    import numpy as np
    sys.path.insert(0, str(Path.home() / "shotmatch_pose"))
    from colmap_io import read_model, image_center

    cams, imgs, _ = read_model(target)
    transformer = pyproj.Transformer.from_crs(crs["epsg"], 4326, always_xy=True)
    ox, oy, _ = crs["offset"]

    groups = {}  # (capture, shot_id) -> list of centers
    for img in imgs.values():
        parts = img.name.split("/")
        if len(parts) < 2:
            continue
        capture = parts[0]
        shot_id = Path(img.name).stem
        groups.setdefault((capture, shot_id), []).append(
            image_center(img).tolist()
        )

    out = {}
    for (capture, shot_id), centers in groups.items():
        mean = np.mean(centers, axis=0)
        lon, lat = transformer.transform(mean[0] + ox, mean[1] + oy)
        out[f"{capture}/{shot_id}"] = {
            "lat": float(lat), "lon": float(lon), "alt": float(mean[2]),
        }
    return out


def load(model_dir=None):
    """Return the ref index for `model_dir` (defaults to paths.REF_MODEL_DIR).
    Per-model_dir in-memory cache + on-disk cache; changing which reference
    you ask about doesn't thrash either one.
    """
    target = Path(model_dir) if model_dir else paths.REF_MODEL_DIR
    key = str(target)
    if key in _MEM_CACHES:
        return _MEM_CACHES[key]
    cp = _cache_path(target)
    if cp.exists():
        try:
            with open(cp) as f:
                idx = json.load(f)
            _MEM_CACHES[key] = idx
            return idx
        except Exception:
            pass
    idx = _build_index(target)
    _MEM_CACHES[key] = idx
    cp.parent.mkdir(parents=True, exist_ok=True)
    with open(cp, "w") as f:
        json.dump(idx, f)
    return idx


def lookup(shot_key, model_dir=None):
    idx = load(model_dir)
    return idx.get(shot_key)


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1 = math.radians(lat1); phi2 = math.radians(lat2)
    dphi = phi2 - phi1; dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi/2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam/2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

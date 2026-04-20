"""SQLite readers for uploaded session files.

Supports two schemas:
  - `snapapp` — the phone capture app. `shots` table with mid_jpeg + wide_jpeg
    BLOBs per shutter press and a `burst_frames` sibling table at the user's
    selected zoom. Preferred path; shotmatch_pose gets a dense multi-view
    reconstruction out of one session.
  - `gcdb` — GeoCam reference format (poses table). Legacy probe; no images.
"""
import sqlite3
from pathlib import Path


def detect_format(path: Path) -> str:
    """Return 'snapapp', 'gcdb', or 'unknown'."""
    if not Path(path).exists():
        return "unknown"
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "shots" in tables and "burst_frames" in tables:
            cols = {r[1] for r in cur.execute("PRAGMA table_info(shots)")}
            if "mid_jpeg" in cols and "wide_jpeg" in cols:
                conn.close()
                return "snapapp"
        if "poses" in tables:
            conn.close()
            return "gcdb"
        conn.close()
        return "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# SnapApp capture-session schema
# ---------------------------------------------------------------------------

def probe_snapapp(path: Path) -> dict:
    """Summarize a SnapApp .db — counts, bounds, per-shot metadata (no BLOBs)."""
    if not Path(path).exists():
        return {"ok": False, "error": "file not found"}
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()

        meta = {}
        try:
            for k, v in cur.execute("SELECT key, value FROM meta"):
                meta[k] = v
        except Exception:
            pass

        shot_count = cur.execute("SELECT COUNT(*) FROM shots").fetchone()[0]

        bounds = None
        try:
            row = cur.execute(
                "SELECT MIN(lat), MAX(lat), MIN(lon), MAX(lon) FROM shots "
                "WHERE lat IS NOT NULL AND lon IS NOT NULL"
            ).fetchone()
            if row and row[0] is not None:
                bounds = {
                    "lat_min": row[0], "lat_max": row[1],
                    "lon_min": row[2], "lon_max": row[3],
                }
        except Exception:
            pass

        shots = []
        try:
            for row in cur.execute(
                "SELECT id, captured_at, lat, lon, altitude_m, accuracy_m, "
                "       bearing_deg, bearing_accuracy_deg, location_source "
                "FROM shots ORDER BY captured_at"
            ):
                shots.append({
                    "id": row[0], "captured_at": row[1],
                    "lat": row[2], "lon": row[3], "alt": row[4],
                    "accuracy_m": row[5],
                    "bearing_deg": row[6], "bearing_accuracy_deg": row[7],
                    "location_source": row[8],
                })
        except Exception:
            pass

        # burst frame count per shot
        burst_counts = {}
        try:
            for sid, cnt in cur.execute(
                "SELECT shot_id, COUNT(*) FROM burst_frames GROUP BY shot_id"
            ):
                burst_counts[sid] = cnt
        except Exception:
            pass
        for s in shots:
            s["n_burst"] = burst_counts.get(s["id"], 0)

        conn.close()
        return {
            "ok": True, "format": "snapapp",
            "meta": meta, "shot_count": shot_count,
            "bounds": bounds, "shots": shots,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def extract_snapapp_wide(path: Path, out_dir: Path) -> list:
    """Extract the 1x `wide_jpeg` BLOB for every shot into `out_dir`.

    Names are `shot_<id:05d>.jpg` so MegaLoc/COLMAP treats each as a distinct
    photo. Returns a list of per-file metadata dicts (shot_id, filename,
    lat, lon, bearing_deg, captured_at) so the web UI can still render GPS,
    bearing, etc. after the BLOBs are gone.
    """
    path = Path(path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    results = []
    for row in cur.execute(
        "SELECT id, captured_at, lat, lon, bearing_deg, accuracy_m, wide_jpeg "
        "FROM shots WHERE wide_jpeg IS NOT NULL ORDER BY captured_at"
    ):
        sid, captured_at, lat, lon, bearing, acc, wide = row
        name = f"shot_{sid:05d}.jpg"
        (out_dir / name).write_bytes(wide)
        results.append({
            "shot_id": sid, "filename": name,
            "lat": lat, "lon": lon,
            "bearing_deg": bearing, "accuracy_m": acc,
            "captured_at": captured_at,
        })
    conn.close()
    return results


# ---------------------------------------------------------------------------
# GCDB reference format — kept for backward compat, not actively used
# ---------------------------------------------------------------------------

def probe_gcdb(path: Path) -> dict:
    """Dispatch: snapapp if we detect that schema, else legacy gcdb probe."""
    fmt = detect_format(path)
    if fmt == "snapapp":
        return probe_snapapp(path)
    if fmt == "unknown":
        return {"ok": False, "error": "unrecognized sqlite schema"}
    # legacy gcdb
    if not Path(path).exists():
        return {"ok": False, "error": "file not found"}
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        pose_count = cur.execute("SELECT COUNT(*) FROM poses").fetchone()[0]
        bounds = None
        try:
            row = cur.execute(
                "SELECT MIN(lat), MAX(lat), MIN(lon), MAX(lon) FROM poses "
                "WHERE lat IS NOT NULL AND lon IS NOT NULL"
            ).fetchone()
            if row and row[0] is not None:
                bounds = {
                    "lat_min": row[0], "lat_max": row[1],
                    "lon_min": row[2], "lon_max": row[3],
                }
        except Exception:
            pass
        capture_name = None
        try:
            row = cur.execute(
                "SELECT value FROM meta WHERE key = 'capture_name'"
            ).fetchone()
            if row:
                capture_name = row[0]
        except Exception:
            pass
        conn.close()
        return {
            "ok": True, "format": "gcdb",
            "tables": sorted(tables),
            "pose_count": pose_count,
            "bounds": bounds,
            "capture_name": capture_name,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def list_poses(path: Path, limit: int = 500) -> list:
    if not Path(path).exists():
        return []
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        try:
            rows = cur.execute(
                "SELECT id, lat, lon, alt, yaw, pitch, roll FROM poses "
                "ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        except Exception:
            conn.close()
            return []
        conn.close()
        return [
            {"id": r[0], "lat": r[1], "lon": r[2], "alt": r[3],
             "yaw": r[4], "pitch": r[5], "roll": r[6]}
            for r in rows
        ]
    except Exception:
        return []

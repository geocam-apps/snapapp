"""Read an uploaded GCDB (SpatiaLite) file and extract pose summary."""
import sqlite3
from pathlib import Path


def probe_gcdb(path: Path) -> dict:
    """Return a summary of the gcdb file: pose count, bounds, etc."""
    if not path.exists():
        return {"ok": False, "error": "file not found"}

    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()

        tables = {row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        has_poses = "poses" in tables
        has_meta = "meta" in tables

        pose_count = 0
        bounds = None
        capture_name = None
        if has_poses:
            pose_count = cur.execute("SELECT COUNT(*) FROM poses").fetchone()[0]
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

        if has_meta:
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
            "ok": True,
            "tables": sorted(tables),
            "pose_count": pose_count,
            "bounds": bounds,
            "capture_name": capture_name,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def list_poses(path: Path, limit: int = 500) -> list:
    """Return up to `limit` poses with id, lat, lon, alt."""
    if not path.exists():
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

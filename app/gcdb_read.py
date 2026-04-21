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


def _table_columns(cur, table):
    try:
        return {r[1] for r in cur.execute(f'PRAGMA table_info("{table}")')}
    except Exception:
        return set()


def inspect(path: Path) -> dict:
    """Return a debug-friendly summary of the sqlite's tables/columns.

    Always returns something, even on unreadable files — callers use this
    to surface actionable errors in the UI.
    """
    out = {"exists": Path(path).exists(), "tables": {}}
    if not out["exists"]:
        out["error"] = "file not found"
        return out
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ):
            name = row[0]
            out["tables"][name] = sorted(_table_columns(cur, name))
        conn.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def detect_format(path: Path) -> str:
    """Return 'snapapp', 'gcdb', or 'unknown'.

    SnapApp detection is lenient: any table named `shots` (case-insensitive)
    that has a BLOB-ish column with 'jpeg' / 'jpg' / 'image' in its name
    counts. This tolerates schema drift — the user's real captures can have
    burst_frames missing, additional columns, or a different JPEG column
    name — and we just need to know there are images to extract.
    """
    if not Path(path).exists():
        return "unknown"
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        tables = {r[0]: r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        lower_tables = {k.lower(): k for k in tables}

        shots_name = lower_tables.get("shots")
        if shots_name:
            cols = _table_columns(cur, shots_name)
            has_image_col = any(
                any(kw in c.lower() for kw in ("jpeg", "jpg", "image", "photo"))
                for c in cols
            )
            if has_image_col:
                conn.close()
                return "snapapp"
        if "poses" in lower_tables:
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


def _pick_image_column(cur, table):
    """Pick the best image column from a shots-like table.
    Preference: wide_jpeg > mid_jpeg > anything *_jpeg > anything with 'image'/'photo'."""
    cols = _table_columns(cur, table)
    if not cols:
        return None
    for preferred in ("wide_jpeg", "mid_jpeg"):
        if preferred in cols:
            return preferred
    jpeg_cols = [c for c in cols if "jpeg" in c.lower() or "jpg" in c.lower()]
    if jpeg_cols:
        return sorted(jpeg_cols)[0]
    for c in cols:
        lc = c.lower()
        if "image" in lc or "photo" in lc:
            return c
    return None


def _optional_col(cols, *candidates):
    for c in candidates:
        if c in cols:
            return c
    return None


def extract_snapapp_bundle(path: Path, out_dir: Path) -> list:
    """Extract every photo of every shot into per-shot subdirs.

    For each row in `shots`:
      out_dir/shot_<id:05d>/wide.jpg          (1× zoom; MegaLoc anchor)
      out_dir/shot_<id:05d>/mid.jpg           (~halfway zoom)
      out_dir/shot_<id:05d>/burst_<k:02d>.jpg (k=0..N, user-zoom burst)

    Returns one entry per phone-shot:
        {shot_id, dir, wide_filename, photo_filenames, lat, lon,
         bearing_deg, accuracy_m, captured_at, n_burst}

    Assumes the new SnapApp schema where each shutter press emits a
    lateral-swipe burst (see PHONE_UPLOAD.md / capture spec). Tolerant
    of missing tables / columns: missing burst_frames is fine; missing
    mid_jpeg is fine; the wide_jpeg / equivalent is required.
    """
    path = Path(path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    cur = conn.cursor()

    # Find the shots table (case-insensitive)
    shots_table = None
    burst_table = None
    for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        n = row[0]
        if n.lower() == "shots":
            shots_table = n
        elif n.lower() == "burst_frames":
            burst_table = n
    if shots_table is None:
        conn.close()
        return []

    cols = _table_columns(cur, shots_table)
    img_col = _pick_image_column(cur, shots_table)
    if img_col is None:
        conn.close()
        return []
    mid_col = "mid_jpeg" if "mid_jpeg" in cols else None

    c_id = "id" if "id" in cols else "rowid"
    c_captured = _optional_col(cols, "captured_at", "timestamp", "ts")
    c_lat = _optional_col(cols, "lat", "latitude")
    c_lon = _optional_col(cols, "lon", "lng", "longitude")
    c_bearing = _optional_col(cols, "bearing_deg", "bearing",
                               "heading_deg", "heading")
    c_accuracy = _optional_col(cols, "accuracy_m", "accuracy",
                                "horiz_accuracy_m")

    def pick(col):
        return col if col else "NULL"

    select_cols = (
        f'"{c_id}", {pick(c_captured)}, {pick(c_lat)}, {pick(c_lon)}, '
        f'{pick(c_bearing)}, {pick(c_accuracy)}, "{img_col}"'
    )
    if mid_col:
        select_cols += f', "{mid_col}"'

    sql = (
        f'SELECT {select_cols} FROM "{shots_table}" '
        f'WHERE "{img_col}" IS NOT NULL '
        f'ORDER BY {pick(c_captured) if c_captured else c_id}'
    )

    burst_cur = conn.cursor()  # separate cursor for nested burst query
    results = []
    for row in cur.execute(sql):
        sid = row[0]
        captured_at, lat, lon, bearing, acc = row[1], row[2], row[3], row[4], row[5]
        wide_blob = row[6]
        mid_blob = row[7] if mid_col else None
        try:
            sid_int = int(sid)
            shot_dirname = f"shot_{sid_int:05d}"
        except Exception:
            shot_dirname = f"shot_{sid}"
        shot_dir = out_dir / shot_dirname
        shot_dir.mkdir(parents=True, exist_ok=True)

        photo_filenames = []
        wide_name = f"{shot_dirname}/wide.jpg"
        (shot_dir / "wide.jpg").write_bytes(wide_blob)
        photo_filenames.append(wide_name)
        # Mirror the wide JPEG into a flat sibling dir for MegaLoc to
        # query — its CSV will be keyed by `shot_<id:05d>`.
        megaloc_in = out_dir.parent / "megaloc_in"
        megaloc_in.mkdir(exist_ok=True)
        (megaloc_in / f"{shot_dirname}.jpg").write_bytes(wide_blob)

        if mid_blob:
            (shot_dir / "mid.jpg").write_bytes(mid_blob)
            photo_filenames.append(f"{shot_dirname}/mid.jpg")

        n_burst = 0
        if burst_table:
            try:
                for bid, frame_idx, jpeg in burst_cur.execute(
                    f'SELECT id, frame_index, jpeg FROM "{burst_table}" '
                    f'WHERE shot_id = ? ORDER BY frame_index',
                    (sid,),
                ):
                    name = f"burst_{frame_idx:02d}.jpg"
                    (shot_dir / name).write_bytes(jpeg)
                    photo_filenames.append(f"{shot_dirname}/{name}")
                    n_burst += 1
            except Exception:
                pass

        results.append({
            "shot_id": sid,
            "dir": shot_dirname,
            "wide_filename": wide_name,
            "photo_filenames": photo_filenames,
            "lat": lat, "lon": lon,
            "bearing_deg": bearing, "accuracy_m": acc,
            "captured_at": captured_at,
            "n_burst": n_burst,
            "n_photos": len(photo_filenames),
        })
    conn.close()
    return results


def extract_snapapp_wide(path: Path, out_dir: Path) -> list:
    """Extract the 1× wide image BLOB for every shot into `out_dir`.

    Tolerant of schema drift: falls back to mid_jpeg / any *_jpeg column /
    any image-ish column if `wide_jpeg` isn't present. Per-shot columns
    (lat, lon, bearing_deg, accuracy_m, captured_at) are looked up by name
    so missing ones degrade to None rather than failing the whole extract.
    """
    path = Path(path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    cur = conn.cursor()

    # Find the shots table (case-insensitive) and the best image column
    shots_table = None
    for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if row[0].lower() == "shots":
            shots_table = row[0]
            break
    if shots_table is None:
        conn.close()
        return []
    img_col = _pick_image_column(cur, shots_table)
    if img_col is None:
        conn.close()
        return []

    cols = _table_columns(cur, shots_table)
    c_id = _optional_col(cols, "id", "rowid") or "id"
    c_captured = _optional_col(cols, "captured_at", "timestamp", "ts")
    c_lat = _optional_col(cols, "lat", "latitude")
    c_lon = _optional_col(cols, "lon", "lng", "longitude")
    c_bearing = _optional_col(cols, "bearing_deg", "bearing", "heading_deg", "heading")
    c_accuracy = _optional_col(cols, "accuracy_m", "accuracy", "horiz_accuracy_m")

    def pick(col):
        return col if col else "NULL"

    sql = (
        f'SELECT "{c_id}", {pick(c_captured)}, {pick(c_lat)}, {pick(c_lon)}, '
        f'{pick(c_bearing)}, {pick(c_accuracy)}, "{img_col}" '
        f'FROM "{shots_table}" WHERE "{img_col}" IS NOT NULL '
        f'ORDER BY {pick(c_captured) if c_captured else c_id}'
    )

    results = []
    for row in cur.execute(sql):
        sid, captured_at, lat, lon, bearing, acc, blob = row
        try:
            sid_int = int(sid)
            name = f"shot_{sid_int:05d}.jpg"
        except Exception:
            name = f"shot_{sid}.jpg"
        (out_dir / name).write_bytes(blob)
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

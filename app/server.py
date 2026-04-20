"""Flask web server for snapapp."""
import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path.home() / "shotmatch_pose"))

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

from app import db, paths, pipeline, gcdb_read

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app = Flask(
    __name__,
    static_folder=str(STATIC_DIR),
    static_url_path="/static",
    template_folder=str(TEMPLATES_DIR),
)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024  # 8 GB
# Don't let browsers (or Cloudflare) cache the JS/CSS — when the chunked
# uploader shipped, users stuck on the old single-shot uploader tried to
# POST 330 MB in one request and got killed at the edge with no server log.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def _no_cache_static(resp):
    if request.path.startswith("/static/") or request.path.endswith((".html", "/")):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

PHOTO_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".HEIC", ".HEIF", ".JPG", ".JPEG", ".PNG"}


ASSET_VERSION = str(int(time.time()))


@app.context_processor
def _inject_asset_version():
    return {"v": ASSET_VERSION}


@app.route("/")
def index():
    return render_template("index.html", v=ASSET_VERSION)


@app.route("/project/<project_id>")
def project_page(project_id):
    if db.get_project(project_id) is None:
        abort(404)
    return render_template("project.html", v=ASSET_VERSION)


@app.route("/shot/<project_id>/<shot_id>")
def shot_viewer(project_id, shot_id):
    if db.get_shot(shot_id) is None:
        abort(404)
    return render_template("viewer.html", v=ASSET_VERSION)


# ---------------------------------------------------------------------------
# Projects API
# ---------------------------------------------------------------------------

@app.route("/api/projects")
def api_projects():
    projects = db.list_projects()
    for p in projects:
        shots = db.list_shots(p["id"])
        p["n_shots"] = len(shots)
        p["shots_summary"] = {
            "pending": sum(1 for s in shots if s["status"] == "pending"),
            "running": sum(1 for s in shots if s["status"] == "running"),
            "done": sum(1 for s in shots if s["status"] == "done"),
            "failed": sum(1 for s in shots if s["status"] == "failed"),
        }
    return jsonify(projects)


@app.route("/api/projects/<project_id>")
def api_project(project_id):
    p = db.get_project(project_id)
    if p is None:
        abort(404)
    shots = db.list_shots(project_id)
    p["shots"] = shots
    # List uploaded photos
    photos_dir = paths.project_photos_dir(project_id)
    photos = sorted([f.name for f in photos_dir.iterdir() if f.is_file()]) if photos_dir.exists() else []
    p["photos"] = photos
    # If sqlite, include probe info
    sqlite_path = paths.project_sqlite_path(project_id)
    if sqlite_path is not None and sqlite_path.exists():
        p["sqlite"] = gcdb_read.probe_gcdb(sqlite_path)
        p["sqlite"]["poses_sample"] = gcdb_read.list_poses(sqlite_path, limit=50)
    # Include MegaLoc matches if available
    csv_path = paths.project_megaloc_csv(project_id)
    p["megaloc_ready"] = csv_path.exists()
    p["megaloc_running"] = project_id in pipeline.MEGALOC_RUNNING
    if csv_path.exists():
        try:
            p["megaloc_matches"] = pipeline._read_megaloc_csv(csv_path)
        except Exception:
            p["megaloc_matches"] = {}
    return jsonify(p)


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def api_project_delete(project_id):
    p = db.get_project(project_id)
    if p is None:
        abort(404)
    # Clean up disk
    proj_dir = paths.project_dir(project_id)
    shutil.rmtree(proj_dir, ignore_errors=True)
    db.delete_project(project_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Chunked upload (bypasses Cloudflare's 100 MB request body cap)
# ---------------------------------------------------------------------------

CHUNK_UPLOADS_DIR = paths.DATA_DIR / "uploads_partial"
CHUNK_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _chunk_path(upload_id: str) -> Path:
    # Keep the ID opaque but filesystem-safe
    safe = "".join(c for c in upload_id if c.isalnum() or c in "-_")
    if not safe or safe != upload_id:
        abort(400, "bad upload id")
    return CHUNK_UPLOADS_DIR / safe


def _ingest_sqlite_as_project(sqlite_path: Path, orig_name: str, project_name: str = "") -> tuple:
    """Turn an uploaded sqlite file into a project + default shot + megaloc kickoff.

    Shared by the browser finalize flow, the legacy single-shot upload, and the
    new mobile chunked upload. Returns (status_code, response_dict).

    `sqlite_path` is moved into the project dir (caller shouldn't use it after).
    On an unrecognized schema we clean up the project row and return 400 with
    gcdb_read.inspect() output so the client can surface it.
    """
    default_name = Path(orig_name).stem if orig_name else "sqlite project"
    pid = db.create_project(project_name or default_name, "sqlite", meta={})

    dst = paths.new_project_sqlite_path(pid, orig_name or "upload.db")
    shutil.move(str(sqlite_path), str(dst))

    probe = gcdb_read.probe_gcdb(dst)
    fmt = probe.get("format")
    if not probe.get("ok") or fmt != "snapapp":
        # Not a SnapApp schema — surface an actionable error.
        probe["inspection"] = gcdb_read.inspect(dst)
        shutil.rmtree(paths.project_dir(pid), ignore_errors=True)
        db.delete_project(pid)
        return 400, {
            "error": "Unrecognized sqlite schema (need SnapApp `shots` table)",
            "format": fmt,
            "probe": probe,
        }

    photos_dir = paths.project_photos_dir(pid)
    extracted = gcdb_read.extract_snapapp_wide(dst, photos_dir)
    if not extracted:
        shutil.rmtree(paths.project_dir(pid), ignore_errors=True)
        db.delete_project(pid)
        return 400, {"error": "SnapApp .db had no wide_jpeg rows"}

    shot_meta = {e["filename"]: {
        "shot_id": e["shot_id"], "lat": e["lat"], "lon": e["lon"],
        "bearing_deg": e["bearing_deg"], "accuracy_m": e["accuracy_m"],
        "captured_at": e["captured_at"],
    } for e in extracted}
    with db.conn() as c:
        c.execute(
            "UPDATE projects SET meta_json = ? WHERE id = ?",
            (json.dumps({
                "source": "snapapp",
                "original_filename": orig_name,
                "n_sqlite_shots": len(extracted),
                "bounds": probe.get("bounds"),
                "photo_meta": shot_meta,
            }), pid),
        )
    photo_stems = [Path(e["filename"]).stem for e in extracted]
    db.create_shot(
        pid, name=f"All {len(extracted)} shots (default)",
        meta={"photo_stems": photo_stems,
              "photo_names": [e["filename"] for e in extracted]},
    )
    pipeline.start_megaloc_prematch(pid)
    return 200, {
        "project_id": pid, "kind": "sqlite",
        "format": "snapapp", "n_shots": len(extracted),
    }


# ---------------------------------------------------------------------------
# Mobile phone chunked upload — RFC 7233 Content-Range semantics
# ---------------------------------------------------------------------------

# Restrict upload-id to our own expected UUID shape. Stricter than the browser
# flow because the phone client controls this directly over the network.
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Phone clients must send chunks ≤ this. Keeps us comfortably under
# Cloudflare's ~100 MB body cap even with header overhead.
MOBILE_MAX_CHUNK = 16 * 1024 * 1024  # 16 MiB
# Absolute ceiling on a single uploaded file. The pipeline is happy with
# SnapApp sqlites well under this; this just prevents runaway upload-id's
# from filling the partials dir.
MOBILE_MAX_TOTAL = 4 * 1024 * 1024 * 1024  # 4 GB


def _mobile_chunk_path(upload_id: str):
    """Validate the phone-supplied upload-id, return (path, err_response).

    Keeps the partial in the same directory as the browser flow so
    `_chunk_path` and this share a storage area — they never collide because
    browser ids come from uuid.v4() and this regex only accepts the same
    alphabet.
    """
    if not upload_id or not _UPLOAD_ID_RE.match(upload_id):
        return None, (jsonify({"error": "bad X-Upload-Id (need 8-64 [A-Za-z0-9_-] chars)"}), 400)
    return CHUNK_UPLOADS_DIR / upload_id, None


def _parse_content_range(header: str):
    """Parse `bytes <start>-<end>/<total>` → (start, end, total) or None.

    `<end>` is inclusive per RFC 7233. Returns None on anything malformed;
    caller turns that into 400.
    """
    if not header:
        return None
    m = re.match(r"^\s*bytes\s+(\d+)-(\d+)/(\d+)\s*$", header)
    if not m:
        return None
    start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if end < start or total <= 0 or end >= total:
        return None
    return start, end, total


def _sanitize_filename(name: str) -> str:
    """Strip paths, forbid traversal, keep just the basename."""
    if not name:
        return "upload.db"
    # Basename only — refuse paths and anything suspicious.
    base = Path(name).name
    # Drop any control chars / leading dots that could trip path logic.
    base = base.lstrip(".") or "upload.db"
    # Allow reasonably broad charset; this is stored as a meta hint, not a path.
    safe = re.sub(r"[^A-Za-z0-9._\- ]+", "_", base)
    return safe[:200] or "upload.db"


@app.route("/api/mobile/upload", methods=["POST"])
def api_mobile_upload():
    """Chunked upload endpoint for the phone capture app.

    Each chunk is a raw octet-stream body (NOT multipart) with:
      - Content-Range: bytes <start>-<end>/<total>   (end inclusive, RFC 7233)
      - X-Upload-Id: <stable uuid across all chunks of one file>
      - X-Filename: <original basename, e.g. 2026-04-20_maple-street.db>

    Returns:
      - 200 + {upload_id, received, total, complete: false} on a partial
      - 200 + {..., complete: true, project_id, project_url, format, n_shots}
        on the final chunk once the sqlite is ingested
      - 400 on malformed headers / bad filename / unrecognized schema
      - 409 {error: "offset_mismatch", expected, got} if start != existing size
      - 413 if a single chunk is larger than MOBILE_MAX_CHUNK
    """
    upload_id = request.headers.get("X-Upload-Id", "").strip()
    path, err = _mobile_chunk_path(upload_id)
    if err is not None:
        return err

    cr = request.headers.get("Content-Range", "")
    parsed = _parse_content_range(cr)
    if parsed is None:
        return jsonify({"error": "bad or missing Content-Range header (need 'bytes S-E/T')"}), 400
    start, end, total = parsed
    chunk_len = end - start + 1

    if total > MOBILE_MAX_TOTAL:
        return jsonify({"error": f"total size {total} exceeds limit {MOBILE_MAX_TOTAL}"}), 413
    if chunk_len > MOBILE_MAX_CHUNK:
        return jsonify({
            "error": f"chunk size {chunk_len} exceeds limit {MOBILE_MAX_CHUNK}",
            "max_chunk": MOBILE_MAX_CHUNK,
        }), 413

    filename = _sanitize_filename(request.headers.get("X-Filename", ""))

    existing = path.stat().st_size if path.exists() else 0
    if start != existing:
        # Tell the phone where to resume from. 409 (Conflict) matches what
        # tus.io uses for this case; easy for the phone client to special-case.
        return jsonify({
            "error": "offset_mismatch",
            "upload_id": upload_id,
            "expected": existing,
            "got": start,
        }), 409

    # Stream the body to disk without buffering it in memory — these can be
    # 8 MiB+ and we don't want to hold every concurrent upload in RAM.
    try:
        with open(path, "ab") as f:
            remaining = chunk_len
            stream = request.stream
            while remaining > 0:
                buf = stream.read(min(remaining, 1024 * 1024))
                if not buf:
                    break
                f.write(buf)
                remaining -= len(buf)
    except Exception as e:
        app.logger.exception("mobile chunk write failed")
        return jsonify({"error": f"write failed: {type(e).__name__}: {e}"}), 500

    received = path.stat().st_size
    # If the client sent fewer bytes than the Content-Range claimed, roll the
    # partial back to `start` so the phone can resend that chunk cleanly.
    if received != existing + chunk_len:
        # Truncate back to where we were — don't leave the partial in a
        # half-chunk state.
        with open(path, "r+b") as f:
            f.truncate(existing)
        return jsonify({
            "error": "short_body",
            "upload_id": upload_id,
            "expected_bytes": chunk_len,
            "got_bytes": received - existing,
        }), 400

    app.logger.info(
        f"[mobile] upload_id={upload_id} range={start}-{end}/{total} "
        f"-> received={received} filename={filename}"
    )

    # Still mid-upload? Just ack the bytes so the phone can send the next chunk.
    if received < total:
        return jsonify({
            "upload_id": upload_id,
            "received": received,
            "total": total,
            "complete": False,
        })

    # Final chunk — ingest. If anything below fails, blow the partial away
    # so the phone can retry with a fresh upload_id without tripping over
    # the stale file.
    try:
        status, body = _ingest_sqlite_as_project(path, filename)
    except Exception as e:
        app.logger.exception("mobile ingest failed")
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"error": f"ingest failed: {type(e).__name__}: {e}"}), 500

    # `_ingest_sqlite_as_project` already moved the partial into the project
    # dir on success; on failure it deleted the project. Either way make sure
    # the partial is gone so a resumed POST starts fresh.
    if path.exists():
        try:
            path.unlink()
        except Exception:
            pass

    if status != 200:
        return jsonify(body), status

    # Build a fully-qualified project URL the phone can deep-link to. Respects
    # X-Forwarded-Proto/Host so Cloudflare tunnel hostnames round-trip.
    project_id = body["project_id"]
    scheme = request.headers.get("X-Forwarded-Proto") or request.scheme
    host = request.headers.get("X-Forwarded-Host") or request.host
    project_url = f"{scheme}://{host}/project/{project_id}"

    return jsonify({
        "upload_id": upload_id,
        "received": received,
        "total": total,
        "complete": True,
        "project_id": project_id,
        "project_url": project_url,
        "format": body.get("format"),
        "n_shots": body.get("n_shots"),
    })


@app.route("/api/mobile/upload/<upload_id>/status", methods=["GET"])
def api_mobile_upload_status(upload_id):
    """Return how many bytes the server has for this upload-id.

    Phone uses this after a reconnect to decide whether to resume or start
    over. `exists: false` means we have no partial — the client should POST
    the first chunk starting at offset 0.
    """
    path, err = _mobile_chunk_path(upload_id)
    if err is not None:
        return err
    if not path.exists():
        return jsonify({"upload_id": upload_id, "exists": False, "received": 0})
    return jsonify({
        "upload_id": upload_id,
        "exists": True,
        "received": path.stat().st_size,
    })


@app.route("/api/upload/chunk", methods=["POST"])
def api_upload_chunk():
    """Append one chunk to a partial upload.

    Form fields:
      upload_id: opaque client-generated id (uuid)
      offset:    byte offset this chunk starts at (must equal current size)
      chunk:     binary blob
    """
    upload_id = request.form.get("upload_id") or ""
    try:
        offset = int(request.form.get("offset") or "0")
    except ValueError:
        return jsonify({"error": "bad offset"}), 400
    chunk = request.files.get("chunk")
    if not chunk:
        return jsonify({"error": "no chunk"}), 400

    try:
        path = _chunk_path(upload_id)
    except Exception as e:
        return jsonify({"error": f"bad upload_id: {e}"}), 400

    existing = path.stat().st_size if path.exists() else 0
    if offset != existing:
        return jsonify({
            "error": "offset mismatch",
            "expected": existing, "got": offset,
        }), 409
    # Append
    try:
        with open(path, "ab") as f:
            chunk.save(f)
    except Exception as e:
        app.logger.exception("chunk write failed")
        return jsonify({"error": f"write failed: {type(e).__name__}: {e}"}), 500
    size = path.stat().st_size
    app.logger.info(f"chunk upload_id={upload_id} offset={offset} -> size={size}")
    return jsonify({"upload_id": upload_id, "size": size})


@app.route("/api/upload/finalize", methods=["POST"])
def api_upload_finalize():
    """Consume one or more completed partial uploads into a new project.

    JSON body:
      name: optional project name
      kind: "images" | "sqlite"
      files: [{upload_id, filename}]  // order matters for display
    """
    import shutil as _sh
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    kind = (data.get("kind") or "").strip()
    files = data.get("files") or []
    if kind not in ("images", "sqlite"):
        return jsonify({"error": "bad kind"}), 400
    if not files:
        return jsonify({"error": "no files"}), 400
    if kind == "sqlite" and len(files) != 1:
        return jsonify({"error": "sqlite upload takes exactly one file"}), 400

    # Validate all partials exist before we create the project
    for f in files:
        p = _chunk_path(f.get("upload_id") or "")
        if not p.exists():
            return jsonify({"error": f"missing chunk {f.get('upload_id')}"}), 400

    default_name = (Path(files[0]["filename"]).stem if kind == "sqlite"
                    else f"{len(files)} photos")
    pid = db.create_project(name or default_name, kind, meta={})

    if kind == "sqlite":
        src = _chunk_path(files[0]["upload_id"])
        orig_name = files[0].get("filename") or "upload.db"
        dst = paths.new_project_sqlite_path(pid, orig_name)
        _sh.move(str(src), str(dst))
        probe = gcdb_read.probe_gcdb(dst)
        fmt = probe.get("format")
        if not probe.get("ok") or fmt == "unknown":
            probe["inspection"] = gcdb_read.inspect(dst)

        if fmt == "snapapp" and probe.get("ok"):
            # Extract the 1× wide JPEG from every capture row — they match the
            # reference panorama sub-views' ~65° HFoV far better than the
            # zoomed-in burst frames would.
            photos_dir = paths.project_photos_dir(pid)
            extracted = gcdb_read.extract_snapapp_wide(dst, photos_dir)
            if not extracted:
                _sh.rmtree(paths.project_dir(pid), ignore_errors=True)
                db.delete_project(pid)
                return jsonify({"error": "SnapApp .db had no wide_jpeg rows"}), 400
            # Preserve the per-photo GPS/bearing in project meta so the UI
            # can render it even after extraction drops the BLOBs.
            shot_meta = {e["filename"]: {
                "shot_id": e["shot_id"],
                "lat": e["lat"], "lon": e["lon"],
                "bearing_deg": e["bearing_deg"],
                "accuracy_m": e["accuracy_m"],
                "captured_at": e["captured_at"],
            } for e in extracted}
            db.update_project_meta = getattr(db, "update_project_meta", None)
            # (no helper — just overwrite via internal SQL below)
            import json as _json
            with db.conn() as c:
                c.execute(
                    "UPDATE projects SET meta_json = ? WHERE id = ?",
                    (_json.dumps({
                        "source": "snapapp",
                        "original_filename": orig_name,
                        "n_sqlite_shots": len(extracted),
                        "bounds": probe.get("bounds"),
                        "photo_meta": shot_meta,
                    }), pid),
                )
            # Default snapapp-shot: one SFM run over every extracted photo
            photo_stems = [Path(e["filename"]).stem for e in extracted]
            db.create_shot(
                pid, name=f"All {len(extracted)} shots (default)",
                meta={"photo_stems": photo_stems,
                      "photo_names": [e["filename"] for e in extracted]},
            )
            pipeline.start_megaloc_prematch(pid)
            return jsonify({
                "project_id": pid, "kind": kind,
                "format": "snapapp", "n_shots": len(extracted),
            })

        # Legacy gcdb or unknown: keep the file around but no pipeline path.
        return jsonify({"project_id": pid, "kind": kind, "sqlite": probe})

    photos_dir = paths.project_photos_dir(pid)
    saved = []
    for f in files:
        filename = Path(f.get("filename") or "").name
        ext = Path(filename).suffix
        if ext not in PHOTO_EXTS:
            continue
        dst = photos_dir / filename
        i = 1
        while dst.exists():
            dst = photos_dir / f"{dst.stem}_{i}{dst.suffix}"
            i += 1
        src = _chunk_path(f["upload_id"])
        _sh.move(str(src), str(dst))
        saved.append(dst.name)

    if not saved:
        _sh.rmtree(paths.project_dir(pid), ignore_errors=True)
        db.delete_project(pid)
        return jsonify({"error": "No valid photo files (need HEIC/JPG/PNG)"}), 400

    photo_stems = [Path(n).stem for n in saved]
    db.create_shot(
        pid, name="All photos (default)",
        meta={"photo_stems": photo_stems, "photo_names": saved},
    )
    pipeline.start_megaloc_prematch(pid)
    return jsonify({"project_id": pid, "kind": kind, "n_photos": len(saved)})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Create a new project. Accepts photos[] (image sequence) OR sqlite file.

    Legacy single-shot path. For anything above ~80 MB the Cloudflare edge
    will reject it before it reaches us (100 MB body cap on free tier).
    New clients use /api/upload/chunk → /api/upload/finalize; keep this
    for small photo bundles and as a fallback.
    """
    # Reject oversize single-shots early so stale browsers get a clear error
    # instead of a Cloudflare 413 at the edge.
    cl = request.content_length or 0
    if cl > 90 * 1024 * 1024:
        return jsonify({
            "error": (
                "Upload too large for the single-shot path "
                f"({cl // 1024 // 1024} MB). "
                "Your browser is using an outdated uploader — hard-refresh "
                "the page (Ctrl/Cmd+Shift+R) to load the chunked uploader."
            ),
        }), 413
    name = (request.form.get("name") or "").strip()
    photo_files = request.files.getlist("photos")
    sqlite_file = request.files.get("sqlite")

    if not photo_files and not sqlite_file:
        return jsonify({"error": "No files uploaded"}), 400

    if sqlite_file and photo_files:
        return jsonify({"error": "Upload either photos or a sqlite file, not both"}), 400

    if sqlite_file:
        kind = "sqlite"
        default_name = Path(sqlite_file.filename).stem if sqlite_file.filename else "sqlite project"
    else:
        kind = "images"
        default_name = f"{len(photo_files)} photos"

    pid = db.create_project(name or default_name, kind, meta={})

    if sqlite_file:
        orig_name = sqlite_file.filename or "upload.db"
        dst = paths.new_project_sqlite_path(pid, orig_name)
        sqlite_file.save(str(dst))
        probe = gcdb_read.probe_gcdb(dst)
        if not probe.get("ok") or probe.get("format") == "unknown":
            probe["inspection"] = gcdb_read.inspect(dst)
        if probe.get("format") == "snapapp" and probe.get("ok"):
            photos_dir = paths.project_photos_dir(pid)
            extracted = gcdb_read.extract_snapapp_wide(dst, photos_dir)
            if not extracted:
                shutil.rmtree(paths.project_dir(pid), ignore_errors=True)
                db.delete_project(pid)
                return jsonify({"error": "SnapApp .db had no wide_jpeg rows"}), 400
            shot_meta = {e["filename"]: {
                "shot_id": e["shot_id"], "lat": e["lat"], "lon": e["lon"],
                "bearing_deg": e["bearing_deg"], "accuracy_m": e["accuracy_m"],
                "captured_at": e["captured_at"],
            } for e in extracted}
            import json as _json
            with db.conn() as c:
                c.execute(
                    "UPDATE projects SET meta_json = ? WHERE id = ?",
                    (_json.dumps({
                        "source": "snapapp", "original_filename": orig_name,
                        "n_sqlite_shots": len(extracted),
                        "bounds": probe.get("bounds"), "photo_meta": shot_meta,
                    }), pid),
                )
            photo_stems = [Path(e["filename"]).stem for e in extracted]
            db.create_shot(
                pid, name=f"All {len(extracted)} shots (default)",
                meta={"photo_stems": photo_stems,
                      "photo_names": [e["filename"] for e in extracted]},
            )
            pipeline.start_megaloc_prematch(pid)
            return jsonify({"project_id": pid, "kind": kind,
                            "format": "snapapp", "n_shots": len(extracted)})
        return jsonify({"project_id": pid, "kind": kind, "sqlite": probe})

    photos_dir = paths.project_photos_dir(pid)
    saved = []
    for f in photo_files:
        if not f.filename:
            continue
        name_safe = Path(f.filename).name
        ext = Path(name_safe).suffix
        if ext not in PHOTO_EXTS:
            continue
        dst = photos_dir / name_safe
        # Avoid collisions
        i = 1
        while dst.exists():
            dst = photos_dir / f"{dst.stem}_{i}{dst.suffix}"
            i += 1
        f.save(str(dst))
        saved.append(dst.name)

    if not saved:
        shutil.rmtree(paths.project_dir(pid), ignore_errors=True)
        db.delete_project(pid)
        return jsonify({"error": "No valid photo files (need HEIC/JPG/PNG)"}), 400

    # Auto-create the default shot: all photos in a single SFM run
    photo_stems = [Path(n).stem for n in saved]
    db.create_shot(
        pid, name="All photos (default)",
        meta={"photo_stems": photo_stems, "photo_names": saved},
    )
    # Kick off MegaLoc in the background so anchor matches are ready before
    # the user triggers the shot — gives them visibility into match quality.
    pipeline.start_megaloc_prematch(pid)
    return jsonify({"project_id": pid, "kind": kind, "n_photos": len(saved)})


# ---------------------------------------------------------------------------
# Shots API
# ---------------------------------------------------------------------------

@app.route("/api/projects/<project_id>/shots", methods=["POST"])
def api_create_shot(project_id):
    p = db.get_project(project_id)
    if p is None:
        abort(404)
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "Unnamed shot").strip()
    photo_stems = data.get("photo_stems") or []
    anchor_override = (data.get("anchor_override") or "").strip() or None
    if not photo_stems:
        return jsonify({"error": "Need at least one photo_stem"}), 400

    photos_dir = paths.project_photos_dir(project_id)
    existing = {p.stem for p in photos_dir.iterdir()} if photos_dir.exists() else set()
    photo_stems = [s for s in photo_stems if s in existing]
    if not photo_stems:
        return jsonify({"error": "None of the selected photos exist"}), 400
    if anchor_override and anchor_override not in photo_stems:
        return jsonify({"error": "anchor_override must be one of the selected photos"}), 400

    meta = {"photo_stems": photo_stems}
    if anchor_override:
        meta["anchor_override"] = anchor_override
    sid = db.create_shot(project_id, name, meta=meta)
    return jsonify({"shot_id": sid})


@app.route("/api/shots/<shot_id>/run", methods=["POST"])
def api_run_shot(shot_id):
    shot = db.get_shot(shot_id)
    if shot is None:
        abort(404)
    if shot["status"] == "running":
        return jsonify({"ok": True, "already_running": True})
    stems = (shot.get("meta") or {}).get("photo_stems") or []
    if len(stems) < 2:
        return jsonify({
            "error": (
                f"Shot has {len(stems)} photo(s); sequence SFM needs at least "
                "two overlapping views. Pick more photos and create a new shot."
            ),
        }), 400
    pipeline.run_shot(shot_id)
    return jsonify({"ok": True})


@app.route("/api/shots/<shot_id>", methods=["DELETE"])
def api_delete_shot(shot_id):
    shot = db.get_shot(shot_id)
    if shot is None:
        abort(404)
    if shot["status"] == "running":
        return jsonify({"error": "Cannot delete a running shot"}), 400
    shot_path = paths.shot_dir(shot["project_id"], shot_id)
    shutil.rmtree(shot_path, ignore_errors=True)
    db.delete_shot(shot_id)
    return jsonify({"ok": True})


@app.route("/api/shots/<shot_id>")
def api_shot(shot_id):
    shot = db.get_shot(shot_id)
    if shot is None:
        abort(404)
    return jsonify(shot)


@app.route("/api/shots/<shot_id>/log")
def api_shot_log(shot_id):
    shot = db.get_shot(shot_id)
    if shot is None:
        abort(404)
    log_path = paths.shot_log_path(shot["project_id"], shot_id)
    if not log_path.exists():
        return Response("", mimetype="text/plain")
    try:
        n = int(request.args.get("tail", "300"))
    except ValueError:
        n = 300
    with open(log_path) as f:
        lines = f.readlines()
    return Response("".join(lines[-n:]), mimetype="text/plain")


# ---------------------------------------------------------------------------
# 3D viewer API — returns the reconstructed scene for a shot
# ---------------------------------------------------------------------------

@app.route("/api/shots/<shot_id>/scene")
def api_shot_scene(shot_id):
    shot = db.get_shot(shot_id)
    if shot is None:
        abort(404)
    output_dir = paths.shot_output_dir(shot["project_id"], shot_id)
    model_dir = output_dir / "model"
    if not model_dir.exists():
        abort(404, "No model yet")

    from colmap_io import read_model, qvec_to_rotmat, image_center

    cams, imgs, pts = read_model(model_dir)

    summary = {}
    summary_path = output_dir / "sequence.json"
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)

    anchor_stems = set(summary.get("anchors", {}).keys())

    cameras_out = []
    for img in sorted(imgs.values(), key=lambda i: i.id):
        cam = cams[img.camera_id]
        center = image_center(img).tolist()
        R = qvec_to_rotmat(img.qvec).tolist()
        is_query = img.name.startswith("query/")
        stem = Path(img.name).stem if is_query else None
        is_anchor = is_query and stem in anchor_stems

        if cam.model == "SIMPLE_PINHOLE":
            fx = fy = cam.params[0]; cx, cy = cam.params[1], cam.params[2]
        elif cam.model == "PINHOLE":
            fx, fy = cam.params[0], cam.params[1]; cx, cy = cam.params[2], cam.params[3]
        else:
            fx = fy = cam.params[0]; cx, cy = cam.width / 2, cam.height / 2

        if is_query:
            image_url = f"/api/shots/{shot_id}/image/{stem}"
        else:
            image_url = f"/api/ref_image?name={img.name}"

        cameras_out.append({
            "name": img.name, "stem": stem,
            "center": center, "rotation": R,
            "qvec": list(img.qvec), "tvec": list(img.tvec),
            "width": cam.width, "height": cam.height,
            "fx": fx, "fy": fy, "cx": cx, "cy": cy,
            "is_query": is_query, "is_anchor": is_anchor,
            "image_url": image_url,
            "num_observations": sum(1 for p in img.point3D_ids if p >= 0),
        })

    pt_list = list(pts.values())
    if len(pt_list) > 20000:
        import random
        pt_list = random.sample(pt_list, 20000)

    points_out = [
        {"xyz": list(pt.xyz), "rgb": list(pt.rgb), "error": pt.error}
        for pt in pt_list
    ]

    return jsonify({
        "shot_id": shot_id,
        "cameras": cameras_out,
        "points": points_out,
        "summary": summary,
    })


@app.route("/api/shots/<shot_id>/image/<stem>")
def api_shot_image(shot_id, stem):
    shot = db.get_shot(shot_id)
    if shot is None:
        abort(404)
    output_dir = paths.shot_output_dir(shot["project_id"], shot_id)
    p = output_dir / "images" / f"{stem}.jpg"
    if not p.exists():
        abort(404)
    return send_file(p)


@app.route("/api/ref_image")
def api_ref_image():
    name = request.args.get("name", "")
    if ".." in name:
        abort(400)
    full = paths.REF_IMAGES_DIR / name
    if not full.exists():
        abort(404)
    return send_file(full)


# ---------------------------------------------------------------------------
# Photo thumbnails for the project page
# ---------------------------------------------------------------------------

@app.route("/api/projects/<project_id>/photo/<path:name>")
def api_project_photo(project_id, name):
    if ".." in name:
        abort(400)
    p = paths.project_photos_dir(project_id) / name
    if not p.exists():
        abort(404)
    return send_file(p)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    db.init_db()
    db.mark_stale_running()
    print(f"snapapp running on http://{args.host}:{args.port}")
    print(f"  ref model: {paths.REF_MODEL_DIR}")
    print(f"  ref images: {paths.REF_IMAGES_DIR}")
    print(f"  megaloc db: {paths.MEGALOC_DB}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()

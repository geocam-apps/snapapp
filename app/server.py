"""Flask web server for snapapp."""
import json
import shutil
import sys
import threading
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

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

PHOTO_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".HEIC", ".HEIF", ".JPG", ".JPEG", ".PNG"}


@app.route("/")
def index():
    return send_file(TEMPLATES_DIR / "index.html")


@app.route("/project/<project_id>")
def project_page(project_id):
    if db.get_project(project_id) is None:
        abort(404)
    return send_file(TEMPLATES_DIR / "project.html")


@app.route("/shot/<project_id>/<shot_id>")
def shot_viewer(project_id, shot_id):
    if db.get_shot(shot_id) is None:
        abort(404)
    return send_file(TEMPLATES_DIR / "viewer.html")


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
    if sqlite_path.exists():
        p["sqlite"] = gcdb_read.probe_gcdb(sqlite_path)
        p["sqlite"]["poses_sample"] = gcdb_read.list_poses(sqlite_path, limit=50)
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


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Create a new project. Accepts photos[] (image sequence) OR sqlite file."""
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
        dst = paths.project_sqlite_path(pid)
        sqlite_file.save(str(dst))
        probe = gcdb_read.probe_gcdb(dst)
        db.update_shot  # noop
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
    if not photo_stems:
        return jsonify({"error": "Need at least one photo_stem"}), 400

    photos_dir = paths.project_photos_dir(project_id)
    existing = {p.stem for p in photos_dir.iterdir()} if photos_dir.exists() else set()
    photo_stems = [s for s in photo_stems if s in existing]
    if not photo_stems:
        return jsonify({"error": "None of the selected photos exist"}), 400

    sid = db.create_shot(project_id, name,
                         meta={"photo_stems": photo_stems})
    return jsonify({"shot_id": sid})


@app.route("/api/shots/<shot_id>/run", methods=["POST"])
def api_run_shot(shot_id):
    shot = db.get_shot(shot_id)
    if shot is None:
        abort(404)
    if shot["status"] == "running":
        return jsonify({"ok": True, "already_running": True})
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

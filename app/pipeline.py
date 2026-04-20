"""Pipeline: run MegaLoc + shotmatch_pose for a shot."""
import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import db, paths


# Keep the subprocess environment off the GPU — MegaLoc + COLMAP here are
# CPU-only; small CUDA allocations OOM on this host.
CHILD_ENV = {**os.environ, "CUDA_VISIBLE_DEVICES": ""}


# Track in-flight shots: shot_id -> thread
RUNNING = {}


def log_append(path: Path, msg: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(msg + "\n")


def run_megaloc_for_project(project_id: str) -> dict:
    """Run MegaLoc on all photos in a project, cache result CSV.

    Returns {"photo_stem": {"shot_key": str, "score": float, "capture": str}, ...}
    """
    project = db.get_project(project_id)
    if project is None:
        raise RuntimeError("Project not found")

    photos_dir = paths.project_photos_dir(project_id)
    csv_path = paths.project_megaloc_csv(project_id)
    log_path = paths.project_dir(project_id) / "megaloc.log"

    if csv_path.exists():
        return _read_megaloc_csv(csv_path)

    cmd = [
        "python3", str(paths.MEGALOC_REPO / "match_photos.py"),
        "--photos", str(photos_dir),
        "--output", str(csv_path),
        "--top-k", "3",
        "--db", str(paths.MEGALOC_DB),
        "--gcdb-dir", str(paths.MEGALOC_GCDB_DIR),
    ]
    log_append(log_path, f"[megaloc] cmd: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                          env=CHILD_ENV)
    log_append(log_path, f"[megaloc] returncode={proc.returncode}")
    log_append(log_path, f"[megaloc] stdout:\n{proc.stdout[-2000:]}")
    if proc.returncode != 0:
        log_append(log_path, f"[megaloc] stderr:\n{proc.stderr[-2000:]}")
        raise RuntimeError(f"MegaLoc failed: {proc.stderr[-500:]}")

    return _read_megaloc_csv(csv_path)


def _read_megaloc_csv(csv_path: Path) -> dict:
    out = {}
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        photo_stem = Path(r["photo"]).stem
        shot_key = r.get("match_1_shot_key", "")
        capture = None
        shot_id = shot_key
        if "/" in shot_key:
            capture, shot_id = shot_key.rsplit("/", 1)
        out[photo_stem] = {
            "photo": r["photo"],
            "shot_key": shot_key,
            "shot_id": shot_id,
            "capture": capture,
            "score": float(r.get("match_1_score") or 0.0),
        }
    return out


def _choose_anchor(matches: dict, photo_stems: list) -> tuple:
    """Given megaloc matches, pick the best-scoring photo stem as anchor."""
    best = None
    for stem in photo_stems:
        m = matches.get(stem)
        if m is None:
            continue
        if best is None or m["score"] > best[1]["score"]:
            best = (stem, m)
    if best is None:
        raise RuntimeError("No MegaLoc matches for any photo")
    return best


def run_shot(shot_id: str):
    """Runs the pipeline for one shot in a background thread. Blocks caller
    until kicked off; actual work happens in a thread."""
    shot = db.get_shot(shot_id)
    if shot is None:
        raise RuntimeError("Shot not found")
    if shot_id in RUNNING:
        return  # already running
    t = threading.Thread(target=_process_shot, args=(shot_id,), daemon=True)
    RUNNING[shot_id] = t
    t.start()


def _process_shot(shot_id: str):
    try:
        shot = db.get_shot(shot_id)
        project = db.get_project(shot["project_id"])
        log_path = paths.shot_log_path(project["id"], shot_id)

        db.update_shot(shot_id, status="running", phase="init",
                       phase_label="Starting", progress=0.02, error=None)

        meta = shot.get("meta") or {}
        photo_stems = meta.get("photo_stems") or []
        if not photo_stems:
            raise RuntimeError("Shot has no photos assigned")

        photos_dir = paths.project_photos_dir(project["id"])
        # Resolve photo stems to file paths
        photo_files = []
        for stem in photo_stems:
            found = None
            for p in photos_dir.iterdir():
                if p.stem == stem:
                    found = p
                    break
            if found is None:
                raise RuntimeError(f"Photo for stem '{stem}' not found")
            photo_files.append(found)

        # Step 1: megaloc
        db.update_shot(shot_id, phase="megaloc", phase_label="Running MegaLoc", progress=0.05)
        log_append(log_path, "[megaloc] running...")
        matches = run_megaloc_for_project(project["id"])
        log_append(log_path, f"[megaloc] got {len(matches)} matches total")

        anchor_stem, anchor_info = _choose_anchor(matches, photo_stems)
        anchor_shot_id = anchor_info["shot_id"]
        anchor_capture = anchor_info["capture"]
        anchor_score = anchor_info["score"]
        log_append(log_path, f"[megaloc] anchor: {anchor_stem} -> {anchor_info['shot_key']} "
                              f"(score={anchor_score:.3f})")
        db.update_shot(
            shot_id, anchor_shot=anchor_shot_id, anchor_score=anchor_score,
            progress=0.10,
            meta={**meta, "anchor_stem": anchor_stem,
                  "anchor_shot_key": anchor_info["shot_key"],
                  "anchor_capture": anchor_capture,
                  "n_photos": len(photo_files)},
        )

        # Step 2: Copy photos into a staging dir for the subprocess
        shot_out_dir = paths.shot_output_dir(project["id"], shot_id)
        staging_dir = paths.shot_dir(project["id"], shot_id) / "photos"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)
        for p in photo_files:
            shutil.copy(p, staging_dir / p.name)

        # Step 3: Run register_sequence_natural.py
        db.update_shot(shot_id, phase="sfm", phase_label="Preparing SFM",
                       progress=0.15, n_queries=len(photo_files))
        cmd = [
            "python3", "-u", str(paths.SHOTMATCH_REPO / "register_sequence_natural.py"),
            "--photos-dir", str(staging_dir),
            "--anchor", f"{anchor_stem}={anchor_shot_id}",
            "--capture", anchor_capture or "",
            "--model", str(paths.REF_MODEL_DIR),
            "--images", str(paths.REF_IMAGES_DIR),
            "--output", str(shot_out_dir),
            "--camera-model", "OPENCV",
        ]
        log_append(log_path, f"[sfm] cmd: {' '.join(cmd)}")

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=CHILD_ENV,
        )

        phase_map = [
            ("Phase 1:", "phase1", "Registering anchor against refs", 0.20),
            ("Phase 2:", "sift", "SIFT extract + match", 0.30),
            ("SIFT extract", "sift", "SIFT feature extraction", 0.35),
            ("Exhaustive matching", "match", "Matching features", 0.50),
            ("Phase 3:", "mapper", "Incremental SFM", 0.65),
            ("Phase 4:", "retry", "Retrying missing photos", 0.80),
            ("Phase 5:", "align", "Aligning to reference frame", 0.85),
            ("Phase 6:", "ba", "Global bundle adjustment", 0.90),
            ("Phase 7:", "extract", "Extracting results", 0.95),
        ]
        for line in proc.stdout:
            line = line.rstrip()
            log_append(log_path, f"[sfm] {line}")
            for marker, phase, label, progress in phase_map:
                if marker in line:
                    db.update_shot(shot_id, phase=phase, phase_label=label, progress=progress)
                    break
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"SFM failed (returncode={proc.returncode})")

        summary_path = shot_out_dir / "sequence.json"
        if not summary_path.exists():
            raise RuntimeError("No sequence.json generated")
        with open(summary_path) as f:
            summary = json.load(f)

        n_reg = summary.get("n_registered", 0)
        n_q = summary.get("n_queries", len(photo_files))
        failed = summary.get("failed", [])
        log_append(log_path, f"[sfm] Done — registered {n_reg}/{n_q}; failed={failed}")

        status = "done" if n_reg > 0 else "failed"
        err = None if status == "done" else "No photos registered"
        db.update_shot(
            shot_id, status=status, phase="done", phase_label="Done",
            progress=1.0, n_registered=n_reg, n_queries=n_q,
            error=err,
            meta={**meta,
                  "anchor_stem": anchor_stem,
                  "anchor_shot_key": anchor_info["shot_key"],
                  "anchor_capture": anchor_capture,
                  "n_photos": len(photo_files),
                  "failed": failed},
        )
    except Exception as e:
        tb = traceback.format_exc()
        try:
            shot = db.get_shot(shot_id)
            project = db.get_project(shot["project_id"]) if shot else None
            if project:
                log_path = paths.shot_log_path(project["id"], shot_id)
                log_append(log_path, f"[error] {type(e).__name__}: {e}")
                log_append(log_path, tb)
        except Exception:
            pass
        db.update_shot(shot_id, status="failed", phase="error",
                       phase_label=f"Failed: {e}", error=str(e)[:500])
    finally:
        RUNNING.pop(shot_id, None)

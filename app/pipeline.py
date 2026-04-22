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

from . import db, paths, ref_cache, ref_index


# Both MegaLoc and shotmatch_pose can use the GPU. The GB10's unified-memory
# CUDA allocator hit a transient OOM on the very first cold MegaLoc load
# during scaffolding, but that doesn't recur in steady state — a subprocess
# env is sufficient and CPU is an adequate fallback at ~2 img/s.
CHILD_ENV = os.environ.copy()


# Track in-flight shots: shot_id -> thread
RUNNING = {}

# Track in-flight megaloc prematching: project_id -> thread
MEGALOC_RUNNING = {}


def start_megaloc_prematch(project_id: str):
    """Kick off MegaLoc in the background at upload time so matches are
    visible in the UI before the user triggers a shot."""
    if project_id in MEGALOC_RUNNING:
        return
    csv_path = paths.project_megaloc_csv(project_id)
    if csv_path.exists():
        return  # already cached

    def _run():
        try:
            run_megaloc_for_project(project_id)
        except Exception:
            # Non-fatal; the shot run will retry and surface the error.
            pass
        finally:
            MEGALOC_RUNNING.pop(project_id, None)

    t = threading.Thread(target=_run, daemon=True)
    MEGALOC_RUNNING[project_id] = t
    t.start()


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

    # Run MegaLoc against the flat one-per-shot dir of wide JPEGs, not the
    # per-shot bundle subdirs. Match keys end up as `shot_<id:05d>`.
    photos_dir = paths.project_megaloc_in_dir(project_id)
    if not photos_dir.exists() or not any(photos_dir.iterdir()):
        # Legacy projects (or image-uploaded projects) used the flat
        # photos/ directly.
        photos_dir = paths.project_photos_dir(project_id)
    csv_path = paths.project_megaloc_csv(project_id)
    log_path = paths.project_dir(project_id) / "megaloc.log"

    if csv_path.exists():
        return _read_megaloc_csv(csv_path)

    cmd = [
        "python3", str(paths.MEGALOC_REPO / "match_photos.py"),
        "--photos", str(photos_dir),
        "--output", str(csv_path),
        # top-20 instead of the default 3 — phone photos often don't land
        # in the top-3 on this dataset (different camera, lighting, etc.),
        # but the correct ref is usually in the top-20 and GPS-filtering
        # will pull it out.
        "--top-k", "20",
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
    """Parse MegaLoc's top-K CSV into `{photo_stem: {score, shot_key, shot_id,
    capture, top_k:[…]}}`. Top-1 is mirrored at the root for backward
    compatibility with earlier API consumers.
    """
    out = {}
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        photo_stem = Path(r["photo"]).stem
        top_k = []
        for i in range(1, 100):  # supports up to top-99
            sk = r.get(f"match_{i}_shot_key")
            if not sk:
                break
            score = float(r.get(f"match_{i}_score") or 0.0)
            capture, shot_id = (sk.rsplit("/", 1)
                                if "/" in sk else (None, sk))
            top_k.append({
                "shot_key": sk, "shot_id": shot_id,
                "capture": capture, "score": score,
            })
        if not top_k:
            continue
        best = top_k[0]
        out[photo_stem] = {
            "photo": r["photo"],
            "shot_key": best["shot_key"],
            "shot_id": best["shot_id"],
            "capture": best["capture"],
            "score": best["score"],
            "top_k": top_k,
        }
    return out


def _annotate_matches_with_gps(matches: dict, photo_meta: dict) -> dict:
    """For each photo's top-K MegaLoc matches, compute the distance from the
    phone's reported GPS to the reference shot's known lat/lon, and mark
    which matches fall inside the phone's accuracy radius.

    Mutates `matches` in place; returns it for convenience. Does nothing if
    the ref_index isn't available.
    """
    idx = ref_index.load()
    if not idx:
        return matches
    for stem, m in matches.items():
        # New layout: MegaLoc keys are `shot_<id:05d>`; photo_meta keys are
        # the full per-bundle path like `shot_<id:05d>/wide.jpg`. Try the
        # full path first, fall back to the legacy flat key.
        meta = ((photo_meta or {}).get(f"{stem}/wide.jpg")
                or (photo_meta or {}).get(stem + ".jpg")
                or {})
        phone_lat = meta.get("lat")
        phone_lon = meta.get("lon")
        accuracy = meta.get("accuracy_m") or 0
        # Accept within max(15m, 2× accuracy) to cover GPS noise
        radius_m = max(15.0, 2.0 * (accuracy or 0))
        m["radius_m"] = radius_m
        # Cache phone position so _nearest_ref_by_gps doesn't have to
        # re-walk photo_meta.
        m["_phone_lat"] = phone_lat
        m["_phone_lon"] = phone_lon
        for cand in m["top_k"]:
            pos = idx.get(cand["shot_key"])
            if pos is None:
                cand["ref_lat"] = None
                cand["ref_lon"] = None
                cand["distance_m"] = None
                cand["gps_valid"] = None
                continue
            cand["ref_lat"] = pos["lat"]
            cand["ref_lon"] = pos["lon"]
            if phone_lat is not None and phone_lon is not None:
                d = ref_index.haversine_m(phone_lat, phone_lon,
                                          pos["lat"], pos["lon"])
                cand["distance_m"] = d
                cand["gps_valid"] = d <= radius_m
            else:
                cand["distance_m"] = None
                cand["gps_valid"] = None
        # Best GPS-valid match (if any)
        gps_ok = [c for c in m["top_k"] if c.get("gps_valid")]
        if gps_ok:
            best = max(gps_ok, key=lambda c: c["score"])
            m["gps_best"] = best
    return matches


def _choose_anchor(matches: dict, photo_stems: list,
                   pinned_stem: str = None) -> tuple:
    """Pick (anchor_stem, chosen_match_dict) for the shotmatch_pose subprocess.

    Strategy, in order:
      1. If `pinned_stem` is given, honor it — use that photo's best
         GPS-valid match, falling back to its MegaLoc top-1 if none.
      2. Otherwise, walk every photo's top-K MegaLoc matches, keep only
         those within the phone's GPS accuracy radius, and pick the one
         with the highest MegaLoc score. The ref shot in that match becomes
         the anchor, and the photo it belongs to becomes the anchor photo.
      3. If no photo has any GPS-valid match, fall back to the best
         MegaLoc top-1 across all photos and flag `gps_valid=False` so
         the UI can warn.

    Returns (anchor_stem, {shot_key, shot_id, capture, score, gps_valid,
                            distance_m, ref_lat, ref_lon, radius_m}).
    """
    def _pick_for_photo(stem):
        m = matches.get(stem)
        if not m:
            return None
        chosen = m.get("gps_best")
        if chosen:
            return {**chosen, "gps_valid": True,
                    "radius_m": m.get("radius_m")}
        top = m["top_k"][0]
        return {**top, "gps_valid": top.get("gps_valid"),
                "radius_m": m.get("radius_m")}

    if pinned_stem and pinned_stem in matches:
        return pinned_stem, _pick_for_photo(pinned_stem)

    best = None
    for stem in photo_stems:
        m = matches.get(stem)
        if not m:
            continue
        gps_best = m.get("gps_best")
        if gps_best is None:
            continue
        if best is None or gps_best["score"] > best[1]["score"]:
            best = (stem, {**gps_best, "gps_valid": True,
                           "radius_m": m.get("radius_m")})
    if best is not None:
        return best

    # Fallback 1: no GPS-valid match in any top-K — try pure GPS.
    # Pick each photo's NEAREST ref shot (from the full ref_index) and
    # keep the single closest one across all photos. MegaLoc isn't
    # picking here; geography is.
    gps_pick = _nearest_ref_by_gps(photo_stems, matches)
    if gps_pick is not None:
        return gps_pick

    # Fallback 2: no phone GPS at all — use the best MegaLoc score across
    # all photos and flag gps_valid=None.
    for stem in photo_stems:
        pick = _pick_for_photo(stem)
        if pick is None:
            continue
        if best is None or pick["score"] > best[1]["score"]:
            best = (stem, pick)
    if best is None:
        raise RuntimeError("No MegaLoc matches for any photo")
    return best


def _nearest_ref_by_gps(photo_stems, matches):
    """Pick (anchor_stem, anchor_info) by finding the closest reference shot
    to any of the selected photos' reported GPS. Used when MegaLoc top-K
    misses but the phone's GPS is trustworthy enough to stand on its own.

    Scans the full ref_index; O(n_refs × n_photos) which is fine at the
    scales we see (12k refs × a few dozen photos).
    """
    idx = ref_index.load()
    if not idx:
        return None
    best = None  # (distance, stem, shot_key, pos, radius)
    for stem in photo_stems:
        m = matches.get(stem) or {}
        radius_m = m.get("radius_m") or 15.0
        # Photo GPS lives on any top-K entry's radius context; but the
        # phone's actual lat/lon came from photo_meta. Pull it from the
        # first top-K entry's annotation if available, otherwise skip.
        # Easier: reach back into matches' annotation by requiring at
        # least one top-K distance to exist — then reconstruct from
        # distance + ref pos? No: instead, bail out if we don't have
        # phone lat/lon cached. We'll get it from _annotate's side-data.
        phone_lat = m.get("_phone_lat")
        phone_lon = m.get("_phone_lon")
        if phone_lat is None or phone_lon is None:
            continue
        for shot_key, pos in idx.items():
            d = ref_index.haversine_m(phone_lat, phone_lon,
                                      pos["lat"], pos["lon"])
            if best is None or d < best[0]:
                best = (d, stem, shot_key, pos, radius_m)
    if best is None:
        return None
    d, stem, shot_key, pos, radius_m = best
    capture, shot_id = (shot_key.rsplit("/", 1)
                        if "/" in shot_key else (None, shot_key))
    return stem, {
        "shot_key": shot_key,
        "shot_id": shot_id,
        "capture": capture,
        "score": 0.0,               # MegaLoc didn't pick this
        "gps_valid": d <= radius_m,
        "distance_m": d,
        "ref_lat": pos["lat"], "ref_lon": pos["lon"],
        "radius_m": radius_m,
        "source": "gps_nearest",
    }


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


_REF_MODEL_CACHE = None


def _load_ref_model_once():
    """Read the reference COLMAP model once (36k images' metadata, ~50 MB
    of text). Reused across subprocess calls within the server lifetime.
    """
    global _REF_MODEL_CACHE
    if _REF_MODEL_CACHE is not None:
        return _REF_MODEL_CACHE
    sys.path.insert(0, str(paths.SHOTMATCH_REPO))
    from colmap_io import read_model
    _REF_MODEL_CACHE = read_model(paths.REF_MODEL_DIR)
    return _REF_MODEL_CACHE


def _fetch_ref_images_for_run(matched_shot_id, matched_capture, n_neighbors=15):
    """Make sure every ref image the SFM subprocess needs is present in the
    local cache. Returns the local directory to pass as `--images`.

    Bounds what we localize: only the matched shot (3 images) plus its
    n_neighbors nearest shots (another ~45 images) — never the full ref
    set, which can be hundreds of GB.
    """
    sys.path.insert(0, str(paths.SHOTMATCH_REPO))
    from register_photo import find_images_for_shot, find_nearby_shots

    _, imgs, _ = _load_ref_model_once()
    matched_ids = find_images_for_shot(imgs, matched_shot_id, matched_capture)
    if not matched_ids:
        # fall back: return the configured root; subprocess will fail
        # with a clearer error than "S3 key not found".
        return ref_cache.root_path()
    keep_ids = find_nearby_shots(imgs, matched_ids, n_neighbors)
    rel_names = [imgs[i].name for i in keep_ids]
    return ref_cache.ensure_local(rel_names)


def _run_register_sequence(shot_id, log_path, staging_dir, anchor_stem,
                            anchor_shot_id, anchor_capture, shot_out_dir,
                            n_shots_neighbours=15):
    """Multi-photo SFM via register_sequence_natural.py."""
    images_root = _fetch_ref_images_for_run(
        anchor_shot_id, anchor_capture, n_shots_neighbours,
    )
    log_append(log_path, f"[sfm] ref images root: {images_root}")
    cmd = [
        "python3", "-u",
        str(paths.SHOTMATCH_REPO / "register_sequence_natural.py"),
        "--photos-dir", str(staging_dir),
        "--anchor", f"{anchor_stem}={anchor_shot_id}",
        "--capture", anchor_capture or "",
        "--model", str(paths.REF_MODEL_DIR),
        "--images", str(images_root),
        "--output", str(shot_out_dir),
        "--camera-model", "OPENCV",
        "--n-shots", str(n_shots_neighbours),
        # Force one camera per zoom role (wide / mid / burst) regardless
        # of EXIF — snapapp's 7-photo bundles always have this structure.
        "--group-mode", "role",
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
                db.update_shot(shot_id, phase=phase, phase_label=label,
                               progress=progress)
                break
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"SFM failed (returncode={proc.returncode})")


def _run_register_independent(shot_id, log_path, staging_dir, matches,
                               photo_stems, shot_out_dir):
    """Register each phone photo independently against the reference model.

    This is the right default for SnapApp-style captures: individual
    shutter presses at different positions along a street rarely share
    enough texture for sequence SFM's SIFT chaining to work. Each photo
    gets its own register_photo.py pass (DISK+LightGlue vs ~10 nearby
    reference shots), then we union the per-photo COLMAP models into one
    and synthesize a sequence.json the viewer can consume.

    Ref anchor for each photo: its best GPS-valid MegaLoc match, falling
    back to the nearest ref by GPS if none of the top-K is valid.
    """
    sys.path.insert(0, str(paths.SHOTMATCH_REPO))
    from colmap_io import read_model, write_model_text, Camera, Image as ColImage

    per_photo_dir = shot_out_dir / "_per_photo"
    per_photo_dir.mkdir(parents=True, exist_ok=True)
    results = []  # per-photo: {stem, ok, anchor_shot, anchor_capture, mini_dir, pose?, err?}

    for i, stem in enumerate(photo_stems):
        # Decide this photo's anchor
        m = matches.get(stem) or {}
        pick = m.get("gps_best") or (m.get("top_k") or [None])[0]
        if pick is None:
            # pure-GPS fallback for this photo
            from . import ref_index
            idx = ref_index.load()
            phone_lat = m.get("_phone_lat")
            phone_lon = m.get("_phone_lon")
            if idx and phone_lat is not None:
                nearest = min(
                    idx.items(),
                    key=lambda kv: ref_index.haversine_m(
                        phone_lat, phone_lon, kv[1]["lat"], kv[1]["lon"])
                )
                shot_key, _pos = nearest
                capture, shot_id = (shot_key.rsplit("/", 1)
                                    if "/" in shot_key else (None, shot_key))
                pick = {"shot_key": shot_key, "shot_id": shot_id,
                        "capture": capture, "score": 0.0}
        if pick is None:
            results.append({"stem": stem, "ok": False,
                            "err": "no anchor candidate"})
            continue

        anchor_shot_id = pick["shot_id"]
        anchor_capture = pick["capture"] or ""
        photo_path = staging_dir / next(
            (f.name for f in staging_dir.iterdir() if f.stem == stem), ""
        )
        if not photo_path.exists():
            results.append({"stem": stem, "ok": False,
                            "err": "photo file not found"})
            continue

        mini_dir = per_photo_dir / stem
        mini_dir.mkdir(parents=True, exist_ok=True)

        frac = 0.20 + 0.70 * ((i + 0.5) / max(1, len(photo_stems)))
        db.update_shot(
            shot_id, phase="phase1",
            phase_label=f"Registering {stem} ({i+1}/{len(photo_stems)})",
            progress=frac,
        )

        images_root = _fetch_ref_images_for_run(
            anchor_shot_id, anchor_capture, n_neighbors=10,
        )
        cmd = [
            "python3", "-u", str(paths.SHOTMATCH_REPO / "register_photo.py"),
            "--photo", str(photo_path),
            "--model", str(paths.REF_MODEL_DIR),
            "--images", str(images_root),
            "--matched-shot", anchor_shot_id,
            "--capture", anchor_capture,
            "--output", str(mini_dir),
            # 'fallback' tries strict → aggressive → max in register_photo
            # until one registers. Trades accuracy for yield — the right
            # call here since MegaLoc+GPS have already pinned the general
            # location and an "almost" pose still lands in the right area.
            "--profile", "fallback",
        ]
        log_append(log_path, f"[sfm] ({i+1}/{len(photo_stems)}) {stem} "
                             f"→ ref {anchor_shot_id} (score={pick['score']:.3f})")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=CHILD_ENV,
        )
        for line in proc.stdout:
            log_append(log_path, f"[sfm]   {line.rstrip()}")
        proc.wait()

        sidecar = mini_dir / f"{stem}_pose.json"
        if proc.returncode == 0 and sidecar.exists():
            with open(sidecar) as f:
                pose = json.load(f)
            results.append({"stem": stem, "ok": True,
                            "anchor_shot": anchor_shot_id,
                            "anchor_capture": pick["capture"],
                            "anchor_score": pick["score"],
                            "mini_dir": mini_dir,
                            "pose": pose})
        else:
            results.append({"stem": stem, "ok": False,
                            "anchor_shot": anchor_shot_id,
                            "err": f"register_photo rc={proc.returncode}"})

    # Merge: take the first successful mini-project's model as the base
    # (it already has refs + triangulated ref points + its query). For
    # subsequent successes, graft in their query Image + Camera.
    successful = [r for r in results if r["ok"]]
    if not successful:
        raise RuntimeError(
            "No photos registered. See log for per-photo failures."
        )

    base = successful[0]
    base_model_dir = base["mini_dir"] / "model"
    cams, imgs, pts = read_model(base_model_dir)
    next_cam_id = (max(cams.keys()) + 1) if cams else 1
    next_img_id = (max(imgs.keys()) + 1) if imgs else 1

    for r in successful[1:]:
        m_cams, m_imgs, _ = read_model(r["mini_dir"] / "model")
        for img in m_imgs.values():
            if not img.name.startswith("query/"):
                continue  # ref images are already present from the base
            if any(bi.name == img.name for bi in imgs.values()):
                continue
            cam = m_cams[img.camera_id]
            cam_id = next_cam_id
            cams[cam_id] = Camera(
                id=cam_id, model=cam.model, width=cam.width,
                height=cam.height, params=cam.params,
            )
            next_cam_id += 1
            imgs[next_img_id] = ColImage(
                id=next_img_id, qvec=img.qvec, tvec=img.tvec,
                camera_id=cam_id, name=img.name, xys=[], point3D_ids=[],
            )
            next_img_id += 1

    model_out = shot_out_dir / "model"
    if model_out.exists():
        shutil.rmtree(model_out)
    model_out.mkdir(parents=True)
    write_model_text(cams, imgs, pts, model_out)

    # Move query JPEGs into the expected location for the viewer.
    images_out = shot_out_dir / "images"
    images_out.mkdir(exist_ok=True)
    for r in successful:
        src = r["mini_dir"] / f"{r['stem']}.jpg"
        if src.exists():
            shutil.move(str(src), str(images_out / f"{r['stem']}.jpg"))

    # Synthesize sequence.json from per-photo poses.
    queries = []
    for r in successful:
        p = r["pose"]
        queries.append({
            "stem": r["stem"],
            "image_name": f"query/{r['stem']}.jpg",
            "qvec": p.get("qvec"), "tvec": p.get("tvec"),
            "center_world": p.get("center_world"),
            "num_observations": p.get("num_observations"),
            "num_points2D": p.get("num_points2D"),
            "latlon": p.get("latlon"),
            "anchor_shot": r["anchor_shot"],
            "anchor_score": r.get("anchor_score"),
        })
    anchors = {r["stem"]: {"shot_id": r["anchor_shot"],
                          "capture": r.get("anchor_capture")}
               for r in successful}
    failed_stems = [r["stem"] for r in results if not r["ok"]]
    summary = {
        "sequence_name": shot_out_dir.name,
        "method": "independent per-photo PnP vs ref-triangulated scene",
        "n_queries": len(photo_stems),
        "n_registered": len(successful),
        "failed": failed_stems,
        "anchors": anchors,
        "queries": queries,
    }
    with open(shot_out_dir / "sequence.json", "w") as f:
        json.dump(summary, f, indent=2)

    # Clean up per-photo work dirs
    shutil.rmtree(per_photo_dir, ignore_errors=True)


def _run_register_photo(shot_id, log_path, photo_path, anchor_stem,
                         anchor_shot_id, anchor_capture, shot_out_dir):
    """Single-photo PnP via register_photo.py.

    That script doesn't emit phase markers like the sequence pipeline, so
    we run it to completion then synthesize a `sequence.json` compatible
    with the viewer. Copies the query image into `shot_out_dir/images/` so
    the three.js scene endpoint can serve it.
    """
    images_root = _fetch_ref_images_for_run(
        anchor_shot_id, anchor_capture or None, n_neighbors=10,
    )
    cmd = [
        "python3", "-u", str(paths.SHOTMATCH_REPO / "register_photo.py"),
        "--photo", str(photo_path),
        "--model", str(paths.REF_MODEL_DIR),
        "--images", str(images_root),
        "--matched-shot", anchor_shot_id,
        "--capture", anchor_capture or "",
        "--output", str(shot_out_dir),
        "--profile", "fallback",
    ]
    log_append(log_path, f"[sfm] cmd: {' '.join(cmd)}")
    log_append(log_path, f"[sfm] ref images root: {images_root}")
    db.update_shot(shot_id, phase="phase1",
                   phase_label="Registering photo via PnP", progress=0.30)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=CHILD_ENV,
    )
    for line in proc.stdout:
        log_append(log_path, f"[sfm] {line.rstrip()}")
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"register_photo failed (returncode={proc.returncode})")

    # register_photo.py writes the query JPEG at <stem>.jpg alongside the
    # sidecar. The viewer expects output/images/<stem>.jpg, so move it there.
    sidecar_path = shot_out_dir / f"{anchor_stem}_pose.json"
    img_src = shot_out_dir / f"{anchor_stem}.jpg"
    if img_src.exists():
        images_dir = shot_out_dir / "images"
        images_dir.mkdir(exist_ok=True)
        shutil.move(str(img_src), str(images_dir / f"{anchor_stem}.jpg"))

    pose = {}
    if sidecar_path.exists():
        with open(sidecar_path) as f:
            pose = json.load(f)

    # Compose a sequence.json so the viewer + status endpoint can find it.
    sequence = {
        "sequence_name": Path(shot_out_dir).name,
        "method": "register_photo.py (single-photo PnP vs ref-triangulated)",
        "n_queries": 1,
        "n_registered": 1 if pose else 0,
        "failed": [] if pose else [anchor_stem],
        "anchors": {anchor_stem: {
            "shot_id": anchor_shot_id,
            "capture": anchor_capture,
        }},
        "queries": [{
            "stem": anchor_stem,
            "image_name": f"query/{anchor_stem}.jpg",
            "qvec": pose.get("qvec"),
            "tvec": pose.get("tvec"),
            "center_world": pose.get("center_world"),
            "num_observations": pose.get("num_observations"),
            "num_points2D": pose.get("num_points2D"),
            "latlon": pose.get("latlon"),
        }] if pose else [],
    }
    with open(shot_out_dir / "sequence.json", "w") as f:
        json.dump(sequence, f, indent=2)

    if not pose:
        raise RuntimeError("register_photo completed but no pose sidecar "
                           "was produced")


def _process_shot(shot_id: str):
    try:
        shot = db.get_shot(shot_id)
        project = db.get_project(shot["project_id"])
        log_path = paths.shot_log_path(project["id"], shot_id)

        db.update_shot(shot_id, status="running", phase="init",
                       phase_label="Starting", progress=0.02, error=None)

        meta = shot.get("meta") or {}
        photo_names = meta.get("photo_names") or []
        photo_stems = meta.get("photo_stems") or []
        if not photo_names:
            raise RuntimeError("Shot has no photos assigned")

        photos_root = paths.project_photos_dir(project["id"])
        # Resolve relative photo names ("shot_00001/wide.jpg") to file paths.
        # Fall back to a flat lookup for legacy projects.
        photo_files = []
        for name in photo_names:
            full = photos_root / name
            if not full.exists():
                # Legacy flat layout: try to find by stem
                stem = Path(name).stem
                full = next((p for p in photos_root.iterdir()
                             if p.is_file() and p.stem == stem), None)
            if full is None or not full.exists():
                raise RuntimeError(f"Photo '{name}' not found in {photos_root}")
            photo_files.append(full)

        # Step 1: megaloc — runs per-project on the flat wides dir; we just
        # consume its cached output here.
        db.update_shot(shot_id, phase="megaloc", phase_label="Running MegaLoc", progress=0.05)
        log_append(log_path, "[megaloc] running...")
        matches = run_megaloc_for_project(project["id"])
        project_meta = project.get("meta") or {}
        photo_meta = project_meta.get("photo_meta") or {}
        _annotate_matches_with_gps(matches, photo_meta)
        log_append(log_path, f"[megaloc] got {len(matches)} matches total")

        # Anchor selection. Bundles always anchor on the wide JPEG (best
        # match for the reference 65° HFoV pano sub-views). MegaLoc keys
        # the wide as `shot_<id:05d>` (the bundle's directory name).
        shot_dir_name = meta.get("shot_dir") or (
            photo_stems[0] if photo_stems else None
        )
        if shot_dir_name and shot_dir_name in matches:
            # Single-photo lookup against the bundle's wide entry.
            anchor_stem_for_pick = shot_dir_name
            megaloc_keys = [shot_dir_name]
        else:
            # Legacy / non-bundle: iterate over individual photo stems.
            megaloc_keys = photo_stems
            anchor_stem_for_pick = None

        anchor_key, anchor_info = _choose_anchor(
            matches, megaloc_keys,
            pinned_stem=meta.get("anchor_override") or anchor_stem_for_pick,
        )
        anchor_shot_id = anchor_info["shot_id"]
        anchor_capture = anchor_info["capture"]
        anchor_score = anchor_info["score"]
        # The "anchor" inside the bundle is always the wide.jpg file in
        # the bundle dir; that's the photo whose pose is solved against
        # refs in Phase 1.
        anchor_stem_in_bundle = meta.get("wide_stem") or "wide"
        gps_note = ""
        if anchor_info.get("distance_m") is not None:
            gps_note = (f" GPS_d={anchor_info['distance_m']:.0f}m"
                        f" within={anchor_info.get('gps_valid')}")
        log_append(log_path, f"[megaloc] anchor: {anchor_key} -> "
                              f"{anchor_info['shot_key']} "
                              f"(score={anchor_score:.3f}{gps_note})")
        db.update_shot(
            shot_id, anchor_shot=anchor_shot_id, anchor_score=anchor_score,
            progress=0.10,
            meta={**meta, "anchor_stem": anchor_stem_in_bundle,
                  "anchor_shot_key": anchor_info["shot_key"],
                  "anchor_capture": anchor_capture,
                  "anchor_distance_m": anchor_info.get("distance_m"),
                  "anchor_gps_valid": anchor_info.get("gps_valid"),
                  "n_photos": len(photo_files)},
        )

        # Step 2: stage the bundle photos into a flat dir for the SFM
        # subprocess (register_sequence_natural globs *.jpg non-recursively).
        shot_out_dir = paths.shot_output_dir(project["id"], shot_id)
        staging_dir = paths.shot_dir(project["id"], shot_id) / "photos"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)
        for p in photo_files:
            shutil.copy(p, staging_dir / p.name)
        anchor_stem = anchor_stem_in_bundle

        # Step 3: branch on photo count — single-photo shots go through
        # register_photo.py (PnP against the ref-triangulated scene);
        # multi-photo shots go through register_sequence_natural.py.
        db.update_shot(shot_id, phase="sfm", phase_label="Preparing SFM",
                       progress=0.15, n_queries=len(photo_files))

        # Mode dispatch:
        #   single photo                            → register_photo.py (PnP)
        #   bundle from one phone shot (default)    → register_sequence_natural
        #                                             (anchored multi-camera SFM
        #                                              over wide+mid+5 burst —
        #                                              the lateral swipe gives
        #                                              real baseline)
        #   meta.sfm_mode="independent"             → independent per-photo
        #                                             (one PnP per photo,
        #                                              results unioned)
        if len(photo_files) == 1:
            anchor_photo = staging_dir / photo_files[0].name
            _run_register_photo(
                shot_id, log_path, anchor_photo, anchor_stem,
                anchor_shot_id, anchor_capture, shot_out_dir,
            )
        elif meta.get("sfm_mode") == "independent":
            _run_register_independent(
                shot_id, log_path, staging_dir, matches, photo_stems,
                shot_out_dir,
            )
        else:
            _run_register_sequence(
                shot_id, log_path, staging_dir, anchor_stem,
                anchor_shot_id, anchor_capture, shot_out_dir,
                n_shots_neighbours=int(meta.get("n_shots") or 15),
            )

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

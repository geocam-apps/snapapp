"""Filesystem layout for project uploads and pipeline outputs."""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

HOME = Path.home()


def _p(env_name: str, default: Path) -> Path:
    v = os.environ.get(env_name)
    return Path(v) if v else default


# External tools (repo clones, usually in $HOME)
MEGALOC_REPO = _p("SNAPAPP_MEGALOC_REPO", HOME / "megaloc_shotmatch")
SHOTMATCH_REPO = _p("SNAPAPP_SHOTMATCH_REPO", HOME / "shotmatch_pose")

# Reference dataset.
#   MEGALOC_DB          — dir containing reference.faiss + reference_paths.json
#   MEGALOC_GCDB_DIR    — dir of .gcdb metadata files (for GPS on ref matches,
#                         optional — harmless if missing)
#   REF_MODEL_DIR       — COLMAP text model (cameras.txt / images.txt) for the
#                         reference scene
#   REF_IMAGES_DIR      — local fallback for reference JPEGs; the actual
#                         `--images` root is resolved by app.ref_cache, which
#                         can also pull from S3/MinIO on demand
MEGALOC_DB = _p("SNAPAPP_MEGALOC_DB", HOME / "reference_db")
MEGALOC_GCDB_DIR = _p("SNAPAPP_MEGALOC_GCDB_DIR", HOME / "reference_images")
REF_MODEL_DIR = _p("SNAPAPP_REF_MODEL_DIR", HOME / "undistorted_images" / "sparse")
REF_IMAGES_DIR = _p("SNAPAPP_REF_IMAGES_DIR", HOME / "undistorted_images" / "images")


def project_dir(project_id: str) -> Path:
    d = PROJECTS_DIR / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_photos_dir(project_id: str) -> Path:
    d = project_dir(project_id) / "photos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_megaloc_in_dir(project_id: str) -> Path:
    """Flat directory of one image per phone-shot (its wide.jpg) used as
    the MegaLoc query set. Lives alongside `photos/` so MegaLoc doesn't
    have to walk the per-shot subdirs or know about burst frames."""
    d = project_dir(project_id) / "megaloc_in"
    d.mkdir(parents=True, exist_ok=True)
    return d


SQLITE_EXTS = (".db", ".sqlite", ".sqlite3", ".gcdb")


def project_sqlite_path(project_id: str) -> Path | None:
    """Return the uploaded sqlite path if present, regardless of extension."""
    d = project_dir(project_id)
    for ext in SQLITE_EXTS:
        p = d / f"upload{ext}"
        if p.exists():
            return p
    return None


def new_project_sqlite_path(project_id: str, original_filename: str) -> Path:
    """Decide where to store an uploaded sqlite — preserves the user's extension
    so they see something familiar in logs. Defaults to .db."""
    ext = Path(original_filename).suffix.lower()
    if ext not in SQLITE_EXTS:
        ext = ".db"
    return project_dir(project_id) / f"upload{ext}"


def project_megaloc_csv(project_id: str) -> Path:
    return project_dir(project_id) / "megaloc.csv"


def shot_dir(project_id: str, shot_id: str) -> Path:
    d = project_dir(project_id) / "shots" / shot_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def shot_output_dir(project_id: str, shot_id: str) -> Path:
    d = shot_dir(project_id, shot_id) / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def shot_log_path(project_id: str, shot_id: str) -> Path:
    return shot_dir(project_id, shot_id) / "log.txt"

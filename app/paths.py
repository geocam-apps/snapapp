"""Filesystem layout for project uploads and pipeline outputs."""
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# External tools
HOME = Path.home()
MEGALOC_REPO = HOME / "megaloc_shotmatch"
SHOTMATCH_REPO = HOME / "shotmatch_pose"
MEGALOC_DB = HOME / "reference_db"
MEGALOC_GCDB_DIR = HOME / "reference_images"
REF_MODEL_DIR = HOME / "undistorted_images" / "sparse"
REF_IMAGES_DIR = HOME / "undistorted_images" / "images"


def project_dir(project_id: str) -> Path:
    d = PROJECTS_DIR / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_photos_dir(project_id: str) -> Path:
    d = project_dir(project_id) / "photos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_sqlite_path(project_id: str) -> Path:
    return project_dir(project_id) / "upload.gcdb"


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

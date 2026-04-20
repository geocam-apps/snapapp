"""SQLite storage for projects and shots."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "snapapp.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS shots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT,
    phase_label TEXT,
    progress REAL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    anchor_shot TEXT,
    anchor_score REAL,
    n_queries INTEGER,
    n_registered INTEGER,
    error TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_shots_project ON shots(project_id);
"""


@contextmanager
def conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)


def _row_to_project(r):
    if r is None:
        return None
    d = dict(r)
    if d.get("meta_json"):
        d["meta"] = json.loads(d["meta_json"])
    d.pop("meta_json", None)
    return d


def _row_to_shot(r):
    if r is None:
        return None
    d = dict(r)
    if d.get("meta_json"):
        d["meta"] = json.loads(d["meta_json"])
    d.pop("meta_json", None)
    return d


def create_project(name, kind, meta=None):
    pid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    with conn() as c:
        c.execute(
            "INSERT INTO projects(id, name, kind, created_at, meta_json) VALUES (?,?,?,?,?)",
            (pid, name, kind, time.time(), json.dumps(meta or {})),
        )
    return pid


def get_project(pid):
    with conn() as c:
        r = c.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    return _row_to_project(r)


def list_projects():
    with conn() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    return [_row_to_project(r) for r in rows]


def delete_project(pid):
    with conn() as c:
        c.execute("DELETE FROM shots WHERE project_id = ?", (pid,))
        c.execute("DELETE FROM projects WHERE id = ?", (pid,))


def create_shot(project_id, name, meta=None):
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    with conn() as c:
        c.execute(
            """INSERT INTO shots(id, project_id, name, status, phase, phase_label,
                progress, created_at, updated_at, meta_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (sid, project_id, name, "pending", "init", "Queued",
             0.0, now, now, json.dumps(meta or {})),
        )
    return sid


def get_shot(sid):
    with conn() as c:
        r = c.execute("SELECT * FROM shots WHERE id = ?", (sid,)).fetchone()
    return _row_to_shot(r)


def list_shots(project_id):
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM shots WHERE project_id = ? ORDER BY created_at ASC",
            (project_id,),
        ).fetchall()
    return [_row_to_shot(r) for r in rows]


def update_shot(sid, **kwargs):
    if not kwargs:
        return
    kwargs["updated_at"] = time.time()
    if "meta" in kwargs:
        kwargs["meta_json"] = json.dumps(kwargs.pop("meta"))
    cols = list(kwargs.keys())
    vals = [kwargs[k] for k in cols]
    set_clause = ", ".join(f"{c} = ?" for c in cols)
    with conn() as c:
        c.execute(f"UPDATE shots SET {set_clause} WHERE id = ?", (*vals, sid))


def delete_shot(sid):
    with conn() as c:
        c.execute("DELETE FROM shots WHERE id = ?", (sid,))


def mark_stale_running():
    """Mark any shot stuck in status=running (left from a server crash)
    as failed. Called at startup."""
    now = time.time()
    with conn() as c:
        c.execute(
            "UPDATE shots SET status='failed', phase='error', "
            "phase_label='Server restarted mid-run', "
            "error='server-restart', updated_at=? WHERE status='running'",
            (now,),
        )

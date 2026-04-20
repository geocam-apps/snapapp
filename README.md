# snapapp

Web UI for end-to-end camera pose recovery:
**upload photos → MegaLoc anchor match → COLMAP SFM → 3D viewer**.

Each upload is a *project*. Inside a project there are *shots* — each shot
runs `shotmatch_pose/register_sequence_natural.py` on a subset of the
project's photos and produces its own 3D reconstruction.

## Run

```bash
./start_webserver.sh         # listens on :8080 for the cloudflare tunnel
```

The server reads/writes:

| Path | Role |
|------|------|
| `~/megaloc_shotmatch/match_photos.py` | MegaLoc anchor lookup (CPU-forced; GPU OOMs DINOv2 on this host) |
| `~/shotmatch_pose/register_sequence_natural.py` | 7-phase COLMAP pipeline (GPU when available for DISK+LightGlue) |
| `~/reference_db/` | MegaLoc FAISS index |
| `~/undistorted_images/sparse/` | Reference COLMAP text model (36k images, 6 cams) |
| `~/undistorted_images/images/` | Reference image root |
| `data/snapapp.db` | Project / shot metadata (sqlite) |
| `data/projects/<project_id>/photos/` | Uploaded photos |
| `data/projects/<project_id>/shots/<shot_id>/output/` | Per-shot SFM output + enriched model + JPEG thumbs |

## Data flow

1. `POST /api/upload` — creates a project dir, saves photos or a `.gcdb`
   file. Image projects auto-get a default shot over all photos.
2. `POST /api/shots/<id>/run` — spawns a background thread that:
   - Caches a MegaLoc CSV at project scope
   - Picks the highest-scoring photo as the anchor
   - Spawns `register_sequence_natural.py`, streaming its stdout into
     `log.txt` and mapping phase markers to progress 0.05→1.0
3. `GET /api/shots/<id>/scene` — reads the written COLMAP model + the
   per-shot `sequence.json` and returns the data the three.js viewer
   needs: cameras (ref, query, anchor), sparse point cloud, summary.

Server restart logic: `mark_stale_running()` flips any `status=running`
shot to `failed` with `error='server-restart'` on startup.

## File tree

```
app/
├── db.py           # sqlite schema + project/shot CRUD
├── paths.py        # filesystem layout + external tool paths
├── pipeline.py     # MegaLoc + shotmatch_pose orchestration
├── gcdb_read.py    # SpatiaLite probe for uploaded .gcdb files
└── server.py       # Flask routes + entrypoint
static/
├── index.js        # projects list + upload UI
├── project.js      # shot list, photo grid, per-shot progress
├── viewer.js       # three.js scene loader
└── three/          # vendored three.module.js + OrbitControls
templates/
├── index.html
├── project.html
└── viewer.html
```

## Limitations

- **SQLite-only projects** are parsed + displayed but can't be run through
  the SFM pipeline yet — that would require companion imagery the user
  doesn't currently have a way to upload.
- **Test coverage**: verified end-to-end on the working directory with
  synthetic test inputs from the reference images. Real phone HEICs
  with overlapping views (e.g. the `myles_coffee` dataset from
  `shotmatch_pose`) are the intended happy path.

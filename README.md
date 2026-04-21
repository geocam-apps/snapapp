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

## Configuring a reference dataset

Each reference dataset is three things on disk:

| | Default | Env override |
|---|---|---|
| FAISS index + paths.json | `~/reference_db/` | `SNAPAPP_MEGALOC_DB` |
| COLMAP text model | `~/undistorted_images/sparse/` | `SNAPAPP_REF_MODEL_DIR` |
| Ref image root | `~/undistorted_images/images/` | `SNAPAPP_REF_IMAGES_DIR` (local pass-through) or `SNAPAPP_REF_IMAGES_URL` (see below) |
| Optional GCDB metadata | `~/reference_images/` | `SNAPAPP_MEGALOC_GCDB_DIR` |

To switch references (e.g. Manhattan Beach → Lomita), set those four
env vars and restart the server. `ref_index.json` is keyed by a hash
of `REF_MODEL_DIR` so stale geographic indexes from a previous dataset
don't leak in.

Build a new FAISS index with the right layout:

```bash
# For split-pano datasets (default):
python3 ~/megaloc_shotmatch/build_faiss.py \
    --input /path/to/images --output-dir /path/to/ref_db

# For flat already-undistorted datasets:
python3 ~/megaloc_shotmatch/build_faiss.py \
    --input /path/to/images --output-dir /path/to/ref_db \
    --layout flat
```

The layout is recorded in `reference_paths.json` so `match_photos.py`
picks the right shot-key derivation automatically.

## Reference imagery storage

Reference photos are read through `app/ref_cache.py`, which supports
two modes:

| Env | Behaviour |
|-----|-----------|
| `SNAPAPP_REF_IMAGES_URL` unset or local path | Pass-through: subprocesses read directly from the path. Zero copy. |
| `SNAPAPP_REF_IMAGES_URL=s3://bucket/prefix` | On-demand: before each SFM run, compute the 45-60 images that subprocess will touch (matched shot + N nearest neighbors), download from S3 to `data/ref_cache/` and pass that as `--images`. |
| `SNAPAPP_REF_CACHE_MAX_GB=20` | LRU-evict (mtime-based) to stay under this cap across runs. |

The reference COLMAP model itself (text files at `REF_MODEL_DIR`,
~50 MB) stays local — we need it in memory to decide which ref images
each run needs. The pixel data is what we fetch on demand.

## Limitations

- **SQLite-only projects** are parsed + displayed but can't be run through
  the SFM pipeline yet — that would require companion imagery the user
  doesn't currently have a way to upload.
- **Test coverage**: verified end-to-end on the working directory with
  synthetic test inputs from the reference images. Real phone HEICs
  with overlapping views (e.g. the `myles_coffee` dataset from
  `shotmatch_pose`) are the intended happy path.

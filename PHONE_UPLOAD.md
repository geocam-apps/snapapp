# Phone-App Upload Protocol

This doc describes how the Android phone-app should upload its captured
SnapApp sqlite files to the snapapp web server. It's written for another
Claude Code instance working in the phone-app codebase and assumes no
context from the server codebase. You can implement the uploader from
this doc alone.

## Context

The phone app captures photos + GPS into a single SQLite file on the
device. After a session, the file needs to be uploaded to the snapapp
server, which runs the photos through a MegaLoc + `shotmatch_pose`
pipeline to produce a 3D reconstruction aligned to a reference model.
The user then views the result in a web browser.

The server is on the other side of a **Cloudflare tunnel** (free tier).
Cloudflare's edge rejects any single HTTP request body larger than about
100 MB with a 413. SnapApp sqlite files are routinely 100 MB – 500 MB
(the wide JPEGs are embedded as BLOBs), so every upload **must** be
chunked. There is no "small file" fast path worth supporting; always
chunk.

## Server URL

Read the base URL from a user-configurable setting in the app. Do **not**
hardcode it. It is typically a Cloudflare tunnel hostname like
`https://something-random.trycloudflare.com`, but during local dev the
user may point it at `http://192.168.x.x:8080` or similar.

All endpoints in this doc are rooted at `<BASE>/api/mobile/upload`.

## Endpoints

### `POST /api/mobile/upload` — one chunk

Upload one chunk of a file. Repeat until the whole file is uploaded. The
final chunk triggers server-side ingestion and returns the created project.

**Headers:**

| Header | Required | Value |
| --- | --- | --- |
| `Content-Type` | yes | `application/octet-stream` (raw bytes, **NOT** multipart) |
| `Content-Range` | yes | `bytes <start>-<end>/<total>` — RFC 7233 form, `<end>` is **inclusive** |
| `X-Upload-Id` | yes | 8–64 chars from `[A-Za-z0-9_-]`. Client-generated, stable across all chunks of one file. A UUID v4 with dashes works fine. |
| `X-Filename` | yes | Original basename only (e.g. `2026-04-20_maple-street.db`). The server sanitizes to basename and uses this to name the project. Path separators are stripped server-side but don't send them. |

**Body:** the raw chunk bytes, length `end - start + 1`.

**Chunk size:** use **8 MiB** (`8 * 1024 * 1024 = 8388608` bytes) per
chunk. The server's hard cap is 16 MiB per chunk; anything bigger gets
413. 8 MiB keeps a generous safety margin under Cloudflare's 100 MB edge
limit even with request overhead.

**Response on a partial chunk (`received < total`), HTTP 200:**

```json
{
  "upload_id": "abc-123-...",
  "received": 8388608,
  "total": 125829120,
  "complete": false
}
```

**Response on the final chunk (`received == total`), HTTP 200:**

```json
{
  "upload_id": "abc-123-...",
  "received": 125829120,
  "total": 125829120,
  "complete": true,
  "project_id": "20260420-223926-1c0072",
  "project_url": "https://example.trycloudflare.com/project/20260420-223926-1c0072",
  "format": "snapapp",
  "n_shots": 42
}
```

Store `project_url` locally and show it to the user as a "View results"
tap target — it opens the project page where MegaLoc matches and, once
SFM finishes, the 3D viewer become available.

**Error responses:**

| Status | Body | Meaning |
| --- | --- | --- |
| 400 | `{"error": "bad or missing Content-Range header ..."}` | Malformed headers or filename. Fix client; don't retry. |
| 400 | `{"error": "Unrecognized sqlite schema ...", "probe": {...}, "format": null}` | The final file isn't a SnapApp sqlite. Show `probe.inspection.tables` to the user so they can see which tables the server found. |
| 400 | `{"error": "short_body", "expected_bytes": N, "got_bytes": M}` | The body was shorter than the `Content-Range` claimed. Server rolled the partial back — just retry this chunk with the correct body. |
| 409 | `{"error": "offset_mismatch", "expected": N, "got": M}` | The server has `N` bytes for this upload-id but you sent a chunk starting at `M`. Resume from `expected`. See "Retry and resumption" below. |
| 413 | `{"error": "chunk size ... exceeds limit ...", "max_chunk": 16777216}` | Your chunk is too big. Shouldn't happen if you use 8 MiB; if it does it's a bug — surface to the user. |
| 413 | `{"error": "total size ... exceeds limit ..."}` | The whole file is bigger than 4 GB. Very unlikely; tell the user. |
| 5xx | any | Transient. Retry the same chunk with exponential backoff (see below). |

### `GET /api/mobile/upload/<upload_id>/status` — progress check

Get how many bytes the server currently has for this upload-id.

**Response, HTTP 200:**

```json
{
  "upload_id": "abc-123-...",
  "exists": true,
  "received": 16777216
}
```

If nothing has been uploaded for this id yet (or the server cleaned it up
after a failure), `exists` is `false` and `received` is `0`. In that
case start from offset 0.

## Why chunking is mandatory

Cloudflare's free tunnel edge rejects request bodies larger than ~100 MB
before they reach the server. SnapApp sqlite files carry the wide JPEGs
as BLOBs and easily hit 100–500 MB. Every upload goes through the edge,
so every upload gets chunked. 8 MiB keeps you safely under the limit
with headroom for future edge changes.

## Filename convention

The phone app already names capture files as
`YYYY-MM-DD_<street-slug>.db` (e.g. `2026-04-20_maple-street.db`).
Pass that exact string as `X-Filename`. The server uses the stem
(`2026-04-20_maple-street`) as the default project name, so a good
filename directly becomes a good human-readable project title.

Send only the basename — no directory path. The server strips paths
defensively, but sending the clean basename keeps logs tidy.

## Retry and resumption

**On network error or 5xx:** retry the same chunk. Use exponential
backoff: 1 s, 2 s, 4 s, 8 s, 16 s, cap at 30 s. Give up after ~5 min
of continuous failure and surface the error to the user.

**On 409 offset_mismatch:** the server has a different amount of data
than you think. Do this:

1. `GET /api/mobile/upload/<upload_id>/status`.
2. Read `received` from the response.
3. Seek your local file to that offset.
4. Resume by POSTing the next 8 MiB starting at `start = received`.

This is the normal path after the app is backgrounded, the process is
killed, or the network drops mid-upload. Keep the `upload_id` in
persistent storage (e.g. SharedPreferences / Room) keyed by the local
file path so you can resume across app restarts.

**On 400 short_body:** the server rolled your chunk back. Re-read the
chunk from disk and POST it again. This usually means the connection
dropped mid-body.

## Example curl — single-file end-to-end

Chunking a 20 MB file into three pieces:

```bash
U=$(uuidgen)  # one id across all chunks

# chunk 0: bytes 0-8388607 (8 MiB)
curl -X POST "$BASE/api/mobile/upload" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Upload-Id: $U" \
  -H "X-Filename: 2026-04-20_maple-street.db" \
  -H "Content-Range: bytes 0-8388607/20971520" \
  --data-binary @chunk0.bin
# => {"complete":false, "received":8388608, ...}

# chunk 1: bytes 8388608-16777215
curl -X POST "$BASE/api/mobile/upload" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Upload-Id: $U" \
  -H "X-Filename: 2026-04-20_maple-street.db" \
  -H "Content-Range: bytes 8388608-16777215/20971520" \
  --data-binary @chunk1.bin

# chunk 2: bytes 16777216-20971519 (final, < 8 MiB)
curl -X POST "$BASE/api/mobile/upload" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Upload-Id: $U" \
  -H "X-Filename: 2026-04-20_maple-street.db" \
  -H "Content-Range: bytes 16777216-20971519/20971520" \
  --data-binary @chunk2.bin
# => {"complete":true, "project_id":"...", "project_url":"https://.../project/...", ...}
```

And the status endpoint:

```bash
curl "$BASE/api/mobile/upload/$U/status"
# => {"upload_id":"...", "exists":true, "received":16777216}
```

## What the server does with the file

For reference so you know what you're plugging into:

1. Final chunk arrives → server verifies the file is a SnapApp sqlite
   (has a `shots` table with an image column).
2. Extracts the wide-angle JPEG from every `shots` row into the project's
   photos dir.
3. Creates a project + a default "all shots" shot row in the server's
   own sqlite database.
4. Kicks off **MegaLoc** in a background thread. MegaLoc matches every
   extracted photo against a precomputed reference database to find the
   best-scoring anchor for 3D alignment.
5. When the user triggers the shot (or automatically, depending on UI
   flow), runs **`shotmatch_pose`** — COLMAP-based SFM that registers
   every phone photo against the reference model, producing a 3D scene
   you can view in the browser.

Your upload completes as soon as step 3 finishes. Steps 4 and 5 run
asynchronously — the phone doesn't need to wait. The `project_url` you
got back will show progress as those stages complete.

## Implementation checklist for the phone client

- [ ] User-configurable base URL setting.
- [ ] Per-upload stable `upload_id` stored alongside the local file path
      so resumption survives process death.
- [ ] Chunker that reads 8 MiB windows from the file with random seek
      support (so you can jump to `received` on resume).
- [ ] 409 → GET status → seek → continue loop.
- [ ] 5xx / network → exponential backoff retry of the same chunk.
- [ ] On `complete: true`, persist `project_url` and present it in the UI.
- [ ] On 400 with `probe` payload, surface the schema detail to the user
      so they can tell whether the capture session got corrupted.

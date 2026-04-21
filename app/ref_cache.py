"""On-demand local cache for reference images.

The reference image set will grow to hundreds of GB; we can't keep it all
on disk. Each SFM subprocess only needs a narrow slice (the matched shot
and its ~15 nearest neighbors = ~45 images). This module:

1. Picks a local cache dir `data/ref_cache/` under a configurable size cap.
2. Given a list of image rel-paths, ensures each is present locally,
   downloading from S3 (or hardlinking from a local root) as needed.
3. LRU-evicts when the cache exceeds its size cap, using file mtime as
   the recency marker.

Configuration (env):
  SNAPAPP_REF_IMAGES_URL   = s3://bucket/prefix    (S3 source)
                           | /abs/path             (local passthrough)
                           | (unset → paths.REF_IMAGES_DIR)
  SNAPAPP_REF_CACHE_MAX_GB = 20 (default)

When the URL is a local path that already exists, `root_path()` returns
it directly and `ensure_local()` is a no-op — zero-copy passthrough so
existing local-only deployments are unaffected.
"""
import os
import shutil
import time
from pathlib import Path

from . import paths


CACHE_ROOT = paths.DATA_DIR / "ref_cache"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)


def _url() -> str:
    return os.environ.get("SNAPAPP_REF_IMAGES_URL") or str(paths.REF_IMAGES_DIR)


def _is_s3() -> bool:
    return _url().startswith("s3://")


def _cap_bytes() -> int:
    gb = float(os.environ.get("SNAPAPP_REF_CACHE_MAX_GB", "20"))
    return int(gb * (1024 ** 3))


def root_path() -> Path:
    """Local directory to use as the `--images` root for COLMAP subprocess
    calls. For S3 or non-existent local paths this is the on-demand cache;
    for a real local path it's that path directly.
    """
    u = _url()
    if _is_s3():
        return CACHE_ROOT
    p = Path(u)
    return p if p.exists() else CACHE_ROOT


def _parse_s3(url: str):
    # "s3://bucket/key/prefix" → ("bucket", "key/prefix")
    path = url[len("s3://"):]
    bucket, _, prefix = path.partition("/")
    return bucket, prefix.rstrip("/")


def _touch(p: Path):
    try:
        now = time.time()
        os.utime(p, (now, now))
    except Exception:
        pass


def _evict_lru():
    cap = _cap_bytes()
    files = []
    total = 0
    for r, _dirs, names in os.walk(CACHE_ROOT):
        for n in names:
            fp = Path(r) / n
            try:
                st = fp.stat()
            except OSError:
                continue
            total += st.st_size
            files.append((st.st_mtime, st.st_size, fp))
    if total <= cap:
        return
    files.sort()  # oldest mtime first
    while total > cap and files:
        _, size, fp = files.pop(0)
        try:
            fp.unlink()
            total -= size
        except OSError:
            pass


def _fetch_one(rel: str):
    dst = CACHE_ROOT / rel
    if dst.exists():
        _touch(dst)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    u = _url()
    if _is_s3():
        try:
            import boto3  # lazy import
        except ImportError as e:
            raise RuntimeError(
                "SNAPAPP_REF_IMAGES_URL is s3:// but boto3 is not installed"
            ) from e
        bucket, prefix = _parse_s3(u)
        key = f"{prefix}/{rel}" if prefix else rel
        s3 = boto3.client("s3")
        s3.download_file(bucket, key, str(dst))
    else:
        # Local-but-not-root source (rare path): copy in. The common
        # case is handled upstream — root_path() returns the source dir
        # itself and ensure_local is a no-op.
        src = Path(u) / rel
        if not src.exists():
            raise FileNotFoundError(f"Ref image missing at source: {src}")
        shutil.copy(str(src), str(dst))
    _touch(dst)


def ensure_local(rel_paths):
    """Make sure every rel path exists under `root_path()` locally.

    Returns the local directory suitable to pass as `--images`.
    No-op when the configured source is already the local root.
    """
    source = Path(_url())
    if not _is_s3() and source.exists():
        # Pass-through: images already live at `source` → return as-is.
        return source
    missed = 0
    for rel in rel_paths:
        rel = rel.lstrip("/")
        if not (CACHE_ROOT / rel).exists():
            missed += 1
        _fetch_one(rel)
    if missed:
        _evict_lru()
    return CACHE_ROOT


def local_has(rel: str) -> bool:
    """Is this rel path currently available under root_path() without a fetch?"""
    return (root_path() / rel).exists()

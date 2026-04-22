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


def _url(override: str = None) -> str:
    if override:
        return override
    return os.environ.get("SNAPAPP_REF_IMAGES_URL") or str(paths.REF_IMAGES_DIR)


def _is_s3(url: str = None) -> bool:
    return _url(url).startswith("s3://")


def _cap_bytes() -> int:
    gb = float(os.environ.get("SNAPAPP_REF_CACHE_MAX_GB", "20"))
    return int(gb * (1024 ** 3))


def root_path(url: str = None) -> Path:
    """Local directory to use as the `--images` root for COLMAP subprocess
    calls. For S3 or non-existent local paths this is the on-demand cache;
    for a real local path it's that path directly.

    Optional `url` overrides the env-configured source — used when the
    pipeline has a specific reference selected per-project.
    """
    u = _url(url)
    if _is_s3(u):
        return CACHE_ROOT
    # Support file:// URLs for registry entries that pin a local path.
    if u.startswith("file://"):
        u = u[len("file://"):]
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


_S3_CLIENT = None


def _s3_client():
    """Lazily create a boto3 S3 client, honoring MinIO-style env vars:

      SNAPAPP_S3_ENDPOINT_URL   custom endpoint (e.g. http://minio-dn.geocam.io)
      SNAPAPP_S3_ACCESS_KEY     access key (falls back to AWS_ACCESS_KEY_ID)
      SNAPAPP_S3_SECRET_KEY     secret key (falls back to AWS_SECRET_ACCESS_KEY)
      SNAPAPP_S3_REGION         region name (default us-east-1)
    """
    global _S3_CLIENT
    if _S3_CLIENT is not None:
        return _S3_CLIENT
    try:
        import boto3
    except ImportError as e:
        raise RuntimeError(
            "SNAPAPP_REF_IMAGES_URL is s3:// but boto3 is not installed"
        ) from e
    kwargs = {}
    endpoint = os.environ.get("SNAPAPP_S3_ENDPOINT_URL")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    ak = os.environ.get("SNAPAPP_S3_ACCESS_KEY") or os.environ.get("AWS_ACCESS_KEY_ID")
    sk = os.environ.get("SNAPAPP_S3_SECRET_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if ak and sk:
        kwargs["aws_access_key_id"] = ak
        kwargs["aws_secret_access_key"] = sk
    kwargs["region_name"] = os.environ.get("SNAPAPP_S3_REGION", "us-east-1")
    _S3_CLIENT = boto3.client("s3", **kwargs)
    return _S3_CLIENT


def _fetch_one(rel: str, url: str = None):
    dst = CACHE_ROOT / rel
    if dst.exists():
        _touch(dst)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    u = _url(url)
    if _is_s3(u):
        bucket, prefix = _parse_s3(u)
        key = f"{prefix}/{rel}" if prefix else rel
        _s3_client().download_file(bucket, key, str(dst))
    else:
        if u.startswith("file://"):
            u = u[len("file://"):]
        src = Path(u) / rel
        if not src.exists():
            raise FileNotFoundError(f"Ref image missing at source: {src}")
        shutil.copy(str(src), str(dst))
    _touch(dst)


def ensure_local(rel_paths, images_url: str = None):
    """Make sure every rel path exists under `root_path()` locally.

    Returns the local directory suitable to pass as `--images`.
    No-op when the configured source is already the local root.

    `images_url` overrides the env-configured source for this call —
    e.g. when the pipeline has picked a registry-specific reference.
    """
    u = _url(images_url)
    source_str = u[len("file://"):] if u.startswith("file://") else u
    source = Path(source_str) if not _is_s3(u) else None
    if source and source.exists():
        return source
    missed = 0
    for rel in rel_paths:
        rel = rel.lstrip("/")
        if not (CACHE_ROOT / rel).exists():
            missed += 1
        _fetch_one(rel, url=images_url)
    if missed:
        _evict_lru()
    return CACHE_ROOT


def local_has(rel: str) -> bool:
    """Is this rel path currently available under root_path() without a fetch?"""
    return (root_path() / rel).exists()

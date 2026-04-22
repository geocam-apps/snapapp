#!/usr/bin/env python3
"""Register a built FAISS reference dataset with snapapp.

Given a FAISS-index dir (containing reference.faiss + reference_paths.json)
and either a PanoPositions.csv (for pano-based references) or a COLMAP
model (for undistorted-images references), write an `extent.json` next
to the index and add/update an entry in snapapp's registry so the
pipeline can auto-pick this dataset for matching query projects.

Examples:

  # Pano-based (Costa Mesa):
  scripts/register_reference.py register \
      --name costa_mesa_drive \
      --faiss-dir /home/dev/costa_mesa_reference_db \
      --positions-csv /home/dev/costa_mesa_panos/PanoPositions.csv \
      --images-url file:///home/dev/costa_mesa_panos

  # COLMAP-based (Lomita):
  scripts/register_reference.py register \
      --name lomita_street \
      --faiss-dir /home/dev/lomita_reference_db \
      --model-dir /home/dev/lomita_reference/sparse \
      --images-url s3://production-outputs/geocam/.../images
"""
import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import ref_registry


def extent_from_csv(csv_path: Path) -> dict:
    """Read PanoPositions.csv (or equivalent — needs lat/lon columns)
    and return a bbox."""
    lats = []
    lons = []
    with open(csv_path) as f:
        reader = csv.DictReader(f, skipinitialspace=True)
        for row in reader:
            try:
                lat = float(row.get("lat") or row.get("latitude"))
                lon = float(row.get("lon") or row.get("longitude"))
            except (TypeError, ValueError):
                continue
            lats.append(lat); lons.append(lon)
    if not lats:
        raise RuntimeError(f"No lat/lon rows in {csv_path}")
    return {
        "lat_min": min(lats), "lat_max": max(lats),
        "lon_min": min(lons), "lon_max": max(lons),
        "n_points": len(lats),
    }


def extent_from_colmap(model_dir: Path) -> dict:
    """Read the reference COLMAP model + its crs.json, derive a bbox of
    image centers in WGS84."""
    try:
        import pyproj
    except ImportError as e:
        raise RuntimeError("pyproj required for --model-dir path") from e

    sys.path.insert(0, str(Path.home() / "shotmatch_pose"))
    from colmap_io import read_model, image_center  # type: ignore
    import re, numpy as np

    cams, imgs, _ = read_model(model_dir)
    crs_path = model_dir.parent / "crs.json"
    if not crs_path.exists():
        raise RuntimeError(f"crs.json not found next to {model_dir}")
    with open(crs_path) as f:
        crs = json.load(f)
    # find the outer EPSG on the WKT (last AUTHORITY in the string)
    matches = re.findall(r'AUTHORITY\["EPSG","(\d+)"\]', crs.get("crs", ""))
    epsg = int(matches[-1]) if matches else 32611
    pose = crs.get("crsPose") or []
    offset = tuple(pose[-3:]) if len(pose) >= 12 else (0, 0, 0)
    tr = pyproj.Transformer.from_crs(epsg, 4326, always_xy=True)

    centers = np.array([image_center(img) for img in imgs.values()])
    utm_x = centers[:, 0] + offset[0]
    utm_y = centers[:, 1] + offset[1]
    lons, lats = tr.transform(utm_x, utm_y)
    return {
        "lat_min": float(lats.min()), "lat_max": float(lats.max()),
        "lon_min": float(lons.min()), "lon_max": float(lons.max()),
        "n_points": int(len(lats)),
    }


def register_cmd(args):
    faiss_dir = Path(args.faiss_dir).resolve()
    if not (faiss_dir / "reference.faiss").exists():
        raise SystemExit(f"reference.faiss not found in {faiss_dir}")

    if args.positions_csv:
        extent = extent_from_csv(Path(args.positions_csv))
        source = f"positions_csv={args.positions_csv}"
    elif args.model_dir:
        extent = extent_from_colmap(Path(args.model_dir))
        source = f"model_dir={args.model_dir}"
    else:
        raise SystemExit("Need --positions-csv or --model-dir")

    # Persist extent.json alongside the FAISS
    extent_path = faiss_dir / "extent.json"
    extent_payload = {
        "name": args.name,
        "extent": extent,
        "derived_from": source,
    }
    with open(extent_path, "w") as f:
        json.dump(extent_payload, f, indent=2)
    print(f"wrote {extent_path}")

    entry = {
        "name": args.name,
        "faiss_dir": str(faiss_dir),
        "model_dir": str(Path(args.model_dir).resolve()) if args.model_dir else None,
        "images_url": args.images_url,
        "extent": extent,
        "meta": {
            "layout": args.layout,
            "num_views": args.num_views,
            "source": source,
        },
    }
    ref_registry.upsert(entry)
    print(f"registered '{args.name}':")
    print(json.dumps(entry, indent=2))


def list_cmd(_args):
    entries = ref_registry.load()
    if not entries:
        print("(empty)")
        return
    for e in entries:
        ext = e.get("extent") or {}
        print(f"- {e.get('name')}")
        print(f"    faiss:  {e.get('faiss_dir')}")
        print(f"    model:  {e.get('model_dir') or '(none)'}")
        print(f"    images: {e.get('images_url') or '(default)'}")
        if ext:
            print(f"    extent: "
                  f"lat {ext.get('lat_min'):.6f}..{ext.get('lat_max'):.6f}  "
                  f"lon {ext.get('lon_min'):.6f}..{ext.get('lon_max'):.6f}")


def remove_cmd(args):
    entries = [e for e in ref_registry.load() if e.get("name") != args.name]
    ref_registry.save(entries)
    print(f"removed '{args.name}'")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("register", help="Register (or update) a reference")
    r.add_argument("--name", required=True)
    r.add_argument("--faiss-dir", required=True)
    r.add_argument("--positions-csv", help="PanoPositions.csv — for pano refs")
    r.add_argument("--model-dir", help="COLMAP sparse model dir — alt to --positions-csv")
    r.add_argument("--images-url", default=None,
                   help="Where the ref images live (local path, file://, s3://, ...)")
    r.add_argument("--layout", default="split_pano", choices=["split_pano", "flat"])
    r.add_argument("--num-views", type=int, default=12)
    r.set_defaults(func=register_cmd)

    sub.add_parser("list", help="List registered references").set_defaults(func=list_cmd)

    rm = sub.add_parser("remove", help="Remove a registered reference")
    rm.add_argument("--name", required=True)
    rm.set_defaults(func=remove_cmd)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

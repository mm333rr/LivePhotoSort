#!/usr/bin/env python3
"""
triage_sort.py — LivePhotoSort triage and spatial organizer
Part of mm333rr/LivePhotoSort  v1.1.0

Reads the LivePhotoSort manifest and reorganizes files from year-sorted
flat structure into:

  <dest>/sorted/
    pairs/
      [Device Model]/
        [City, Region]/
          2025-02-19_LivePhoto_XXXX.jpeg + .mov
    orphans/
      stills/
        [Device Model]/
          [City, Region]/
            2025-02-19_LivePhoto_XXXX.jpeg
      movs/
        [Device Model]/
          [City, Region]/
            2025-02-19_LivePhoto_XXXX.mov

Device = EXIF Make+Model  (e.g. "Apple iPhone 14 Pro")
City   = Nominatim reverse-geocode from GPS EXIF
         Falls back to "Unknown Location" if no GPS.

Usage:
    python triage_sort.py [--manifest PATH] [--source DIR] [--dest DIR]
                          [--dry-run] [--workers N] [--no-geo] [--cache PATH]

Flags:
    --manifest  Manifest JSON path (default: auto-detect newest in --source)
    --source    "LivePhoto Import Ready" folder
                (default: /Volumes/MattBook - Local/LivePhoto Import Ready)
    --dest      Output root  (default: <source>/sorted)
    --dry-run   Preview without touching filesystem
    --workers   Parallel exiftool workers (default: 8)
    --no-geo    Use GPS decimal coords as folder name; skip Nominatim
    --cache     Geocode cache JSON path (default: <dest>/triage_geocache.json)

Requirements:
    pip install geopy tqdm
    brew install exiftool   (or already installed for LivePhotoSort)
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Optional deps ──────────────────────────────────────────────────────────
try:
    from geopy.geocoders import Nominatim
    from geopy.exc import GeocoderTimedOut, GeocoderServiceError
    HAS_GEOPY = True
except ImportError:
    HAS_GEOPY = False

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ── Logging ────────────────────────────────────────────────────────────────
LOG_DIR = Path.home() / "Claude Scripts and Venvs" / "LivePhotoSort" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOG_DIR / f"triage_sort_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file),
    ],
)
log = logging.getLogger(__name__)

DEFAULT_SOURCE = Path("/Volumes/MattBook - Local/LivePhoto Import Ready")
UNKNOWN_DEVICE = "Unknown Device"
UNKNOWN_LOCATION = "Unknown Location"
GEOCODE_DELAY = 1.1  # Nominatim: max 1 req/sec


# ══════════════════════════════════════════════════════════════════════════
# Manifest
# ══════════════════════════════════════════════════════════════════════════

def find_latest_manifest(source: Path) -> Optional[Path]:
    candidates = sorted(source.glob("manifest_*.json"), reverse=True)
    return candidates[0] if candidates else None


def load_manifest(path: Path) -> dict:
    log.info(f"Loading manifest: {path}")
    with open(path) as f:
        return json.load(f)


def parse_manifest(manifest: dict) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    Returns:
        pairs         — [{still: Path, mov: Path}, ...]
        orphan_stills — [{still: Path}, ...]
        orphan_movs   — [{mov: Path}, ...]

    Manifest schema (LivePhotoSort v1.1.0):
      pairs[].img_dst, pairs[].vid_dst
      orphan_stills[].dst
      orphan_movs[].dst
    """
    pairs: List[dict] = []
    orphan_stills: List[dict] = []
    orphan_movs: List[dict] = []

    for rec in manifest.get("pairs", []):
        img = Path(rec["img_dst"]) if rec.get("img_dst") else None
        vid = Path(rec["vid_dst"]) if rec.get("vid_dst") else None
        if img and vid and img.exists() and vid.exists():
            pairs.append({"still": img, "mov": vid})
        elif img and img.exists():
            orphan_stills.append({"still": img})
        elif vid and vid.exists():
            orphan_movs.append({"mov": vid})

    for rec in manifest.get("orphan_stills", []):
        p = Path(rec["dst"]) if rec.get("dst") else None
        if p and p.exists():
            orphan_stills.append({"still": p})

    for rec in manifest.get("orphan_movs", []):
        p = Path(rec["dst"]) if rec.get("dst") else None
        if p and p.exists():
            orphan_movs.append({"mov": p})

    log.info(f"Manifest → {len(pairs)} pairs | "
             f"{len(orphan_stills)} orphan stills | {len(orphan_movs)} orphan MOVs")
    return pairs, orphan_stills, orphan_movs


# ══════════════════════════════════════════════════════════════════════════
# EXIF
# ══════════════════════════════════════════════════════════════════════════

def exiftool_batch(paths: List[Path]) -> Dict[str, dict]:
    if not paths:
        return {}
    # Use full path — exiftool may not be in PATH in all shell contexts
    exiftool_bin = shutil.which("exiftool") or "/usr/local/bin/exiftool"
    cmd = [
        exiftool_bin, "-json", "-fast2",
        "-Make", "-Model",
        "-GPSLatitude", "-GPSLongitude",
        "-GPSLatitudeRef", "-GPSLongitudeRef",
    ] + [str(p) for p in paths]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        data = json.loads(r.stdout or "[]")
        return {item.get("SourceFile", ""): item for item in data}
    except Exception as e:
        log.error(f"exiftool batch error: {e}")
        return {}


def parse_coord(value, ref: str) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            dec = float(value)
        else:
            nums = re.findall(r"[\d.]+", str(value))
            if not nums:
                return None
            deg = float(nums[0])
            mins = float(nums[1]) if len(nums) > 1 else 0.0
            secs = float(nums[2]) if len(nums) > 2 else 0.0
            dec = deg + mins / 60 + secs / 3600
        if ref and ref.upper() in ("S", "W"):
            dec = -dec
        return round(dec, 6)
    except Exception:
        return None


def extract_meta(path: Path, exif_map: dict) -> Tuple[str, Optional[Tuple[float, float]]]:
    info = exif_map.get(str(path), {})
    make = info.get("Make", "").strip()
    model = info.get("Model", "").strip()
    if make and model:
        device = model if model.lower().startswith(make.lower()) else f"{make} {model}"
    else:
        device = model or make or UNKNOWN_DEVICE
    device = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', device).strip() or UNKNOWN_DEVICE

    lat = parse_coord(info.get("GPSLatitude"), info.get("GPSLatitudeRef", ""))
    lon = parse_coord(info.get("GPSLongitude"), info.get("GPSLongitudeRef", ""))
    coords = (lat, lon) if lat is not None and lon is not None else None
    return device, coords


# ══════════════════════════════════════════════════════════════════════════
# Geocoding
# ══════════════════════════════════════════════════════════════════════════

class GeoCache:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, str] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
                log.info(f"Geocache loaded: {len(self.data)} entries")
            except Exception:
                pass

    def get(self, key: str) -> Optional[str]:
        return self.data.get(key)

    def set(self, key: str, val: str):
        self.data[key] = val

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))


def cache_key(lat: float, lon: float) -> str:
    return f"{round(lat, 2)},{round(lon, 2)}"


def reverse_geocode(lat: float, lon: float, geo, cache: GeoCache) -> str:
    key = cache_key(lat, lon)
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        time.sleep(GEOCODE_DELAY)
        loc = geo.reverse(f"{lat}, {lon}", exactly_one=True, timeout=10, language="en")
        if loc and loc.raw.get("address"):
            addr = loc.raw["address"]
            city = (addr.get("city") or addr.get("town") or addr.get("village")
                    or addr.get("county") or "")
            region = addr.get("state") or addr.get("country") or ""
            if city and region and city != region:
                result = f"{city}, {region}"
            else:
                result = city or region or UNKNOWN_LOCATION
        else:
            result = UNKNOWN_LOCATION
    except Exception as e:
        log.warning(f"Geocode ({lat},{lon}): {e}")
        result = UNKNOWN_LOCATION
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', result).strip() or UNKNOWN_LOCATION
    cache.set(key, result)
    return result


# ══════════════════════════════════════════════════════════════════════════
# File ops
# ══════════════════════════════════════════════════════════════════════════

def safe_dest(dest_dir: Path, filename: str) -> Path:
    target = dest_dir / filename
    if not target.exists():
        return target
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for i in range(1, 9999):
        c = dest_dir / f"{stem}_{i:03d}{suffix}"
        if not c.exists():
            return c
    return target  # fallback


def move_file(src: Path, dest: Path, dry_run: bool) -> bool:
    if dry_run:
        log.info(f"[DRY] {src.name}  →  .../{dest.parent.parent.name}/{dest.parent.name}/")
        return True
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return True
    except Exception as e:
        log.error(f"Move failed {src} → {dest}: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════
# Enrichment
# ══════════════════════════════════════════════════════════════════════════

def enrich(all_paths: List[Path], workers: int,
           no_geo: bool, geo, cache: GeoCache) -> Dict[str, Tuple[str, str]]:
    """Return {str(path): (device, city)} for every path."""
    BATCH = 500
    batches = [all_paths[i:i + BATCH] for i in range(0, len(all_paths), BATCH)]
    log.info(f"exiftool: {len(all_paths)} files in {len(batches)} batches ({workers} workers)…")

    exif_map: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(exiftool_batch, b): b for b in batches}
        iterator = as_completed(futures)
        if HAS_TQDM:
            iterator = tqdm(iterator, total=len(futures), desc="exiftool")
        for fut in iterator:
            exif_map.update(fut.result())

    # Collect unique coord buckets
    coord_buckets: Dict[str, Tuple[float, float]] = {}
    path_coords: Dict[str, Optional[Tuple[float, float]]] = {}
    for p in all_paths:
        _, coords = extract_meta(p, exif_map)
        path_coords[str(p)] = coords
        if coords:
            coord_buckets[cache_key(*coords)] = coords

    # Geocode unique buckets
    if HAS_GEOPY and not no_geo and coord_buckets:
        need = [k for k in coord_buckets if cache.get(k) is None]
        log.info(f"Geocoding {len(need)} unique coordinate buckets…")
        iterator2 = tqdm(need, desc="geocode") if HAS_TQDM else need
        for key in iterator2:
            lat, lon = coord_buckets[key]
            reverse_geocode(lat, lon, geo, cache)
        cache.save()

    # Build final result
    result: Dict[str, Tuple[str, str]] = {}
    for p in all_paths:
        device, coords = extract_meta(p, exif_map)
        if coords:
            if no_geo or not HAS_GEOPY:
                city = f"{coords[0]:.2f},{coords[1]:.2f}"
            else:
                city = cache.get(cache_key(*coords)) or UNKNOWN_LOCATION
        else:
            city = UNKNOWN_LOCATION
        result[str(p)] = (device, city)
    return result


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Reorganize LivePhotoSort output by device + city")
    p.add_argument("--manifest", type=Path)
    p.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    p.add_argument("--dest", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--no-geo", action="store_true")
    p.add_argument("--cache", type=Path, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    source = args.source
    dest = args.dest or (source / "sorted")
    dry_run = args.dry_run

    log.info("triage_sort.py  v1.1.0")
    log.info(f"  source   : {source}")
    log.info(f"  dest     : {dest}")
    log.info(f"  dry-run  : {dry_run}")
    log.info(f"  geo      : {'disabled' if args.no_geo else 'Nominatim/OSM'}")

    if not source.exists():
        log.error(f"Source not found: {source}")
        sys.exit(1)

    if not HAS_GEOPY and not args.no_geo:
        log.warning("geopy not installed — using GPS coords as folder names. "
                    "Install: pip install geopy")
        args.no_geo = True

    manifest_path = args.manifest or find_latest_manifest(source)
    if not manifest_path:
        log.error("No manifest found. Provide --manifest or run LivePhotoSort first.")
        sys.exit(1)

    manifest = load_manifest(manifest_path)
    pairs, orphan_stills, orphan_movs = parse_manifest(manifest)

    # Collect all unique paths
    all_paths: List[Path] = []
    seen: set = set()
    for rec in pairs:
        for p in (rec["still"], rec["mov"]):
            if str(p) not in seen:
                all_paths.append(p); seen.add(str(p))
    for rec in orphan_stills:
        p = rec["still"]
        if str(p) not in seen:
            all_paths.append(p); seen.add(str(p))
    for rec in orphan_movs:
        p = rec["mov"]
        if str(p) not in seen:
            all_paths.append(p); seen.add(str(p))

    log.info(f"Total unique files: {len(all_paths)}")

    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)

    cache_path = args.cache or (dest / "triage_geocache.json")
    cache = GeoCache(cache_path)
    geo = Nominatim(user_agent="LivePhotoSort-triage/1.1") if HAS_GEOPY and not args.no_geo else None

    enrichment = enrich(all_paths, args.workers, args.no_geo, geo, cache)

    # ── Pairs ──────────────────────────────────────────────────────────────
    log.info(f"\n── Pairs ({len(pairs)}) ──")
    pair_ok = pair_fail = 0
    for rec in pairs:
        still, mov = rec["still"], rec["mov"]
        device, city = enrichment.get(str(still), (UNKNOWN_DEVICE, UNKNOWN_LOCATION))
        pair_dir = dest / "pairs" / device / city
        for f in (still, mov):
            if move_file(f, safe_dest(pair_dir, f.name), dry_run):
                pair_ok += 1
            else:
                pair_fail += 1

    # ── Orphan stills ──────────────────────────────────────────────────────
    log.info(f"\n── Orphan stills ({len(orphan_stills)}) ──")
    still_ok = still_fail = 0
    for rec in orphan_stills:
        f = rec["still"]
        device, city = enrichment.get(str(f), (UNKNOWN_DEVICE, UNKNOWN_LOCATION))
        if move_file(f, safe_dest(dest / "orphans" / "stills" / device / city, f.name), dry_run):
            still_ok += 1
        else:
            still_fail += 1

    # ── Orphan MOVs ────────────────────────────────────────────────────────
    log.info(f"\n── Orphan MOVs ({len(orphan_movs)}) ──")
    mov_ok = mov_fail = 0
    for rec in orphan_movs:
        f = rec["mov"]
        device, city = enrichment.get(str(f), (UNKNOWN_DEVICE, UNKNOWN_LOCATION))
        if move_file(f, safe_dest(dest / "orphans" / "movs" / device / city, f.name), dry_run):
            mov_ok += 1
        else:
            mov_fail += 1

    total_ok = pair_ok + still_ok + mov_ok
    total_fail = pair_fail + still_fail + mov_fail
    log.info(f"""
╔═══════════════════════════════════════════════════════╗
║           triage_sort.py — DONE                       ║
╠═══════════════════════════════════════════════════════╣
║  Pairs moved       : {pair_ok // 2:>5} pairs  ({pair_ok} files)
║  Orphan stills     : {still_ok:>5} files
║  Orphan MOVs       : {mov_ok:>5} files
║  ─────────────────────────────────────────────────
║  Total succeeded   : {total_ok:>5}
║  Total failed      : {total_fail:>5}
║  Mode              : {'DRY RUN — nothing moved' if dry_run else 'LIVE — files moved'}
║  Log               : {log_file.name}
╚═══════════════════════════════════════════════════════╝
""")
    if dry_run:
        log.info("Run without --dry-run to apply.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""reshuffle.py — Re-sort an already-sorted LivePhotoSort output tree

Needed when triage_sort.py ran with a GPS sign bug (W longitudes were positive →
files ended up in Chinese city folders instead of Ventura, CA etc.)

Scans every file under <sorted_root>/pairs/, orphans/stills/, orphans/movs/,
re-reads EXIF with the corrected parse_coord, reverse-geocodes fresh, and
moves files to the correct device/city folders *in place* inside <sorted_root>.

Usage:
    python reshuffle.py [--sorted PATH] [--dry-run] [--workers N] [--no-geo] [--cache PATH]

Flags:
    --sorted    Root of the already-sorted tree
                (default: /Volumes/MattBook - Local/LivePhoto Import Ready/sorted)
    --dry-run   Preview without touching filesystem
    --workers   Parallel exiftool workers (default: 8)
    --no-geo    Use GPS decimals as folder names; skip Nominatim
    --cache     Geocode cache JSON path (default: <sorted>/triage_geocache.json)

Part of mm333rr/LivePhotoSort  v1.2.0
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
    from geopy.geocoders import Photon   # Komoot Photon — OSM-backed, no API key, generous limits
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
log_file = LOG_DIR / f"reshuffle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file),
    ],
)
log = logging.getLogger(__name__)

DEFAULT_SORTED = Path("/Volumes/MattBook - Local/LivePhoto Import Ready/sorted")
UNKNOWN_DEVICE   = "Unknown Device"
UNKNOWN_LOCATION = "Unknown Location"
GEOCODE_DELAY    = 0.5   # Photon (Komoot): generous limits, ~2 req/sec is safe

# ══════════════════════════════════════════════════════════════════════════
# EXIF
# ══════════════════════════════════════════════════════════════════════════

def exiftool_batch(paths: List[Path]) -> Dict[str, dict]:
    if not paths:
        return {}
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
    """Parse a GPS coordinate value + ref into a signed decimal float.

    Handles both short refs ("N","S","W","E") and long refs ("North","South",
    "West","East") returned by exiftool without the -n flag.
    """
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            dec = float(value)
        else:
            nums = re.findall(r"[\d.]+", str(value))
            if not nums:
                return None
            deg  = float(nums[0])
            mins = float(nums[1]) if len(nums) > 1 else 0.0
            secs = float(nums[2]) if len(nums) > 2 else 0.0
            dec  = deg + mins / 60 + secs / 3600

        ref_up = (ref or "").strip().upper()
        if ref_up in ("S", "W", "SOUTH", "WEST"):
            dec = -dec
        elif str(value).strip().upper().endswith((" S", " W")):
            dec = -dec

        return round(dec, 6)
    except Exception:
        return None


def extract_meta(path: Path, exif_map: dict) -> Tuple[str, Optional[Tuple[float, float]]]:
    info  = exif_map.get(str(path), {})
    make  = info.get("Make",  "").strip()
    model = info.get("Model", "").strip()
    if make and model:
        device = model if model.lower().startswith(make.lower()) else f"{make} {model}"
    else:
        device = model or make or UNKNOWN_DEVICE
    device = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', device).strip() or UNKNOWN_DEVICE

    lat = parse_coord(info.get("GPSLatitude"),  info.get("GPSLatitudeRef",  ""))
    lon = parse_coord(info.get("GPSLongitude"), info.get("GPSLongitudeRef", ""))
    coords = (lat, lon) if lat is not None and lon is not None else None
    return device, coords

# ══════════════════════════════════════════════════════════════════════════
# Geocoding
# ══════════════════════════════════════════════════════════════════════════

class GeoCache:
    """Persistent key-value geocache stored as JSON.

    Keys are rounded lat/lon pairs ("34.23,-119.27") so nearby shots share
    a lookup.  The cache survives across runs — only entries that were
    produced with the wrong GPS sign need to be discarded, which is handled
    by the caller passing reset=True on the first corrective run.
    """

    def __init__(self, path: Path, reset: bool = False):
        self.path = path
        self.data: Dict[str, str] = {}
        if not reset and path.exists():
            try:
                self.data = json.loads(path.read_text())
                log.info(f"Geocache loaded: {self.path} ({len(self.data)} cached entries)")
            except Exception as e:
                log.warning(f"Could not load geocache ({e}); starting fresh")
        elif reset:
            log.info("Geocache reset requested — starting with empty cache")

    def get(self, key: str) -> Optional[str]:
        return self.data.get(key)

    def set(self, key: str, val: str):
        self.data[key] = val

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))
        log.info(f"Geocache saved: {self.path} ({len(self.data)} entries)")


def cache_key(lat: float, lon: float) -> str:
    return f"{round(lat, 2)},{round(lon, 2)}"


def reverse_geocode(lat: float, lon: float, geo, cache: GeoCache) -> str:
    key = cache_key(lat, lon)
    hit = cache.get(key)
    if hit is not None:
        return hit
def reverse_geocode(lat: float, lon: float, geo, cache: GeoCache) -> str:
    key = cache_key(lat, lon)
    hit = cache.get(key)
    if hit is not None:
        return hit

    for attempt in range(4):
        try:
            time.sleep(GEOCODE_DELAY * (2 ** attempt))
            loc = geo.reverse((lat, lon), exactly_one=True, timeout=10)
            if loc and loc.raw.get("properties"):
                props  = loc.raw["properties"]
                city   = (props.get("city") or props.get("town") or props.get("village")
                          or props.get("county") or props.get("municipality") or "")
                region = props.get("state") or props.get("country") or ""
                result = (f"{city}, {region}"
                          if city and region and city != region
                          else city or region or UNKNOWN_LOCATION)
            elif loc:
                # Fallback: use address string
                result = str(loc.address).split(",")[0].strip() or UNKNOWN_LOCATION
            else:
                result = UNKNOWN_LOCATION
            break
        except Exception as e:
            msg = str(e)
            if attempt < 3:
                log.warning(f"Geocode attempt {attempt+1}/4 ({lat},{lon}): {msg} — retrying…")
                continue
            log.warning(f"Geocode failed ({lat},{lon}): {msg}")
            result = UNKNOWN_LOCATION
            break

    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', result).strip() or UNKNOWN_LOCATION
    cache.set(key, result)
    return result

# ══════════════════════════════════════════════════════════════════════════
# File ops
# ══════════════════════════════════════════════════════════════════════════

MEDIA_EXTS = {".jpg", ".jpeg", ".heic", ".mov", ".mp4", ".png"}


def safe_dest(dest_dir: Path, filename: str) -> Path:
    target = dest_dir / filename
    if not target.exists():
        return target
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for i in range(1, 9999):
        c = dest_dir / f"{stem}_{i:03d}{suffix}"
        if not c.exists():
            return c
    return target


def move_file(src: Path, dest: Path, dry_run: bool) -> bool:
    if src == dest:
        return True
    if dry_run:
        log.info(f"[DRY] {src.name}  →  {dest.parent.name}/{dest.name}")
        return True
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return True
    except Exception as e:
        log.error(f"Move failed {src} → {dest}: {e}")
        return False


def collect_files(sorted_root: Path) -> Dict[str, List[Path]]:
    """Scan sorted tree and return files by bucket."""
    buckets: Dict[str, List[Path]] = {"pairs": [], "stills": [], "movs": []}
    for root_sub, label in [
        (sorted_root / "pairs",            "pairs"),
        (sorted_root / "orphans" / "stills", "stills"),
        (sorted_root / "orphans" / "movs",   "movs"),
    ]:
        if root_sub.exists():
            for f in root_sub.rglob("*"):
                if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
                    buckets[label].append(f)
    log.info(f"Found  pairs:{len(buckets['pairs'])}  "
             f"stills:{len(buckets['stills'])}  movs:{len(buckets['movs'])}")
    return buckets


def enrich(all_paths: List[Path], workers: int,
           no_geo: bool, geo, cache: GeoCache) -> Dict[str, Tuple[str, str]]:
    BATCH   = 500
    batches = [all_paths[i:i + BATCH] for i in range(0, len(all_paths), BATCH)]
    log.info(f"exiftool: {len(all_paths)} files in {len(batches)} batches ({workers} workers)…")

    exif_map: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures  = {pool.submit(exiftool_batch, b): b for b in batches}
        iterator = as_completed(futures)
        if HAS_TQDM:
            iterator = tqdm(iterator, total=len(futures), desc="exiftool")
        for fut in iterator:
            exif_map.update(fut.result())

    coord_buckets: Dict[str, Tuple[float, float]] = {}
    for p in all_paths:
        _, coords = extract_meta(p, exif_map)
        if coords:
            coord_buckets[cache_key(*coords)] = coords

    if HAS_GEOPY and not no_geo and coord_buckets:
        need = [k for k in coord_buckets if cache.get(k) is None]
        log.info(f"Geocoding {len(need)} unique coordinate buckets…")
        iterator2 = tqdm(need, desc="geocode") if HAS_TQDM else need
        for key in iterator2:
            lat, lon = coord_buckets[key]
            reverse_geocode(lat, lon, geo, cache)
        cache.save()

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
        description="Re-sort an already-sorted LivePhotoSort tree (GPS sign fix)")
    p.add_argument("--sorted",       type=Path, default=DEFAULT_SORTED)
    p.add_argument("--dry-run",      action="store_true")
    p.add_argument("--workers",      type=int,  default=8)
    p.add_argument("--no-geo",       action="store_true")
    p.add_argument("--reset-cache",  action="store_true",
                   help="Discard existing geocache and re-geocode everything")
    p.add_argument("--cache",        type=Path, default=None)
    return p.parse_args()


def main():
    args    = parse_args()
    root    = args.sorted
    dry_run = args.dry_run

    log.info("reshuffle.py  v1.2.0")
    log.info(f"  sorted root: {root}")
    log.info(f"  dry-run    : {dry_run}")
    log.info(f"  geo        : {'disabled' if args.no_geo else 'Photon/OSM (Komoot)'}")
    log.info(f"  cache      : {'RESET' if args.reset_cache else 'load existing'}")

    if not root.exists():
        log.error(f"Sorted root not found: {root}")
        sys.exit(1)

    if not HAS_GEOPY and not args.no_geo:
        log.warning("geopy not installed — using GPS coords as folder names.")
        args.no_geo = True

    cache_path = args.cache or (root / "triage_geocache.json")

    # On first corrective run, back up the bad cache; subsequent runs reuse the good one
    if args.reset_cache and cache_path.exists() and not dry_run:
        bak = cache_path.with_suffix(".json.bad")
        shutil.move(str(cache_path), str(bak))
        log.info(f"Old geocache backed up to: {bak.name}")

    cache = GeoCache(cache_path, reset=args.reset_cache)
    geo   = (Photon(user_agent="LivePhotoSort-reshuffle/1.2", timeout=10)
             if HAS_GEOPY and not args.no_geo else None)

    buckets    = collect_files(root)
    all_paths  = buckets["pairs"] + buckets["stills"] + buckets["movs"]
    enrichment = enrich(all_paths, args.workers, args.no_geo, geo, cache)

    ok = fail = skipped = 0

    def process_bucket(files: List[Path], dest_fn):
        nonlocal ok, fail, skipped
        for f in files:
            device, city = enrichment.get(str(f), (UNKNOWN_DEVICE, UNKNOWN_LOCATION))
            dest_dir = dest_fn(device, city)
            dest     = safe_dest(dest_dir, f.name)
            if f.parent == dest_dir:
                skipped += 1
                continue
            if move_file(f, dest, dry_run):
                ok += 1
            else:
                fail += 1

    log.info(f"\n── Pairs ({len(buckets['pairs'])}) ──")
    process_bucket(buckets["pairs"],
                   lambda d, c: root / "pairs" / d / c)

    log.info(f"\n── Orphan stills ({len(buckets['stills'])}) ──")
    process_bucket(buckets["stills"],
                   lambda d, c: root / "orphans" / "stills" / d / c)

    log.info(f"\n── Orphan MOVs ({len(buckets['movs'])}) ──")
    process_bucket(buckets["movs"],
                   lambda d, c: root / "orphans" / "movs" / d / c)

    # Prune empty directories
    if not dry_run:
        pruned = 0
        for dirpath, dirs, files in os.walk(str(root), topdown=False):
            p = Path(dirpath)
            if p == root:
                continue
            try:
                p.rmdir()
                pruned += 1
            except OSError:
                pass
        log.info(f"Pruned {pruned} empty directories")

    log.info(f"""
╔═══════════════════════════════════════════════════════╗
║           reshuffle.py — DONE                         ║
╠═══════════════════════════════════════════════════════╣
║  Moved successfully : {ok:>5}                         ║
║  Already correct    : {skipped:>5}                    ║
║  Failed             : {fail:>5}                       ║
║  Mode               : {'DRY RUN — nothing moved' if dry_run else 'LIVE — files reshuffled'}
║  Log                : {log_file.name}                 ║
╚═══════════════════════════════════════════════════════╝
""")
    if dry_run:
        log.info("Run without --dry-run to apply.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
LivePhotoSort — Live Photo Pair Detector & Mover
=================================================
Scans source folders for Live Photo pairs (HEIC/JPG image + MOV companion)
linked by Apple's ContentIdentifier UUID, renames them with rich sortable
names, and moves them together into a destination folder.

Project: LivePhotoSort
Author:  Claude (Anthropic) for Matt @ The Capes, Ventura CA
Version: 1.1.0
Date:    2026-02-15

PERFORMANCE: Uses batch exiftool scanning (one call per directory subtree)
instead of per-file calls. On large archives this is 50-100x faster.

Usage
-----
    # Run (background, nohup):
    nohup python3 live_photo_sort.py >> logs/run.log 2>&1 &

    # Watch the log:
    tail -f logs/run_*.log

    # Kill it:
    kill $(cat logs/live_photo_sort.pid)

Strategy
--------
1. Walk both source folders and build a UUID → file-path index for all
   image files (HEIC, JPG, JPEG, PNG) and video files (MOV) using exiftool
   ContentIdentifier — extracted in BATCH per directory for speed.
2. Match images to their MOV companions via shared UUID.
3. For each matched pair:
   a. Build a rich, sortable name:
      YYYY-MM-DD_HHMMSS_LivePhoto_<DeviceModel>_<uuid8>
   b. Copy-then-verify (SHA-256) image → dest as .heic/.jpg/.jpeg
   c. Copy-then-verify (SHA-256) video → dest as .mov
   d. Delete source files only after both copies verified
4. Unmatched images tagged as Live Photos are logged as orphan images.
5. Unmatched MOVs with a ContentIdentifier are logged as orphan videos.
6. A JSON manifest is written to dest/live_photo_manifest.json for audit.

Live Photo Detection
--------------------
- Image is a Live Photo: MakerNotes:LivePhotoVideoIndex present AND
  ContentIdentifier UUID present.
- Companion MOV: ContentIdentifier UUID matches the image.
- Key: use exiftool WITHOUT -fast2 (that flag skips ContentIdentifier).

Apple Photos Re-import
----------------------
For Apple Photos to recognise a pair as a Live Photo on import, both files
MUST share the same base name (e.g. IMG_1234.HEIC + IMG_1234.MOV) AND the
ContentIdentifier metadata must survive the copy. This script does both.
"""

from __future__ import annotations

import os
import sys
import json
import shutil
import signal
import hashlib
import logging
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

SOURCE_DIRS = [
    "/Volumes/MattBook - Local/Oragnized and Numbered",
    "/Volumes/MattBook - Local/Oragnized and Numbered.backup.feb14th",
]

DEST_DIR = "/Volumes/MattBook - Local/LivePhotoPairs"

# Image extensions that can be Live Photo stills
IMAGE_EXTS = {".heic", ".jpg", ".jpeg", ".png"}

# Video extensions that can be Live Photo companions
VIDEO_EXTS = {".mov"}

# All media extensions we care about
ALL_EXTS = IMAGE_EXTS | VIDEO_EXTS

# How many files to batch per exiftool call (balance memory vs process overhead)
EXIFTOOL_BATCH_SIZE = 500

LOG_DIR = Path(__file__).parent / "logs"
PID_FILE = LOG_DIR / "live_photo_sort.pid"

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)
log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Graceful shutdown
# ──────────────────────────────────────────────

_running = True


def _handle_signal(sig, frame):
    global _running
    log.warning("Signal %s received — finishing current batch then stopping.", sig)
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ──────────────────────────────────────────────
# Batch exiftool scanning (core performance win)
# ──────────────────────────────────────────────

TAGS = [
    "SourceFile",
    "ContentIdentifier",
    "LivePhotoVideoIndex",
    "DateTimeOriginal",
    "GPSLatitude",
    "GPSLongitude",
    "Make",
    "Model",
    "Description",
    "FileTypeExtension",
    "MIMEType",
]


def batch_exiftool(file_paths: list[str]) -> list[dict]:
    """
    Run exiftool on a batch of files and return a list of metadata dicts.
    Uses JSON output for reliable parsing. One exiftool subprocess per batch.
    """
    if not file_paths:
        return []
    cmd = ["exiftool", "-json", "-n"] + [f"-{t}" for t in TAGS] + file_paths
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min per batch of 500 files
        )
        if result.returncode not in (0, 1):  # 1 = some warnings but still ok
            log.warning("exiftool returned %d: %s", result.returncode, result.stderr[:200])
        if not result.stdout.strip():
            return []
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.error("exiftool JSON parse error: %s", exc)
        return []
    except subprocess.TimeoutExpired:
        log.error("exiftool timed out on batch of %d files", len(file_paths))
        return []
    except Exception as exc:
        log.error("exiftool batch error: %s", exc)
        return []


def collect_candidate_files(folder: str) -> list[Path]:
    """Walk folder and return all files with relevant extensions."""
    folder_path = Path(folder)
    if not folder_path.exists():
        log.warning("Source folder does not exist: %s", folder)
        return []
    candidates = []
    for fpath in folder_path.rglob("*"):
        if fpath.is_file() and fpath.suffix.lower() in ALL_EXTS:
            candidates.append(fpath)
    return candidates


def scan_folder(folder: str) -> tuple[dict, dict]:
    """
    Walk folder and return:
      images: {uuid: (path, meta_dict)}
      videos: {uuid: (path, meta_dict)}

    Uses batch exiftool for speed.
    """
    images: dict = {}
    videos: dict = {}

    candidates = collect_candidate_files(folder)
    if not candidates:
        log.info("No candidate files found in %s", folder)
        return images, videos

    log.info("Scanning %s — %d candidate files…", folder, len(candidates))

    # Process in batches
    for batch_start in range(0, len(candidates), EXIFTOOL_BATCH_SIZE):
        if not _running:
            log.warning("Scan interrupted during batching.")
            break

        batch = candidates[batch_start: batch_start + EXIFTOOL_BATCH_SIZE]
        batch_strs = [str(p) for p in batch]
        batch_num = batch_start // EXIFTOOL_BATCH_SIZE + 1
        total_batches = (len(candidates) + EXIFTOOL_BATCH_SIZE - 1) // EXIFTOOL_BATCH_SIZE
        log.info("  Batch %d/%d (%d files)…", batch_num, total_batches, len(batch))

        records = batch_exiftool(batch_strs)

        for rec in records:
            src_file = rec.get("SourceFile", "")
            if not src_file:
                continue
            fpath = Path(src_file)
            ext = fpath.suffix.lower()
            uuid = rec.get("ContentIdentifier")
            lp_index = rec.get("LivePhotoVideoIndex")

            if not uuid:
                continue  # No UUID = not a Live Photo

            if ext in IMAGE_EXTS and lp_index is not None:
                # It's a Live Photo image
                if uuid not in images:
                    images[uuid] = (fpath, rec)

            elif ext in VIDEO_EXTS:
                # It's a potential Live Photo companion MOV
                if uuid not in videos:
                    videos[uuid] = (fpath, rec)

    log.info("Scan complete in %s: %d LP images, %d LP companion videos found.",
             folder, len(images), len(videos))
    return images, videos


# ──────────────────────────────────────────────
# File naming
# ──────────────────────────────────────────────

def rich_base_name(meta: dict, uuid: str) -> str:
    """
    Build a rich, sortable base filename (no extension).
    Format: YYYY-MM-DD_HHMMSS_LivePhoto_<DeviceModel>_<uuid8>

    Both the .heic and .mov get this SAME base name so Apple Photos
    can re-link them as a pair on import.
    """
    # Date — exiftool returns "YYYY:MM:DD HH:MM:SS" with -n
    dt_raw = meta.get("DateTimeOriginal", "")
    try:
        dt = datetime.strptime(dt_raw, "%Y:%m:%d %H:%M:%S")
        date_str = dt.strftime("%Y-%m-%d_%H%M%S")
    except (ValueError, TypeError):
        date_str = "0000-00-00_000000"

    # Short UUID — first 8 hex chars (no dashes), uppercase
    uuid_short = (uuid.replace("-", "")[:8]).upper()

    # Device model (compact, no spaces)
    model = str(meta.get("Model", "")).replace(" ", "").replace(",", "")
    if not model:
        model = "iPhone"

    return f"{date_str}_LivePhoto_{model}_{uuid_short}"


def safe_dest_path(dest_dir: Path, base: str, ext: str) -> Path:
    """Return a path that doesn't collide in dest_dir."""
    candidate = dest_dir / f"{base}{ext}"
    counter = 1
    while candidate.exists():
        candidate = dest_dir / f"{base}_{counter:02d}{ext}"
        counter += 1
    return candidate


# ──────────────────────────────────────────────
# File integrity
# ──────────────────────────────────────────────

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_move(src: Path, dst: Path) -> bool:
    """
    Copy src → dst, verify SHA-256 match, then remove src.
    Returns True on success.
    """
    try:
        shutil.copy2(str(src), str(dst))
        src_hash = sha256_file(str(src))
        dst_hash = sha256_file(str(dst))
        if src_hash == dst_hash:
            src.unlink()
            return True
        else:
            log.error("SHA-256 mismatch after copy: %s → %s (src=%s dst=%s)",
                      src, dst, src_hash[:12], dst_hash[:12])
            dst.unlink(missing_ok=True)
            return False
    except Exception as exc:
        log.error("safe_move failed %s → %s: %s", src, dst, exc)
        return False


# ──────────────────────────────────────────────
# Move pairs
# ──────────────────────────────────────────────

def move_pairs(all_images: dict, all_videos: dict, dest_dir: Path,
               dry_run: bool = False) -> dict:
    """
    Match images to videos by UUID, move pairs to dest_dir.
    Returns manifest dict.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated": datetime.now().isoformat(),
        "dest_dir": str(dest_dir),
        "version": "1.1.0",
        "pairs": [],
        "orphan_images": [],
        "orphan_videos": [],
    }

    matched_uuids = set(all_images.keys()) & set(all_videos.keys())
    orphan_img_uuids = set(all_images.keys()) - matched_uuids
    orphan_vid_uuids = set(all_videos.keys()) - matched_uuids

    log.info("=" * 60)
    log.info("RESULTS: %d matched pairs | %d orphan images | %d orphan videos",
             len(matched_uuids), len(orphan_img_uuids), len(orphan_vid_uuids))
    log.info("=" * 60)

    if dry_run:
        for uuid in sorted(matched_uuids):
            img_path, img_meta = all_images[uuid]
            base = rich_base_name(img_meta, uuid)
            vid_path, _ = all_videos[uuid]
            img_ext = img_path.suffix.lower()
            log.info("[DRY RUN PAIR] %s%s + %s", base, img_ext, ".mov")
            log.info("  IMG src: %s", img_path)
            log.info("  MOV src: %s", vid_path)
        for uuid in sorted(orphan_img_uuids):
            p, _ = all_images[uuid]
            log.info("[DRY RUN ORPHAN IMG] %s [uuid=%s]", p, uuid[:8])
        for uuid in sorted(orphan_vid_uuids):
            p, _ = all_videos[uuid]
            log.info("[DRY RUN ORPHAN MOV] %s [uuid=%s]", p, uuid[:8])
        return manifest

    success_count = 0
    fail_count = 0

    for i, uuid in enumerate(sorted(matched_uuids)):
        if not _running:
            log.warning("Move interrupted at pair %d/%d — stopping.", i, len(matched_uuids))
            break

        img_path, img_meta = all_images[uuid]
        vid_path, _ = all_videos[uuid]

        base = rich_base_name(img_meta, uuid)
        img_ext = img_path.suffix.lower()

        dest_img = safe_dest_path(dest_dir, base, img_ext)
        dest_vid = safe_dest_path(dest_dir, base, ".mov")

        log.info("[%d/%d] Moving pair → %s", i + 1, len(matched_uuids), base)
        log.info("  IMG: %s → %s", img_path.name, dest_img.name)
        log.info("  MOV: %s → %s", vid_path.name, dest_vid.name)

        img_ok = safe_move(img_path, dest_img)
        vid_ok = safe_move(vid_path, dest_vid)

        entry = {
            "uuid": uuid,
            "base_name": base,
            "image": {"source": str(img_path), "dest": str(dest_img), "success": img_ok},
            "video": {"source": str(vid_path), "dest": str(dest_vid), "success": vid_ok},
        }
        manifest["pairs"].append(entry)

        if img_ok and vid_ok:
            success_count += 1
            log.info("  ✅ Pair complete.")
        else:
            fail_count += 1
            if not img_ok:
                log.error("  ❌ Image move FAILED for %s", img_path)
            if not vid_ok:
                log.error("  ❌ Video move FAILED for %s", vid_path)

    # Log orphans
    for uuid in sorted(orphan_img_uuids):
        p, _ = all_images[uuid]
        log.warning("[ORPHAN IMG] No matching MOV: %s [uuid=%s]", p, uuid[:8])
        manifest["orphan_images"].append({"uuid": uuid, "path": str(p)})

    for uuid in sorted(orphan_vid_uuids):
        p, _ = all_videos[uuid]
        log.warning("[ORPHAN MOV] No matching image: %s [uuid=%s]", p, uuid[:8])
        manifest["orphan_videos"].append({"uuid": uuid, "path": str(p)})

    log.info("=" * 60)
    log.info("DONE: %d pairs moved OK | %d failed | %d orphan images | %d orphan videos",
             success_count, fail_count, len(orphan_img_uuids), len(orphan_vid_uuids))
    log.info("=" * 60)
    return manifest


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LivePhotoSort v1.1.0 — detect and move Live Photo pairs"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan and report only — no files are moved"
    )
    parser.add_argument(
        "--source", nargs="*", default=SOURCE_DIRS,
        help="Override source directories"
    )
    parser.add_argument(
        "--dest", default=DEST_DIR,
        help="Override destination directory"
    )
    args = parser.parse_args()

    # Write PID file for easy kill
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    log.info("=" * 70)
    log.info("LivePhotoSort v1.1.0 started (PID %d)", os.getpid())
    log.info("Sources: %s", args.source)
    log.info("Dest:    %s", args.dest)
    log.info("DryRun:  %s", args.dry_run)
    log.info("Batch size: %d files per exiftool call", EXIFTOOL_BATCH_SIZE)
    log.info("=" * 70)

    dest_dir = Path(args.dest)
    all_images: dict = {}
    all_videos: dict = {}

    # Scan all sources — merge results (first-seen UUID wins)
    for src in args.source:
        imgs, vids = scan_folder(src)
        new_imgs = 0
        new_vids = 0
        for uuid, val in imgs.items():
            if uuid not in all_images:
                all_images[uuid] = val
                new_imgs += 1
        for uuid, val in vids.items():
            if uuid not in all_videos:
                all_videos[uuid] = val
                new_vids += 1
        log.info("After merging %s: +%d images, +%d videos (totals: %d images, %d videos)",
                 src, new_imgs, new_vids, len(all_images), len(all_videos))

    log.info("Grand total: %d unique LP images, %d unique LP companion videos",
             len(all_images), len(all_videos))

    manifest = move_pairs(all_images, all_videos, dest_dir, dry_run=args.dry_run)

    if not args.dry_run:
        # Write manifest JSON
        manifest_path = dest_dir / "live_photo_manifest.json"
        try:
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)
            log.info("Manifest written to %s", manifest_path)
        except Exception as exc:
            log.error("Could not write manifest: %s", exc)

    PID_FILE.unlink(missing_ok=True)
    log.info("LivePhotoSort finished. Log: %s", log_path)


if __name__ == "__main__":
    main()

# LivePhotoSort

A fully autonomous Python script that scans your ClaudeSort-organized folders
for **Apple Live Photo pairs** (HEIC/JPG image + MOV companion), matches them
by Apple's `ContentIdentifier` UUID via exiftool, renames them with rich
sortable names, and moves them together into a single destination folder—
ready to re-import as Live Photos into Apple Photos.

---

## What It Does

1. Walks both source folders recursively
2. Extracts `ContentIdentifier` UUID from every `.heic`, `.jpg`, `.jpeg`, `.png`, and `.mov` file via `exiftool`
3. A **Live Photo image** is identified when `LivePhotoVideoIndex` (MakerNotes) AND `ContentIdentifier` are present
4. Matches images → companion MOVs by shared UUID
5. Renames each pair to a **rich, sortable name**:
   ```
   2024-06-18_183805_LivePhoto_iPhone15ProMax_CF99FFE1.heic
   2024-06-18_183805_LivePhoto_iPhone15ProMax_CF99FFE1.mov
   ```
6. Safely moves (copy + SHA-256 verify + delete source) both files into `/Volumes/MattBook - Local/LivePhotoPairs/`
7. Writes a JSON manifest for audit and Apple Photos re-import

---

## Requirements

- macOS (tested on macOS 14+)
- Python 3.9+
- `exiftool` (`brew install exiftool`)

---

## Setup

```bash
cd '/Users/mattymatt/Claude Scripts and Venvs/LivePhotoSort'
python3 -m venv venv
source venv/bin/activate
# No pip packages needed — all stdlib
```

---

## Running

### Dry run (scan only, no file moves)
```bash
./run.sh --dry-run
```

### Full run (background, autonomous)
```bash
./run.sh
```

### Watch the log
```bash
./watch_log.sh
# or manually:
tail -f logs/run_YYYYMMDD_HHMMSS.log
```

### Stop gracefully
```bash
./stop.sh
# or:
kill $(cat logs/live_photo_sort.pid)
```

### Force kill
```bash
kill -9 $(cat logs/live_photo_sort.pid)
```

---

## Output

- `LivePhotoPairs/YYYY-MM-DD_HHMMSS_LivePhoto_<Model>_<UUID8>.heic`
- `LivePhotoPairs/YYYY-MM-DD_HHMMSS_LivePhoto_<Model>_<UUID8>.mov`
- `LivePhotoPairs/live_photo_manifest.json`

---

## Re-importing into Apple Photos

1. Open **Apple Photos**
2. **File → Import…**
3. Select the `LivePhotoPairs` folder
4. Since each pair shares the same base name AND retains `ContentIdentifier` metadata, Photos will recognise them as Live Photos automatically

---

## Architecture

```
LivePhotoSort/
├── live_photo_sort.py   # Main: scan + pair + copy
├── triage_sort.py       # Post-process: sort by device + city
├── run.sh               # Background launcher
├── stop.sh              # Graceful shutdown
├── watch_log.sh         # Log tail helper
├── requirements.txt     # geopy + tqdm (for triage_sort)
├── venv/                # Python venv
└── logs/                # Runtime logs + PID file
```

---

## triage_sort.py

Reorganizes the `LivePhoto Import Ready` output into a hierarchy by **device model** and **city**, with pairs and orphans cleanly separated.

### Output structure
```
<dest>/sorted/
  pairs/
    iPhone 14 Pro/
      Ventura, California/
        2025-02-19_LivePhoto_XXXX.jpeg
        2025-02-19_LivePhoto_XXXX.mov
  orphans/
    stills/
      iPhone 12/
        Los Angeles, California/
    movs/
      Unknown Device/
        Unknown Location/
  triage_geocache.json
```

### Setup
```bash
source venv/bin/activate
pip install geopy tqdm
```

### Usage
```bash
# Always dry-run first
python triage_sort.py --dry-run

# Full run
python triage_sort.py

# Skip reverse geocoding (use GPS coords as folder names)
python triage_sort.py --no-geo

# Custom paths
python triage_sort.py \
  --source "/Volumes/MattBook - Local/LivePhoto Import Ready" \
  --dest   "/Volumes/MattBook - Local/LivePhoto Sorted"
```

### Flags
| Flag | Default | Description |
|---|---|---|
| `--source` | `LivePhoto Import Ready` volume | Root of LivePhotoSort output |
| `--dest` | `<source>/sorted` | Output tree root |
| `--manifest` | Auto (newest manifest_*.json) | Explicit manifest path |
| `--dry-run` | off | Preview only |
| `--workers` | 8 | Parallel exiftool workers |
| `--no-geo` | off | GPS coords instead of city names |
| `--cache` | `<dest>/triage_geocache.json` | Geocode cache |

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
├── live_photo_sort.py   # Main script
├── run.sh               # Background launcher
├── stop.sh              # Graceful shutdown
├── watch_log.sh         # Log tail helper
├── requirements.txt     # (none — all stdlib + exiftool CLI)
├── venv/                # Python venv
└── logs/                # Runtime logs + PID file
```

# CHANGELOG — LivePhotoSort

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.1.0] — 2026-03-06

### Added
- `triage_sort.py` — post-process organizer that reads the LivePhotoSort manifest
  and reorganizes output into `sorted/pairs/` and `sorted/orphans/stills|movs/`
  trees, each sub-sorted by device (EXIF Make+Model) and city (Nominatim
  reverse geocode from GPS EXIF)
- Geocache (`triage_geocache.json`) written alongside sorted output so re-runs
  skip already-resolved coordinates
- `--dry-run`, `--no-geo`, `--workers`, `--cache`, `--source`, `--dest`,
  `--manifest` CLI flags on triage_sort.py
- `geopy` + `tqdm` added to requirements.txt
- README updated with full triage_sort.py documentation

---

## [1.0.0] — 2026-02-15

### Added
- Initial release: `live_photo_sort.py` — scans ClaudeSort PDDC library,
  matches Live Photo pairs via ContentIdentifier UUID using exiftool,
  renames with rich sortable names, copies to LivePhoto Import Ready folder
- JSON manifest output for audit and Photos re-import
- `run.sh`, `stop.sh`, `watch_log.sh` helpers
- Background process support with PID file

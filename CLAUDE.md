# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PixEver is a two-module monorepo: a Python FastAPI server (`pix-ever-server/`) and a Flutter Android client (`pix_ever_client/`). They communicate over a local Wi-Fi network via HTTP REST. No cloud, no accounts, no telemetry.

## Server (`pix-ever-server/`)

### Commands

```bash
cd pix-ever-server
pip install -r requirements.txt
python server.py
```

```bash
# Optional: face tagging. Backup works fully without both of these.
pip install -r requirements-faces.txt
python tools/fetch_models.py
```

```bash
python -m unittest discover -s test          # stdlib unittest; no pytest
```

```bash
# Two build targets. The exclude flags are REQUIRED for the lean one:
# PyInstaller follows the `import cv2` in faces.py statically, even though it
# sits in a try/except, so without them this quietly bundles OpenCV anyway.
pyinstaller --onefile --name PixEverServer --exclude-module cv2 --exclude-module numpy server.py
pyinstaller --onefile --name PixEverServer-Faces server.py
```

Models ship **beside** the executable, not inside it — `MODELS_DIR` resolves next to `sys.executable` when frozen. See "Packaging" in the server README.

**Important:** Always start the server with `python server.py`, not `uvicorn server:app` directly. The tkinter folder-picker dialog runs at module import time (before uvicorn starts), and bypassing `server.py` skips it.

### Architecture

`server.py` was once the whole server; face tagging split it into six modules. `server.py` is now the HTTP layer only — routes, request/response shapes, status codes — with no logic of its own.

| module | responsibility |
|---|---|
| `server.py` | routes and error codes. Also path resolution & the folder picker (runs at import, so `STORAGE_DIR` and `DB_FILE` exist before any handler) |
| `faces.py` | model registry, availability reporting, `FaceEngine` (detect → align → embed), the dominance rule |
| `db.py` | the whole schema, and `connect()` with the pragmas it depends on |
| `people.py` | enrolment: create / list / delete a person |
| `face_worker.py` | background scanning thread, matching, re-tagging, the model guard |
| `library.py` | search query and thumbnail cache |

**[face-scan.md](pix-ever-server/face-scan.md) is the reference for all of this** — flows, every table and column, and the misconceptions that keep coming up. Read it before changing anything face-related; don't duplicate it here.

#### Rules that are easy to break

- **Route handlers must be plain `def`, never `async def`.** Every one does blocking work (SQLite, disk, model inference); an `async def` handler runs on the event loop and freezes all other requests. A test enforces this.
- **Originals are never modified.** Tags live only in SQLite — no EXIF or XMP is written into any image. The SHA-256 is the dedup key, so changing a byte would make the file re-upload as a new photo. Derived data goes under `STORAGE_DIR/.pixever/`.
- **Face tagging is optional at every level.** With no models, or no OpenCV, the server boots and backs up normally; the five face endpoints answer 503. Never let a tagging failure reach the backup path.
- **The scanner commits per photo**, not per batch, so it never holds the SQLite write lock across inference and block uploads.

### Storage

Everything lives inside `STORAGE_DIR` (the user's chosen backup folder), not beside `server.py`:

```
STORAGE_DIR/
  sync_registry.db                   all tables
  2024/01/January_15/IMG_001.jpg     backed-up photos, never modified
  .pixever/
    people/<person_id>/1.jpg         reference photos for enrolment
    thumbs/<hash>.jpg                thumbnail cache, disposable
```

`Files(hash TEXT PRIMARY KEY, path TEXT, timestamp DATETIME)` is the backup registry and is unchanged; `path` is relative to `STORAGE_DIR`. The face tables are added by `db.init()` with `CREATE TABLE IF NOT EXISTS`, so an existing backup folder needs no migration.

**EXIF fallback:** If a file has no `EXIF DateTimeOriginal` tag, `os.path.getmtime()` is used instead.

## Client (`pix_ever_client/`)

### Commands

```bash
cd pix_ever_client
flutter pub get
flutter run                          # debug on connected device
flutter build apk --release          # APK at build/app/outputs/flutter-apk/app-release.apk
flutter analyze                      # lint
```

### Architecture

Three Dart files in `lib/`:

- **`main.dart`** — Single-screen UI (`SyncDashboard`). Owns the server IP text field and the two action buttons ("Select Media" / "Sync Now"). Calls into `DatabaseHelper` for stats and into `SyncEngine` for the upload loop. All state is local to `_SyncDashboardState`; there is no state management library.

- **`sync_engine.dart`** (`SyncEngine` class) — All network logic. The `startSync(onProgress)` method is the core loop:
  1. Pings server (`/ping`, 3 s timeout).
  2. Fetches pending items from local DB in batches of 5.
  3. For each item: chunked SHA-256 hash → `/check-hash` → `/upload` (multipart) if not duplicate → mark `BACKED_UP`.
  4. On any error, stops immediately (does not `continue` to the next file).

- **`database_helper.dart`** (`DatabaseHelper` singleton) — sqflite wrapper for `sync_queue.db` (stored in the app's private data dir). Table: `media(id TEXT PRIMARY KEY, file_path TEXT, status TEXT)`. The file's local path is used as both `id` and `file_path`, so if a file is moved on the device it becomes a new record.

### Key design constraints

- **Chunked hashing:** `SyncEngine._generateHash` uses `sha256.startChunkedConversion` over a file stream so 4K videos don't load entirely into RAM.
- **Batch size 5:** `getPendingMedia(limit: 5)` keeps the sync loop memory-bounded.
- **`ConflictAlgorithm.ignore`** on insert means re-selecting already-queued media is a no-op.
- **`photo_manager`** is listed in `pubspec.yaml` but not currently used; only `image_picker` is active.

## End-to-End Sync Flow

```
Phone                                Server
  │                                    │
  ├─ GET /ping ──────────────────────► │  (verify reachable)
  │                                    │
  ├─ SHA-256 hash file (chunked)       │
  ├─ POST /check-hash ───────────────► │  (skip if duplicate)
  │                                    │
  ├─ POST /upload (multipart) ───────► │  write .tmp → read EXIF
  │                                    │  → move to YYYY/MM/Day/
  │                                    │  → INSERT into SQLite
  │ ◄── {status, saved_path} ─────────┤
  │                                    │
  └─ markAsBackedUp (local DB)         │
```

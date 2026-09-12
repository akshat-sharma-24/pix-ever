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
# Build standalone Windows executable (no Python required to run)
pyinstaller --onefile --name PixEverServer server.py
```

**Important:** Always start the server with `python server.py`, not `uvicorn server:app` directly. The tkinter folder-picker dialog runs at module import time (before uvicorn starts), and bypassing `server.py` skips it.

### Architecture

The entire server is a single file, `server.py`, with three logical sections:

1. **Path resolution & folder picker** — On first run a tkinter dialog asks the user to choose a backup destination. The choice is persisted to `config.json` (gitignored). On subsequent runs `config.json` is read instead. This runs at module load, so `STORAGE_DIR` and `DB_FILE` are set before any request handler runs.

2. **API endpoints** — Three routes:
   - `GET /ping` — health check
   - `POST /check-hash` — accepts `{"hash": "..."}`, returns `{"exists": bool}` by querying the SQLite registry
   - `POST /upload` — multipart form with `hash` field + `file`. Writes to a `.tmp` file first, reads EXIF to determine the destination folder, then atomically moves to `YYYY/MM/MonthName_DD/filename`. Registers the hash+relative path in SQLite.

3. **SQLite registry** (`sync_registry.db`) — lives inside `STORAGE_DIR` (the user's chosen backup folder), not beside `server.py`. Table: `Files(hash TEXT PRIMARY KEY, path TEXT, timestamp DATETIME)`. The `path` column stores the path relative to `STORAGE_DIR`.

**Folder structure for saved files:** `STORAGE_DIR/2024/01/January_15/IMG_001.jpg`

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

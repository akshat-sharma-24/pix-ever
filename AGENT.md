# PixEver - AI Agent System Context & Guidelines

## 1. Project Overview
**PixEver** is a private, zero-telemetry, self-hosted media backup system operating exclusively over local networks (LAN). 

**Primary Directive for Agents:** Maintain the system's lightweight, offline-first nature. Do not introduce cloud dependencies, background sync services, Docker containers, or complex reverse proxy requirements. The system must remain runnable on bare metal via double-clicking an executable (Server) or tapping a button (Client).

---

## 2. System Architecture

The monorepo consists of two decoupled modules communicating via plain HTTP.

### PixEver Server (`pix-ever-server`)
* **Stack:** Python 3.x, FastAPI, SQLite, `exifread`, PyInstaller.
* **Role:** A highly portable HTTP listener. It ingests binary files, extracts EXIF data to determine directory paths, and writes files to the local disk.
* **Portability:** Designed to be compiled into a single `.exe` using PyInstaller. It dynamically resolves its execution path to create the database and backup directories relative to itself.

### PixEver Client (`pix-ever-client`)
* **Stack:** Flutter (Dart), `sqflite`, `crypto`, `http`, `image_picker`.
* **Role:** A manual-sync mobile app. It bypasses aggressive OS background killers by keeping the app in the foreground during sync.
* **Performance constraint:** Must process files using streaming/chunked conversion to avoid out-of-memory (OOM) crashes on mobile devices when handling large 4K video files.

---

## 3. Core Logic & Workflows

### 3.1 The Sync Protocol
1. **Selection:** User manually picks files via native OS gallery (`image_picker`).
2. **Local Queueing:** Client saves paths to its local SQLite database with status `PENDING`.
3. **Hashing:** Client computes a SHA-256 hash using chunked streaming (`crypto`).
4. **Validation:** Client sends hash to Server `POST /check-hash`.
5. **Upload or Skip:** 
    * If hash exists: Client marks local DB as `BACKED_UP` and skips.
    * If hash missing: Client sends multipart/form-data to Server `POST /upload`.
6. **Server Processing:** Server saves as `.tmp`, reads EXIF, resolves naming collisions, moves to final directory, logs to Server SQLite, and returns success.
7. **Completion:** Client marks file as `BACKED_UP`.

### 3.2 Dynamic Folder Routing (Server)
* **Logic:** Files are organized natively based on creation date: `YYYY/MM/Month_DD/filename.ext`.
* **EXIF Priority:** Server attempts to read `EXIF DateTimeOriginal` using `exifread`.
* **Fallback:** If EXIF is missing (e.g., WhatsApp images, videos), it falls back to the OS-level file modification time (`os.path.getmtime`).

### 3.3 Resilience Mechanisms
* **Temporary Files:** To prevent corrupted files on network drops, the Server writes incoming uploads to a `{hash}.tmp` file. It only renames and moves the file to the final destination upon a 100% successful transfer.
* **Collision Handling:** If two different files have the same filename (e.g., `IMG_001.jpg`), the Server natively appends a counter (`IMG_001_1.jpg`) without overwriting or failing.

---

## 4. API Contract

The Python Server exposes the following endpoints on port `8000`:

* **`GET /ping`**
  * Returns: `{"status": "online", "version": "1.0"}`
* **`POST /check-hash`**
  * Payload: `{"hash": "<SHA-256-string>"}`
  * Returns: `{"exists": true/false}`
* **`POST /upload`**
  * Payload: `multipart/form-data` (Fields: `hash` (string), `file` (binary blob))
  * Returns: `{"status": "success", "saved_path": "<relative_path>"}`

---

## 5. Database Schemas

### Server (`sync_registry.db`)
Stores global state of all backed-up files to enforce absolute deduplication.
* `hash` (TEXT, PRIMARY KEY): The SHA-256 hash of the binary file.
* `path` (TEXT): The relative path where the file was saved on the HDD.
* `timestamp` (DATETIME): Server-side timestamp of the upload.

### Client (`sync_queue.db`)
Stores device-specific state to resume sync operations across app restarts.
* `id` (TEXT, PRIMARY KEY): The absolute local file path (used as a unique ID).
* `file_path` (TEXT): The absolute local file path.
* `status` (TEXT): Enum of either `PENDING` or `BACKED_UP`.

---

## 6. Rules for Future Modifications
* **Do not use heavy frameworks:** If modifying the server, stick to built-in Python libraries where possible. Do not introduce PostgreSQL, Redis, or Docker.
* **Keep the UI dumb:** The Flutter UI should only reflect the state of the `SyncEngine` and the local SQLite database. All heavy lifting must remain in asynchronous isolates or streams.
* **Always chunk binaries:** Never load an entire file into memory (Server or Client). Always use chunked streams for hashing and uploading.
* **No authentication:** This system assumes a trusted local network. Do not add JWTs, OAuth, or user accounts.
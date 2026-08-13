# PixEver Server

The backend component of the PixEver DIY photo backup system. This is an ultra-lightweight, zero-telemetry Python server that receives photos over your local network, reads their EXIF data, and safely organizes them into human-readable folders.

## Features

- **EXIF-Driven Routing:** Automatically reads image metadata to sort files into `YYYY/MM/Month_DD/` directories.
- **Smart Deduplication:** Verifies SHA-256 hashes against a local SQLite database (`sync_registry.db`) to ensure zero duplicates.
- **Resilient Transfers:** Uses temporary `.tmp` files during upload to prevent corrupted files if the network drops.
- **Fully Portable:** Can be compiled into a standalone `.exe` that runs on any Windows machine without requiring Python.

## Setup & Execution

### Step 1: Start the Server (From Source)

1. Ensure Python 3.x is installed.
2. Navigate into the server directory:
   ```bash
   cd pix-ever-server
   ```
3. Install the required dependencies:
   ```bash
   pip install fastapi uvicorn python-multipart exifread pyinstaller
   ```
4. Run the server:
   ```bash
   python server.py
   ```

> **Note:** On the first run, a folder picker will appear asking where you want to save your backups. This choice is saved to a `config.json` file for future runs.

### Step 2: Build the Executable (Optional)

To compile the server into a standalone Windows executable so you don't need Python installed in the future:

```bash
pyinstaller --onefile --name PixEverServer server.py
```

The compiled file will be located at `dist/PixEverServer.exe`. You can move this file anywhere on your drive — double-clicking it instantly starts the server on port `8000`.

## Connectivity

The server binds to `0.0.0.0:8000`, making it accessible to any device on the same Wi-Fi network. Find your computer's local IP (e.g., `192.168.1.15`) via `ipconfig` (Windows) or `ifconfig` (Mac/Linux) to connect the mobile app.

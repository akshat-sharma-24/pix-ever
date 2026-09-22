# PixEver Server

The backend component of the PixEver DIY photo backup system. This is an ultra-lightweight, zero-telemetry Python server that receives photos over your local network, reads their EXIF data, and safely organizes them into human-readable folders.

## Features

- **EXIF-Driven Routing:** Automatically reads image metadata to sort files into `YYYY/MM/Month_DD/` directories.
- **Smart Deduplication:** Verifies SHA-256 hashes against a local SQLite database (`sync_registry.db`) to ensure zero duplicates.
- **Resilient Transfers:** Uses temporary `.tmp` files during upload to prevent corrupted files if the network drops.
- **Fully Portable:** Can be compiled into a standalone `.exe` that runs on any Windows machine without requiring Python.
- **Face Tagging (optional):** Recognises people in your backed-up photos so you can search by person. Entirely opt-in — without it the server behaves exactly as before. See [face-scan.md](face-scan.md).

## Setup & Execution

### Step 1: Start the Server (From Source)

1. Ensure Python 3.x is installed.
2. Navigate into the server directory:
   ```bash
   cd pix-ever-server
   ```
3. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the server:
   ```bash
   python server.py
   ```

> **Note:** On the first run, a folder picker will appear asking where you want to save your backups. This choice is saved to a `config.json` file for future runs.

### Step 2: Enable Face Tagging (Optional)

Backup works without this. Skip it and the tagging endpoints simply report that
the feature is unavailable; nothing else changes.

```bash
pip install -r requirements-faces.txt    # opencv-python-headless + numpy
python tools/fetch_models.py             # downloads ~39 MB of models into models/
```

`fetch_models.py` verifies every download against a pinned SHA-256, because a
truncated model file loads and runs normally while returning wrong results.

Check it worked:

```bash
curl http://localhost:8000/faces/status
```

`"state": "ready"` means tagging is on. `models_missing` or `opencv_missing`
tells you which of the two steps above to run.

### Step 3: Build the Executable (Optional)

See [Packaging](#packaging) below — there are now two build targets, and the
old single command no longer produces the small one.

## Packaging

Two targets, because face tagging roughly quadruples the binary.

### Lean build — backup only

```bash
pyinstaller --onefile --name PixEverServer \
  --exclude-module cv2 --exclude-module numpy server.py
```

**The exclude flags are required.** PyInstaller resolves imports statically, so
it follows the `import cv2` in `faces.py` even though that import sits inside a
`try/except` and is optional at runtime. Without the flags this command quietly
produces the large binary instead of the small one.

### Face tagging build

```bash
pyinstaller --onefile --name PixEverServer-Faces server.py
```

Models are **not** bundled into the executable. `MODELS_DIR` resolves next to
the running binary, so ship them alongside it:

```
PixEver-Faces-v1.zip
  PixEverServer-Faces.exe
  models/
    face_detection_yunet_2023mar.onnx      0.23 MB
    face_recognition_sface_2021dec.onnx   38.70 MB
```

Keeping the models outside means a model can be swapped without rebuilding, and
the same executable serves users who never download them.

> **Sizes are estimates and have not been measured on Windows.** PyInstaller
> cannot cross-compile, so these numbers need confirming on a real Windows
> build: roughly 20 MB lean, roughly 76 MB with tagging, plus ~39 MB of models
> beside it.

## Tests

```bash
python -m unittest discover -s test
```

Standard library only — no pytest needed. Each test pins a specific bug found
in review; see [test/test_regressions.py](test/test_regressions.py).

`test/compare_faces.py` is a separate offline harness for measuring face
recognition accuracy against your own photos. It is not part of the server and
is not run by the test suite.

## API

| endpoint | purpose |
|---|---|
| `GET /ping` | health check |
| `POST /check-hash` | `{"hash": "..."}` → `{"exists": bool}` |
| `POST /upload` | multipart `hash` + `file`; files into `YYYY/MM/Month_DD/` |
| `GET /faces/status` | whether tagging is available, and scan progress |
| `POST /people` | multipart `name` + 1–5 images — enrol a person |
| `GET /people` | everyone enrolled, with reference and photo counts |
| `DELETE /people/{id}` | remove a person, their references and their tags |
| `GET /search?person=1&person=3` | photos containing **all** listed people |
| `GET /files/{hash}/thumbnail` | cached 256 px JPEG |

The five face endpoints return **503** when tagging is unavailable, with a
message saying which setup step is missing. `/ping`, `/check-hash` and
`/upload` never depend on it.

## Connectivity

The server binds to `0.0.0.0:8000`, making it accessible to any device on the same Wi-Fi network. Find your computer's local IP (e.g., `192.168.1.15`) via `ipconfig` (Windows) or `ifconfig` (Mac/Linux) to connect the mobile app.

> **No authentication.** The server is open to everyone on the Wi-Fi, and with
> face tagging enabled it also holds names and face vectors. Deliberate for a
> trusted home network; worth knowing before running it on one you don't
> control.

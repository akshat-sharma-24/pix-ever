import os
import sys
import shutil
import sqlite3
import datetime
import json
import tkinter as tk
from tkinter import filedialog
from typing import List
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Query
from fastapi.responses import FileResponse
import exifread
import faces
import db
import people
import face_worker
import library

app = FastAPI()

# --- 1. Path Resolution & Folder Picker ---
if getattr(sys, 'frozen', False):
    CODE_DIR = os.path.dirname(sys.executable)
else:
    CODE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(CODE_DIR, "config.json")

def get_storage_dir():
    # Check if we already saved a folder preference
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            saved_dir = data.get("storage_dir")
            if saved_dir and os.path.exists(saved_dir):
                return saved_dir

    # If no config exists, prompt the user with a popup
    print("Please select a backup directory in the popup window...")
    root = tk.Tk()
    root.withdraw() # Hide the main empty tkinter window
    root.attributes('-topmost', True) # Force the folder dialog to the front
    
    folder_path = filedialog.askdirectory(title="Select PixEver Backup Destination Folder")
    
    if not folder_path:
        print("No folder selected. Exiting...")
        sys.exit(1)
        
    # Save the choice for next time
    with open(CONFIG_FILE, "w") as f:
        json.dump({"storage_dir": folder_path}, f)
        
    return folder_path

# Initialize paths dynamically based on user selection
STORAGE_DIR = get_storage_dir()
DB_FILE = os.path.join(STORAGE_DIR, "sync_registry.db")

# --- 2. Database & API Logic ---
# The schema lives in db.py — every table in one place. Files is unchanged.

# The background scanner. None when tagging is unavailable, so every use of it
# is guarded — the backup path must work with no models and no OpenCV.
WORKER = None

@app.on_event("startup")
def startup_event():
    global WORKER
    db.init(DB_FILE)
    if faces.engine_available():
        WORKER = face_worker.FaceWorker(DB_FILE, STORAGE_DIR)
        WORKER.start()


@app.on_event("shutdown")
def shutdown_event():
    if WORKER is not None:
        WORKER.stop()

@app.get("/ping")
def ping():
    return {"status": "online", "version": "1.0"}

# --- Face tagging availability ---
# Tagging is optional and must never take the backup path down with it. The
# two endpoints above keep working whatever this reports.

@app.get("/faces/status")
def faces_status():
    """Always answers, whether or not tagging works — that is the point of it."""
    state = faces.status()
    if WORKER is not None:
        state.update(WORKER.status())
        if state["worker_error"]:
            # The model guard tripped: embeddings on disk came from a different
            # model. Report it as unavailable so clients stop offering tagging.
            state["available"] = False
            state["state"] = "model_changed"
            state["detail"] = state["worker_error"]
    return state


def require_faces():
    """Dependency for tagging endpoints: 503 unless tagging is available.

    The 503 body is the same shape /faces/status returns, so a client can read
    `state` and `detail` without a second request and without special-casing
    the error. 503 rather than 500: nothing is broken, the feature is simply
    not installed, and retrying after fetching the models will work.
    """
    state = faces.status()
    if not state["available"]:
        raise HTTPException(status_code=503, detail=state)
    return state


# --- People (enrolment) ---
# Every route here depends on require_faces, so with no models installed they
# all answer 503 while /upload and /check-hash carry on working.

def get_engine():
    """One FaceEngine per request. Loading the models costs ~50 ms."""
    return faces.FaceEngine()


@app.post("/people")
def create_person(name: str = Form(...),
                  images: List[UploadFile] = File(...),
                  _state=Depends(require_faces)):
    # Plain def, not async: this loads two ONNX models and then detects and
    # embeds up to five photos. On the event loop that would freeze every
    # other request for the duration; as a def, FastAPI runs it in its
    # threadpool and uploads from a syncing phone keep flowing.
    payload = [(image.filename, image.file.read()) for image in images]
    conn = db.connect(DB_FILE)
    try:
        result = people.create(conn, get_engine(), STORAGE_DIR, name, payload)
        if WORKER is not None:
            # Their photos are already scanned; only the cheap half re-runs.
            WORKER.request_retag()
        return result
    except people.EnrolmentError as e:
        # 400: the photo or the name is the problem, and the message says how
        # to fix it. Not 500 — nothing here is broken.
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.get("/people")
def list_people(_state=Depends(require_faces)):
    conn = db.connect(DB_FILE)
    try:
        return {"people": people.list_all(conn)}
    finally:
        conn.close()


@app.delete("/people/{person_id}")
def delete_person(person_id: int, _state=Depends(require_faces)):
    conn = db.connect(DB_FILE)
    try:
        removed = people.delete(conn, STORAGE_DIR, person_id)
        if removed is None:
            raise HTTPException(status_code=404,
                                detail=f"No person with id {person_id}.")
        if WORKER is not None:
            # Their tags cascaded away, but a face they were winning may now
            # belong to someone else, so re-match rather than just delete.
            WORKER.request_retag()
        return removed
    finally:
        conn.close()

# --- Searching the tagged library ---

@app.get("/search")
def search(person: List[int] = Query(..., description="person id; repeat for AND"),
           limit: int = Query(library.DEFAULT_LIMIT, ge=1, le=library.MAX_LIMIT),
           offset: int = Query(0, ge=0),
           _state=Depends(require_faces)):
    """Photos containing EVERY person listed, newest first.

        /search?person=1              photos of person 1
        /search?person=1&person=3     photos with BOTH of them
    """
    conn = db.connect(DB_FILE)
    try:
        missing = library.unknown_people(conn, person)
        if missing:
            # Better than silently returning nothing: an id that does not
            # exist is a bug in the caller, not an empty result.
            raise HTTPException(
                status_code=404,
                detail=f"No person with id {', '.join(str(m) for m in missing)}.")
        return library.search(conn, person, limit, offset)
    finally:
        conn.close()


@app.get("/files/{file_hash}/thumbnail")
def thumbnail(file_hash: str, _state=Depends(require_faces)):
    """A small JPEG for the grid, generated once and cached on disk.

    Immutable: the cache is keyed by the photo's content hash, so a client
    may cache it forever. Different bytes mean a different hash.
    """
    conn = db.connect(DB_FILE)
    try:
        path = library.thumbnail(conn, STORAGE_DIR, file_hash)
    finally:
        conn.close()
    if path is None:
        raise HTTPException(status_code=404,
                            detail=f"No thumbnail available for {file_hash}.")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


from pydantic import BaseModel
class HashCheckRequest(BaseModel):
    hash: str

@app.post("/check-hash")
def check_hash(req: HashCheckRequest):
    conn = db.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM Files WHERE hash = ?", (req.hash,))
    exists = cursor.fetchone() is not None
    conn.close()
    return {"exists": exists}

def get_creation_date(file_path: str) -> datetime.datetime:
    try:
        with open(file_path, 'rb') as f:
            tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal")
            if "EXIF DateTimeOriginal" in tags:
                date_str = str(tags["EXIF DateTimeOriginal"])
                return datetime.datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    
    mtime = os.path.getmtime(file_path)
    return datetime.datetime.fromtimestamp(mtime)

@app.post("/upload")
def upload_file(hash: str = Form(...), file: UploadFile = File(...)):
    # Plain def for the same reason as /people: this copies a whole photo to
    # disk, reads EXIF and writes to SQLite. None of that is awaitable, so on
    # the event loop it would block every other request.
    # db.connect, not raw sqlite3: this is the path that must not fail when
    # the scanner happens to be writing, and it is db.connect that sets the
    # busy timeout.
    conn = db.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT path FROM Files WHERE hash = ?", (hash,))
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return {"status": "success", "saved_path": existing[0], "note": "already_exists"}

    tmp_path = os.path.join(STORAGE_DIR, f"{hash}.tmp")
    try:
        with open(tmp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        dt = get_creation_date(tmp_path)
        year = dt.strftime("%Y")
        month_num = dt.strftime("%m")
        month_day = f"{dt.strftime('%B')}_{dt.strftime('%d')}" 
        
        target_dir = os.path.join(STORAGE_DIR, year, month_num, month_day)
        os.makedirs(target_dir, exist_ok=True)

        final_filename = file.filename
        final_path = os.path.join(target_dir, final_filename)
        
        counter = 1
        while os.path.exists(final_path):
            name, ext = os.path.splitext(file.filename)
            final_path = os.path.join(target_dir, f"{name}_{counter}{ext}")
            counter += 1

        shutil.move(tmp_path, final_path)
        rel_path = os.path.relpath(final_path, STORAGE_DIR)

        cursor.execute("INSERT INTO Files (hash, path) VALUES (?, ?)", (hash, rel_path))
        conn.commit()

        if WORKER is not None:
            WORKER.nudge()      # scanning happens off the request path

        return {"status": "success", "saved_path": rel_path}

    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

# --- 3. Programmatic Uvicorn Execution ---
if __name__ == "__main__":
    import uvicorn
    import multiprocessing
    multiprocessing.freeze_support() 
    
    print("="*50)
    print("🚀 PixEver Server is Running!")
    print(f"📁 Saving photos to: {STORAGE_DIR}")
    if faces.models_available():
        print(f"🙂 Face models found in: {faces.MODELS_DIR}")
    else:
        # Not fatal: backup works exactly as before, tagging is simply off.
        print("🙂 Face tagging disabled — models missing. Run: python tools/fetch_models.py")
    print("="*50)
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
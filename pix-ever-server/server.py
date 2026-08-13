import os
import sys
import shutil
import sqlite3
import datetime
import json
import tkinter as tk
from tkinter import filedialog
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
import exifread 

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
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Files (
            hash TEXT PRIMARY KEY,
            path TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

@app.on_event("startup")
def startup_event():
    init_db()

@app.get("/ping")
def ping():
    return {"status": "online", "version": "1.0"}

from pydantic import BaseModel
class HashCheckRequest(BaseModel):
    hash: str

@app.post("/check-hash")
def check_hash(req: HashCheckRequest):
    conn = sqlite3.connect(DB_FILE)
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
async def upload_file(hash: str = Form(...), file: UploadFile = File(...)):
    conn = sqlite3.connect(DB_FILE)
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
    print("="*50)
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
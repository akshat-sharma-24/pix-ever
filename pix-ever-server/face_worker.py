"""Background scanning: find faces in backed-up photos and tag them.

Runs on its own thread so uploads never wait for it. A library of twenty
thousand photos takes a while, so the work is resumable: `FaceScans` records
where it got to, and a restart picks up from there.

Everything it writes is that installation's own data, derived from that
person's own photos, in their own backup folder. Nothing here ships.

Two kinds of work, deliberately separated:

  SCANNING   decode, detect, embed. Expensive — hundreds of milliseconds a
             photo — and done once per photo, ever. Results go in `Faces`.

  MATCHING   compare stored embeddings to stored references. Pure arithmetic,
             microseconds a photo. Results go in `FileTags`, and can be redone
             from scratch whenever people or the threshold change, without
             reopening a single image.

That split is what makes enrolling someone new cheap: their photos are already
scanned, so only the matching re-runs.
"""

import datetime
import os
import threading

import db
import faces

# Photos per transaction. Small enough that a crash loses little, large enough
# that commits are not the bottleneck.
BATCH = 25

# How long to doze when there is nothing to do. An upload or an enrolment
# wakes the worker immediately, so this is only a backstop.
IDLE_SECONDS = 30


class ModelMismatch(RuntimeError):
    """The configured model is not the one the stored embeddings came from."""


# --- model guard (plan §6.3) ---

def ensure_model(conn, model_id: str) -> None:
    """Record which model produced the embeddings, or refuse to mix them.

    Vectors from two different models are not comparable — measured, the same
    face embeds ~0.965 similar across int8 and fp32, which is enough drift to
    move borderline matches. Mixing them silently degrades accuracy in a way
    that is very hard to diagnose, so a mismatch is a hard stop.

    Switching models before anything has been scanned is harmless and allowed.
    """
    row = conn.execute("SELECT value FROM Meta WHERE key = 'face_model_id'").fetchone()
    if row is None:
        conn.execute("INSERT INTO Meta (key, value) VALUES ('face_model_id', ?)",
                     (model_id,))
        conn.commit()
        return
    if row[0] == model_id:
        return

    scanned = conn.execute("SELECT COUNT(*) FROM Faces").fetchone()[0]
    if scanned == 0:
        conn.execute("UPDATE Meta SET value = ? WHERE key = 'face_model_id'",
                     (model_id,))
        conn.commit()
        return

    raise ModelMismatch(
        f"This library was scanned with '{row[0]}' but the server is configured "
        f"for '{model_id}'. Their face data is not comparable, so tagging is "
        f"disabled to avoid corrupting it. Either switch back to '{row[0]}', or "
        f"start again on an empty backup folder. Backup itself is unaffected."
    )


# --- scanning ---

def enqueue_new(conn) -> int:
    """Queue any backed-up file that has never been looked at. Returns how many.

    This is also the backfill: on first run every existing photo is new.
    """
    cursor = conn.execute(
        "INSERT OR IGNORE INTO FaceScans (file_hash, status) "
        "SELECT hash, 'pending' FROM Files"
    )
    conn.commit()
    return cursor.rowcount


def _record(conn, file_hash: str, status: str, error: str = None) -> None:
    conn.execute(
        "UPDATE FaceScans SET status = ?, scanned_at = ?, error = ? WHERE file_hash = ?",
        (status, datetime.datetime.now().isoformat(timespec="seconds"), error, file_hash),
    )


def scan_one(conn, engine, storage_dir: str, file_hash: str, rel_path: str) -> str:
    """Find and store every face in one photo. Returns the new status.

    Failures are recorded against the file and never raised: one unreadable
    photo must not stop a twenty-thousand photo backfill.
    """
    path = os.path.join(storage_dir, rel_path or "")
    if not rel_path or not os.path.exists(path):
        _record(conn, file_hash, "failed", "file is missing from the backup folder")
        return "failed"
    if not faces.is_supported(path):
        # Decision 3: no HEIC in v1. Marked, not failed, so support can be
        # added later and only these files rescanned.
        _record(conn, file_hash, "skipped", f"unsupported type: "
                                            f"{os.path.splitext(path)[1] or 'none'}")
        return "skipped"

    image = faces.read_image(path)
    if image is None:
        _record(conn, file_hash, "failed", "file could not be decoded as an image")
        return "failed"

    try:
        found = engine.faces_in_array(image)
    except Exception as e:                       # a bad file should not be fatal
        _record(conn, file_hash, "failed", f"{type(e).__name__}: {e}")
        return "failed"

    # Replace rather than append, so re-scanning a photo is idempotent.
    conn.execute("DELETE FROM Faces WHERE file_hash = ?", (file_hash,))
    for face in found:
        conn.execute(
            "INSERT INTO Faces (file_hash, x, y, w, h, det_score, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_hash, face.x, face.y, face.w, face.h, face.det_score,
             faces.to_blob(face.embedding)),
        )
    _record(conn, file_hash, "done")
    return "done"


# --- matching ---

def load_references(conn) -> dict:
    """{person_id: [embedding]} — every enrolled reference, kept separate.

    Separate, never averaged: reference photos span years and angles, and a
    person matches when *one* of them matches, not when a blurred average does.
    """
    enrolled = {}
    for person_id, blob in conn.execute("SELECT person_id, embedding FROM PersonRefs"):
        enrolled.setdefault(person_id, []).append(faces.from_blob(blob))
    return enrolled


def match_one(conn, file_hash: str, enrolled: dict, threshold: float) -> int:
    """Recompute this photo's tags from its stored faces. Returns tags written.

    Two rules:
      * one face maps to at most one person — the best-scoring person takes
        it, so two lookalike relatives can never both be tagged on one face;
      * one person gets at most one row per photo, at their best score.

    Only `auto` tags are replaced, so a future manual correction survives.
    """
    best = {}
    for face_id, blob in conn.execute(
            "SELECT id, embedding FROM Faces WHERE file_hash = ?", (file_hash,)):
        embedding = faces.from_blob(blob)
        scores = {
            person_id: max(faces.cosine_similarity(embedding, ref) for ref in refs)
            for person_id, refs in enrolled.items() if refs
        }
        if not scores:
            continue
        person_id = max(scores, key=scores.get)
        score = scores[person_id]
        if score < threshold:
            continue
        if person_id not in best or score > best[person_id][0]:
            best[person_id] = (score, face_id)

    conn.execute("DELETE FROM FileTags WHERE file_hash = ? AND source = 'auto'",
                 (file_hash,))
    for person_id, (score, face_id) in best.items():
        conn.execute(
            "INSERT OR REPLACE INTO FileTags "
            "(file_hash, person_id, face_id, score, source) VALUES (?, ?, ?, ?, 'auto')",
            (file_hash, person_id, face_id, score),
        )
    return len(best)


def retag_all(conn, threshold: float = None) -> dict:
    """Rebuild every tag from stored faces. No image is reopened.

    This is what runs after someone is enrolled or removed. On a library that
    is already scanned it is seconds, because the expensive half is done.
    """
    threshold = faces.COSINE_THRESHOLD if threshold is None else threshold
    enrolled = load_references(conn)
    hashes = [r[0] for r in conn.execute("SELECT DISTINCT file_hash FROM Faces")]
    tags = sum(match_one(conn, h, enrolled, threshold) for h in hashes)
    conn.commit()
    return {"photos": len(hashes), "tags": tags, "people": len(enrolled)}


# --- the worker thread ---

class FaceWorker:
    """Owns the scanning thread. One per server."""

    def __init__(self, db_file: str, storage_dir: str, model_id: str = None):
        self.db_file = db_file
        self.storage_dir = storage_dir
        self.model_id = model_id or faces.DEFAULT_MODEL_ID
        self.error = None            # fatal: tagging is off until restart
        self.scanning = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._retag_wanted = False
        self._thread = None

    # -- control, called from request threads --

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="face-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def nudge(self) -> None:
        """A new photo arrived."""
        self._wake.set()

    def request_retag(self) -> None:
        """People changed. Re-match everything already scanned.

        Deliberately handed to the worker rather than done inline: every write
        to Faces and FileTags then happens on one thread, so there is no
        concurrent-writer case to reason about.
        """
        self._retag_wanted = True
        self._wake.set()

    def status(self) -> dict:
        counts = {"pending": 0, "done": 0, "failed": 0, "skipped": 0}
        conn = db.connect(self.db_file)
        try:
            for status, n in conn.execute(
                    "SELECT status, COUNT(*) FROM FaceScans GROUP BY status"):
                counts[status] = n
            tagged = conn.execute("SELECT COUNT(DISTINCT file_hash) FROM FileTags")\
                         .fetchone()[0]
            model = conn.execute(
                "SELECT value FROM Meta WHERE key = 'face_model_id'").fetchone()
        finally:
            conn.close()
        total = sum(counts.values())
        return {
            "scan": {
                **counts,
                "total": total,
                "tagged_photos": tagged,
                "complete": counts["pending"] == 0 and total > 0,
                "scanning": self.scanning,
                "scanned_with": model[0] if model else None,
            },
            "worker_error": self.error,
        }

    # -- the loop --

    def _run(self) -> None:
        conn = db.connect(self.db_file)
        try:
            engine = faces.FaceEngine(self.model_id)
            ensure_model(conn, self.model_id)
        except (ModelMismatch, RuntimeError) as e:
            # Tagging is off, loudly. Backup carries on regardless.
            self.error = str(e)
            print(f"\n[face worker] DISABLED: {e}\n")
            conn.close()
            return

        while not self._stop.is_set():
            try:
                did_work = self._tick(conn, engine)
            except Exception as e:                # never let the thread die
                print(f"[face worker] {type(e).__name__}: {e}")
                did_work = False
            if not did_work:
                self._wake.wait(IDLE_SECONDS)
                self._wake.clear()
        conn.close()

    def _tick(self, conn, engine) -> bool:
        """One unit of work. True if anything happened."""
        if self._retag_wanted:
            self._retag_wanted = False
            result = retag_all(conn)
            print(f"[face worker] re-tagged {result['photos']} photos for "
                  f"{result['people']} people -> {result['tags']} tags")
            return True

        enqueue_new(conn)
        batch = conn.execute(
            "SELECT s.file_hash, f.path FROM FaceScans s "
            "JOIN Files f ON f.hash = s.file_hash "
            "WHERE s.status = 'pending' LIMIT ?", (BATCH,)).fetchall()
        if not batch:
            self.scanning = False
            return False

        self.scanning = True
        enrolled = load_references(conn)
        for file_hash, rel_path in batch:
            if self._stop.is_set():
                break
            if scan_one(conn, engine, self.storage_dir, file_hash, rel_path) == "done":
                match_one(conn, file_hash, enrolled, faces.COSINE_THRESHOLD)
        conn.commit()
        return True

"""Reading the backed-up library for a client: searching it, and thumbnails.

Both are read-only as far as photos are concerned. Thumbnails are written to a
separate cache folder; no original is ever touched.
"""

import os

try:
    import cv2
    import numpy as np
except ImportError:                    # lean build — thumbnails unavailable
    cv2 = None
    np = None

import faces

# Longest side of a cached thumbnail. Big enough for a phone grid at 2-3
# columns on a high-density screen, small enough that a few thousand of them
# cost tens of megabytes rather than gigabytes.
THUMB_SIZE = 256
THUMB_QUALITY = 80

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


# --- search ---

def search(conn, person_ids: list, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
    """Photos containing EVERY listed person, newest first.

    `HAVING COUNT(DISTINCT person_id) = len(ids)` is what makes this an AND
    rather than an OR: a photo only qualifies once it has a tag for each
    person asked for.

    Ordered by path, not by Files.timestamp — the timestamp records when a
    photo was backed up, which for a library imported in one go is nearly the
    same instant for everything. The path carries the capture date as
    YYYY/MM/MonthName_DD, and because the year and month are zero-padded
    numbers and the month name is constant inside a folder, a plain descending
    string sort of the path is true reverse-chronological order.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    placeholders = ",".join("?" * len(person_ids))

    total = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT file_hash FROM FileTags "
        f"WHERE person_id IN ({placeholders}) GROUP BY file_hash "
        f"HAVING COUNT(DISTINCT person_id) = ?)",
        (*person_ids, len(person_ids)),
    ).fetchone()[0]

    rows = conn.execute(
        f"""SELECT f.hash, f.path, f.timestamp
            FROM FileTags t
            JOIN Files f ON f.hash = t.file_hash
            WHERE t.person_id IN ({placeholders})
            GROUP BY t.file_hash
            HAVING COUNT(DISTINCT t.person_id) = ?
            ORDER BY f.path DESC
            LIMIT ? OFFSET ?""",
        (*person_ids, len(person_ids), limit, offset),
    ).fetchall()

    # Who else is in each of these photos, so the client can label a result
    # without another round trip.
    photos = []
    for file_hash, path, backed_up in rows:
        people_in = [
            {"id": pid, "name": name, "score": round(score, 3)}
            for pid, name, score in conn.execute(
                "SELECT p.id, p.name, t.score FROM FileTags t "
                "JOIN People p ON p.id = t.person_id "
                "WHERE t.file_hash = ? ORDER BY t.score DESC", (file_hash,))
        ]
        photos.append({"hash": file_hash, "path": path,
                       "backed_up": backed_up, "people": people_in})

    return {"people": person_ids, "count": total, "limit": limit,
            "offset": offset, "photos": photos}


def unknown_people(conn, person_ids: list) -> list:
    """Which of these ids are not enrolled. Empty when they all are."""
    placeholders = ",".join("?" * len(person_ids))
    known = {r[0] for r in conn.execute(
        f"SELECT id FROM People WHERE id IN ({placeholders})", person_ids)}
    return [pid for pid in person_ids if pid not in known]


# --- thumbnails ---

def thumbs_dir(storage_dir: str) -> str:
    return os.path.join(storage_dir, faces.PIXEVER_DIRNAME, "thumbs")


def _decode_for_thumbnail(data: bytes, target: int):
    """Decode only as many pixels as a `target`-px thumbnail actually needs.

    A full decode of a 12 MP photo costs about 36 MB of pixel buffer to
    produce a 256 px image; a quarter-scale decode costs 2 MB. That matters
    here because a cold search result fires many of these at once, and they
    run concurrently across the threadpool. Decode *time* improves much less
    than the memory does — the JPEG entropy stream still has to be walked —
    so this is mainly about peak memory.

    Steps run most-reduced first and stop at the first result still at least
    `target` across, so a small original falls back to a fuller decode instead
    of being upscaled into a blurry thumbnail. EXIF rotation is applied at
    every reduction level, and reduction works for JPEG, PNG and WebP alike.
    """
    buffer = np.frombuffer(data, np.uint8)
    steps = (cv2.IMREAD_REDUCED_COLOR_4, cv2.IMREAD_REDUCED_COLOR_2, cv2.IMREAD_COLOR)
    for flag in steps:
        image = cv2.imdecode(buffer, flag)
        if image is None:
            return None                       # not a decodable image at all
        if max(image.shape[:2]) >= target or flag == cv2.IMREAD_COLOR:
            return image
    return None


def thumbnail(conn, storage_dir: str, file_hash: str):
    """Path to this photo's cached thumbnail, generating it if needed.

    Returns None when the hash is unknown or the original cannot be read.

    The cache is disposable: deleting the whole thumbs folder costs nothing
    but the time to regenerate. It is keyed by content hash, so a thumbnail
    can never go stale — a changed photo is a different hash.
    """
    if cv2 is None:
        return None

    row = conn.execute("SELECT path FROM Files WHERE hash = ?", (file_hash,)).fetchone()
    if row is None:
        return None

    cache_path = os.path.join(thumbs_dir(storage_dir), f"{file_hash}.jpg")
    if os.path.exists(cache_path):
        return cache_path

    source = os.path.join(storage_dir, row[0] or "")
    if not row[0] or not os.path.exists(source):
        return None
    try:
        # Read the bytes here rather than via cv2.imread: imread cannot open
        # non-ASCII paths on Windows, which is where this runs.
        with open(source, "rb") as f:
            data = f.read()
    except OSError:
        return None
    image = _decode_for_thumbnail(data, THUMB_SIZE)
    if image is None:
        return None

    h, w = image.shape[:2]
    scale = min(1.0, THUMB_SIZE / max(h, w))
    if scale < 1.0:
        image = cv2.resize(image, (max(1, round(w * scale)), max(1, round(h * scale))),
                           interpolation=cv2.INTER_AREA)

    ok, buffer = cv2.imencode(".jpg", image,
                              [cv2.IMWRITE_JPEG_QUALITY, THUMB_QUALITY])
    if not ok:
        return None

    # Write then rename, so a reader never sees a half-written thumbnail and
    # two simultaneous requests cannot corrupt one another.
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    temp_path = f"{cache_path}.{os.getpid()}.tmp"
    try:
        with open(temp_path, "wb") as f:
            f.write(buffer.tobytes())
        os.replace(temp_path, cache_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return cache_path

"""Enrolling, listing and removing the people whose faces get tagged.

Enrolment is entirely separate from backup. Reference photos are uploaded from
the phone, kept outside the dated folders, never registered in `Files` and
never deduplicated — they are not backups, they are the yardstick every
scanned face is measured against.

Reference images are stored byte-for-byte as they arrived. Nothing in PixEver
ever rewrites an image.
"""

import os
import shutil
import sqlite3

import faces

# Per photo, not per person: a person's score is the best of their references.
MAX_REFS = 5

class EnrolmentError(ValueError):
    """Enrolment was refused. The message is meant to be shown to a person."""


def people_dir(storage_dir: str, person_id: int) -> str:
    return os.path.join(storage_dir, faces.PIXEVER_DIRNAME, "people", str(person_id))


def _validate(engine, images: list) -> list:
    """[(filename, data, Face)] — every image, or raise on the first bad one.

    All-or-nothing on purpose. Enrolling three of five photos and reporting a
    partial success leaves the person unsure what was stored; refusing the
    request lets them swap one photo and retry with the set they intended.
    """
    if not images:
        raise EnrolmentError("Add at least one photo of this person.")
    if len(images) > MAX_REFS:
        raise EnrolmentError(
            f"Up to {MAX_REFS} reference photos, but {len(images)} were sent.")

    checked = []
    for filename, data in images:
        label = filename or "photo"
        if not faces.is_supported(label):
            raise EnrolmentError(
                f"{label}: unsupported file type. Use JPG, PNG or WebP.")
        image = faces.decode_image(data)
        if image is None:
            raise EnrolmentError(f"{label}: this file could not be read as an image.")
        try:
            face = faces.subject_face(engine.faces_in_array(image))
        except faces.NoSubjectFace as e:
            raise EnrolmentError(f"{label}: {e}") from e
        checked.append((label, data, face))
    return checked


def create(conn: sqlite3.Connection, engine, storage_dir: str,
           name: str, images: list) -> dict:
    """Enrol a person from reference photos. `images` is [(filename, bytes)].

    Every photo is checked before anything is written, so a rejected request
    leaves no half-made person behind.
    """
    name = (name or "").strip()
    if not name:
        raise EnrolmentError("Give this person a name.")

    checked = _validate(engine, images)

    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO People (name) VALUES (?)", (name,))
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise EnrolmentError(f"Someone called '{name}' is already enrolled.") from e

    person_id = cursor.lastrowid
    folder = people_dir(storage_dir, person_id)
    written = []
    try:
        os.makedirs(folder, exist_ok=True)
        for index, (label, data, face) in enumerate(checked, start=1):
            ext = os.path.splitext(label)[1].lower() or ".jpg"
            path = os.path.join(folder, f"{index}{ext}")
            with open(path, "wb") as f:          # exactly the bytes we received
                f.write(data)
            written.append(path)
            cursor.execute(
                "INSERT INTO PersonRefs (person_id, image_path, embedding) "
                "VALUES (?, ?, ?)",
                (person_id, os.path.relpath(path, storage_dir),
                 faces.to_blob(face.embedding)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        for path in written:                     # leave no orphaned files
            try:
                os.remove(path)
            except OSError:
                pass
        shutil.rmtree(folder, ignore_errors=True)
        raise

    return {"id": person_id, "name": name, "references": len(checked)}


def list_all(conn: sqlite3.Connection) -> list:
    """Everyone enrolled, with how many references and tagged photos each has."""
    rows = conn.execute("""
        SELECT p.id, p.name, p.created_at,
               (SELECT COUNT(*) FROM PersonRefs r WHERE r.person_id = p.id),
               (SELECT COUNT(*) FROM FileTags  t WHERE t.person_id = p.id)
        FROM People p
        ORDER BY p.name
    """).fetchall()
    return [{"id": r[0], "name": r[1], "created_at": r[2],
             "references": r[3], "photos": r[4]} for r in rows]


def delete(conn: sqlite3.Connection, storage_dir: str, person_id: int) -> dict:
    """Remove a person, their references and their tags. None if not found.

    Rows go through ON DELETE CASCADE, which requires the foreign_keys pragma
    that db.connect sets. The reference images are deleted here, since SQLite
    cannot remove files.
    """
    row = conn.execute("SELECT name FROM People WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        return None
    name = row[0]

    n_refs = conn.execute("SELECT COUNT(*) FROM PersonRefs WHERE person_id = ?",
                          (person_id,)).fetchone()[0]
    n_tags = conn.execute("SELECT COUNT(*) FROM FileTags WHERE person_id = ?",
                          (person_id,)).fetchone()[0]

    conn.execute("DELETE FROM People WHERE id = ?", (person_id,))
    conn.commit()

    shutil.rmtree(people_dir(storage_dir, person_id), ignore_errors=True)
    return {"id": person_id, "name": name,
            "references_removed": n_refs, "tags_removed": n_tags}

# Face tagging — how it works

Tagging backed-up photos with the people in them, so the library can be
searched by person. This document covers the **flows**, **what each file
does**, **what each table holds**, and the handful of things that are easy to
misread. It is not a tuning guide: the reasoning behind each constant lives in
comments next to the constant.

Everything here is optional. With no models installed the server backs up
photos exactly as before and the tagging endpoints answer 503.

---

## 1. The idea, in one paragraph

Two networks do two different jobs. **YuNet** finds *where* faces are in a
photo. **SFace** turns *one* cropped face into 128 numbers — a fingerprint.
Neither one identifies anybody, and nothing is ever trained. Recognition is
plain arithmetic we do ourselves: two faces are the same person when their 128
numbers point in nearly the same direction.

Enrolling someone stores a few of these fingerprints under their name.
Scanning a photo computes a fingerprint per face and compares it to the stored
ones.

---

## 2. The four flows

### 2.1 Backup — unchanged by any of this

```
POST /upload
   │
   ├─ write bytes to <hash>.tmp
   ├─ read EXIF date  →  move into  STORAGE_DIR/YYYY/MM/MonthName_DD/
   ├─ INSERT INTO Files (hash, relative path)
   ├─ nudge the worker
   └─ 200 OK                        ← the request ends HERE
```

No face work happens on this path. The upload returns as soon as the file is
filed and recorded.

### 2.2 Enrolment — "seeding" a person

```
POST /people        multipart: name + 1..5 images
   │
   ├─ VALIDATE EVERY IMAGE FIRST, writing nothing:
   │     supported type?  decodable?  exactly one clear subject face?
   │     any failure  →  400, whole request refused, nothing stored
   │
   ├─ INSERT INTO People                     (name is UNIQUE)
   ├─ save each image byte-for-byte to
   │     STORAGE_DIR/.pixever/people/<person_id>/<n>.<ext>
   ├─ INSERT INTO PersonRefs                 one row PER PHOTO
   └─ ask the worker to re-tag the library   (cheap — see 2.3)
```

Reference photos are **not** backups: they are not in `Files`, not
deduplicated, and never land in the dated folders.

`DELETE /people/{id}` removes the person, their references and their tags, and
deletes the image folder — then re-tags, because a face that person was
winning may now belong to someone else.

### 2.3 Scanning — the background worker

Everything below runs on a separate thread. Nothing blocks an upload.

```
wake up  (on a nudge, or every 30s as a backstop)
   │
   ├─ queue anything new:
   │     INSERT OR IGNORE INTO FaceScans … SELECT hash FROM Files
   │     any photo with no FaceScans row becomes 'pending'
   │     — on first run that is the whole existing library (the backfill)
   │
   └─ take up to 25 pending photos:
        │
        ├─ decode (EXIF-rotated), downscale longest side to 1600px
        ├─ YuNet     → every face box, discarding any under 40px
        ├─ alignCrop → 112×112, cut from the FULL-resolution image
        ├─ SFace     → 128 float32 per face, L2-normalised
        ├─ INSERT INTO Faces        one row per face, embedding included
        │
        ├─ match each face against every PersonRefs row
        ├─ INSERT INTO FileTags     one row per (photo, person)
        └─ UPDATE FaceScans         status = done
```

The two halves have wildly different costs, and the whole design follows from
that:

| | per photo | 20,000 photos |
|---|---|---|
| **scan** — decode, detect, embed | ~57 ms | ~19 min |
| **match** — compare stored numbers | ~0.02 ms | under a second |

Matching is about 3,000× cheaper. So **every** face is stored whether or not
it matches anyone, and tags are treated as disposable, recomputable output.
Enrolling a new person re-runs only the cheap half.

### 2.4 Search

```
GET /people                        → ids + names + counts, for the chips
GET /search?person=1               → photos of person 1, newest first
GET /search?person=1&person=3      → photos containing BOTH
GET /files/{hash}/thumbnail        → 256px JPEG, generated once and cached
```

`/search` reads `FileTags` and joins `Files` for the path. Results are ordered
by path descending, which is true reverse-chronological order because the path
is `YYYY/MM/MonthName_DD/…` — zero-padded year and month, and a constant month
name inside a folder. `Files.timestamp` is *backup* time, not capture time, so
it is not used for ordering.

Results carry the photo's **hash**; the client fetches thumbnails by hash and
never needs the path.

---

## 3. What each file does

| file | responsibility |
|---|---|
| `server.py` | HTTP only — routes, request/response shapes, error codes. No logic. |
| `faces.py` | Models and face mechanics: the model registry, where models live, whether tagging is available, `FaceEngine` (detect → align → embed), and the dominance rule. |
| `db.py` | The whole schema in one place, plus `connect()` with the two pragmas the schema needs. |
| `people.py` | Enrolment: create, list, delete a person; storing reference images and embeddings. |
| `face_worker.py` | The background thread: queueing, scanning, matching, re-tagging, and the model guard. |
| `library.py` | Reading the library for a client: the search query and thumbnail generation/caching. |
| `tools/fetch_models.py` | Downloads the two `.onnx` models into `models/`, verifying size and SHA-256. Standard library only. |
| `test/compare_faces.py` | Offline accuracy/speed harness. Not used by the server; imports nothing from it; writes nothing. |

Two structural notes:

- `faces.py` imports OpenCV in a `try/except` and stays importable without it.
  That is what lets a build ship with tagging simply absent.
- `faces.py` works out its own directory rather than importing `server.py`,
  because importing `server.py` opens the folder-picker dialog — a
  command-line tool would pop a window.

### Where files live on disk

```
STORAGE_DIR/                          the folder the user picked
  sync_registry.db                    every table below
  2024/01/January_15/IMG_001.jpg      backed-up photos, never modified
  .pixever/
    people/<person_id>/1.jpg          reference photos
    thumbs/<hash>.jpg                 thumbnail cache, disposable
```

Everything derived sits under `.pixever/`, so it travels with the drive and
never mixes into the dated folders.

---

## 4. The tables

All in `sync_registry.db`, all created by `db.init()` on every start.

### Files — one row per backed-up photo

| column | purpose |
|---|---|
| `hash` **PK** | SHA-256 of the file's bytes. The identity of a photo everywhere else. |
| `path` | Location relative to `STORAGE_DIR`. Also encodes the capture date. |
| `timestamp` | When it was *backed up*. Not the capture date. |

Written by `/upload`. The backup registry; predates face tagging.

### People — one row per enrolled person

| column | purpose |
|---|---|
| `id` **PK** | What the API and every other table refer to. |
| `name` UNIQUE | Display name. Editable in principle, which is why ids are the key. |

### PersonRefs — one row per **reference photo**

| column | purpose |
|---|---|
| `person_id` → People | Cascades on delete. |
| `image_path` | The stored reference image, relative to `STORAGE_DIR`. |
| `embedding` | That photo's 128 float32, L2-normalised. **512 bytes.** |

Five reference photos means **five rows**, never one averaged row. See §5.

### FaceScans — one row per photo: *have we looked at it yet*

| column | purpose |
|---|---|
| `file_hash` **PK** → Files | The photo. |
| `status` | `pending` / `done` / `failed` / `skipped`. |
| `scanned_at`, `error` | When, and why it failed if it did. |

Bookkeeping only — no results here. This is what makes a 20,000-photo backfill
resumable: a restart picks up the `pending` rows. `failed` is a bad or missing
file; `skipped` is an unsupported type such as HEIC, kept separate so support
can be added later and only those files rescanned.

### Faces — one row per **detected face**

| column | purpose |
|---|---|
| `id` **PK** | Referenced by `FileTags.face_id`. |
| `file_hash` → Files | Which photo it was found in. |
| `x, y, w, h` | Box in **original** image coordinates, so a crop can be taken from the file as it sits on disk. |
| `det_score` | The detector's confidence. |
| `embedding` | 128 float32, L2-normalised, 512 bytes. |

**Every face is stored, matched or not.** The unmatched ones are what make
enrolling a new person cost seconds instead of a full rescan, let the
threshold be retuned without re-reading images, and leave the door open to
"this face appears 40 times, who is it?" later.

### FileTags — one row per **(photo, person)**. This is the search index.

| column | purpose |
|---|---|
| `file_hash` + `person_id` **composite PK** | A person appears **at most once per photo**. |
| `face_id` | Which face won the tag. |
| `score` | The similarity that won it. Stored so the threshold can be re-judged later. |
| `source` | `auto` today. Re-tagging replaces only `auto` rows, so a future manual correction survives. |

### Meta — key/value

Holds `face_model_id`: which model produced the stored embeddings. Vectors
from different models are not comparable, so if the configured model ever
differs from this while `Faces` has rows, tagging is disabled with a clear
error rather than silently mixing them. Switching models before anything has
been scanned is allowed.

### Two pragmas the schema depends on

`db.connect()` sets both, on every connection:

- **`foreign_keys = ON`** — SQLite disables these *by default, per connection*.
  Without it every `ON DELETE CASCADE` above is silently inert and deleting a
  person would orphan their references and tags while appearing to succeed.
- **`journal_mode = WAL`** — lets the worker write while request handlers read.

---

## 5. Easy to get wrong

Collected because each of these has actually been misread.

**`Files` records the photo. `FileTags` records who is in it.**
Three similarly named tables do quite different jobs:
`Files` = the photo exists · `FaceScans` = have we looked at it ·
`Faces` = what we found · `FileTags` = who it was.

**An embedding is 128 *numbers*, not 128 bits.**
128 × float32 = **512 bytes** per face. A 20,000-photo library holds roughly
50,000 faces ≈ 27 MB of embeddings, not 800 KB.

**The embedding is created before the `Faces` row, not after.**
It is a column of that row. Detect → align → embed → insert.

**Tags are not written during upload.**
`/upload` writes only the `Files` row and returns. The worker adds `Faces` and
`FileTags` a moment later — usually under a second, because the upload nudges
it. A photo can be backed up but not yet searchable; `GET /faces/status`
reports `pending: 0, complete: true` when the queue has caught up.

**The worker is event-driven *and* polls.**
An upload or enrolment wakes it immediately. It also sweeps every 30 seconds,
so a missed wake-up, or `pending` rows left by a restart, can never stall the
queue forever. Queueing is idempotent, so the sweep is nearly free.

**There is no cap on people per photo.**
A photo with five enrolled people gets five `FileTags` rows. Capping it would
make group photos — the ones most worth finding — unsearchable for whoever got
cut. What *is* capped: one **face** maps to at most one **person** (the best
score wins, which is the defence against lookalike relatives), and one person
gets at most one row per photo (two faces matching the same person still
produce one row, at the higher score).

So for a photo with three faces matching three different people: **3 `Faces`
rows, 3 `FileTags` rows.** If two of those faces are the same person: 3
`Faces` rows, 2 `FileTags` rows. If one face is a stranger: 3 `Faces` rows, 2
`FileTags` rows.

**Reference photos are never averaged together.**
A person's score is the **best** of their references, not the mean. Reference
photos deliberately span years, angles and lighting — measured, one person's
own references scored as low as 0.469 against each other, barely above the
0.370 that separates *different people*. Averaging them produces a blurred
face resembling none of the originals, and defeats the reason several
references are allowed: the point is that **one** of them matches.

**SFace does not search anything.**
It is an encoder: one aligned face in, 128 numbers out. Stateless, with no
database and no notion of who is enrolled. The searching is our own arithmetic
— a dot product over stored vectors, which is why it is microseconds and why
re-tagging is nearly free.

**Search is by id, and multiple people means AND.**
`?person=1&person=3` returns photos containing **both**, not either. Names are
for display; the client reads them from `GET /people` and sends ids. There is
no OR query.

**Enrolment resolves one face; scanning keeps all of them.**
They are deliberately different. A name on an upload says *who* the photo is
of, never *which face*, so enrolment takes the largest face only when the next
one is under 80% of its size, and otherwise refuses the photo. Scanning has no
such question to answer and tags everyone it finds.

**Originals are never modified.**
Tags live only in SQLite. No EXIF or XMP is written into any image and nothing
is re-encoded or rotated in place. This is not only a promise: the SHA-256 is
the deduplication key, so changing one byte would make the file re-upload as a
new photo. Derived data goes under `.pixever/` or into the database.

---

## 6. Key constants

Each is set in code with its reasoning beside it; re-measure with
`test/compare_faces.py`.

| constant | value | meaning |
|---|---|---|
| `MAX_SIDE` | 1600 | longest side a photo is downscaled to before detection |
| `MIN_FACE_PX` | 40 | faces smaller than this (after downscaling) are ignored |
| `DET_SCORE_THRESHOLD` | 0.6 | detector confidence floor for calling something a face |
| `COSINE_THRESHOLD` | 0.370 | similarity above which a face is accepted as a person |
| `DOMINANCE_RATIO` | 0.80 | enrolment: the largest face wins only if the next is smaller than this |
| `MAX_REFS` | 5 | reference photos per person |
| `BATCH` / `IDLE_SECONDS` | 25 / 30 | photos per transaction; backstop sweep interval |
| `THUMB_SIZE` | 256 | longest side of a cached thumbnail |

---

## 7. Status

Built: models and engine, availability reporting, the accuracy harness, the
schema, enrolment, the background scanner, search and thumbnails.

Remaining: the Flutter client (persist the server IP, a People screen, a
Search screen), a Windows build measurement, and updating the top-level README
and CLAUDE.md.

Not in v1: clustering unnamed faces, manual tag correction, video frames,
HEIC, XMP sidecars, and authentication — the server binds `0.0.0.0` with no
auth and holds names and face vectors, so anyone on the same Wi-Fi can read
them. Deliberate, and noted.

One open risk: the threshold has never been tested against two people who
actually look alike. Siblings or a parent/child pair are the case most likely
to produce a wrong tag, and enrolling such a pair and re-running the harness is
the cheapest way to find out before the client is built on top.

"""Deterministic guards for bugs found in review. Run them before merging.

    python -m unittest discover -s test          (from pix-ever-server/)
    python test/test_regressions.py

Every test here pins one specific past mistake, and each one FAILS against the
code as it was written before the fix. They use a fake engine and an event
handshake rather than real inference and sleeps, so they are fast and give the
same answer on a slow machine as on a fast one.

Standard library only, so `python -m unittest` works on a bare checkout.
"""

import os
import sqlite3
import sys
import tempfile
import threading
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db          # noqa: E402
import faces       # noqa: E402
import face_worker  # noqa: E402

HAS_OPENCV = faces.cv2 is not None


class PausableEngine:
    """Stands in for FaceEngine, with inference we can freeze mid-batch.

    Finding no faces is fine: scan_one still writes to Faces and FaceScans,
    which is the part these tests are about.
    """

    def __init__(self, pause_on_call=2):
        self.pause_on_call = pause_on_call
        self.inside = threading.Event()    # set while paused inside inference
        self.resume = threading.Event()
        self.calls = 0

    def faces_in_array(self, image):
        self.calls += 1
        if self.calls == self.pause_on_call:
            self.inside.set()
            self.resume.wait(30)
        return []


@unittest.skipUnless(HAS_OPENCV, "needs OpenCV to write a test image")
class WorkerHoldsNoLockAcrossPhotos(unittest.TestCase):
    """The scanner must not hold a write transaction across a whole batch.

    It used to commit once per 25-photo batch. SQLite opens a write
    transaction at the first INSERT and holds it until commit, and WAL allows
    only one writer, so the lock was held across 25 photos of decode + detect
    + embed. Uploads could not write their Files row for seconds, and on a
    slower machine long enough to exceed the busy timeout and fail outright.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pixever_test_")
        self.db_file = os.path.join(self.tmp, "t.db")
        db.init(self.db_file)
        self.conn = db.connect(self.db_file)
        os.makedirs(os.path.join(self.tmp, "lib"))
        for i in range(3):
            rel = f"lib/p{i}.jpg"
            faces.cv2.imwrite(os.path.join(self.tmp, rel),
                              faces.np.zeros((80, 80, 3), faces.np.uint8))
            self.conn.execute("INSERT INTO Files (hash, path) VALUES (?, ?)",
                              (f"h{i}", rel))
        self.conn.commit()
        self.engine = PausableEngine()
        self.worker = face_worker.FaceWorker(self.db_file, self.tmp)

    def tearDown(self):
        self.engine.resume.set()
        if getattr(self, "thread", None):
            self.thread.join(10)
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start_batch_and_pause(self):
        """Run one batch until the engine is frozen inside the 2nd photo."""
        face_worker.ensure_model(self.conn, faces.DEFAULT_MODEL_ID)
        self.worker_error = None

        def run():
            # Opened inside the thread: SQLite connections are bound to the
            # thread that created them, which is why FaceWorker._run does the
            # same rather than taking a connection from its caller.
            worker_conn = db.connect(self.db_file)
            try:
                self.worker._tick(worker_conn, self.engine)
            except Exception as e:
                self.worker_error = e
                self.engine.inside.set()      # unblock the assertion below
            finally:
                worker_conn.close()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        reached = self.engine.inside.wait(10)
        if self.worker_error is not None:
            self.fail(f"scanner thread raised: {self.worker_error!r}")
        self.assertTrue(reached, "engine never reached the second photo")

    def test_another_connection_can_write_mid_batch(self):
        """An upload's INSERT must not block while the scanner is working.

        The probing connection uses a 0.25 s timeout so that a held lock
        fails fast and unambiguously instead of merely being slow.
        """
        self._start_batch_and_pause()

        probe = sqlite3.connect(self.db_file, timeout=0.25)
        try:
            probe.execute("INSERT INTO Files (hash, path) VALUES (?, ?)",
                          ("uploaded-while-scanning", "lib/new.jpg"))
            probe.commit()
        except sqlite3.OperationalError as e:
            self.fail(f"upload blocked by the scanner's transaction: {e}")
        finally:
            probe.close()

    def test_finished_photos_are_committed_immediately(self):
        """Photo 1's rows must be visible to other connections while photo 2 runs."""
        self._start_batch_and_pause()

        reader = sqlite3.connect(self.db_file, timeout=0.25)
        try:
            status = reader.execute(
                "SELECT status FROM FaceScans WHERE file_hash = 'h0'").fetchone()
        finally:
            reader.close()
        self.assertIsNotNone(status, "no FaceScans row for the first photo")
        self.assertEqual(status[0], "done",
                         "the first photo's result was still uncommitted")


class ConnectionPragmas(unittest.TestCase):
    """db.connect must set the pragmas the schema and the workload depend on."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pixever_test_")
        self.db_file = os.path.join(self.tmp, "t.db")
        db.init(self.db_file)
        self.conn = db.connect(self.db_file)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pragmas_are_set(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertGreaterEqual(
            self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000,
            "a collision should wait its turn, not raise immediately")

    def test_delete_cascades(self):
        """Proves foreign_keys is actually in force, not just reported as on.

        SQLite disables foreign keys by default per connection, which would
        make every ON DELETE CASCADE silently inert while DELETE still
        appeared to succeed.
        """
        self.conn.execute("INSERT INTO People (id, name) VALUES (1, 'Ada')")
        self.conn.execute(
            "INSERT INTO PersonRefs (person_id, image_path, embedding) "
            "VALUES (1, 'x.jpg', X'00')")
        self.conn.execute(
            "INSERT INTO FileTags (file_hash, person_id, score) VALUES ('h', 1, 0.9)")
        self.conn.commit()

        self.conn.execute("DELETE FROM People WHERE id = 1")
        self.conn.commit()

        for table in ("PersonRefs", "FileTags"):
            left = self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE person_id = 1").fetchone()[0]
            self.assertEqual(left, 0, f"{table} rows orphaned — cascade did not fire")


class BackupPathUsesSharedConnection(unittest.TestCase):
    """/upload and /check-hash must go through db.connect.

    Read as a source check because importing server.py opens the tkinter
    folder picker. Raw sqlite3.connect there would skip the busy timeout, so
    an upload colliding with the scanner would raise instead of waiting.
    """

    def test_no_raw_sqlite_connect_on_the_db(self):
        server_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")
        with open(server_py) as f:
            source = f.read()
        # assertFalse on a bool, not assertNotIn on the file: a failure should
        # print one line, not the whole of server.py.
        self.assertFalse("sqlite3.connect(DB_FILE)" in source,
                         "server.py opens the database with raw sqlite3.connect; "
                         "use db.connect(DB_FILE) so the pragmas apply")


if __name__ == "__main__":
    unittest.main(verbosity=2)

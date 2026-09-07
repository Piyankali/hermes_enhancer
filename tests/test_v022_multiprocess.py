"""v0.22 multi-process concurrent-writer validation.

Determines empirically whether the public API supports several OS
processes writing to the same database file: 4 processes x 1500
deterministic push() events against one isolated tmp_path DB, then
independent raw-sqlite3 verification (unique == submitted, 0 dupes,
integrity PASS). Child workers exit(0) after graceful shutdown; any
abnormal child exit fails the test.
"""
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, "src")
from federated_db import FederatedDB

DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_child.py")
PROCS = 4
PER_PROC = 1500


def q(db_path, sql, params=()):
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_multi_process_concurrent_writers(tmp_path):
    """4 processes x 1500 pushes to one DB: no loss, no dupes, healthy."""
    db_path = str(tmp_path / "mp.db")
    # Initialize schema once so children race on rows, not DDL.
    seed = FederatedDB(db_path=db_path)
    seed.shutdown()

    procs = [
        subprocess.Popen(
            [sys.executable, DRIVER, db_path, "push_bulk",
             str(PER_PROC), "mp-%d" % p],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for p in range(PROCS)
    ]
    for p in procs:
        _, err = p.communicate(timeout=600)
        assert p.returncode == 0, "child failed rc=%d err=%s" % (
            p.returncode, err.decode()[:300])

    total = PROCS * PER_PROC
    finals = q(db_path, "SELECT event_id FROM sync_queue")
    assert len(finals) == total, "lost: %d/%d" % (len(finals), total)
    assert len({r[0] for r in finals}) == total
    assert q(db_path, "SELECT event_id, COUNT(*) FROM sync_queue"
                      " GROUP BY event_id HAVING COUNT(*) > 1") == []

    db = FederatedDB(db_path=db_path)
    try:
        v = db.verify_database()
        assert v["healthy"] is True, v
        s = db.get_buffer_summary()
        assert s["total"] == total
        assert s.get("PERSISTED", 0) == total
    finally:
        db.shutdown()

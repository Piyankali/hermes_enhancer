"""v0.22 storage-exhaustion failure tests (SIMULATED, deterministic).

Physical disk-full cannot be tested safely on this device, so exhaustion
is injected as the exact sqlite3.OperationalError strings SQLite raises
("database or disk is full" / "disk I/O error") via per-test mock
patching, plus one REAL unwritable-directory case. Every claim below is
therefore labeled simulated storage exhaustion, NOT physical disk-full.

Invariants under test:
  - storage failure during buffer write -> fail-loud, nothing falsely
    reported durable;
  - storage failure during final persistence -> durable buffer retained,
    recovery succeeds after the condition clears.
"""
import os
import sqlite3
import stat
import sys
from unittest import mock

import pytest

sys.path.insert(0, "src")
from federated_db import FederatedDB


def q(db_path, sql, params=()):
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_simulated_enospc_on_buffer_write_is_fail_loud(tmp_path):
    """SIMULATED STORAGE EXHAUSTION on buffer write: loud failure only."""
    db_path = str(tmp_path / "stor_buf.db")
    db = FederatedDB(db_path=db_path)

    with mock.patch.object(
        db, "enqueue_to_buffer",
        side_effect=sqlite3.OperationalError("database or disk is full"),
    ):
        with pytest.raises(sqlite3.OperationalError):
            db.push({"tool": "stor_test", "event_id": "stor-B-1"})

    assert db.get_buffer_event("stor-B-1") is None
    assert q(db_path, "SELECT COUNT(*) FROM sync_queue WHERE event_id=?",
             ("stor-B-1",))[0][0] == 0
    db.shutdown()


def test_simulated_enospc_on_final_persist_then_recovers(tmp_path):
    """SIMULATED STORAGE EXHAUSTION on final insert: retained, then healed."""
    db_path = str(tmp_path / "stor_final.db")
    db = FederatedDB(db_path=db_path)
    assert db.enqueue_to_buffer("stor-F-1", "t", "c", "pre_tool_call",
                                {"tool": "x"}) is True

    with mock.patch.object(
        db, "_insert_sync",
        side_effect=sqlite3.OperationalError("disk I/O error"),
    ):
        with pytest.raises(sqlite3.OperationalError):
            db.push({"tool": "stor_test", "event_id": "stor-F-2"})
        # The pre-existing durable row must survive the outage untouched.
        st = q(db_path, "SELECT state FROM event_buffer WHERE event_id=?",
               ("stor-F-1",))[0][0]
        assert st == "CREATED"

    # Condition clears (patch removed): normal recovery drains everything.
    result = db.recover_buffered_events()
    assert result["recovered"] >= 1
    assert q(db_path, "SELECT COUNT(*) FROM sync_queue WHERE event_id=?",
             ("stor-F-1",))[0][0] == 1
    assert q(db_path, "SELECT COUNT(*) FROM sync_queue WHERE event_id=?",
             ("stor-F-2",))[0][0] == 1
    assert q(db_path, "SELECT COUNT(*) FROM sync_queue GROUP BY event_id"
                      " HAVING COUNT(*) > 1") == []
    v = db.verify_database()
    assert v["healthy"] is True
    db.shutdown()


def test_unwritable_directory_fails_loudly(tmp_path):
    """REAL unwritable location: constructor raises, nothing fabricated."""
    locked = tmp_path / "locked_dir"
    locked.mkdir()
    os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(Exception):
            FederatedDB(db_path=str(locked / "x.db"))
    finally:
        os.chmod(locked, stat.S_IRWXU)

"""v0.22 abnormal process-crash durability tests.

Proves, with genuine abnormal termination (os._exit in a separate OS
process, no shutdown/flush/close), the two durability invariants:

  A. A confirmed durably-buffered event is never permanently lost.
  B. A committed final event is never duplicated by startup recovery.

Process B/C in every test is a fresh FederatedDB started normally;
recovery must happen automatically via __init__ — no test ever calls
recover_buffered_events() on the restarted instance. All database-state
assertions use an independent raw sqlite3 connection, never the
recovered instance's own accessors.

Crash-point injection is test-only: the child driver either stops after
a durable buffer commit (buffer_only/bulk) or monkeypatches
mark_buffer_persisted to os._exit inside the child process only
(push_no_finalize). Production behavior is unchanged when the driver
is not involved.
"""
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, "src")
from federated_db import FederatedDB

DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_child.py")


def run_child(db_path, *args, timeout=120):
    proc = subprocess.run(
        [sys.executable, DRIVER, db_path] + list(args),
        capture_output=True, text=True, timeout=timeout,
    )
    assert proc.returncode != 0, (
        "child must die abnormally (os._exit), got returncode 0"
    )
    return proc


def q(db_path, sql, params=()):
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def buffer_row(db_path, event_id):
    rows = q(
        db_path,
        "SELECT state, buffered_at, persisted_at FROM event_buffer"
        " WHERE event_id=?",
        (event_id,),
    )
    assert len(rows) == 1, "expected exactly 1 buffer row for %s" % event_id
    return {"state": rows[0][0], "buffered_at": rows[0][1],
            "persisted_at": rows[0][2]}


def final_count(db_path, event_id):
    rows = q(
        db_path, "SELECT COUNT(*) FROM sync_queue WHERE event_id=?",
        (event_id,),
    )
    return rows[0][0]


def duplicate_event_ids(db_path):
    return q(
        db_path,
        "SELECT event_id, COUNT(*) FROM sync_queue"
        " GROUP BY event_id HAVING COUNT(*) > 1",
    )


def test_crash_a_buffer_commit_then_die(tmp_path):
    """Crash after durable buffer, before final persistence: no loss."""
    db_path = str(tmp_path / "crash_a.db")
    eid = "crash-A-1"

    proc = run_child(db_path, "buffer_only", eid)
    assert proc.returncode == 99

    # Committed: buffer row CREATED. Not committed: final event.
    row = buffer_row(db_path, eid)
    assert row["state"] == "CREATED"
    assert row["buffered_at"] is not None
    assert final_count(db_path, eid) == 0

    # Process B starts normally; recovery is automatic.
    db_b = FederatedDB(db_path=db_path)
    info = db_b.get_startup_recovery_info()
    assert info["executed"] is True
    assert info["recovered"] >= 1
    db_b.shutdown()

    # Exactly one final event; buffer finalized with timestamp.
    assert final_count(db_path, eid) == 1
    assert duplicate_event_ids(db_path) == []
    row = buffer_row(db_path, eid)
    assert row["state"] == "PERSISTED"
    assert row["persisted_at"] is not None


def test_crash_b_final_commit_then_die_before_finalize(tmp_path):
    """Crash after final commit, before buffer finalization: no duplicate."""
    db_path = str(tmp_path / "crash_b.db")
    eid = "crash-B-1"

    proc = run_child(db_path, "push_no_finalize", eid)
    assert proc.returncode == 88

    # Committed: buffer row AND final event. Not committed: finalization.
    assert final_count(db_path, eid) == 1
    row = buffer_row(db_path, eid)
    assert row["state"] == "CREATED", (
        "buffer must still look pending so recovery can observe it"
    )

    # Process B starts normally; recovery must detect the existing event.
    db_b = FederatedDB(db_path=db_path)
    info = db_b.get_startup_recovery_info()
    assert info["executed"] is True
    db_b.shutdown()

    # Still exactly one final event; buffer now finalized.
    assert final_count(db_path, eid) == 1
    assert duplicate_event_ids(db_path) == []
    row = buffer_row(db_path, eid)
    assert row["state"] == "PERSISTED"
    assert row["persisted_at"] is not None


def test_crash_c_bulk_backlog_across_restarts(tmp_path):
    """120 buffered events + crash: bounded recovery drains across restarts."""
    db_path = str(tmp_path / "crash_c.db")
    n = 120

    proc = run_child(db_path, "bulk", str(n), "crash-C")
    assert proc.returncode == 99

    # Process B: automatic startup recovery handles the first bounded batch.
    db_b = FederatedDB(db_path=db_path)
    info_b = db_b.get_startup_recovery_info()
    assert info_b["executed"] is True
    assert info_b["recovered"] == 100, (
        "startup recovery must respect the 100-row bound, got %r" % info_b
    )
    db_b.shutdown()

    pending = q(
        db_path,
        "SELECT COUNT(*) FROM event_buffer WHERE state!='PERSISTED'",
    )[0][0]
    assert pending == n - 100

    # Process C: automatic startup recovery drains the remainder.
    db_c = FederatedDB(db_path=db_path)
    info_c = db_c.get_startup_recovery_info()
    assert info_c["executed"] is True
    assert info_c["recovered"] == n - 100
    db_c.shutdown()

    # All durable events recovered exactly once; nothing lost, no dupes.
    total = q(db_path, "SELECT COUNT(*) FROM sync_queue")[0][0]
    assert total == n
    assert duplicate_event_ids(db_path) == []
    states = q(
        db_path, "SELECT DISTINCT state FROM event_buffer"
    )
    assert states == [("PERSISTED",)], "all buffers finalized, got %r" % states


def test_crash_d_repeated_restart(tmp_path):
    """Crash -> recovery -> crash -> recovery: stable, no loss, no dupes."""
    db_path = str(tmp_path / "crash_d.db")
    eids = ["crash-D-%d" % i for i in range(3)]

    proc_a = run_child(db_path, "bulk", str(len(eids)), "crash-D")
    assert proc_a.returncode == 99

    # Process B recovers automatically, then dies abnormally (no shutdown).
    proc_b = run_child(db_path, "recover_then_die")
    assert proc_b.returncode == 77

    # Process C recovers whatever remains and holds the final state.
    db_c = FederatedDB(db_path=db_path)
    info_c = db_c.get_startup_recovery_info()
    assert info_c["executed"] is True
    db_c.shutdown()

    for eid in eids:
        assert final_count(db_path, eid) == 1, "lost or duplicated: %s" % eid
        row = buffer_row(db_path, eid)
        assert row["state"] == "PERSISTED"
    assert duplicate_event_ids(db_path) == []

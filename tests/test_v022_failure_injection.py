"""v0.22 failure injection and fault recovery tests.

Validates behavior when the buffer or final SQLite persistence hits
operational failures. All injection is deterministic monkeypatching,
scoped per-test (unittest.mock), disabled by default — production code
paths are unchanged when no patch is active. Every test uses an
isolated tmp_path database; final assertions read state through an
independent raw sqlite3 connection.

Covered invariants:
  1. Final persistence fails -> buffer stays recoverable.
  2. Buffer persistence fails -> never falsely reported as durable.
  3. Retry/recovery never duplicates a persisted event_id.
  4. A corrupt event never destroys healthy buffered events.
  5. Failures stay observable (raised errors, FAILED state, counters).
"""
import asyncio
import sqlite3
import sys
import time
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


def buffer_state(db_path, event_id):
    rows = q(
        db_path,
        "SELECT state, last_error, persisted_at FROM event_buffer"
        " WHERE event_id=?",
        (event_id,),
    )
    assert len(rows) == 1, "expected 1 buffer row for %s" % event_id
    return {"state": rows[0][0], "last_error": rows[0][1],
            "persisted_at": rows[0][2]}


def final_count(db_path, event_id):
    return q(
        db_path, "SELECT COUNT(*) FROM sync_queue WHERE event_id=?",
        (event_id,),
    )[0][0]


def duplicate_event_ids(db_path):
    return q(
        db_path,
        "SELECT event_id, COUNT(*) FROM sync_queue"
        " GROUP BY event_id HAVING COUNT(*) > 1",
    )


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_a_transient_lock_retries_then_persists(tmp_path):
    """Transient 'database is locked' during final insert: retry, no loss."""
    db = FederatedDB(db_path=str(tmp_path / "fail_a.db"))
    calls = {"n": 0}
    real_insert = db._insert_sync

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_insert(*args, **kwargs)

    with mock.patch.object(db, "_insert_sync", side_effect=flaky):
        row_id = db.push({"tool": "lock_test", "event_id": "fail-A-1"})

    assert isinstance(row_id, int)
    assert calls["n"] >= 2, "retry path must activate"
    counters = db.get_counters()
    assert counters["events_retried"] >= 1
    assert counters["transient_errors"] >= 1
    assert final_count(str(tmp_path / "fail_a.db"), "fail-A-1") == 1
    assert buffer_state(str(tmp_path / "fail_a.db"), "fail-A-1")["state"] \
        == "PERSISTED"
    db.shutdown()


def test_b_final_tx_failure_stays_buffered_then_recovers(tmp_path):
    """Final persistence fails: buffer NOT finalized, later retry succeeds."""
    db_path = str(tmp_path / "fail_b.db")
    db = FederatedDB(db_path=db_path)

    with mock.patch.object(
        db, "_insert_sync",
        side_effect=RuntimeError("injected FINAL_INSERT failure"),
    ):
        with pytest.raises(RuntimeError):
            db.push({"tool": "tx_test", "event_id": "fail-B-1"})

    # Invariant 1: still buffered and recoverable, nothing finalized.
    assert buffer_state(db_path, "fail-B-1")["state"] == "CREATED"
    assert final_count(db_path, "fail-B-1") == 0
    assert db.get_counters()["unknown_errors"] >= 1  # observable

    # Scheduler-tick retry through the normal recovery path.
    result = db.recover_buffered_events()
    assert result["recovered"] == 1
    assert final_count(db_path, "fail-B-1") == 1
    assert buffer_state(db_path, "fail-B-1")["state"] == "PERSISTED"
    assert duplicate_event_ids(db_path) == []
    db.shutdown()


def test_c_buffer_write_failure_is_fail_loud(tmp_path):
    """Buffer write fails: push raises, nothing falsely reported durable."""
    db_path = str(tmp_path / "fail_c.db")
    db = FederatedDB(db_path=db_path)

    with mock.patch.object(
        db, "enqueue_to_buffer",
        side_effect=RuntimeError("injected BUFFER_WRITE failure"),
    ):
        with pytest.raises(RuntimeError):
            db.push({"tool": "buf_test", "event_id": "fail-C-1"})

    # Invariant 2: no buffer row, no final row, no success value returned.
    assert db.get_buffer_event("fail-C-1") is None
    assert final_count(db_path, "fail-C-1") == 0

    # Async path follows the same fail-loud rule.
    with mock.patch.object(
        db, "enqueue_to_buffer",
        side_effect=RuntimeError("injected BUFFER_WRITE failure"),
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(db.async_push({"tool": "buf_test"}))
    db.shutdown()


def test_d_corrupt_payload_quarantined_healthy_events_flow(tmp_path):
    """Corrupt buffer payload: detected, preserved, worker survives."""
    db_path = str(tmp_path / "fail_d.db")
    db = FederatedDB(db_path=db_path)
    assert db.enqueue_to_buffer("fail-D-good", "t", "c", "pre_tool_call",
                                {"tool": "ok"})
    assert db.enqueue_to_buffer("fail-D-bad", "t", "c", "pre_tool_call",
                                {"tool": "ok"})
    assert db.enqueue_to_buffer("fail-D-weird", "t", "c", "pre_tool_call",
                                {"tool": "ok"})
    db.shutdown()

    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute(
            "UPDATE event_buffer SET payload=? WHERE event_id=?",
            ("{{{not-json", "fail-D-bad"),
        )
        conn.execute(
            "UPDATE event_buffer SET state=? WHERE event_id=?",
            ("GARBLED", "fail-D-weird"),
        )
        conn.commit()
    finally:
        conn.close()

    db2 = FederatedDB(db_path=db_path)  # automatic startup recovery
    info = db2.get_startup_recovery_info()
    assert info["executed"] is True

    # Corrupt record: detected, FAILED with diagnosis, NOT deleted.
    bad = buffer_state(db_path, "fail-D-bad")
    assert bad["state"] == "FAILED"
    assert bad["last_error"] is not None and "corrupt_payload" in bad["last_error"]
    kept = q(db_path, "SELECT payload FROM event_buffer WHERE event_id=?",
             ("fail-D-bad",))[0][0]
    assert kept == "{{{not-json"
    assert final_count(db_path, "fail-D-bad") == 0

    # Unknown-state record: left in place, never silently deleted.
    weird = buffer_state(db_path, "fail-D-weird")
    assert weird["state"] == "GARBLED"

    # Healthy event unaffected: persisted exactly once, finalized.
    assert final_count(db_path, "fail-D-good") == 1
    assert buffer_state(db_path, "fail-D-good")["state"] == "PERSISTED"
    assert duplicate_event_ids(db_path) == []
    db2.shutdown()


def test_e_duplicate_event_id_never_duplicated(tmp_path):
    """Same event_id twice: one buffer row, one final row, UNIQUE holds."""
    db_path = str(tmp_path / "fail_e.db")
    db = FederatedDB(db_path=db_path)

    assert db.enqueue_to_buffer("fail-E-1", "t", "c", "pre_tool_call",
                                {"tool": "x"}) is True
    assert db.enqueue_to_buffer("fail-E-1", "t", "c", "pre_tool_call",
                                {"tool": "x"}) is False

    r1 = db.push({"tool": "dup_test", "event_id": "fail-E-1"})
    r2 = db.push({"tool": "dup_test", "event_id": "fail-E-1"})
    assert r1 == r2, "retried insert must return the existing row id"
    db.flush()

    assert final_count(db_path, "fail-E-1") == 1
    assert duplicate_event_ids(db_path) == []

    # DB-level guard itself: raw duplicate buffer insert must fail.
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO event_buffer (event_id, trace_id, tool_call_id,"
                " event_type, payload, created_at, state, buffered_at)"
                " VALUES (?,?,?,?,?,datetime('now'),'CREATED',datetime('now'))",
                ("fail-E-1", "t", "c", "pre_tool_call", "{}"),
            )
    finally:
        conn.close()
    db.shutdown()


def test_f_worker_interruption_recovers_without_duplicate(tmp_path):
    """Worker batch fails mid-persistence: buffer saves it, restart heals."""
    db_path = str(tmp_path / "fail_f.db")
    db = FederatedDB(db_path=db_path)
    real_insert = db._insert_sync
    state = {"fail_once": True}

    def fail_once(*args, **kwargs):
        if state["fail_once"]:
            state["fail_once"] = False
            raise RuntimeError("injected worker FINAL_INSERT failure")
        return real_insert(*args, **kwargs)

    with mock.patch.object(db, "_insert_sync", side_effect=fail_once):
        eid = asyncio.run(db.async_push({"tool": "worker_test"}))
        assert eid is not None
        # Durable buffer commit happened synchronously before queueing.
        assert db.get_buffer_event(eid)["state"] == "CREATED"
        # Worker attempts the batch and fails observably.
        assert wait_for(lambda: db.get_counters()["events_failed"] >= 1), (
            "worker failure must surface in counters"
        )
    # Failed batch never reached final persistence...
    assert final_count(db_path, eid) == 0
    # ...but the buffer row survived the interruption.
    assert db.get_buffer_event(eid)["state"] == "CREATED"
    db.shutdown()

    # Fresh process heals automatically: exactly-once final persistence.
    db2 = FederatedDB(db_path=db_path)
    info = db2.get_startup_recovery_info()
    assert info["executed"] is True
    assert final_count(db_path, eid) == 1
    assert duplicate_event_ids(db_path) == []
    db2.shutdown()
    st = buffer_state(db_path, eid)["state"]
    assert st == "PERSISTED"

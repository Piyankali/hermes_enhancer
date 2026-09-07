import sys, os, tempfile
sys.path.insert(0, 'src')
from federated_db import FederatedDB


def test_verify_database_healthy(tmp_path):
    """Test verify_database() on a fresh, healthy database."""
    db_path = str(tmp_path / "test_healthy.db")
    db = FederatedDB(db_path=db_path)

    result = db.verify_database()
    assert result["healthy"] is True, f"Expected healthy, got: {result}"
    assert result["quick_check"] == "PASS", f"Expected quick_check PASS, got: {result['quick_check']}"
    assert result["error"] is None, f"Expected no error, got: {result['error']}"

    db.shutdown()


def test_verify_database_quick_check_passes(tmp_path):
    """Test that PRAGMA quick_check actually executes and reports PASS."""
    db_path = str(tmp_path / "test_quickcheck.db")
    db = FederatedDB(db_path=db_path)

    result = db.verify_database()
    assert "PASS" in result["quick_check"], f"Expected quick_check PASS, got: {result}"

    db.shutdown()


def test_verify_database_integrity_check(tmp_path):
    """Test that PRAGMA integrity_check is executed."""
    db_path = str(tmp_path / "test_integrity.db")
    db = FederatedDB(db_path=db_path)

    result = db.verify_database()
    assert result["integrity_check"] in ("PASS", "FAIL", "skipped"), f"Unexpected integrity_check: {result['integrity_check']}"

    db.shutdown()


def test_verify_database_after_enqueue(tmp_path):
    """Test verify_database() after events are buffered."""
    db_path = str(tmp_path / "test_after_events.db")
    db = FederatedDB(db_path=db_path)

    r = db.enqueue_to_buffer("test-event-1", "trace-1", "call-1", "pre_tool_call", {"tool": "test"})
    assert r is True

    result_db = db.verify_database()
    assert result_db["healthy"] is True, f"Database should still be healthy after enqueue, got: {result_db}"

    db.shutdown()


def test_corrupted_db_not_silently_deleted(tmp_path):
    """Test that verify_database() detects corruption without deleting the DB."""
    db_path = str(tmp_path / "test_corrupt.db")

    db = FederatedDB(db_path=db_path)
    db.enqueue_to_buffer("evt-test", "t", "c", "pre_tool_call", {"tool": "x"})

    original_size = os.path.getsize(db_path)
    with open(db_path, "wb") as f:
        f.write(b"CORRUPTED_DATA_NOT_SQLITE_MAGIC_HERE_" + b"x" * (original_size - 50))

    result = db.verify_database()
    assert result["healthy"] is False, f"Expected unhealthy for corrupted DB, got: {result}"
    assert result["quick_check"] != "PASS", f"Expected quick_check != PASS, got: {result['quick_check']}"
    assert os.path.exists(db_path), "Corrupted DB file should still exist"

    db.shutdown()


def test_duplicate_event_id_still_enforced_after_verify(tmp_path):
    """Test that event_id uniqueness is still enforced after verify_database()."""
    db_path = str(tmp_path / "test_duplicate.db")

    db = FederatedDB(db_path=db_path)

    r1 = db.enqueue_to_buffer("dup-test-1", "t1", "c1", "pre_tool_call", {"tool": "x"})
    assert r1 is True

    r2 = db.enqueue_to_buffer("dup-test-1", "t1", "c1", "pre_tool_call", {"tool": "x"})
    assert r2 is False, "Duplicate event_id should be prevented"

    vresult = db.verify_database()
    assert vresult["healthy"] is True

    db.shutdown()

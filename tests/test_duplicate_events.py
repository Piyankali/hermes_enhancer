"""v0.23 duplicate-event tests: same event_id never double-persists."""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from federated_db import FederatedDB


def test_push_same_event_id_idempotent(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "d.db"))
    try:
        eid = uuid.uuid4().hex
        base = {"hook": "post_tool_call", "tool": "t", "event_id": eid,
                "trace_id": "tr", "tool_call_id": "c"}
        r1 = db.push(dict(base))
        r2 = db.push(dict(base))
        db.flush()
        assert r1 == r2 > 0
        assert db.count() == 1
    finally:
        db.shutdown()


def test_recovery_does_not_duplicate(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "r.db"))
    try:
        eid = uuid.uuid4().hex
        assert db.enqueue_to_buffer(eid, "tr", "c1", "sync_push",
                                    {"hook": "x"}) is True
        # Second buffer commit of the same event is a no-op (INSERT OR IGNORE).
        assert db.enqueue_to_buffer(eid, "tr", "c1", "sync_push",
                                    {"hook": "x"}) is False
        out = db.recover_buffered_events()
        assert out["recovered"] == 1
        out2 = db.recover_buffered_events()
        assert out2["recovered"] == 0
        assert db.count() == 1
    finally:
        db.shutdown()


def test_distinct_events_all_persist(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "m.db"))
    try:
        for _ in range(10):
            db.push({"hook": "x", "event_id": uuid.uuid4().hex})
        db.flush()
        assert db.count() == 10
    finally:
        db.shutdown()

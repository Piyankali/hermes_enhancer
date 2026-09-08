"""v0.23 orphan-recovery tests: completed/failed/orphaned/unknown."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from federated_db import FederatedDB


def _pre_post(db, call_id, status="ok"):
    db.push({"hook": "pre_tool_call", "tool": "t", "event_id": "pre-" + call_id,
             "trace_id": "tr", "tool_call_id": call_id})
    db.push({"hook": "post_tool_call", "tool": "t", "success": status == "ok",
             "status": "success" if status == "ok" else "failed",
             "event_id": "post-" + call_id, "trace_id": "tr",
             "tool_call_id": call_id})
    db.flush()


def test_completed_pair_not_orphan(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "o.db"))
    try:
        _pre_post(db, "c1")
        assert db.find_orphans() == []
    finally:
        db.shutdown()


def test_lone_pre_is_orphan_no_duration_invented(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "o2.db"))
    try:
        db.push({"hook": "pre_tool_call", "tool": "t", "event_id": "pre-x",
                 "trace_id": "tr", "tool_call_id": "lonely"})
        db.flush()
        orphans = db.find_orphans()
        assert len(orphans) == 1
        assert orphans[0]["status"] == "orphaned"
        assert "duration" not in orphans[0] and "delta_us" not in orphans[0]
        assert db.mark_orphans() == 1
        assert db.mark_orphans() == 0  # idempotent
    finally:
        db.shutdown()


def test_failed_pair_not_orphan(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "o3.db"))
    try:
        _pre_post(db, "c9", status="fail")
        assert db.find_orphans() == []
    finally:
        db.shutdown()


def test_empty_call_id_ignored(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "o4.db"))
    try:
        db.push({"hook": "pre_tool_call", "tool": "t", "event_id": "e1"})
        db.flush()
        assert db.find_orphans() == []
    finally:
        db.shutdown()

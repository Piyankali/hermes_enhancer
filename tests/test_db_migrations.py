"""v0.23 migration tests: additive, idempotent, backward-compatible."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from federated_db import FederatedDB, SCHEMA_VERSION


def test_fresh_db_has_v23_schema(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "m.db"))
    try:
        assert db.get_schema_version() == str(SCHEMA_VERSION)
        con = sqlite3.connect(db.db_path)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        for t in ("tool_feedback", "tool_transitions", "meta_tool_stats",
                  "meta_sequences", "predictions", "orphan_events",
                  "schema_meta", "sync_queue", "event_buffer"):
            assert t in tables, t
    finally:
        db.shutdown()


def test_upgrade_from_v1_layout(tmp_path):
    """A pre-v0.23 DB (sync_queue only) migrates without data loss."""
    dbp = str(tmp_path / "old.db")
    con = sqlite3.connect(dbp)
    try:
        con.execute("CREATE TABLE sync_queue (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " payload TEXT NOT NULL, timestamp TEXT NOT NULL)")
        con.execute("INSERT INTO sync_queue (payload, timestamp) VALUES ('{}', 't')")
        con.commit()
    finally:
        con.close()
    db = FederatedDB(db_path=dbp)
    try:
        assert db.count() == 1
        assert db.get_schema_version() == str(SCHEMA_VERSION)
        assert db.verify_database()["healthy"] is True
    finally:
        db.shutdown()


def test_init_idempotent(tmp_path):
    dbp = str(tmp_path / "idem.db")
    db = FederatedDB(db_path=dbp)
    db.shutdown()
    db2 = FederatedDB(db_path=dbp)
    try:
        assert db2.get_schema_version() == str(SCHEMA_VERSION)
        assert db2.verify_database()["healthy"] is True
    finally:
        db2.shutdown()


def test_no_destructive_migration_markers(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "nd.db"))
    try:
        con = sqlite3.connect(db.db_path)
        try:
            sql = " ".join(
                r[0] for r in con.execute(
                    "SELECT sql FROM sqlite_master").fetchall() if r[0])
        finally:
            con.close()
        assert "DROP TABLE" not in sql
    finally:
        db.shutdown()

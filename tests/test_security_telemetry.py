"""v0.23 security-telemetry tests: plaintext secrets never reach SQLite."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from federated_db import FederatedDB
from redaction import assert_no_plaintext


SECRETS = ["s3cr3t-pw", "tok-ABC-123", "sk-live-999"]


def _db_text(db_path):
    con = sqlite3.connect(db_path)
    try:
        texts = []
        for t in ("sync_queue", "event_buffer"):
            try:
                texts += [r[0] for r in con.execute(
                    "SELECT payload FROM %s" % t).fetchall()]
            except sqlite3.OperationalError:
                pass
        return "\n".join(texts)
    finally:
        con.close()


def test_hook_payload_secrets_redacted(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "s.db"))
    try:
        db.enqueue_event({"hook": "pre_tool_call", "tool": "login",
                          "kwargs": {"username": "alice", "password": SECRETS[0],
                                     "headers": {"Authorization": "Bearer " + SECRETS[1]}},
                          "event_id": "e1"})
        db.push({"hook": "post_tool_call", "tool": "api",
                 "api_key": SECRETS[2], "event_id": "e2"})
        db.flush()
        assert db.count() == 2
        assert assert_no_plaintext(_db_text(db.db_path), SECRETS) == []
    finally:
        db.shutdown()


def test_buffer_path_redacted(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "sb.db"))
    try:
        db.enqueue_to_buffer("eid-1", "tr", "c", "sync_push",
                             {"session": SECRETS[0]})
        assert assert_no_plaintext(_db_text(db.db_path), SECRETS) == []
    finally:
        db.shutdown()


def test_async_path_redacted(tmp_path):
    import asyncio
    db = FederatedDB(db_path=str(tmp_path / "sa.db"))
    try:
        asyncio.run(db.async_push({"tool": "t", "cookie": SECRETS[1],
                                   "event_id": "ea"}))
        db.flush()
        assert assert_no_plaintext(_db_text(db.db_path), SECRETS) == []
    finally:
        db.shutdown()


def test_meta_ingest_sanitized(tmp_path):
    from meta_learner import MetaLearner
    db = FederatedDB(db_path=str(tmp_path / "sm.db"))
    try:
        ml = MetaLearner()
        ml.ingest({"tool": "t", "success": True, "delta_us": 5,
                   "token": SECRETS[0]})
        assert assert_no_plaintext(
            json.dumps(list(ml.history), default=str), SECRETS) == []
    finally:
        db.shutdown()


def test_no_eval_exec_pickle_in_src():
    src = Path(__file__).resolve().parents[1] / "src"
    import re
    banned = re.compile(r"\beval\s*\(|\bexec\s*\(|pickle\.loads|__import__\s*\(")
    hits = []
    for f in src.glob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if banned.search(line):
                hits.append("%s:%d:%s" % (f.name, i, line.strip()))
    assert hits == [], hits

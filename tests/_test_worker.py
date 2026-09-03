from __future__ import annotations
import asyncio, sys, time, random, tempfile, shutil, json
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(sys.argv[1]).resolve()
TEST_NAME = sys.argv[2]
PKG_DIR = ROOT / "hermes_enhancer"
sys.path.insert(0, str(ROOT))

import hermes_enhancer.federated_db as federated_db_mod
import hermes_enhancer.feedback_optimizer as feedback_mod
import hermes_enhancer.enhancer as enhancer_mod

FederatedDB = federated_db_mod.FederatedDB
FeedbackOptimizer = feedback_mod.FeedbackOptimizer
HermesEnhancer = enhancer_mod.HermesEnhancer


def run(name):
    tmp_dir = Path(tempfile.mkdtemp(prefix="hermes_enhancer_test_"))
    tmp_db = tmp_dir / "test.db"
    try:
        if name == "sync_db_insert_and_count":
            db = FederatedDB(db_path=str(tmp_db))
            row_id = db.push({"tool": "terminal", "duration": 0.5, "success": True}, node_id="test")
            assert row_id == 1 and db.count() == 1
            assert db.get_recent(1)[0]["payload"]["tool"] == "terminal"

        elif name == "async_db_insert_and_count":
            async def go():
                db = FederatedDB(db_path=str(tmp_db))
                await db.async_push({"tool": "async_terminal", "duration": 0.1, "success": True}, node_id="test")
                assert await db.async_count() == 1
                assert (await db.async_get_recent(1))[0]["payload"]["tool"] == "async_terminal"
            asyncio.run(go())

        elif name == "async_concurrent_writes":
            async def go():
                db = FederatedDB(db_path=str(tmp_db))
                await asyncio.gather(*[db.async_push({"tool": "concurrent", "duration": 0.1, "success": True}, node_id="test") for _ in range(20)])
                assert await db.async_count() == 20
            asyncio.run(go())

        elif name == "anomalous_flagging":
            fo = FeedbackOptimizer()
            fo.record_outcome("terminal", success=True, duration_s=0.5)
            s = fo.get_score("terminal")
            fo.record_outcome("terminal", success=True, duration_s=400.0)
            assert fo.get_score("terminal") == s
            fo.record_outcome("write_file", success=True, duration_s=-1.0)
            assert fo.get_score("write_file") == fo.default_score
            fo.record_outcome("read_file", success=True, duration_s=0.5, anomalous=True)
            assert fo.get_score("read_file") == fo.default_score

        elif name == "enhancer_anomalous_flagging":
            enhancer = HermesEnhancer(node_id="test")
            enhancer.db = FederatedDB(db_path=str(tmp_db))
            enhancer.pre_tool_call(("terminal",), {"tool": "terminal"})
            time.sleep(0.001)
            enhancer.on_post_tool_call(result="ok", tool_name="terminal", status="success", duration_ms=5)
            payload = enhancer.db.get_recent(1)[0]["payload"]
            assert payload.get("anomalous", 0) == 0
            enhancer.pre_tool_call(("terminal",), {"tool": "terminal"})
            enhancer.on_post_tool_call(result="slow", tool_name="terminal", status="success", duration_ms=9999999)
            payload = enhancer.db.get_recent(1)[0]["payload"]
            assert payload.get("anomalous", 0) == 1
            assert payload["success"] is False

        elif name == "auto_prune_on_threshold":
            db = FederatedDB(db_path=str(tmp_db), prune_threshold=50, retention_days=14)
            conn = db._get_conn()
            conn.execute("DELETE FROM sync_queue")
            old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
            for i in range(60):
                conn.execute(
                    "INSERT INTO sync_queue (payload, timestamp, node_id, tool, anomalous) VALUES (?, ?, ?, ?, ?)",
                    (json.dumps({"tool": f"tool_{i % 5}", "duration": 0.1, "success": True}), old, "test", f"tool_{i % 5}", 0),
                )
            conn.commit()
            conn.close()
            db.prune()
            assert db.count() <= 50, f"got {db.count()}"
            conn = db._get_conn()
            s = conn.execute("SELECT COUNT(*) FROM summary_analytics").fetchone()[0]
            conn.close()
            assert s > 0

        elif name == "manual_prune_cli":
            db = FederatedDB(db_path=str(tmp_db), prune_threshold=10, retention_days=1)
            conn = db._get_conn()
            conn.execute("DELETE FROM sync_queue")
            old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            for i in range(20):
                conn.execute(
                    "INSERT INTO sync_queue (payload, timestamp, node_id, tool, anomalous) VALUES (?, ?, ?, ?, ?)",
                    (json.dumps({"tool": "cli_tool", "duration": 0.1, "success": True}), old, "test", "cli_tool", 0),
                )
            conn.commit()
            conn.close()
            r = db.prune()
            assert r["before_count"] > r["after_count"]
            assert r["deleted_rows"] > 0

        elif name == "feedback_ema_excludes_anomalies":
            fo = FeedbackOptimizer(alpha=0.5)
            fo.record_outcome("terminal", success=True, duration_s=1.0)
            s = fo.get_score("terminal")
            fo.record_outcome("terminal", success=True, duration_s=0.0, anomalous=True)
            assert fo.get_score("terminal") == s
            fo.record_outcome("terminal", success=True, duration_s=-5.0, anomalous=True)
            assert fo.get_score("terminal") == s

        elif name == "success_definition_empty_result":
            e = HermesEnhancer(node_id="test")
            assert e._is_empty_result("") is True
            assert e._is_empty_result([]) is True
            assert e._is_empty_result(None) is True
            assert e._is_empty_result("ok") is False

        else:
            raise ValueError(f"Unknown test: {name}")
    except Exception as exc:
        print(f"FAIL: {name}: {exc}")
        sys.exit(1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"PASS: {name}")
    sys.exit(0)

if __name__ == "__main__":
    run(sys.argv[2])

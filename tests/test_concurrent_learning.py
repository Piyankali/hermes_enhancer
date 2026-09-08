"""v0.23 concurrent-learning tests: threads + duplicate event_ids."""
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from federated_db import FederatedDB
from feedback_optimizer import FeedbackOptimizer
from predictive_preload import PredictivePreload


def test_concurrent_duplicate_event_id_single_row(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "dup.db"))
    try:
        eid = uuid.uuid4().hex
        results = []

        def worker():
            results.append(db.push({"hook": "post_tool_call", "tool": "t",
                                    "event_id": eid, "trace_id": "tr",
                                    "tool_call_id": "c"}))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        db.flush()
        assert db.count() == 1, results
        assert all(r == results[0] for r in results)
    finally:
        db.shutdown()


def test_concurrent_learner_updates_safe(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "cl.db"))
    try:
        fo = FeedbackOptimizer()
        pp = PredictivePreload()
        lock = threading.Lock()

        def worker(n):
            for i in range(50):
                with lock:
                    fo.record_outcome("t%d" % (n % 4), success=bool(i % 2),
                                      duration_s=1.0)
                    pp.record("t%d" % (n % 4))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert fo.save_to_db(db) == 4
        assert pp.save_to_db(db) > 0
        fo2 = FeedbackOptimizer()
        assert fo2.load_from_db(db) == 4
        assert sum(fo2.samples.values()) == 8 * 50
    finally:
        db.shutdown()


def test_concurrent_enqueue_no_loss(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "q.db"), db_max_queue=5000)
    try:
        def worker(n):
            for i in range(25):
                db.enqueue_event({"tool": "t", "n": n, "i": i,
                                  "event_id": uuid.uuid4().hex})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        db.flush()
        assert db.count() == 200, db.get_counters()
    finally:
        db.shutdown()

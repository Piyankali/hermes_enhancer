"""v0.22 10K+ stress and telemetry reconciliation tests.

Isolated tmp_path databases only. Counts are verified against SQLite
directly (independent raw connections); counters are cross-checked, not
trusted. Event IDs are deterministic (uuid5) so every run is reproducible.
"""
import gc
import os
import sqlite3
import sys
import threading
import time
import uuid

sys.path.insert(0, "src")
from federated_db import FederatedDB

SEQ_N = 10000
CONC_THREADS = 8
CONC_PER_THREAD = 1250  # 8 x 1250 = 10000
BACKLOG_N = 500
RECOVERY_BOUND = 100


def det_eid(tag, i):
    return uuid.uuid5(uuid.NAMESPACE_URL, "%s-%d" % (tag, i)).hex


def q(db_path, sql, params=()):
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def rss_kb():
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return -1


def db_size_bytes(db_path):
    total = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            total += os.path.getsize(db_path + suffix)
        except OSError:
            pass
    return total


def unique_final_ids(db_path):
    return [r[0] for r in q(db_path, "SELECT event_id FROM sync_queue")]


def duplicates(db_path):
    return q(
        db_path,
        "SELECT event_id, COUNT(*) FROM sync_queue"
        " GROUP BY event_id HAVING COUNT(*) > 1",
    )


def make_payload(tag, i):
    eid = det_eid(tag, i)
    return {
        "tool": "stress",
        "hook": "pre_tool_call",
        "idx": i,
        "event_id": eid,
        "trace_id": det_eid(tag + "-trace", i),
        "tool_call_id": det_eid(tag + "-call", i),
    }


def test_10k_sequential_push(tmp_path):
    """10K sync pushes: buffer -> final -> finalize, 0 loss, 0 dupes."""
    db_path = str(tmp_path / "stress_seq.db")
    db = FederatedDB(db_path=db_path)
    gc.collect()
    base_rss = rss_kb()
    peak_rss = base_rss
    size_before = db_size_bytes(db_path)

    t0 = time.monotonic()
    for i in range(SEQ_N):
        db.push(make_payload("seq", i))
        if i % 500 == 0:
            peak_rss = max(peak_rss, rss_kb())
    elapsed = time.monotonic() - t0
    peak_rss = max(peak_rss, rss_kb())
    db.flush()
    db.shutdown()
    gc.collect()
    final_rss = rss_kb()

    ids = unique_final_ids(db_path)
    assert len(ids) == SEQ_N, "lost events: %d/%d" % (len(ids), SEQ_N)
    assert len(set(ids)) == SEQ_N
    assert duplicates(db_path) == []
    summary = FederatedDB(db_path=db_path)
    try:
        s = summary.get_buffer_summary()
        assert s["total"] == SEQ_N
        assert s.get("PERSISTED", 0) == SEQ_N
        v = summary.verify_database()
        assert v["healthy"] is True, v
        assert v["quick_check"] == "PASS"
        assert v["integrity_check"] == "PASS"
    finally:
        summary.shutdown()

    print("SEQ events=%d elapsed=%.1fs eps=%.0f base_rss=%dKB peak_rss=%dKB "
          "final_rss=%dKB db_before=%dB db_after=%dB" % (
              SEQ_N, elapsed, SEQ_N / max(elapsed, 1e-9), base_rss,
              peak_rss, final_rss, size_before, db_size_bytes(db_path)))
    assert peak_rss - base_rss < 200 * 1024, "unbounded memory growth"


def test_worker_backlog_bounded_and_drains(tmp_path):
    """Queue bound enforced; persistent buffer absorbs burst; all drain."""
    db_path = str(tmp_path / "stress_burst.db")
    db = FederatedDB(db_path=db_path, db_max_queue=50)

    # The worker drains concurrently, so a single fill attempt is racy;
    # instead fire many rapid attempts: refusals must occur (bound holds)
    # while every accepted event is accounted for.
    accepted_ids, refused = [], 0
    for i in range(400):
        eid = det_eid("bound", i)
        rid = db.enqueue_event({"tool": "burst"}, event_id=eid)
        if rid is None:
            refused += 1
        else:
            accepted_ids.append(eid)
    assert refused >= 1, "queue bound never engaged"
    assert db.get_counters()["events_dropped"] == refused
    db.flush()
    finals = unique_final_ids(db_path)
    assert len(finals) == len(accepted_ids)
    assert set(finals) == set(accepted_ids), "accepted event lost"
    assert duplicates(db_path) == []
    db.shutdown()

    # Async burst: durable buffer absorbs what the worker has not drained.
    db2 = FederatedDB(db_path=str(tmp_path / "stress_burst2.db"))
    n = 300
    for i in range(n):
        eid = det_eid("burst", i)
        assert db2.enqueue_event({"tool": "burst"}, event_id=eid) is not None
    db2.flush()
    ids = unique_final_ids(str(tmp_path / "stress_burst2.db"))
    assert len(ids) == n and len(set(ids)) == n
    db2.shutdown()


def test_concurrent_producers_10k(tmp_path):
    """8 producers x 1250 pushes: 10K unique finals, 0 dupes, 0 loss."""
    db_path = str(tmp_path / "stress_conc.db")
    db = FederatedDB(db_path=db_path)
    errors = []

    def worker(t):
        try:
            for i in range(CONC_PER_THREAD):
                db.push(make_payload("conc-%d" % t, i))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker, args=(t,))
               for t in range(CONC_THREADS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=300)
    elapsed = time.monotonic() - t0
    assert not errors, errors[:1]
    assert all(not th.is_alive() for th in threads)
    db.flush()
    db.shutdown()

    total = CONC_THREADS * CONC_PER_THREAD
    ids = unique_final_ids(db_path)
    assert len(ids) == total, "lost: %d/%d" % (len(ids), total)
    assert len(set(ids)) == total
    assert duplicates(db_path) == []
    print("CONC events=%d elapsed=%.1fs eps=%.0f" % (
        total, elapsed, total / max(elapsed, 1e-9)))


def test_recovery_backlog_500_bounded(tmp_path):
    """500 pending buffers drain in <=100 batches across invocations."""
    db_path = str(tmp_path / "stress_backlog.db")
    db = FederatedDB(db_path=db_path)
    for i in range(BACKLOG_N):
        eid = det_eid("backlog", i)
        assert db.enqueue_to_buffer(eid, "t", "c", "pre_tool_call",
                                    {"tool": "x"}) is True

    batches = []
    for _ in range(10):
        r = db.recover_buffered_events()
        batches.append(r["recovered"] + r["skipped"])
        pending = q(db_path, "SELECT COUNT(*) FROM event_buffer"
                             " WHERE state!='PERSISTED'")[0][0]
        if pending == 0:
            break
    db.shutdown()

    assert all(b <= RECOVERY_BOUND for b in batches), batches
    assert sum(batches) == BACKLOG_N, batches
    assert len(batches) == 5, batches  # 500 / 100 exact
    ids = unique_final_ids(db_path)
    assert len(ids) == BACKLOG_N and len(set(ids)) == BACKLOG_N
    assert duplicates(db_path) == []
    print("BACKLOG batches=%r" % (batches,))


def test_telemetry_reconciliation(tmp_path):
    """Counters vs DB truth: documented semantics, no lies."""
    db_path = str(tmp_path / "stress_tel.db")
    db = FederatedDB(db_path=db_path)

    n_sync, n_async = 200, 50
    for i in range(n_sync):
        db.push(make_payload("tel-sync", i))
    import asyncio
    for i in range(n_async):
        asyncio.run(db.async_push(make_payload("tel-async", i)))
    db.flush()
    db_total = q(db_path, "SELECT COUNT(*) FROM sync_queue")[0][0]
    counters = db.get_counters()
    db.shutdown()

    # Sync pushes persist without touching the in-memory queue.
    assert db_total == n_sync + n_async
    assert counters["events_persisted"] == n_sync + n_async, counters
    assert counters["events_enqueued"] == n_async, counters
    assert counters["events_dropped"] == 0
    assert counters["events_failed"] == 0
    print("TELEMETRY db=%d persisted=%d enqueued=%d dropped=%d failed=%d "
          "retried=%d" % (db_total, counters["events_persisted"],
                          counters["events_enqueued"], counters["events_dropped"],
                          counters["events_failed"], counters["events_retried"]))

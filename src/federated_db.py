"""FederatedDB - SQLite WAL-mode persistence for telemetry sync queue.

v0.21.0-dev: Batched async persistence, bounded event queue, retry
classification, lifecycle counters, graceful shutdown, and stable event
identities.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sqlite3
import threading
import time
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

try:
    from .redaction import sanitize
except ImportError:
    from redaction import sanitize

DB_PATH = os.path.expanduser("~/.hermes/federated.db")
SCHEMA_VERSION = 2
DEFAULT_PRUNE_THRESHOLD = 5000
DEFAULT_RETENTION_DAYS = 14

DEFAULT_DB_BATCH_SIZE = 100
DEFAULT_DB_FLUSH_INTERVAL = 0.25
DEFAULT_DB_MAX_QUEUE = 5000

_EXECUTOR = ThreadPoolExecutor(max_workers=2)
logger = logging.getLogger("hermes.enhancer.db")


class FederatedDB:
    """Thread-safe SQLite database handler with batched persistence."""

    def __init__(
        self,
        db_path: str = DB_PATH,
        prune_threshold: int = DEFAULT_PRUNE_THRESHOLD,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        db_batch_size: int = DEFAULT_DB_BATCH_SIZE,
        db_flush_interval: float = DEFAULT_DB_FLUSH_INTERVAL,
        db_max_queue: int = DEFAULT_DB_MAX_QUEUE,
    ) -> None:
        self.db_path = db_path
        self.prune_threshold = prune_threshold
        self.retention_days = retention_days
        self._batch_size = db_batch_size
        self._flush_interval = db_flush_interval
        self._max_queue = db_max_queue
        self._queue: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=db_max_queue)
        self._shutdown = threading.Event()
        self._worker = threading.Thread(target=self._batch_worker, daemon=True)
        self._worker.start()
        self._counters: Dict[str, int] = {
            "events_enqueued": 0,
            "events_persisted": 0,
            "events_dropped": 0,
            "events_failed": 0,
            "events_retried": 0,
            "transient_errors": 0,
            "permanent_errors": 0,
            "unknown_errors": 0,
        }
        self._counters_lock = threading.Lock()
        self._ensure_initialized()

    # ------------------------------------------------------------------ #
    # Connection and schema
    # ------------------------------------------------------------------ #
    def _get_conn(self) -> sqlite3.Connection:
        """Create a new connection with WAL mode enabled."""
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _integrity_check(self) -> None:
        conn = self._get_conn()
        try:
            row = conn.execute("PRAGMA integrity_check;").fetchone()
            status = row[0] if row else "unknown"
            if status != "ok":
                logger.error("SQLite integrity_check failed: %s", status)
            else:
                logger.debug("SQLite integrity_check: ok")
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sync_queue)").fetchall()}
        if "tool" not in existing_cols:
            conn.execute("ALTER TABLE sync_queue ADD COLUMN tool TEXT")
        if "anomalous" not in existing_cols:
            conn.execute("ALTER TABLE sync_queue ADD COLUMN anomalous INTEGER DEFAULT 0")

        extra_cols = {
            "event_id": "TEXT DEFAULT ''",
            "trace_id": "TEXT DEFAULT ''",
            "tool_call_id": "TEXT DEFAULT ''",
            "persisted": "INTEGER DEFAULT 1",
            "retry_count": "INTEGER DEFAULT 0",
        }
        for col, decl in extra_cols.items():
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE sync_queue ADD COLUMN {col} {decl}")

        sa_cols = {row["name"] for row in conn.execute("PRAGMA table_info(summary_analytics)").fetchall()}
        sa_pk = conn.execute("PRAGMA table_info(summary_analytics)").fetchall()
        has_pk = any(row["pk"] > 0 for row in sa_pk)
        if not has_pk and sa_cols:
            try:
                conn.execute("ALTER TABLE summary_analytics RENAME TO summary_analytics_legacy")
                conn.execute(
                    """
                    CREATE TABLE summary_analytics (
                        tool TEXT,
                        day TEXT NOT NULL,
                        avg_duration REAL,
                        total_calls INTEGER,
                        success_rate REAL,
                        pruned_at TEXT NOT NULL,
                        PRIMARY KEY (tool, day)
                    );
                    """
                )
                cols = sorted(sa_cols)
                insert_sql = (
                    "INSERT INTO summary_analytics (" + ", ".join(cols) + ") "
                    "SELECT " + ", ".join(cols) + " FROM summary_analytics_legacy"
                )
                conn.execute(insert_sql)
                conn.execute("DROP TABLE summary_analytics_legacy")
            except Exception:
                pass
        conn.commit()

    def _ensure_initialized(self) -> None:
        conn = self._get_conn()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    node_id TEXT DEFAULT 'local',
                    tool TEXT,
                    anomalous INTEGER DEFAULT 0,
                    event_id TEXT DEFAULT '',
                    trace_id TEXT DEFAULT '',
                    tool_call_id TEXT DEFAULT '',
                    persisted INTEGER DEFAULT 1,
                    retry_count INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS event_buffer (
                    event_id TEXT UNIQUE NOT NULL PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'CREATED',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    next_retry_at TEXT,
                    buffered_at TEXT NOT NULL,
                    persisted_at TEXT,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    checksum TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_event_buffer_event_id
                    ON event_buffer(event_id);
                CREATE TABLE IF NOT EXISTS summary_analytics (
                    tool TEXT,
                    day TEXT NOT NULL,
                    avg_duration REAL,
                    total_calls INTEGER,
                    success_rate REAL,
                    pruned_at TEXT NOT NULL,
                    PRIMARY KEY (tool, day)
                );
                CREATE TABLE IF NOT EXISTS skill_graph_nodes (
                    skill TEXT PRIMARY KEY,
                    meta TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS skill_graph_edges (
                    src TEXT NOT NULL,
                    dst TEXT NOT NULL,
                    PRIMARY KEY (src, dst)
                );
                CREATE TABLE IF NOT EXISTS winning_workflows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow TEXT NOT NULL,
                    score REAL NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_feedback (
                    tool TEXT PRIMARY KEY,
                    score REAL NOT NULL DEFAULT 0.5,
                    samples INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_transitions (
                    prev_key TEXT NOT NULL,
                    next_tool TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (prev_key, next_tool)
                );
                CREATE TABLE IF NOT EXISTS meta_tool_stats (
                    tool TEXT PRIMARY KEY,
                    calls INTEGER NOT NULL DEFAULT 0,
                    fails INTEGER NOT NULL DEFAULT 0,
                    total_duration_us INTEGER NOT NULL DEFAULT 0,
                    last_updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta_sequences (
                    seq_key TEXT PRIMARY KEY,
                    pattern TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    current_tool TEXT NOT NULL,
                    predicted_tool TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS orphan_events (
                    event_id TEXT PRIMARY KEY,
                    tool_call_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    pre_ts TEXT NOT NULL,
                    marked_at TEXT NOT NULL
                );
            """
            )
            conn.commit()
            self._migrate(conn)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_sync_queue_timestamp
                    ON sync_queue(timestamp);
                CREATE INDEX IF NOT EXISTS idx_sync_queue_tool
                    ON sync_queue(tool);
                CREATE INDEX IF NOT EXISTS idx_sync_queue_event_id
                    ON sync_queue(event_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_queue_event_id_unique
                    ON sync_queue(event_id) WHERE event_id != '';
                CREATE INDEX IF NOT EXISTS idx_sync_queue_trace_id
                    ON sync_queue(trace_id);
                CREATE INDEX IF NOT EXISTS idx_sync_queue_tool_call_id
                    ON sync_queue(tool_call_id);
                CREATE INDEX IF NOT EXISTS idx_predictions_tool
                    ON predictions(current_tool, status);
                """
            )
            conn.commit()
        finally:
            conn.close()
        self._integrity_check()
        self._startup_recovery()

    # ------------------------------------------------------------------ #
    # v0.22 buffer management
    # ------------------------------------------------------------------ #
    def enqueue_to_buffer(self, event_id, trace_id, tool_call_id, event_type, payload):
        import json as _json
        try:
            payload = sanitize(payload)
        except Exception:
            pass
        if isinstance(payload, dict):
            payload = _json.dumps(payload)
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO event_buffer"
                " (event_id, trace_id, tool_call_id, event_type, payload,"
                " created_at, state, attempt_count, last_error, next_retry_at,"
                " buffered_at, persisted_at, schema_version, checksum)"
                " VALUES (?, ?, ?, ?, ?, datetime('now'), 'CREATED', 0, NULL, NULL,"
                " datetime('now'), NULL, 1, NULL)",
                (event_id, trace_id, tool_call_id, event_type, payload),
            )
            if cur.rowcount > 0:
                conn.commit()
                return True
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def mark_buffer_persisted(self, event_id):
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE event_buffer SET state='PERSISTED',"
                " persisted_at=datetime('now')"
                " WHERE event_id=? AND state!='PERSISTED'",
                (event_id,),
            )
            if cur.rowcount > 0:
                conn.commit()
                return True
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_buffer_event(self, event_id):
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT event_id, trace_id, tool_call_id, event_type, payload,"
                " created_at, state, attempt_count, last_error, next_retry_at,"
                " buffered_at, persisted_at, schema_version, checksum"
                " FROM event_buffer WHERE event_id=?",
                (event_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "event_id": row[0],
                "trace_id": row[1],
                "tool_call_id": row[2],
                "event_type": row[3],
                "payload": row[4],
                "created_at": row[5],
                "state": row[6],
                "attempt_count": row[7],
                "last_error": row[8],
                "next_retry_at": row[9],
                "buffered_at": row[10],
                "persisted_at": row[11],
                "schema_version": row[12],
                "checksum": row[13],
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_buffer_summary(self):
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT state, COUNT(*) FROM event_buffer GROUP BY state"
            )
            by_state = {r[0]: r[1] for r in cur.fetchall()}
            total = sum(by_state.values())
            summary = {"total": total}
            summary.update(by_state)
            return summary
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_event(self, event_id):
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT id, payload, timestamp, node_id, event_id,"
                " trace_id, tool_call_id FROM sync_queue WHERE event_id=?",
                (event_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "id": row[0],
                "payload": row[1],
                "timestamp": row[2],
                "node_id": row[3],
                "event_id": row[4],
                "trace_id": row[5],
                "tool_call_id": row[6],
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def recover_buffered_events(self):
        conn = self._get_conn()
        try:
            # Bounded: at most 100 rows per invocation. Rows finalized
            # during this call are excluded by the WHERE filter, so each
            # call re-reads from the head; leftover backlog is picked up
            # by the next scheduler tick or process startup.
            batch_size = 100
            recovered = 0
            skipped = 0
            states_seen = set()
            cur = conn.execute(
                "SELECT event_id, trace_id, tool_call_id, event_type, payload,"
                " created_at, state, attempt_count, last_error, next_retry_at,"
                " buffered_at, persisted_at, schema_version, checksum"
                " FROM event_buffer"
                " WHERE state IN"
                " ('CREATED','BUFFERED','QUEUED','PERSISTING','FAILED','RETRY_WAIT')"
                " ORDER BY created_at LIMIT ?",
                (batch_size,),
            )
            rows = cur.fetchall()
            for row in rows:
                eid = row[0]
                states_seen.add(row[6])
                try:
                    json.loads(row[4])
                except Exception as exc:
                    try:
                        conn.execute(
                            "UPDATE event_buffer SET state='FAILED',"
                            " last_error=?,"
                            " attempt_count=attempt_count+1"
                            " WHERE event_id=?",
                            ("corrupt_payload: %s" % exc, eid),
                        )
                        conn.commit()
                    except Exception:
                        pass
                    continue
                try:
                    exists = self.get_event(eid)
                except Exception:
                    exists = None
                if exists is not None:
                    try:
                        self.mark_buffer_persisted(eid)
                    except Exception:
                        pass
                    skipped += 1
                else:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO sync_queue"
                            " (payload, timestamp, node_id, event_id,"
                            " trace_id, tool_call_id, persisted)"
                            " VALUES (?, datetime('now'), 'local', ?, ?, ?, 1)",
                            (row[4], eid, row[1], row[2]),
                        )
                        conn.commit()
                        try:
                            self.mark_buffer_persisted(eid)
                        except Exception:
                            pass
                        recovered += 1
                    except Exception:
                        try:
                            conn.execute(
                                "UPDATE event_buffer SET state='FAILED',"
                                " last_error='recovery_persistence_failed',"
                                " attempt_count=attempt_count+1"
                                " WHERE event_id=?",
                                (eid,),
                            )
                            conn.commit()
                        except Exception:
                            pass
            return {
                "recovered": recovered,
                "skipped": skipped,
                "states_seen": sorted(states_seen),
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def verify_database(self):
        import sqlite3 as _sqlite3
        try:
            conn = self._get_conn()
        except Exception as exc:
            return {
                "healthy": False,
                "quick_check": "CONNECT_ERROR (%s)" % exc,
                "integrity_check": "skipped",
                "error": "Database inaccessible: %s" % exc,
                "database": self.db_path,
            }
        try:
            try:
                quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            except _sqlite3.DatabaseError as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                return {
                    "healthy": False,
                    "quick_check": "FAIL (%s)" % exc,
                    "integrity_check": "skipped",
                    "error": "Database corruption detected: %s" % exc,
                    "database": self.db_path,
                }
            quick_result = "PASS" if quick == "ok" else "FAIL (%s)" % quick
            try:
                integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
                integ_result = "PASS" if integ == "ok" else "FAIL (%s)" % integ
            except Exception as exc:
                integ_result = "ERROR (%s)" % exc
            healthy = quick_result == "PASS" and integ_result == "PASS"
            try:
                conn.close()
            except Exception:
                pass
            return {
                "healthy": healthy,
                "quick_check": quick_result,
                "integrity_check": integ_result,
                "error": None if healthy else (
                    "Database health check failed: quick=%s, integrity=%s"
                    % (quick_result, integ_result)
                ),
                "database": self.db_path,
            }
        except _sqlite3.OperationalError as exc:
            try:
                conn.close()
            except Exception:
                pass
            return {
                "healthy": False,
                "quick_check": "OPERATIONAL_ERROR (%s)" % exc,
                "integrity_check": "skipped",
                "error": "Database inaccessible: %s" % exc,
                "database": self.db_path,
            }
        except Exception as exc:
            try:
                conn.close()
            except Exception:
                pass
            return {
                "healthy": False,
                "quick_check": "ERROR (%s)" % exc,
                "integrity_check": "skipped",
                "error": "Unexpected health check error: %s" % exc,
                "database": self.db_path,
            }

    def _startup_recovery(self):
        try:
            result = self.recover_buffered_events()
        except Exception as exc:
            try:
                logger.warning("startup recovery failed: %s", exc)
            except Exception:
                pass
            self._startup_recovery_info = {
                "executed": True,
                "recovered": 0,
                "error": str(exc),
            }
            return self._startup_recovery_info
        if isinstance(result, dict):
            recovered = int(result.get("recovered", 0)) + int(
                result.get("skipped", 0)
            )
        else:
            recovered = int(result)
        info = {"executed": True, "recovered": recovered, "error": None}
        self._startup_recovery_info = info
        return info

    def get_startup_recovery_info(self):
        return getattr(self, "_startup_recovery_info", {"executed": False})

    # ------------------------------------------------------------------ #
    # Retry logic
    # ------------------------------------------------------------------ #
    def _with_retry(self, fn, *args, **kwargs):
        backoffs = [0.0, 0.5, 1.5]
        last_exc = None
        for delay in backoffs:
            try:
                if delay:
                    time.sleep(delay)
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "busy" in str(exc).lower() or "locked" in str(exc).lower():
                    with self._counters_lock:
                        self._counters["events_retried"] += 1
                        self._counters["transient_errors"] += 1
                    continue
                with self._counters_lock:
                    self._counters["permanent_errors"] += 1
                raise
            except Exception:
                with self._counters_lock:
                    self._counters["unknown_errors"] += 1
                raise
        if last_exc is None:
            raise RuntimeError("Database operation failed after retries")
        raise last_exc

    # ------------------------------------------------------------------ #
    # Insert helpers
    # ------------------------------------------------------------------ #
    def _insert_sync(
        self,
        conn,
        payload,
        now,
        node_id,
        tool,
        anomalous,
        event_id="",
        trace_id="",
        tool_call_id="",
        persisted=1,
        retry_count=0,
    ):
        # Idempotent final persistence: the partial unique index on
        # sync_queue(event_id) turns a retried insert of the same event_id
        # into a no-op; the existing row id is returned instead.
        cur = conn.execute(
            "INSERT OR IGNORE INTO sync_queue "
            "(payload, timestamp, node_id, tool, anomalous, event_id, trace_id, tool_call_id, persisted, retry_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                json.dumps(payload, default=str),
                now,
                node_id,
                tool,
                anomalous,
                event_id,
                trace_id,
                tool_call_id,
                persisted,
                retry_count,
            ),
        )
        if cur.lastrowid:
            return cur.lastrowid
        if event_id:
            row = conn.execute(
                "SELECT id FROM sync_queue WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is not None:
                return row[0]
        return 0

    # ------------------------------------------------------------------ #
    # Public sync API (backward compatible)
    # ------------------------------------------------------------------ #
    def push(self, payload: dict, node_id: str = "local") -> int:
        payload = sanitize(payload)

        def _do_push(conn=None):
            created_here = conn is None
            if created_here:
                conn = self._get_conn()
            try:
                now = datetime.now(timezone.utc).isoformat()
                tool = payload.get("tool") or payload.get("tool_name") or "unknown"
                anomalous = 1 if payload.get("anomalous") else 0
                event_id = payload.get("event_id") or uuid.uuid4().hex
                trace_id = payload.get("trace_id") or uuid.uuid4().hex
                tool_call_id = payload.get("tool_call_id") or ""
                # Crash-safe ordering: the buffer commit must succeed before
                # the event is considered queued. A buffer-write failure is
                # fail-loud (propagates) so durability is never falsely
                # reported; INSERT OR IGNORE means duplicates return False,
                # never raise.
                self.enqueue_to_buffer(
                    event_id, trace_id, tool_call_id, "sync_push", payload
                )
                row_id = self._with_retry(
                    self._insert_sync,
                    conn,
                    payload,
                    now,
                    node_id,
                    tool,
                    anomalous,
                    event_id,
                    trace_id,
                    tool_call_id,
                )
                conn.commit()
                with self._counters_lock:
                    self._counters["events_persisted"] += 1
                try:
                    self.mark_buffer_persisted(event_id)
                except Exception:
                    pass
                self._maybe_prune(conn)
                return row_id
            finally:
                if created_here:
                    conn.close()

        return self._with_retry(_do_push)

    # ------------------------------------------------------------------ #
    # Event queue / batch API
    # ------------------------------------------------------------------ #
    @staticmethod
    def _generate_event_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _generate_trace_id() -> str:
        return uuid.uuid4().hex

    def enqueue_event(
        self,
        payload: Dict[str, Any],
        node_id: str = "local",
        event_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> Optional[str]:
        """Enqueue a telemetry event for batched persistence.

        Returns the event_id on success, or None if the queue is full.
        The payload is sanitized (secrets redacted) before anything is
        buffered or queued, so plaintext secrets never reach SQLite.
        """
        payload = sanitize(payload)
        if self._shutdown.is_set():
            with self._counters_lock:
                self._counters["events_dropped"] += 1
            return None

        event: Dict[str, Any] = {
            "event_id": event_id or self._generate_event_id(),
            "trace_id": trace_id or self._generate_trace_id(),
            "payload": payload,
            "node_id": node_id,
            "queued_at": time.time(),
            "tool_call_id": tool_call_id or "",
            "status": "queued",
        }
        try:
            self._queue.put_nowait(event)
            with self._counters_lock:
                self._counters["events_enqueued"] += 1
            return event["event_id"]
        except queue.Full:
            with self._counters_lock:
                self._counters["events_dropped"] += 1
            return None

    def _flush_buffer(self, buffer):
        if not buffer:
            return
        max_attempts = 3
        backoffs = [0.0, 0.5, 1.5]
        last_exc = None
        for attempt, delay in enumerate(backoffs):
            if delay:
                time.sleep(delay)
            conn = self._get_conn()
            try:
                conn.execute("BEGIN")
                for event in buffer:
                    payload = event["payload"]
                    now = datetime.now(timezone.utc).isoformat()
                    tool = payload.get("tool") or payload.get("tool_name") or "unknown"
                    anomalous = 1 if payload.get("anomalous") else 0
                    self._insert_sync(
                        conn,
                        payload,
                        now,
                        event.get("node_id", "local"),
                        tool,
                        anomalous,
                        event_id=event.get("event_id", ""),
                        trace_id=event.get("trace_id", ""),
                        tool_call_id=event.get("tool_call_id", ""),
                    )
                conn.commit()
                with self._counters_lock:
                    self._counters["events_persisted"] += len(buffer)
                for event in buffer:
                    event["status"] = "persisted"
                    try:
                        eid = event.get("event_id", "")
                        if eid:
                            self.mark_buffer_persisted(eid)
                    except Exception:
                        pass
                self._maybe_prune(conn)
                return
            except sqlite3.OperationalError as exc:
                last_exc = exc
                conn.rollback()
                if "busy" in str(exc).lower() or "locked" in str(exc).lower():
                    with self._counters_lock:
                        self._counters["events_retried"] += len(buffer)
                        self._counters["transient_errors"] += 1
                    continue
                with self._counters_lock:
                    self._counters["permanent_errors"] += 1
                break
            except Exception as exc:
                last_exc = exc
                conn.rollback()
                with self._counters_lock:
                    self._counters["unknown_errors"] += 1
                break
            finally:
                conn.close()

        for event in buffer:
            event["status"] = "failed"
        with self._counters_lock:
            self._counters["events_failed"] += len(buffer)
        if last_exc:
            logger.warning("Batch flush failed after retries: %s", last_exc)

    def _batch_worker(self):
        buffer = []
        last_flush = time.monotonic()
        while not self._shutdown.is_set() or buffer:
            try:
                item = self._queue.get(timeout=0.05)
                buffer.append(item)
            except queue.Empty:
                pass

            should_flush = (
                len(buffer) >= self._batch_size
                or (buffer and time.monotonic() - last_flush >= self._flush_interval)
                or (self._shutdown.is_set() and not self._queue.empty() and len(buffer) > 0)
            )
            if should_flush:
                self._flush_buffer(buffer)
                for _ in buffer:
                    self._queue.task_done()
                buffer.clear()
                last_flush = time.monotonic()

    def flush(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Block until queued events are persisted or timeout expires."""
        start = time.monotonic()
        self._queue.join()
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            logger.warning("flush() blocked longer than timeout: %.2fs", elapsed)
        return {
            "flushed": True,
            "elapsed_s": round(elapsed, 4),
            "queue_remaining": self._queue.qsize(),
        }

    async def async_flush(self, timeout: float = 5.0) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self.flush, timeout)

    def shutdown(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Graceful shutdown: stop accepting, drain queue, close worker."""
        self._shutdown.set()
        self._worker.join(timeout=timeout)
        if self._worker.is_alive():
            remaining = self._queue.qsize()
            logger.warning("Batch worker did not finish; %d events may be lost", remaining)
            return {
                "shutdown": "timeout",
                "persisted": self._counters["events_persisted"],
                "dropped": self._counters["events_dropped"],
                "failed": self._counters["events_failed"],
                "queued_remaining": remaining,
            }
        return {
            "shutdown": "clean",
            "persisted": self._counters["events_persisted"],
            "dropped": self._counters["events_dropped"],
            "failed": self._counters["events_failed"],
            "queued_remaining": 0,
        }

    def get_counters(self) -> Dict[str, int]:
        with self._counters_lock:
            return dict(self._counters)

    def get_event_counters(self) -> Dict[str, int]:
        return self.get_counters()

    # ------------------------------------------------------------------ #
    # Query helpers
    # ------------------------------------------------------------------ #
    def get_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT id, payload, timestamp, node_id, tool, anomalous, event_id, trace_id, tool_call_id, persisted, retry_count "
                "FROM sync_queue ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [
                {
                    "id": row["id"],
                    "payload": json.loads(row["payload"]),
                    "timestamp": row["timestamp"],
                    "node_id": row["node_id"],
                    "tool": row["tool"],
                    "anomalous": row["anomalous"],
                    "event_id": row["event_id"],
                    "trace_id": row["trace_id"],
                    "tool_call_id": row["tool_call_id"],
                    "persisted": row["persisted"],
                    "retry_count": row["retry_count"],
                }
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._get_conn()
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM sync_queue")
            result = cursor.fetchone()
            return int(result[0]) if result else 0
        finally:
            conn.close()

    def summary_counts(self) -> dict[str, Any]:
        conn = self._get_conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM sync_queue").fetchone()[0]
            anomalous = conn.execute(
                "SELECT COUNT(*) FROM sync_queue WHERE anomalous = 1"
            ).fetchone()[0]
            pre = conn.execute(
                "SELECT COUNT(*) FROM sync_queue WHERE json_extract(payload, '$.hook') = 'pre_tool_call'"
            ).fetchone()[0]
            post = conn.execute(
                "SELECT COUNT(*) FROM sync_queue WHERE json_extract(payload, '$.hook') = 'post_tool_call'"
            ).fetchone()[0]
            errors = conn.execute(
                "SELECT COUNT(*) FROM sync_queue WHERE json_extract(payload, '$.success') = 0"
            ).fetchone()[0]
            return {
                "total": total,
                "anomalous": anomalous,
                "pre_tool_call": pre,
                "post_tool_call": post,
                "failed": errors,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Pruning
    # ------------------------------------------------------------------ #
    def _maybe_prune(self, conn: sqlite3.Connection) -> None:
        count = conn.execute("SELECT COUNT(*) FROM sync_queue").fetchone()[0]
        if count < self.prune_threshold:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        cutoff_iso = cutoff.isoformat()
        conn.execute(
            """
            INSERT INTO summary_analytics (tool, day, avg_duration, total_calls, success_rate, pruned_at)
            SELECT
                tool,
                DATE(timestamp) AS day,
                AVG(CAST(json_extract(payload, '$.duration') AS REAL)) AS avg_duration,
                COUNT(*) AS total_calls,
                AVG(CAST(json_extract(payload, '$.success') AS REAL)) AS success_rate,
                ?
            FROM sync_queue
            WHERE timestamp < ?
            GROUP BY tool, DATE(timestamp)
            ON CONFLICT(tool, day) DO UPDATE SET
                avg_duration = excluded.avg_duration,
                total_calls = excluded.total_calls,
                success_rate = excluded.success_rate,
                pruned_at = excluded.pruned_at
            """,
            (datetime.now(timezone.utc).isoformat(), cutoff_iso),
        )
        conn.execute("DELETE FROM sync_queue WHERE timestamp < ?", (cutoff_iso,))
        conn.commit()

    def prune(self) -> dict[str, Any]:
        conn = self._get_conn()
        try:
            before_count = conn.execute("SELECT COUNT(*) FROM sync_queue").fetchone()[0]
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
            cutoff_iso = cutoff.isoformat()
            conn.execute(
                """
                INSERT INTO summary_analytics (tool, day, avg_duration, total_calls, success_rate, pruned_at)
                SELECT
                    tool,
                    DATE(timestamp) AS day,
                    AVG(CAST(json_extract(payload, '$.duration') AS REAL)) AS avg_duration,
                    COUNT(*) AS total_calls,
                    AVG(CAST(json_extract(payload, '$.success') AS REAL)) AS success_rate,
                    ?
                FROM sync_queue
                WHERE timestamp < ?
                GROUP BY tool, DATE(timestamp)
                ON CONFLICT(tool, day) DO UPDATE SET
                    avg_duration = excluded.avg_duration,
                    total_calls = excluded.total_calls,
                    success_rate = excluded.success_rate,
                    pruned_at = excluded.pruned_at
                """,
                (datetime.now(timezone.utc).isoformat(), cutoff_iso),
            )
            deleted = int(conn.execute("DELETE FROM sync_queue WHERE timestamp < ?", (cutoff_iso,)).rowcount or 0)
            conn.commit()
            after_count = conn.execute("SELECT COUNT(*) FROM sync_queue").fetchone()[0]
            return {
                "before_count": before_count,
                "after_count": after_count,
                "deleted_rows": deleted,
                "retention_days": self.retention_days,
                "cutoff": cutoff_iso,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Async wrappers
    # ------------------------------------------------------------------ #
    def _enqueue_durable(self, payload: dict, node_id: str = "local"):
        payload = sanitize(payload)
        event_id = payload.get("event_id") or self._generate_event_id()
        trace_id = payload.get("trace_id") or self._generate_trace_id()
        tool_call_id = payload.get("tool_call_id") or ""
        # Same fail-loud rule as push(): no silent buffering bypass.
        self.enqueue_to_buffer(
            event_id, trace_id, tool_call_id, "async_push", payload
        )
        return self.enqueue_event(
            payload,
            node_id,
            event_id=event_id,
            trace_id=trace_id,
            tool_call_id=tool_call_id,
        )

    async def async_push(self, payload: dict, node_id: str = "local") -> Optional[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self._enqueue_durable, payload, node_id)

    async def async_get_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self.get_recent, limit)

    async def async_count(self) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self.count)

    async def async_summary_counts(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self.summary_counts)

    async def async_prune(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self.prune)


    # ------------------------------------------------------------------ #
    # v0.23 persistent learning / predictions / orphans
    # ------------------------------------------------------------------ #
    def get_schema_version(self) -> str:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            return row[0] if row else "1"
        finally:
            conn.close()

    def save_feedback(self, rows: list[dict[str, Any]]) -> int:
        """Idempotent upsert of per-tool EMA state. Returns rows written."""
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute("BEGIN")
            for r in rows:
                conn.execute(
                    "INSERT INTO tool_feedback (tool, score, samples, successes,"
                    " failures, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(tool) DO UPDATE SET score=excluded.score,"
                    " samples=excluded.samples, successes=excluded.successes,"
                    " failures=excluded.failures, updated_at=excluded.updated_at",
                    (str(r["tool"]), float(r["score"]), int(r["samples"]),
                     int(r.get("successes", 0)), int(r.get("failures", 0)), now),
                )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def load_feedback(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            return [
                {"tool": r[0], "score": r[1], "samples": r[2],
                 "successes": r[3], "failures": r[4]}
                for r in conn.execute(
                    "SELECT tool, score, samples, successes, failures"
                    " FROM tool_feedback"
                ).fetchall()
            ]
        finally:
            conn.close()

    def save_transitions(self, rows: list[dict[str, Any]]) -> int:
        """Idempotent upsert of Markov transition counts."""
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute("BEGIN")
            for r in rows:
                conn.execute(
                    "INSERT INTO tool_transitions (prev_key, next_tool, count,"
                    " updated_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(prev_key, next_tool) DO UPDATE"
                    " SET count=excluded.count, updated_at=excluded.updated_at",
                    (str(r["prev_key"]), str(r["next_tool"]), int(r["count"]), now),
                )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def load_transitions(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            return [
                {"prev_key": r[0], "next_tool": r[1], "count": r[2]}
                for r in conn.execute(
                    "SELECT prev_key, next_tool, count FROM tool_transitions"
                ).fetchall()
            ]
        finally:
            conn.close()

    def prune_transitions(self, keep_top_per_key: int = 20) -> int:
        """Bound transition table growth: keep top-N targets per prev_key."""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM tool_transitions WHERE rowid NOT IN ("
                " SELECT rowid FROM ("
                "  SELECT rowid, ROW_NUMBER() OVER (PARTITION BY prev_key"
                "   ORDER BY count DESC) AS rn FROM tool_transitions)"
                " WHERE rn <= ?)",
                (keep_top_per_key,),
            )
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()

    def save_tool_stats(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute("BEGIN")
            for r in rows:
                conn.execute(
                    "INSERT INTO meta_tool_stats (tool, calls, fails,"
                    " total_duration_us, last_updated) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(tool) DO UPDATE SET calls=excluded.calls,"
                    " fails=excluded.fails,"
                    " total_duration_us=excluded.total_duration_us,"
                    " last_updated=excluded.last_updated",
                    (str(r["tool"]), int(r["calls"]), int(r["fails"]),
                     int(r["total_duration_us"]), now),
                )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def load_tool_stats(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            return [
                {"tool": r[0], "calls": r[1], "fails": r[2],
                 "total_duration_us": r[3]}
                for r in conn.execute(
                    "SELECT tool, calls, fails, total_duration_us"
                    " FROM meta_tool_stats"
                ).fetchall()
            ]
        finally:
            conn.close()

    def save_sequences(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute("BEGIN")
            for r in rows:
                conn.execute(
                    "INSERT INTO meta_sequences (seq_key, pattern, occurrences,"
                    " successes, updated_at) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(seq_key) DO UPDATE"
                    " SET occurrences=excluded.occurrences,"
                    " successes=excluded.successes, updated_at=excluded.updated_at",
                    (str(r["seq_key"]), str(r["pattern"]),
                     int(r["occurrences"]), int(r["successes"]), now),
                )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def load_sequences(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            return [
                {"seq_key": r[0], "pattern": r[1], "occurrences": r[2],
                 "successes": r[3]}
                for r in conn.execute(
                    "SELECT seq_key, pattern, occurrences, successes"
                    " FROM meta_sequences ORDER BY occurrences DESC LIMIT 500"
                ).fetchall()
            ]
        finally:
            conn.close()

    def log_prediction(self, current_tool: str, predicted_tool: str,
                       confidence: float) -> int:
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO predictions (created_at, current_tool,"
                " predicted_tool, confidence, status)"
                " VALUES (datetime('now'), ?, ?, ?, 'proposed')",
                (current_tool, predicted_tool, float(confidence)),
            )
            conn.commit()
            pred_id = cur.lastrowid or 0
            conn.execute(
                "DELETE FROM predictions WHERE id NOT IN ("
                " SELECT id FROM predictions ORDER BY id DESC LIMIT 500)"
            )
            conn.commit()
            return pred_id
        finally:
            conn.close()

    def get_predictions(self, current_tool: str,
                        status: str = "proposed") -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            return [
                {"id": r[0], "created_at": r[1], "current_tool": r[2],
                 "predicted_tool": r[3], "confidence": r[4], "status": r[5]}
                for r in conn.execute(
                    "SELECT id, created_at, current_tool, predicted_tool,"
                    " confidence, status FROM predictions"
                    " WHERE current_tool=? AND status=? ORDER BY id DESC LIMIT 50",
                    (current_tool, status),
                ).fetchall()
            ]
        finally:
            conn.close()

    def set_prediction_status(self, pred_id: int, status: str) -> bool:
        if status not in ("proposed", "consumed", "invalidated", "expired"):
            raise ValueError("unknown prediction status: %s" % status)
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE predictions SET status=?,"
                " consumed_at=CASE WHEN ?= 'consumed'"
                " THEN datetime('now') ELSE consumed_at END WHERE id=?",
                (status, status, pred_id),
            )
            conn.commit()
            return (cur.rowcount or 0) > 0
        finally:
            conn.close()

    def persist_learner_state(self, feedback_rows, transition_rows,
                              stat_rows, seq_rows) -> Dict[str, int]:
        """Write all learner tables over ONE connection/transaction.

        Cheaper than four separate save_* calls from the periodic hook
        path. All-or-nothing per table group; never raises past caller.
        """
        out = {"feedback": 0, "transitions": 0, "stats": 0, "sequences": 0}
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute("BEGIN")
            for r in feedback_rows or []:
                conn.execute(
                    "INSERT INTO tool_feedback (tool, score, samples, successes,"
                    " failures, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(tool) DO UPDATE SET score=excluded.score,"
                    " samples=excluded.samples, successes=excluded.successes,"
                    " failures=excluded.failures, updated_at=excluded.updated_at",
                    (str(r["tool"]), float(r["score"]), int(r["samples"]),
                     int(r.get("successes", 0)), int(r.get("failures", 0)), now),
                )
                out["feedback"] += 1
            for r in transition_rows or []:
                conn.execute(
                    "INSERT INTO tool_transitions (prev_key, next_tool, count,"
                    " updated_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(prev_key, next_tool) DO UPDATE"
                    " SET count=excluded.count, updated_at=excluded.updated_at",
                    (str(r["prev_key"]), str(r["next_tool"]), int(r["count"]), now),
                )
                out["transitions"] += 1
            for r in stat_rows or []:
                conn.execute(
                    "INSERT INTO meta_tool_stats (tool, calls, fails,"
                    " total_duration_us, last_updated) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(tool) DO UPDATE SET calls=excluded.calls,"
                    " fails=excluded.fails,"
                    " total_duration_us=excluded.total_duration_us,"
                    " last_updated=excluded.last_updated",
                    (str(r["tool"]), int(r["calls"]), int(r["fails"]),
                     int(r["total_duration_us"]), now),
                )
                out["stats"] += 1
            for r in seq_rows or []:
                conn.execute(
                    "INSERT INTO meta_sequences (seq_key, pattern, occurrences,"
                    " successes, updated_at) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(seq_key) DO UPDATE"
                    " SET occurrences=excluded.occurrences,"
                    " successes=excluded.successes, updated_at=excluded.updated_at",
                    (str(r["seq_key"]), str(r["pattern"]),
                     int(r["occurrences"]), int(r["successes"]), now),
                )
                out["sequences"] += 1
            conn.commit()
            return out
        finally:
            conn.close()

    def find_orphans(self, limit: int = 500) -> list[dict[str, Any]]:
        """Pre events with no matching post (by tool_call_id).

        Status vocabulary: completed / failed / orphaned / unknown.
        No durations are invented for orphans.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT q.tool_call_id, q.tool, q.event_id, q.timestamp"
                " FROM sync_queue q"
                " WHERE json_extract(q.payload, '$.hook') = 'pre_tool_call'"
                " AND q.tool_call_id != ''"
                " AND NOT EXISTS (SELECT 1 FROM sync_queue p"
                "  WHERE json_extract(p.payload, '$.hook') = 'post_tool_call'"
                "  AND p.tool_call_id = q.tool_call_id)"
                " ORDER BY q.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"tool_call_id": r[0], "tool": r[1], "pre_event_id": r[2],
                 "pre_ts": r[3], "status": "orphaned"}
                for r in rows
            ]
        finally:
            conn.close()

    def mark_orphans(self, limit: int = 500) -> int:
        """Record detected orphans idempotently. Returns newly marked count."""
        orphans = self.find_orphans(limit=limit)
        if not orphans:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute("BEGIN")
            added = 0
            for o in orphans:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO orphan_events (event_id,"
                    " tool_call_id, tool, pre_ts, marked_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (o["pre_event_id"], o["tool_call_id"], o["tool"],
                     o["pre_ts"], now),
                )
                added += cur.rowcount or 0
            conn.commit()
            return added
        finally:
            conn.close()


# Singleton instance for convenience
_db_instance: Optional[FederatedDB] = None


def get_db() -> FederatedDB:
    """Get or create the singleton database instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = FederatedDB()
    return _db_instance

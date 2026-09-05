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

DB_PATH = os.path.expanduser("~/.hermes/federated.db")
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
            """
            )
            conn.commit()
            self._migrate(conn)
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_sync_queue_timestamp
                    ON sync_queue(timestamp);
                CREATE INDEX IF NOT EXISTS idx_sync_queue_tool
                    ON sync_queue(tool);
                CREATE INDEX IF NOT EXISTS idx_sync_queue_event_id
                    ON sync_queue(event_id);
                CREATE INDEX IF NOT EXISTS idx_sync_queue_trace_id
                    ON sync_queue(trace_id);
            """
            )
            conn.commit()
        finally:
            conn.close()
        self._integrity_check()

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
        return conn.execute(
            "INSERT INTO sync_queue "
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
        ).lastrowid

    # ------------------------------------------------------------------ #
    # Public sync API (backward compatible)
    # ------------------------------------------------------------------ #
    def push(self, payload: dict, node_id: str = "local") -> int:
        def _do_push(conn=None):
            created_here = conn is None
            if created_here:
                conn = self._get_conn()
            try:
                now = datetime.now(timezone.utc).isoformat()
                tool = payload.get("tool") or payload.get("tool_name") or "unknown"
                anomalous = 1 if payload.get("anomalous") else 0
                row_id = self._with_retry(
                    self._insert_sync,
                    conn,
                    payload,
                    now,
                    node_id,
                    tool,
                    anomalous,
                )
                conn.commit()
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
        """
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
    async def async_push(self, payload: dict, node_id: str = "local") -> Optional[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self.enqueue_event, payload, node_id)

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


# Singleton instance for convenience
_db_instance: Optional[FederatedDB] = None


def get_db() -> FederatedDB:
    """Get or create the singleton database instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = FederatedDB()
    return _db_instance

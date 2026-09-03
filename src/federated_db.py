"""FederatedDB - SQLite WAL-mode persistence for telemetry sync queue.

v0.20.7: Added async/non-blocking I/O, automatic retention pruning,
and summary analytics aggregation.
Enterprise upgrade: WAL, busy timeout, retry backoff, integrity check,
skill graph persistence tables, winning workflows persistence.
"""

from __future__ import annotations

import asyncio
import sqlite3
import json
import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

DB_PATH = os.path.expanduser("~/.hermes/federated.db")
DEFAULT_PRUNE_THRESHOLD = 5000
DEFAULT_RETENTION_DAYS = 14
_EXECUTOR = ThreadPoolExecutor(max_workers=2)
logger = logging.getLogger("hermes.enhancer.db")


class FederatedDB:
    """Thread-safe SQLite database handler with WAL mode enabled."""

    def __init__(
        self,
        db_path: str = DB_PATH,
        prune_threshold: int = DEFAULT_PRUNE_THRESHOLD,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.db_path = db_path
        self.prune_threshold = prune_threshold
        self.retention_days = retention_days
        self._ensure_initialized()

    def _get_conn(self) -> sqlite3.Connection:
        """Create a new connection with WAL mode enabled."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _integrity_check(self) -> None:
        """Run PRAGMA integrity_check on the database at startup."""
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

    def _with_retry(self, fn, *args, **kwargs):
        """Execute fn with exponential backoff retry on SQLITE_BUSY / OperationalError."""
        backoffs = [0.0, 1.0, 2.0, 4.0]
        last_exc = None
        for delay in backoffs:
            try:
                if delay:
                    time.sleep(delay)
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "busy" in str(exc).lower() or "locked" in str(exc).lower():
                    continue
                raise
        if last_exc is None:
            raise RuntimeError("Database operation failed after retries")
        raise last_exc

    def _ensure_initialized(self) -> None:
        """Create tables if missing, migrate schema, and run integrity check."""
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    node_id TEXT DEFAULT 'local',
                    tool TEXT,
                    anomalous INTEGER DEFAULT 0
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
            """)
            conn.commit()
            self._migrate(conn)
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_sync_queue_timestamp
                    ON sync_queue(timestamp);
                CREATE INDEX IF NOT EXISTS idx_sync_queue_tool
                    ON sync_queue(tool);
            """)
            conn.commit()
        finally:
            conn.close()
        self._integrity_check()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add missing columns to legacy tables and enforce summary_analytics PK."""
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sync_queue)").fetchall()}
        if "tool" not in existing_cols:
            conn.execute("ALTER TABLE sync_queue ADD COLUMN tool TEXT")
        if "anomalous" not in existing_cols:
            conn.execute("ALTER TABLE sync_queue ADD COLUMN anomalous INTEGER DEFAULT 0")

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

    def _insert_sync(self, conn, payload, now, node_id, tool, anomalous):
        return conn.execute(
            "INSERT INTO sync_queue (payload, timestamp, node_id, tool, anomalous) VALUES (?, ?, ?, ?, ?)",
            (json.dumps(payload, default=str), now, node_id, tool, anomalous),
        ).lastrowid

    def push(self, payload: dict, node_id: str = "local") -> int:
        """Push a telemetry event to the sync queue with retry backoff."""
        def _do_push(conn=None):
            created_here = conn is None
            if created_here:
                conn = self._get_conn()
            try:
                now = datetime.now(timezone.utc).isoformat()
                tool = payload.get("tool") or payload.get("tool_name") or "unknown"
                anomalous = 1 if payload.get("anomalous") else 0
                row_id = self._with_retry(self._insert_sync, conn, payload, now, node_id, tool, anomalous)
                conn.commit()
                self._maybe_prune(conn)
                return row_id
            finally:
                if created_here:
                    conn.close()

        return self._with_retry(_do_push)

    def _maybe_prune(self, conn: sqlite3.Connection) -> None:
        """Aggregate old records and prune if threshold exceeded."""
        count = conn.execute("SELECT COUNT(*) FROM sync_queue").fetchone()[0]
        if count < self.prune_threshold:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        cutoff_iso = cutoff.isoformat()
        conn.execute("""
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
        """, (datetime.now(timezone.utc).isoformat(), cutoff_iso))
        conn.execute("DELETE FROM sync_queue WHERE timestamp < ?", (cutoff_iso,))
        conn.commit()

    def prune(self) -> dict[str, Any]:
        """Manual prune: return stats about what was aggregated/deleted."""
        conn = self._get_conn()
        try:
            before_count = conn.execute("SELECT COUNT(*) FROM sync_queue").fetchone()[0]
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
            cutoff_iso = cutoff.isoformat()
            conn.execute("""
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
            """, (datetime.now(timezone.utc).isoformat(), cutoff_iso))
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

    def get_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent records from the sync queue."""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT id, payload, timestamp, node_id, tool, anomalous "
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
                }
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    def count(self) -> int:
        """Get total count of records in sync_queue."""
        conn = self._get_conn()
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM sync_queue")
            result = cursor.fetchone()
            return int(result[0]) if result else 0
        finally:
            conn.close()

    def summary_counts(self) -> dict[str, Any]:
        """Return aggregate counts including anomalous flags."""
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

    # Async wrappers (non-blocking)
    async def async_push(self, payload: dict, node_id: str = "local") -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self.push, payload, node_id)

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

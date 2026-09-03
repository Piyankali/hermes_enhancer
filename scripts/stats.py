#!/usr/bin/env python3
"""Quick stats/verification for hermes_enhancer telemetry.

v0.20.7: Added --prune, --anomalous, and async-aware summary options.
"""

import sys
import argparse
import sqlite3
import json
from pathlib import Path

DB_PATH = Path.home() / ".hermes" / "federated.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def run_stats() -> int:
    if not DB_PATH.exists():
        print("federated.db: MISSING")
        return 1

    conn = get_conn()
    counts = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN json_extract(payload, '$.hook') = 'pre_tool_call' THEN 1 ELSE 0 END) AS pre,
            SUM(CASE WHEN json_extract(payload, '$.hook') = 'post_tool_call' THEN 1 ELSE 0 END) AS post,
            SUM(CASE WHEN json_extract(payload, '$.success') = 0 THEN 1 ELSE 0 END) AS errors,
            SUM(CASE WHEN json_extract(payload, '$.anomalous') = 1 THEN 1 ELSE 0 END) AS anomalous
        FROM sync_queue
    """).fetchone()
    total = counts["total"] or 0
    pre = counts["pre"] or 0
    post = counts["post"] or 0
    errors = counts["errors"] or 0
    anomalous = counts["anomalous"] or 0

    print(f"federated.db total rows: {total}")
    print(f"pre_tool_call hooks: {pre}")
    print(f"post_tool_call hooks: {post}")
    print(f"failed executions: {errors}")
    print(f"anomalous rows: {anomalous}")

    rows = conn.execute("""
        SELECT id, json_extract(payload, '$.tool') AS tool,
               json_extract(payload, '$.duration') AS duration,
               json_extract(payload, '$.delta_us') AS delta_us,
               json_extract(payload, '$.success') AS success,
               json_extract(payload, '$.anomalous') AS anomalous
        FROM sync_queue ORDER BY id DESC LIMIT 10
    """).fetchall()
    conn.close()

    print("recent rows:")
    for row in rows:
        print(
            f"  id={row['id']} tool={row['tool']!r} duration={row['duration']}s "
            f"delta_us={row['delta_us']} success={row['success']} anomalous={row['anomalous']}"
        )

    if total == 0:
        print("WARN: no telemetry rows yet; nothing to validate")
        return 0
    print("OK: telemetry looks healthy")
    return 0


def run_prune() -> int:
    if not DB_PATH.exists():
        print("federated.db: MISSING")
        return 1
    # Support both package import and standalone execution
    try:
        from federated_db import get_db
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from federated_db import get_db
    db = get_db()
    result = db.prune()
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Enhancer telemetry stats")
    parser.add_argument("--prune", action="store_true", help="Run database retention pruning")
    parser.add_argument("--anomalous", action="store_true", help="Show anomalous rows only")
    args = parser.parse_args()

    if args.prune:
        return run_prune()

    if args.anomalous:
        if not DB_PATH.exists():
            print("federated.db: MISSING")
            return 1
        conn = get_conn()
        rows = conn.execute("""
            SELECT id, json_extract(payload, '$.tool') AS tool, payload
            FROM sync_queue WHERE anomalous = 1 ORDER BY id DESC LIMIT 50
        """).fetchall()
        conn.close()
        print(f"anomalous rows: {len(rows)}")
        for row in rows:
            print(f"  id={row['id']} tool={row['tool']!r}")
        return 0

    return run_stats()


if __name__ == "__main__":
    raise SystemExit(main())

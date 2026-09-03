#!/usr/bin/env python3
"""Quick stats/verification for hermes_enhancer telemetry."""

import sqlite3
import json
from pathlib import Path

DB_PATH = Path.home() / ".hermes" / "federated.db"


def main() -> int:
    if not DB_PATH.exists():
        print("federated.db: MISSING")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) AS n FROM sync_queue").fetchone()["n"]
    unknown = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_queue WHERE json_extract(payload, '$.tool') = 'unknown'"
    ).fetchone()["n"]
    recent_unknown = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_queue WHERE json_extract(payload, '$.tool') = 'unknown' AND id > ?",
        (max(0, total - 50),),
    ).fetchone()["n"]
    pre = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_queue WHERE json_extract(payload, '$.hook') = 'pre_tool_call'"
    ).fetchone()["n"]
    post = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_queue WHERE json_extract(payload, '$.hook') = 'post_tool_call'"
    ).fetchone()["n"]
    errors = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_queue WHERE json_extract(payload, '$.success') = 0"
    ).fetchone()["n"]
    rows = conn.execute(
        "SELECT id, json_extract(payload, '$.tool') AS tool, json_extract(payload, '$.delta_us') AS delta_us, json_extract(payload, '$.success') AS success FROM sync_queue ORDER BY id DESC LIMIT 10"
    ).fetchall()
    conn.close()

    print(f"federated.db total rows: {total}")
    print(f"pre_tool_call hooks: {pre}")
    print(f"post_tool_call hooks: {post}")
    print(f"unknown tool entries: {unknown}")
    print(f"failed executions: {errors}")
    print("recent rows:")
    for row in rows:
        print(
            f"  id={row['id']} tool={row['tool']!r} delta_us={row['delta_us']} success={row['success']}"
        )

    if recent_unknown:
        print(f"FAIL: still logging 'unknown' tool names recently ({recent_unknown} recent)")
        return 1
    if pre == 0 or post == 0:
        print("FAIL: missing hook telemetry")
        return 1
    if total == 0:
        print("WARN: no telemetry rows yet; nothing to validate")
        return 0
    print("OK: telemetry looks healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

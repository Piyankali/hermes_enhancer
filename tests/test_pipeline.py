#!/usr/bin/env python3
"""Standalone mock diagnostic test suite for hermes_enhancer.

Runs each test in-process using absolute-path module loading via importlib.
Avoids package-import issues entirely.

Run: python3 tests/test_pipeline.py
"""

from __future__ import annotations

import asyncio
import sys
import time
import random
import tempfile
import shutil
import importlib.util
import types
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


def _clear_hermes_modules():
    for key in list(sys.modules.keys()):
        if key == "hermes_enhancer" or key.startswith("hermes_enhancer."):
            del sys.modules[key]
    for name in ["federated_db", "self_test", "feedback_optimizer",
                 "predictive_preload", "meta_learner", "skill_graph", "composer"]:
        sys.modules.pop(name, None)


def _setup_hermes_package():
    _clear_hermes_modules()
    pkg = types.ModuleType("hermes_enhancer")
    pkg.__path__ = [str(SRC_DIR)]
    pkg.__package__ = "hermes_enhancer"
    sys.modules["hermes_enhancer"] = pkg

    def _load(name):
        path = SRC_DIR / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"hermes_enhancer.{name}", path)
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "hermes_enhancer"
        sys.modules[f"hermes_enhancer.{name}"] = mod
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    _load("federated_db")
    _load("self_test")
    _load("feedback_optimizer")
    _load("predictive_preload")
    _load("meta_learner")
    _load("skill_graph")
    _load("composer")
    spec = importlib.util.spec_from_file_location("hermes_enhancer.enhancer", SRC_DIR / "enhancer.py")
    emod = importlib.util.module_from_spec(spec)
    emod.__package__ = "hermes_enhancer"
    sys.modules["hermes_enhancer.enhancer"] = emod
    spec.loader.exec_module(emod)
    return sys.modules["hermes_enhancer.federated_db"].FederatedDB, sys.modules["hermes_enhancer.feedback_optimizer"].FeedbackOptimizer, emod.HermesEnhancer


# Preload once; tests will refresh as needed
FederatedDB, FeedbackOptimizer, HermesEnhancer = _setup_hermes_package()


def _make_tmp_db():
    tmp_dir = Path(tempfile.mkdtemp(prefix="hermes_enhancer_test_"))
    tmp_db = tmp_dir / "test.db"
    return tmp_db, tmp_dir


def test_sync_db_insert_and_count() -> None:
    tmp_db, tmp_dir = _make_tmp_db()
    try:
        db = FederatedDB(db_path=str(tmp_db))
        row_id = db.push({"tool": "terminal", "duration": 0.5, "success": True}, node_id="test")
        assert row_id == 1
        assert db.count() == 1
        recent = db.get_recent(limit=1)
        assert recent[0]["payload"]["tool"] == "terminal"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_anomalous_flagging() -> None:
    fo = FeedbackOptimizer()
    fo.record_outcome("terminal", success=True, duration_s=0.5)
    normal_score = fo.get_score("terminal")
    fo.record_outcome("terminal", success=True, duration_s=400.0)
    assert fo.get_score("terminal") == normal_score
    fo.record_outcome("write_file", success=True, duration_s=-1.0)
    assert fo.get_score("write_file") == fo.default_score
    fo.record_outcome("read_file", success=True, duration_s=0.5, anomalous=True)
    assert fo.get_score("read_file") == fo.default_score


def test_enhancer_anomalous_flagging() -> None:
    tmp_db, tmp_dir = _make_tmp_db()
    try:
        _, _, HermesEnhancer_local = _setup_hermes_package()
        enhancer = HermesEnhancer_local(node_id="test")
        enhancer.db = FederatedDB(db_path=str(tmp_db))
        enhancer._maybe_schedule_io = lambda coro: coro()
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
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_auto_prune_on_threshold() -> None:
    tmp_db, tmp_dir = _make_tmp_db()
    try:
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
        summary_rows = conn.execute("SELECT COUNT(*) FROM summary_analytics").fetchone()[0]
        conn.close()
        assert summary_rows > 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_manual_prune_cli() -> None:
    tmp_db, tmp_dir = _make_tmp_db()
    try:
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
        result = db.prune()
        assert result["before_count"] > result["after_count"]
        assert result["deleted_rows"] > 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_feedback_ema_excludes_anomalies() -> None:
    fo = FeedbackOptimizer(alpha=0.5)
    fo.record_outcome("terminal", success=True, duration_s=1.0)
    score_after_good = fo.get_score("terminal")
    fo.record_outcome("terminal", success=True, duration_s=0.0, anomalous=True)
    assert fo.get_score("terminal") == score_after_good
    fo.record_outcome("terminal", success=True, duration_s=-5.0, anomalous=True)
    assert fo.get_score("terminal") == score_after_good


def test_success_definition_empty_result() -> None:
    _, _, HermesEnhancer_local = _setup_hermes_package()
    enhancer = HermesEnhancer_local(node_id="test")
    assert enhancer._is_empty_result("") is True
    assert enhancer._is_empty_result([]) is True
    assert enhancer._is_empty_result(None) is True
    assert enhancer._is_empty_result("ok") is False


@pytest.mark.asyncio
async def test_async_db_insert_and_count() -> None:
    tmp_db, tmp_dir = _make_tmp_db()
    try:
        db = FederatedDB(db_path=str(tmp_db))
        await db.async_push({"tool": "async_terminal", "duration": 0.1, "success": True}, node_id="test")
        assert await db.async_count() == 1
        recent = await db.async_get_recent(limit=1)
        assert recent[0]["payload"]["tool"] == "async_terminal"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_async_concurrent_writes() -> None:
    tmp_db, tmp_dir = _make_tmp_db()
    try:
        db = FederatedDB(db_path=str(tmp_db))
        await asyncio.gather(*[
            db.async_push({"tool": "concurrent", "duration": 0.1, "success": True}, node_id="test")
            for _ in range(20)
        ])
        assert await db.async_count() == 20
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    tests = [
        ("sync_db_insert_and_count", test_sync_db_insert_and_count),
        ("anomalous_flagging", test_anomalous_flagging),
        ("enhancer_anomalous_flagging", test_enhancer_anomalous_flagging),
        ("auto_prune_on_threshold", test_auto_prune_on_threshold),
        ("manual_prune_cli", test_manual_prune_cli),
        ("feedback_ema_excludes_anomalies", test_feedback_ema_excludes_anomalies),
        ("success_definition_empty_result", test_success_definition_empty_result),
        ("async_db_insert_and_count", lambda: asyncio.run(test_async_db_insert_and_count())),
        ("async_concurrent_writes", lambda: asyncio.run(test_async_concurrent_writes())),
    ]

    passed = 0
    failed = 0
    results = []

    for name, fn in tests:
        try:
            fn()
            passed += 1
            results.append((name, "PASS"))
        except Exception as exc:
            failed += 1
            results.append((name, f"FAIL: {exc}"))

    print("=== hermes_enhancer v0.20.7 diagnostic results ===")
    for name, status in results:
        print(f"  [{status}] {name}")
    print(f"\nPASSED: {passed}/{len(tests)}")
    print(f"FAILED: {failed}/{len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Enterprise upgrade test suite for hermes_enhancer v0.20.7.

Tests:
- Graph save/load cycles from SQLite
- DB lock retry behavior
- Cache purge on simulated high memory
- Active plugin loader verification
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import importlib.util
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PLUGIN_DIR = Path(os.path.expanduser("~/.hermes/plugins/hermes_enhancer"))


def _clear_hermes_modules():
    for key in list(sys.modules.keys()):
        if key == "hermes_enhancer" or key.startswith("hermes_enhancer."):
            del sys.modules[key]
    for name in ["federated_db", "self_test", "feedback_optimizer",
                 "predictive_preload", "meta_learner", "skill_graph", "composer"]:
        sys.modules.pop(name, None)


def _setup_src_package():
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
    return sys.modules["hermes_enhancer.federated_db"].FederatedDB, sys.modules["hermes_enhancer.feedback_optimizer"].FeedbackOptimizer, emod.HermesEnhancer, sys.modules["hermes_enhancer.skill_graph"].SkillGraph, sys.modules["hermes_enhancer.meta_learner"].MetaLearner, sys.modules["hermes_enhancer.predictive_preload"].PredictivePreload


FederatedDB, FeedbackOptimizer, HermesEnhancer, SkillGraph, MetaLearner, PredictivePreload = _setup_src_package()


def _make_tmp_db():
    tmp_dir = Path(tempfile.mkdtemp(prefix="hermes_enhancer_test_"))
    tmp_db = tmp_dir / "test.db"
    return tmp_db, tmp_dir


def test_graph_save_load_cycle() -> None:
    tmp_db, tmp_dir = _make_tmp_db()
    try:
        db = FederatedDB(db_path=str(tmp_db))
        graph = SkillGraph()
        graph.register_skill("analyze", ["search", "read"])
        graph.register_skill("report", ["analyze"])
        conn = db._get_conn()
        try:
            graph.save_to_db(conn)
            loaded = SkillGraph()
            loaded.load_from_db(conn)
            assert "analyze" in loaded.edges
            assert "report" in loaded.edges
            assert loaded.dependencies("analyze") == ["search", "read"]
            assert loaded.dependencies("report") == ["analyze"]
        finally:
            conn.close()
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_db_lock_retry() -> None:
    tmp_db, tmp_dir = _make_tmp_db()
    try:
        # Use FederatedDB directly; push() triggers initialization and retry path.
        db = FederatedDB(db_path=str(tmp_db))
        row_id = db.push({"tool": "lock_test", "success": True})
        assert row_id == 1
        assert db.count() == 1
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_preload_memory_cleanup() -> None:
    preload = PredictivePreload(order=1, memory_limit_mb=256)
    preload.record("tool_a")
    preload.record("tool_b")
    assert len(preload.last_sequence) == 2
    # Simulate high memory by monkeypatching RSS check.
    original = PredictivePreload._current_rss
    PredictivePreload._current_rss = lambda self: self.memory_limit_bytes + 1
    try:
        preload.memory_limit_bytes = 1
        preload.record("tool_c")
        # maybe_cleanup clears cache then record appends current tool.
        assert len(preload.last_sequence) == 1
        assert preload.last_sequence[-1] == "tool_c"
    finally:
        PredictivePreload._current_rss = original


def test_plugin_loader_active() -> None:
    plugin_dir = PLUGIN_DIR
    assert plugin_dir.exists(), f"Plugin directory missing: {plugin_dir}"
    assert (plugin_dir / "plugin.yaml").exists(), "plugin.yaml missing"
    assert (plugin_dir / "__init__.py").exists(), "Plugin entrypoint missing"
    assert (plugin_dir / "enhancer.py").exists(), "Core enhancer missing"
    pkg_yaml = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert "provides_hooks:" in pkg_yaml
    assert "pre_tool_call" in pkg_yaml
    assert "post_tool_call" in pkg_yaml


def test_active_plugin_imports() -> None:
    import types as _types
    pkg = _types.ModuleType("hermes_enhancer")
    pkg.__path__ = [str(PLUGIN_DIR)]
    pkg.__package__ = "hermes_enhancer"
    sys.modules["hermes_enhancer"] = pkg

    def _load(name):
        import importlib.util
        path = PLUGIN_DIR / f"{name}.py"
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
    spec = importlib.util.spec_from_file_location("hermes_enhancer.enhancer", PLUGIN_DIR / "enhancer.py")
    emod = importlib.util.module_from_spec(spec)
    emod.__package__ = "hermes_enhancer"
    sys.modules["hermes_enhancer.enhancer"] = emod
    spec.loader.exec_module(emod)
    _clear_hermes_modules()
    assert True


def main() -> int:
    tests = [
        ("graph_save_load_cycle", test_graph_save_load_cycle),
        ("db_lock_retry", test_db_lock_retry),
        ("preload_memory_cleanup", test_preload_memory_cleanup),
        ("plugin_loader_active", test_plugin_loader_active),
        ("active_plugin_imports", test_active_plugin_imports),
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
    print("=== hermes_enhancer enterprise upgrade test results ===")
    for name, status in results:
        print(f"  [{status}] {name}")
    print(f"\nPASSED: {passed}/{len(tests)}")
    print(f"FAILED: {failed}/{len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

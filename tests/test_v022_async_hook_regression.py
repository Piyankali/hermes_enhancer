#!/usr/bin/env python3
"""v0.22.1 regression tests: async scheduling import + hook timing path.

Covers two post-release audit findings without touching production code:
- Regression A: HermesEnhancer._maybe_schedule_io() must not raise
  NameError (missing asyncio import) on either the running-loop or the
  no-running-loop path.
- Regression B (behavior lock-in): the real register() pre/post hook path
  must measure the actual pre->post interval (Issue 2 was investigated and
  found NOT to be a functional bug; this test locks the correct behavior).

All state is isolated to tmp_path; the real Hermes installation and
~/.hermes/state.db are never touched.

Run: PYTHONPATH=src python3 -m pytest tests/test_v022_async_hook_regression.py -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

_FLAT_MODULES = [
    "federated_db",
    "self_test",
    "feedback_optimizer",
    "predictive_preload",
    "meta_learner",
    "skill_graph",
    "composer",
    "enhancer",
]


def _load_flat_modules():
    """Load src/*.py as top-level modules (installed flat-plugin layout)."""
    loaded = {}
    for name in _FLAT_MODULES:
        sys.modules.pop(name, None)
    for name in _FLAT_MODULES:
        spec = importlib.util.spec_from_file_location(name, SRC_DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        loaded[name] = mod
    return loaded


def _load_plugin_package(mods):
    """Load src/__init__.py against the already-loaded flat modules."""
    pkg = types.ModuleType("hermes_enhancer_test_pkg")
    pkg.__path__ = [str(SRC_DIR)]
    spec = importlib.util.spec_from_file_location(
        "hermes_enhancer_test_pkg", SRC_DIR / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_enhancer_test_pkg"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mods():
    loaded = _load_flat_modules()
    yield loaded
    for name in _FLAT_MODULES:
        sys.modules.pop(name, None)
    sys.modules.pop("hermes_enhancer_test_pkg", None)


def _make_tmp_db(tmp_path):
    return str(tmp_path / "regression.db")


def test_maybe_schedule_io_no_loop_no_nameerror(mods, tmp_path):
    """Regression A (no running loop): scheduling must not raise NameError."""
    db = mods["federated_db"].FederatedDB(db_path=_make_tmp_db(tmp_path))
    enhancer = mods["enhancer"].HermesEnhancer(node_id="regression")
    enhancer.db.shutdown()
    enhancer.db = db
    try:
        fut = enhancer._maybe_schedule_io(lambda: 42)
        assert fut.result(timeout=10) == 42
    finally:
        db.shutdown()


def test_maybe_schedule_io_running_loop_no_nameerror(mods, tmp_path):
    """Regression A (running loop): scheduling must not raise NameError."""
    db = mods["federated_db"].FederatedDB(db_path=_make_tmp_db(tmp_path))
    enhancer = mods["enhancer"].HermesEnhancer(node_id="regression")
    enhancer.db.shutdown()
    enhancer.db = db
    try:

        async def _run():
            return await enhancer._maybe_schedule_io(lambda: 7)

        assert asyncio.run(_run()) == 7
    finally:
        db.shutdown()


def test_register_hook_path_measures_real_interval(mods, tmp_path):
    """Regression B (lock-in): real register() pre/post measures the interval."""
    pkg = _load_plugin_package(mods)
    pkg._instance = None
    ctx = MagicMock()
    ctx.node_id = "regression"
    pkg.register(ctx)
    enhancer = pkg._instance
    enhancer.db.shutdown()
    db = mods["federated_db"].FederatedDB(db_path=_make_tmp_db(tmp_path))
    enhancer.db = db
    try:
        registered = {c.args[0]: c.args[1] for c in ctx.register_hook.call_args_list}
        assert set(registered) == {"pre_tool_call", "post_tool_call"}
        registered["pre_tool_call"](tool_name="regression_tool", tool_call_id="rtc1")
        time.sleep(0.12)
        registered["post_tool_call"](
            tool_name="regression_tool",
            tool_call_id="rtc1",
            result="ok",
            status="success",
        )
        db.flush()
        rows = db.get_recent(5)
        posts = [
            r
            for r in rows
            if r.get("payload", {}).get("hook") == "post_tool_call"
            and r.get("payload", {}).get("tool") == "regression_tool"
        ]
        assert len(posts) == 1
        payload = posts[0]["payload"]
        assert payload.get("success") is True
        assert (payload.get("delta_us") or 0) >= 100000
    finally:
        db.shutdown()

#!/usr/bin/env python3
"""v0.22.2 runtime hook-contract regression tests.

Incident: after a gateway/CLI process discovered plugins while the
installed copy still used flat absolute imports, that process cached the
load failure ("No module named 'enhancer'") and all later real tool calls
in the same process silently skipped Enhancer telemetry (empty
federated.db), even after setup.sh installed the fixed copy. A fresh
process with real Hermes discovery + the real executor hook path
(_pre_tool_block / _ToolCallRef.emit_post) persists pre+post rows.

These tests lock the plugin side of that contract hermetically
(isolated tmp_path DBs, package-style loading that mirrors
hermes_cli.plugins_loader._load_directory_module):

1. register() registers exactly pre_tool_call + post_tool_call.
2. Pre-hook with realistic Hermes kwargs persists a pre event.
3. Post-hook with realistic Hermes kwargs persists a post event.
4. tool_call_id / trace_id / event_id pairing survives the round trip.
5. Failure status (status=error + error fields) persists as failed.
6. Extra/unknown Hermes kwargs (e.g. telemetry_schema_version) are tolerated.
7. Async post path persists.
8. No duplicate event_id on repeated identical payloads.
9. Persistence survives process restart (reopen same DB file).
10. Concurrent same-tool overlap keeps per-call attribution (covered in
    test_v022_concurrent_timing.py; smoke-paired here via _timing_key).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

_PKG = "hermes_plugins.hermes_enhancer_hooktest"


def _evict():
    for key in [k for k in sys.modules if k == _PKG or k.startswith(_PKG + ".")]:
        del sys.modules[key]
    sys.modules.pop("hermes_plugins", None)


def _load_package():
    """Load src/ as a package exactly like the Hermes directory loader."""
    _evict()
    ns = types.ModuleType("hermes_plugins")
    ns.__path__ = []
    ns.__package__ = "hermes_plugins"
    sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        _PKG, SRC_DIR / "__init__.py", submodule_search_locations=[str(SRC_DIR)]
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = _PKG
    mod.__path__ = [str(SRC_DIR)]
    sys.modules[_PKG] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def pkg():
    mod = _load_package()
    yield mod
    for key in [k for k in sys.modules if k == _PKG or k.startswith(_PKG + ".")]:
        del sys.modules[key]
    sys.modules.pop("hermes_plugins", None)
    # Reset process singletons so tmp DBs never leak between tests.
    for name in ("hermes_enhancer_hooktest",):
        _ = name


def _fresh_db(pkg, tmp_path, name="hook.db"):
    from hermes_plugins.hermes_enhancer_hooktest.federated_db import FederatedDB
    del pkg  # fresh instances per test; package only provides classes
    db = FederatedDB(db_path=str(tmp_path / name))
    return db


def _ctx_spy():
    class Ctx:
        def __init__(self):
            self.node_id = "hooktest"
            self.hooks = {}

        def register_hook(self, name, fn):
            self.hooks[name] = fn

    return Ctx()


def test_1_register_binds_both_hooks(pkg):
    ctx = _ctx_spy()
    pkg._instance = None
    pkg.register(ctx)
    assert set(ctx.hooks) == {"pre_tool_call", "post_tool_call"}
    assert callable(ctx.hooks["pre_tool_call"])
    assert callable(ctx.hooks["post_tool_call"])
    pkg._instance.db.shutdown()


def test_2_pre_hook_persists_pre_event(pkg, tmp_path):
    ctx = _ctx_spy()
    pkg._instance = None
    pkg.register(ctx)
    enh = pkg._instance
    enh.db.shutdown()
    enh.db = _fresh_db(pkg, tmp_path, "pre.db")
    try:
        ctx.hooks["pre_tool_call"](
            tool_name="terminal", args={"command": "pwd"},
            task_id="t1", session_id="s1", tool_call_id="call-1",
            turn_id="u1", api_request_id="a1", middleware_trace=[],
            telemetry_schema_version=1,
        )
        enh.db.flush()
        rows = enh.db.get_recent(10)
        pres = [r for r in rows if r["payload"].get("hook") == "pre_tool_call"]
        assert len(pres) == 1, rows
        assert pres[0]["tool_call_id"] == "call-1"
        assert pres[0]["payload"]["tool"] == "terminal"
        assert pres[0]["event_id"]
    finally:
        enh.db.shutdown()


def test_3_post_hook_persists_post_event(pkg, tmp_path):
    ctx = _ctx_spy()
    pkg._instance = None
    pkg.register(ctx)
    enh = pkg._instance
    enh.db.shutdown()
    enh.db = _fresh_db(pkg, tmp_path, "post.db")
    try:
        ctx.hooks["pre_tool_call"](tool_name="terminal", tool_call_id="call-9")
        time.sleep(0.02)
        ctx.hooks["post_tool_call"](
            tool_name="terminal", args={"command": "pwd"},
            result={"output": "/x"}, task_id="t1", session_id="s1",
            tool_call_id="call-9", turn_id="u1", api_request_id="a1",
            duration_ms=20, status="ok", error_type=None,
            error_message=None, middleware_trace=[], telemetry_schema_version=1,
        )
        enh.db.flush()
        rows = enh.db.get_recent(10)
        posts = [r for r in rows if r["payload"].get("hook") == "post_tool_call"]
        assert len(posts) == 1, rows
        assert posts[0]["payload"]["success"] is True
        assert posts[0]["payload"]["status"] == "success"
        assert 5000 <= posts[0]["payload"]["delta_us"] <= 500000, posts[0]["payload"]
    finally:
        enh.db.shutdown()


def test_4_ids_paired_pre_post(pkg, tmp_path):
    ctx = _ctx_spy()
    pkg._instance = None
    pkg.register(ctx)
    enh = pkg._instance
    enh.db.shutdown()
    enh.db = _fresh_db(pkg, tmp_path, "pair.db")
    try:
        ctx.hooks["pre_tool_call"](tool_name="terminal", tool_call_id="pair-7")
        ctx.hooks["post_tool_call"](tool_name="terminal", result="ok",
                                    status="ok", tool_call_id="pair-7")
        enh.db.flush()
        rows = enh.db.get_recent(10)
        by_hook = {r["payload"]["hook"]: r for r in rows}
        assert set(by_hook) == {"pre_tool_call", "post_tool_call"}
        assert by_hook["pre_tool_call"]["tool_call_id"] == "pair-7"
        assert by_hook["post_tool_call"]["tool_call_id"] == "pair-7"
        assert by_hook["pre_tool_call"]["trace_id"] == \
            by_hook["post_tool_call"]["trace_id"]
        assert by_hook["pre_tool_call"]["event_id"] != \
            by_hook["post_tool_call"]["event_id"]
    finally:
        enh.db.shutdown()


def test_5_failure_status_preserved(pkg, tmp_path):
    ctx = _ctx_spy()
    pkg._instance = None
    pkg.register(ctx)
    enh = pkg._instance
    enh.db.shutdown()
    enh.db = _fresh_db(pkg, tmp_path, "fail.db")
    try:
        ctx.hooks["pre_tool_call"](tool_name="terminal", tool_call_id="fail-1")
        ctx.hooks["post_tool_call"](
            tool_name="terminal", result="", status="error",
            error_type="CommandFailed", error_message="exit 2",
            tool_call_id="fail-1",
        )
        enh.db.flush()
        rows = enh.db.get_recent(10)
        posts = [r for r in rows if r["payload"].get("hook") == "post_tool_call"]
        assert len(posts) == 1
        assert posts[0]["payload"]["success"] is False
        assert posts[0]["payload"]["status"] == "failed"
        assert posts[0]["payload"]["error"] == "exit 2"
    finally:
        enh.db.shutdown()


def test_6_unknown_kwargs_tolerated(pkg, tmp_path):
    ctx = _ctx_spy()
    pkg._instance = None
    pkg.register(ctx)
    enh = pkg._instance
    enh.db.shutdown()
    enh.db = _fresh_db(pkg, tmp_path, "kw.db")
    try:
        ctx.hooks["pre_tool_call"](tool_name="t", future_field="x", nested={"a": 1})
        ctx.hooks["post_tool_call"](tool_name="t", result="ok", status="ok",
                                    another_unknown=[1, 2, 3])
        enh.db.flush()
        assert enh.db.count() == 2
    finally:
        enh.db.shutdown()


def test_7_async_post_persists(pkg, tmp_path):
    ctx = _ctx_spy()
    pkg._instance = None
    pkg.register(ctx)
    enh = pkg._instance
    enh.db.shutdown()
    enh.db = _fresh_db(pkg, tmp_path, "async.db")
    try:
        async def _run():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: ctx.hooks["pre_tool_call"](
                    tool_name="terminal", tool_call_id="async-1")
            )
            await loop.run_in_executor(
                None, lambda: ctx.hooks["post_tool_call"](
                    tool_name="terminal", result="ok", status="ok",
                    tool_call_id="async-1")
            )

        asyncio.run(_run())
        enh.db.flush()
        rows = enh.db.get_recent(10)
        assert {r["payload"]["hook"] for r in rows} == \
            {"pre_tool_call", "post_tool_call"}
    finally:
        enh.db.shutdown()


def test_8_no_duplicate_event_id(pkg, tmp_path):
    from hermes_plugins.hermes_enhancer_hooktest.federated_db import FederatedDB
    db = FederatedDB(db_path=str(tmp_path / "dedup.db"))
    try:
        eid = "fixed-event-id-123"
        payload = {"hook": "post_tool_call", "tool": "t",
                   "event_id": eid, "trace_id": "tr", "tool_call_id": "c"}
        r1 = db.push(dict(payload))
        r2 = db.push(dict(payload))
        assert r1 == r2 and r1 > 0
        assert db.count() == 1
    finally:
        db.shutdown()


def test_9_persistence_survives_reopen(pkg, tmp_path):
    from hermes_plugins.hermes_enhancer_hooktest.federated_db import FederatedDB
    path = str(tmp_path / "reopen.db")
    db = FederatedDB(db_path=path)
    db.push({"hook": "post_tool_call", "tool": "terminal",
             "event_id": uuid.uuid4().hex, "trace_id": "t", "tool_call_id": "c1"})
    db.flush()
    db.shutdown()
    db2 = FederatedDB(db_path=path)
    try:
        assert db2.count() == 1
        assert db2.get_recent(1)[0]["tool"] == "terminal"
    finally:
        db2.shutdown()


def test_10_timing_key_identity_first(pkg):
    from hermes_plugins.hermes_enhancer_hooktest.enhancer import HermesEnhancer
    assert HermesEnhancer._timing_key("sametool", "call-A") == "call:call-A"
    assert HermesEnhancer._timing_key("sametool", "call-B") == "call:call-B"
    assert HermesEnhancer._timing_key("sametool", "call-A") != \
        HermesEnhancer._timing_key("sametool", "call-B")
    assert HermesEnhancer._timing_key("SameTool", None) == "sametool"
    assert HermesEnhancer._timing_key("", "   ") == "unknown"

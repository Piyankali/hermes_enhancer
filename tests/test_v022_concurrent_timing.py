#!/usr/bin/env python3
"""v0.22.2 regression tests: identity-safe concurrent hook timing.

Covers the same-tool concurrent timing fix in HermesEnhancer
(_timing_key): overlapping calls carrying distinct tool_call_id values
must each measure their own pre->post interval, in either finish order,
while sequential and identity-less behavior stays unchanged.

All state is isolated to tmp_path; the real Hermes installation and
~/.hermes/state.db are never touched.

Run: PYTHONPATH=src python3 -m pytest tests/test_v022_concurrent_timing.py -q
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

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


@pytest.fixture()
def mods():
    loaded = _load_flat_modules()
    yield loaded
    for name in _FLAT_MODULES:
        sys.modules.pop(name, None)


def _enhancer(mods, tmp_path, name="timing.db"):
    db = mods["federated_db"].FederatedDB(db_path=str(tmp_path / name))
    enhancer = mods["enhancer"].HermesEnhancer(node_id="timing")
    enhancer.db.shutdown()
    enhancer.db = db
    return enhancer, db


def _post_durations_by_call(db, tool):
    db.flush()
    rows = db.get_recent(10)
    out = {}
    for row in rows:
        payload = row.get("payload", {})
        if payload.get("hook") == "post_tool_call" and payload.get("tool") == tool:
            out[payload.get("tool_call_id")] = payload.get("delta_us")
    return out


def _run_overlap(mods, tmp_path, dbname, finish_first):
    enhancer, db = _enhancer(mods, tmp_path, dbname)
    try:
        enhancer.pre_tool_call(("sametool",), {"tool_call_id": "call-A"})
        time.sleep(0.10)
        enhancer.pre_tool_call(("sametool",), {"tool_call_id": "call-B"})
        time.sleep(0.10)
        first, second = ("call-A", "call-B") if finish_first == "A" else ("call-B", "call-A")
        enhancer.on_post_tool_call(
            tool_name="sametool", tool_call_id=first, result="ok", status="success"
        )
        enhancer.on_post_tool_call(
            tool_name="sametool", tool_call_id=second, result="ok", status="success"
        )
        return _post_durations_by_call(db, "sametool")
    finally:
        db.shutdown()


def test_same_tool_overlap_a_finishes_first(mods, tmp_path):
    """A (~200ms) and B (~100ms) each measure their own interval."""
    for i in range(3):
        durations = _run_overlap(mods, tmp_path, f"a_first_{i}.db", "A")
        assert set(durations) == {"call-A", "call-B"}
        assert 150000 <= durations["call-A"] <= 450000, durations
        assert 50000 <= durations["call-B"] <= 250000, durations


def test_same_tool_overlap_b_finishes_first(mods, tmp_path):
    """Reverse finish order still attributes each duration correctly."""
    for i in range(3):
        durations = _run_overlap(mods, tmp_path, f"b_first_{i}.db", "B")
        assert set(durations) == {"call-A", "call-B"}
        assert 150000 <= durations["call-A"] <= 450000, durations
        assert 50000 <= durations["call-B"] <= 250000, durations


def test_different_tools_overlap(mods, tmp_path):
    """Overlapping different tools never shared a timing slot."""
    enhancer, db = _enhancer(mods, tmp_path, "diff.db")
    try:
        enhancer.pre_tool_call(("tool-A",), {"tool_call_id": "id-A"})
        time.sleep(0.08)
        enhancer.pre_tool_call(("tool-B",), {"tool_call_id": "id-B"})
        time.sleep(0.08)
        enhancer.on_post_tool_call(
            tool_name="tool-A", tool_call_id="id-A", result="ok", status="success"
        )
        enhancer.on_post_tool_call(
            tool_name="tool-B", tool_call_id="id-B", result="ok", status="success"
        )
        db.flush()
        rows = db.get_recent(10)
        by_call = {
            r["payload"]["tool_call_id"]: r["payload"]["delta_us"]
            for r in rows
            if r.get("payload", {}).get("hook") == "post_tool_call"
        }
        assert 100000 <= by_call["id-A"] <= 350000, by_call
        assert 40000 <= by_call["id-B"] <= 250000, by_call
    finally:
        db.shutdown()


def test_failure_path_keeps_timing(mods, tmp_path):
    """A failed tool still records its own interval under overlap."""
    enhancer, db = _enhancer(mods, tmp_path, "fail.db")
    try:
        enhancer.pre_tool_call(("flaky",), {"tool_call_id": "ok-1"})
        time.sleep(0.05)
        enhancer.pre_tool_call(("flaky",), {"tool_call_id": "bad-1"})
        time.sleep(0.08)
        enhancer.on_post_tool_call(
            tool_name="flaky", tool_call_id="ok-1", result="ok", status="success"
        )
        enhancer.on_post_tool_call(
            tool_name="flaky",
            tool_call_id="bad-1",
            result="boom",
            status="error",
            error_message="boom",
        )
        durations = _post_durations_by_call(db, "flaky")
        assert set(durations) == {"ok-1", "bad-1"}
        assert 80000 <= durations["ok-1"] <= 350000, durations
        assert 40000 <= durations["bad-1"] <= 250000, durations
        db.flush()
        rows = db.get_recent(10)
        failed = [
            r["payload"]
            for r in rows
            if r.get("payload", {}).get("tool_call_id") == "bad-1"
        ][0]
        assert failed["success"] is False
    finally:
        db.shutdown()


def test_sequential_without_call_id_unchanged(mods, tmp_path):
    """Direct calls without tool_call_id keep legacy tool-key behavior."""
    enhancer, db = _enhancer(mods, tmp_path, "seq.db")
    try:
        enhancer.pre_tool_call(("plain",), {})
        time.sleep(0.05)
        enhancer.on_post_tool_call(tool_name="plain", result="ok", status="success")
        durations = _post_durations_by_call(db, "plain")
        assert set(durations) == {""}
        assert 30000 <= durations[""] <= 250000, durations
    finally:
        db.shutdown()


def test_missing_pre_still_uses_fallback(mods, tmp_path):
    """Post without pre keeps the documented ~5ms fallback (not removed)."""
    enhancer, db = _enhancer(mods, tmp_path, "fallback.db")
    try:
        enhancer.on_post_tool_call(
            tool_name="ghost", tool_call_id="ghost-1", result="ok", status="success"
        )
        durations = _post_durations_by_call(db, "ghost")
        assert set(durations) == {"ghost-1"}
        assert 0 <= durations["ghost-1"] <= 30000, durations
    finally:
        db.shutdown()

"""HermesEnhancer - Core orchestrator for self-healing and telemetry architecture.

v0.21.0-dev: Batched telemetry persistence, event/trace IDs, lifecycle
accounting, memory retention monitoring, and targeted cleanup hooks.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from federated_db import get_db, FederatedDB
from self_test import SelfTestEngine
from feedback_optimizer import FeedbackOptimizer
from predictive_preload import PredictivePreload
from meta_learner import MetaLearner
from skill_graph import SkillGraph
from composer import SkillComposer

logger = logging.getLogger("hermes.enhancer")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

_IO_EXECUTOR = ThreadPoolExecutor(max_workers=2)


class HermesEnhancer:
    """Main orchestrator for the Hermes Agent enhancer plugin."""

    def __init__(self, node_id: str = "local") -> None:
        self.node_id = node_id
        self.db: FederatedDB = get_db()
        self.self_test = SelfTestEngine()
        self.feedback = FeedbackOptimizer()
        self.preload = PredictivePreload()
        self.meta_learner = MetaLearner()
        self.skill_graph = SkillGraph()
        self.composer = SkillComposer()
        self._enabled = True
        self._start_times: Dict[str, float] = {}

        self._trace_id: Optional[str] = None
        self._tool_call_id: Optional[str] = None

        tracemalloc.start()
        self._baseline_snapshot = tracemalloc.take_snapshot()
        self._last_memory_snapshot_bytes = 0

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_empty_result(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True
        if isinstance(value, (bytes, bytearray)) and len(value) == 0:
            return True
        if isinstance(value, (list, dict, set, tuple)) and len(value) == 0:
            return True
        return False

    def _normalize_tool_key(self, tool_name: str) -> str:
        if not tool_name:
            return "unknown"
        return str(tool_name).strip().lower()

    def _extract_tool_name(self, args: tuple, kwargs: Dict[str, Any]) -> str:
        candidates: List[str] = []

        def _coerce_str(value: Any) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, str):
                text = value.strip()
                return text if text else None
            if isinstance(value, (int, float, bool)):
                text = str(value).strip()
                return text if text else None
            return None

        if args:
            for index in (0, 1):
                if index < len(args):
                    text = _coerce_str(args[index])
                    if text:
                        candidates.append(text)

        for key in ("tool", "name", "tool_name", "function_name"):
            text = _coerce_str(kwargs.get(key))
            if text:
                candidates.append(text)

        nested_keys = (
            "args",
            "function_args",
            "arguments",
            "params",
            "payload",
            "context",
            "execution",
            "request",
            "call",
        )
        for key in nested_keys:
            value = kwargs.get(key)
            if isinstance(value, dict):
                for nested_key in ("tool", "name", "tool_name", "function_name"):
                    text = _coerce_str(value.get(nested_key))
                    if text:
                        candidates.append(text)

        if candidates:
            return candidates[0]
        return "unknown"

    def _extract_result(self, args: tuple, kwargs: Dict[str, Any]) -> Any:
        if args:
            return args[0]
        for key in ("result", "output", "response", "value"):
            if key in kwargs:
                return kwargs[key]
        return None

    def _extract_duration_ms(self, kwargs: Dict[str, Any]) -> int:
        for key in ("duration_ms", "duration", "elapsed_ms", "elapsed"):
            value = kwargs.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    def _flag_anomalous(self, duration_s: float) -> bool:
        if duration_s <= 0:
            return True
        if duration_s > 300:
            return True
        return False

    def _maybe_schedule_io(self, coro):
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                return loop.run_in_executor(_IO_EXECUTOR, coro)
        except RuntimeError:
            pass
        return _IO_EXECUTOR.submit(coro)

    # ------------------------------------------------------------------ #
    # Targeted cleanup
    # ------------------------------------------------------------------ #
    def cleanup_completed_tasks(self) -> Dict[str, int]:
        removed = 0
        for key in list(self._start_times.keys()):
            if key.startswith("__completed__"):
                self._start_times.pop(key, None)
                removed += 1
        return {"removed_start_times": removed}

    def cleanup_old_history(self, keep_last: int = 1000) -> Dict[str, int]:
        return {
            "pruned_meta_learner": self.meta_learner.prune(keep_last=keep_last),
        }

    def cleanup_preload_cache(self) -> Dict[str, int]:
        if len(self.preload.last_sequence) > 0 or self.preload.transitions:
            self.preload.clear()
            return {"preload_cleared": 1}
        return {"preload_cleared": 0}

    def self_heal_memory(self) -> Dict[str, Any]:
        report = {
            "triggered": False,
            "steps": [],
            "baseline_bytes": self._last_memory_snapshot_bytes,
            "final_bytes": 0,
        }
        try:
            snapshot = tracemalloc.take_snapshot()
            current = sum(s.size for s in snapshot.compare_to(self._baseline_snapshot, "lineno") if s.size_diff > 0)
            report["baseline_bytes"] = self._last_memory_snapshot_bytes
            report["current_bytes"] = current
            if current > 50 * 1024 * 1024:
                report["triggered"] = True
                report["steps"].append("cleanup_completed_tasks")
                report.update(self.cleanup_completed_tasks())
                report["steps"].append("cleanup_old_history")
                report.update(self.cleanup_old_history())
                report["steps"].append("cleanup_preload_cache")
                report.update(self.cleanup_preload_cache())
                report["steps"].append("gc_collect")
                collected = gc.collect()
                report["gc_collected"] = collected
                after = tracemalloc.take_snapshot()
                after_total = sum(s.size for s in after.compare_to(self._baseline_snapshot, "lineno") if s.size_diff > 0)
                report["final_bytes"] = after_total
        except Exception as exc:
            logger.debug("Memory self-heal check failed: %s", exc)
        return report

    def health_report(self) -> Dict[str, Any]:
        memory = self.self_heal_memory()
        return {
            "node_id": self.node_id,
            "enabled": self._enabled,
            "db": {
                "available": os.path.exists(self.db.db_path),
                "count": self.db.count(),
                "counters": self.db.get_counters(),
            },
            "self_test": self.self_test.run_battery(),
            "feedback": self.feedback.summary(),
            "preload": {"history_len": len(self.preload.last_sequence)},
            "meta_learner": self.meta_learner.summary(),
            "skill_graph": self.skill_graph.summary(),
            "composer": {"steps": len(self.composer.steps), "results": len(self.composer.results)},
            "memory": memory,
        }

    def check_memory_leak(self) -> Dict[str, Any]:
        current_snapshot = tracemalloc.take_snapshot()
        top_stats = current_snapshot.compare_to(self._baseline_snapshot, "lineno")
        leaks = [s for s in top_stats if s.size_diff > 0][:20]
        return {
            "top_deltas": [
                {
                    "file": stat.traceback[0].filename,
                    "line": stat.traceback[0].lineno,
                    "delta_bytes": stat.size_diff,
                }
                for stat in leaks
            ]
        }

    # ------------------------------------------------------------------ #
    # Hooks
    # ------------------------------------------------------------------ #
    def pre_tool_call(self, args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Pre-tool hook: capture state and start timing."""
        if not self._enabled:
            return {}
        t0 = time.perf_counter()
        tool_name = self._extract_tool_name(args, kwargs)
        key = self._normalize_tool_key(tool_name)
        self._start_times[key] = t0

        self._trace_id = kwargs.get("trace_id") or self.db._generate_trace_id()
        self._tool_call_id = kwargs.get("tool_call_id") or ""

        payload = {
            "hook": "pre_tool_call",
            "tool": tool_name,
            "args": list(args),
            "kwargs": kwargs,
            "node_id": self.node_id,
            "t0_us": int(t0 * 1_000_000),
            "event_id": self.db._generate_event_id(),
            "trace_id": self._trace_id,
            "tool_call_id": self._tool_call_id,
        }
        self.db.enqueue_event(payload, node_id=self.node_id, event_id=payload["event_id"], trace_id=payload["trace_id"], tool_call_id=payload["tool_call_id"])
        logger.debug("Pre tool hook: %s", tool_name)
        return {"_enhancer_t0": t0, "_enhancer_tool": tool_name, "_enhancer_trace_id": self._trace_id, "_enhancer_event_id": payload["event_id"]}

    def on_post_tool_call(self, *args: Any, **kwargs: Any) -> None:
        """Post-tool hook: capture duration, outcome, and push telemetry."""
        if not self._enabled:
            return
        tool_name = kwargs.get("tool_name") or self._extract_tool_name(args, kwargs)
        key = self._normalize_tool_key(tool_name)
        t0 = self._start_times.pop(key, None)
        if t0 is None:
            t1 = time.perf_counter()
            t0 = t1 - 0.005
        t1 = time.perf_counter()
        delta_us = int((t1 - t0) * 1_000_000)

        result = self._extract_result(args, kwargs)
        duration_ms = self._extract_duration_ms(kwargs)
        status = kwargs.get("status")
        error_message = kwargs.get("error_message")
        error_type = kwargs.get("error_type")
        if status is None and (error_message or error_type):
            status = "error"

        is_success = True
        status_str = kwargs.get("status")
        if isinstance(status_str, str):
            status_str = status_str.strip().lower()
        if status_str in {"error", "failed"}:
            is_success = False
        elif error_message or error_type:
            is_success = False
        elif isinstance(result, str) and any(k in result.lower() for k in ("error", "fail", "exception", "traceback")):
            is_success = False

        if is_success and duration_ms <= 0 and delta_us > 0:
            duration_ms = max(1, int(delta_us / 1000))
        if is_success and self._is_empty_result(result):
            is_success = True

        duration_s = duration_ms / 1000.0 if duration_ms else delta_us / 1_000_000.0
        if duration_s < 0.001:
            duration_s = 0.001

        anomalous = self._flag_anomalous(duration_s)
        if anomalous:
            is_success = False

        event_id = self.db._generate_event_id()
        trace_id = kwargs.get("trace_id") or self._trace_id or self.db._generate_trace_id()
        tool_call_id = kwargs.get("tool_call_id") or self._tool_call_id or ""

        payload = {
            "hook": "post_tool_call",
            "tool": tool_name,
            "status": "success" if is_success else "failed",
            "success": is_success,
            "duration": round(duration_s, 6),
            "delta_us": delta_us,
            "node_id": self.node_id,
            "anomalous": 1 if anomalous else 0,
            "event_id": event_id,
            "trace_id": trace_id,
            "tool_call_id": tool_call_id,
        }
        if error_message:
            payload["error"] = str(error_message)

        self.db.enqueue_event(payload, node_id=self.node_id, event_id=event_id, trace_id=trace_id, tool_call_id=tool_call_id)

        self.feedback.record_outcome(
            tool_name,
            success=is_success,
            duration_s=duration_s,
            anomalous=anomalous,
        )
        self.preload.record(tool_name)
        self.preload.maybe_cleanup()
        self.meta_learner.ingest({
            "tool": tool_name,
            "success": is_success,
            "delta_us": delta_us,
            "anomalous": 1 if anomalous else 0,
        })

        logger.debug(
            "Post tool hook: %s success=%s dt=%dms anomalous=%s trace=%s event=%s",
            tool_name, is_success, duration_ms, anomalous, trace_id, event_id,
        )

    def run_self_test(self) -> Dict[str, Any]:
        battery = self.self_test.run_battery()
        self.db.enqueue_event({"event": "self_test", "result": battery}, node_id=self.node_id)
        return battery

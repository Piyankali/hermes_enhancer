"""HermesEnhancer - Core orchestrator for self-healing and telemetry architecture.

v0.20.7: Async/non-blocking DB writes, anomalous duration flagging, and EMA
duration filtering hooks.
"""

import os
import sys
import time
import asyncio
import tracemalloc
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from .federated_db import get_db, FederatedDB
    from .self_test import SelfTestEngine
    from .feedback_optimizer import FeedbackOptimizer
    from .predictive_preload import PredictivePreload
    from .meta_learner import MetaLearner
    from .skill_graph import SkillGraph
    from .composer import SkillComposer
except ImportError:
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

        # Memory leak protection baseline
        tracemalloc.start()
        self._baseline_snapshot = tracemalloc.take_snapshot()

    @staticmethod
    def _is_empty_result(value: Any) -> bool:
        """Return True when a result carries no meaningful execution output."""
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
        """Normalize a tool identifier for stable pre/post lookup."""
        if not tool_name:
            return "unknown"
        return str(tool_name).strip().lower()

    def _extract_tool_name(self, args: tuple, kwargs: Dict[str, Any]) -> str:
        """Extract the best tool name from positional args and kwargs.

        Hermes v0.20.6 passes tool context through multiple possible shapes:
        - args[0] / args[1] containing the function name
        - kwargs['tool'] / kwargs['name']
        - kwargs['args'] or kwargs['function_args'] embedding a name
        - nested dicts under keys like 'context', 'execution', 'request'
        """
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

        # Positional args: only use if they are primitive strings/numbers
        if args:
            for index in (0, 1):
                if index < len(args):
                    text = _coerce_str(args[index])
                    if text:
                        candidates.append(text)

        # Top-level kwargs
        for key in ("tool", "name", "tool_name", "function_name"):
            text = _coerce_str(kwargs.get(key))
            if text:
                candidates.append(text)

        # Nested kwargs
        nested_keys = ("args", "function_args", "arguments", "params", "payload", "context", "execution", "request", "call")
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
        """Extract the tool result from positional args or kwargs."""
        if args:
            return args[0]
        for key in ("result", "output", "response", "value"):
            if key in kwargs:
                return kwargs[key]
        return None

    def _extract_duration_ms(self, kwargs: Dict[str, Any]) -> int:
        """Extract duration in milliseconds from kwargs if provided."""
        for key in ("duration_ms", "duration", "elapsed_ms", "elapsed"):
            value = kwargs.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    def _flag_anomalous(self, duration_s: float) -> bool:
        """Return True if duration is anomalous."""
        if duration_s <= 0:
            return True
        if duration_s > 300:
            return True
        return False

    def _maybe_schedule_io(self, coro):
        """Schedule DB write on executor if async loop is running; else run sync."""
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                return loop.run_in_executor(_IO_EXECUTOR, coro)
        except RuntimeError:
            pass
        # Fallback: fire-and-forget via executor when no loop is running
        return _IO_EXECUTOR.submit(coro)

    def pre_tool_call(self, args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Pre-tool hook: capture state and start timing."""
        if not self._enabled:
            return {}
        t0 = time.perf_counter()
        tool_name = self._extract_tool_name(args, kwargs)
        key = self._normalize_tool_key(tool_name)
        self._start_times[key] = t0
        payload = {
            "hook": "pre_tool_call",
            "tool": tool_name,
            "args": list(args),
            "kwargs": kwargs,
            "node_id": self.node_id,
            "t0_us": int(t0 * 1_000_000),
        }
        self._maybe_schedule_io(lambda: self.db.push(payload, node_id=self.node_id))
        logger.debug("Pre tool hook: %s", tool_name)
        return {"_enhancer_t0": t0, "_enhancer_tool": tool_name}

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

        payload = {
            "hook": "post_tool_call",
            "tool": tool_name,
            "status": "success" if is_success else "failed",
            "success": is_success,
            "duration": round(duration_s, 6),
            "delta_us": delta_us,
            "node_id": self.node_id,
            "anomalous": 1 if anomalous else 0,
        }
        if error_message:
            payload["error"] = str(error_message)
        self._maybe_schedule_io(lambda p=payload, n=self.node_id: self.db.push(p, node_id=n))

        self.feedback.record_outcome(
            tool_name,
            success=is_success,
            duration_s=duration_s,
            anomalous=anomalous,
        )
        self.preload.record(tool_name)
        self.meta_learner.ingest({
            "tool": tool_name,
            "success": is_success,
            "delta_us": delta_us,
            "anomalous": 1 if anomalous else 0,
        })

        logger.debug("Post tool hook: %s success=%s dt=%dms anomalous=%s",
                     tool_name, is_success, duration_ms, anomalous)

    def run_self_test(self) -> Dict[str, Any]:
        """Run the self-test battery and return results."""
        battery = self.self_test.run_battery()
        self._maybe_schedule_io(lambda: self.db.push({"event": "self_test", "result": battery}, node_id=self.node_id))
        return battery

    def health_report(self) -> Dict[str, Any]:
        """Generate a structured health report for all components."""
        return {
            "node_id": self.node_id,
            "enabled": self._enabled,
            "db": {"available": os.path.exists(self.db.db_path), "count": self.db.count()},
            "self_test": self.self_test.run_battery(),
            "feedback": self.feedback.summary(),
            "preload": {"history_len": len(self.preload.last_sequence)},
            "meta_learner": self.meta_learner.summary(),
            "skill_graph": self.skill_graph.summary(),
            "composer": {"steps": len(self.composer.steps), "results": len(self.composer.results)},
        }

    def check_memory_leak(self) -> Dict[str, Any]:
        """Compare current memory usage against baseline."""
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

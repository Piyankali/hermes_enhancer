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

try:  # Package import (Hermes plugin loader: hermes_plugins.<slug>).
    from .federated_db import get_db, FederatedDB
    from .self_test import SelfTestEngine
    from .feedback_optimizer import FeedbackOptimizer
    from .predictive_preload import PredictivePreload
    from .meta_learner import MetaLearner
    from .skill_graph import SkillGraph
    from .composer import SkillComposer
    from .decision_engine import DecisionEngine
    from .redaction import sanitize
except ImportError:  # Top-level import (tests / standalone use with src on path).
    from federated_db import get_db, FederatedDB
    from self_test import SelfTestEngine
    from feedback_optimizer import FeedbackOptimizer
    from predictive_preload import PredictivePreload
    from meta_learner import MetaLearner
    from skill_graph import SkillGraph
    from composer import SkillComposer
    from decision_engine import DecisionEngine
    from redaction import sanitize

logger = logging.getLogger("hermes.enhancer")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

_IO_EXECUTOR = ThreadPoolExecutor(max_workers=2)

# Persist learner state every N post-hook events (amortized I/O) and on
# explicit flush. A missed window only delays durability; events are safe.
LEARNER_PERSIST_EVERY = 20


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
        self.decision = DecisionEngine(
            feedback=self.feedback, preload=self.preload,
            meta_learner=self.meta_learner, skill_graph=self.skill_graph)
        self._enabled = True
        self._start_times: Dict[str, float] = {}
        self._post_count = 0
        self._pred_log_at: Dict[Tuple[str, str], float] = {}

        self._trace_id: Optional[str] = None
        self._tool_call_id: Optional[str] = None

        tracemalloc.start()
        self._baseline_snapshot = tracemalloc.take_snapshot()
        self._last_memory_snapshot_bytes = 0
        self._learners_loaded = self._load_learners()

    # ------------------------------------------------------------------ #
    # Persistent learners
    # ------------------------------------------------------------------ #
    def _load_learners(self) -> Dict[str, Any]:
        """Restore learner state from SQLite. Best-effort, never raises."""
        loaded: Dict[str, Any] = {"feedback": 0, "transitions": 0,
                                  "stats": 0, "sequences": 0, "errors": []}
        try:
            loaded["feedback"] = self.feedback.load_from_db(self.db)
        except Exception as exc:
            loaded["errors"].append("feedback: %s" % exc)
        try:
            loaded["transitions"] = self.preload.load_from_db(self.db)
        except Exception as exc:
            loaded["errors"].append("transitions: %s" % exc)
        try:
            stats = self.meta_learner.load_from_db(self.db)
            loaded["stats"] = stats["stats"]
            loaded["sequences"] = stats["sequences"]
        except Exception as exc:
            loaded["errors"].append("meta: %s" % exc)
        if loaded["errors"]:
            logger.warning("Learner restore partial: %s", loaded["errors"])
        return loaded

    def persist_learners(self) -> Dict[str, Any]:
        """Flush learner state to SQLite. Best-effort, never raises."""
        report: Dict[str, Any] = {"feedback": 0, "transitions": 0,
                                  "stats": 0, "sequences": 0, "errors": []}
        try:
            saved = self.db.persist_learner_state(
                self.feedback.to_rows(), self.preload.to_rows(),
                self.meta_learner.to_stat_rows(),
                self.meta_learner.to_sequence_rows())
            report.update(saved)
            try:
                self.db.prune_transitions()
            except Exception:
                pass
        except Exception as exc:
            report["errors"].append("persist: %s" % exc)
        if report["errors"]:
            logger.warning("Learner persist partial: %s", report["errors"])
        return report

    def startup_check(self) -> Dict[str, Any]:
        """Lightweight, non-blocking health check for plugin init.

        Covers config/DB/schema/migrations/queue/learners/redaction/graph.
        Returns {status: PASS|WARN|FAIL, checks: {...}}. Never raises,
        never repairs, never blocks on I/O beyond fast local queries.
        """
        checks: Dict[str, Dict[str, Any]] = {}

        def _record(name: str, ok: bool, detail: str = "",
                    warn: bool = False) -> None:
            checks[name] = {"status": "PASS" if ok else ("WARN" if warn else "FAIL"),
                            "detail": detail}

        _record("config", bool(self.node_id), "node_id=%s" % self.node_id)
        try:
            exists = os.path.exists(self.db.db_path)
            _record("db_available", exists, self.db.db_path)
        except Exception as exc:
            _record("db_available", False, str(exc))
        try:
            version = self.db.get_schema_version()
            _record("schema_version", version == str(2) or version == "2",
                    "schema=%s" % version, warn=True)
        except Exception as exc:
            _record("schema_version", False, str(exc))
        try:
            summary = self.db.get_buffer_summary()
            _record("event_buffer", True, "buffered=%s" % summary.get("total", 0))
        except Exception as exc:
            _record("event_buffer", False, str(exc))
        try:
            qsize = self.db._queue.qsize()
            _record("queue", qsize < self.db._max_queue,
                    "qsize=%d/%d" % (qsize, self.db._max_queue),
                    warn=qsize >= self.db._max_queue)
        except Exception as exc:
            _record("queue", False, str(exc))
        try:
            try:
                from .redaction import sanitize as _san
            except ImportError:
                from redaction import sanitize as _san  # type: ignore[no-redef]
            probe = _san({"password": "x", "ok": 1})
            _record("redaction",
                    probe.get("password") == "[REDACTED]" and probe.get("ok") == 1,
                    "smoke")
        except Exception as exc:
            _record("redaction", False, str(exc))
        try:
            graph_report = self.skill_graph.validate()
            _record("skill_graph", graph_report["status"] == "PASS",
                    "skills=%d cycles=%d" % (
                        graph_report["skills"], len(graph_report["cycles"])),
                    warn=bool(graph_report["cycles"]))
        except Exception as exc:
            _record("skill_graph", False, str(exc))
        loaded = self._learners_loaded or {}
        _record("learners", not loaded.get("errors"),
                "restored=%s" % {k: v for k, v in loaded.items()
                                 if k != "errors"},
                warn=bool(loaded.get("errors")))
        statuses = [c["status"] for c in checks.values()]
        overall = "PASS"
        if "FAIL" in statuses:
            overall = "FAIL"
        elif "WARN" in statuses:
            overall = "WARN"
        return {"status": overall, "checks": checks}

    def run_full_diagnostics(self) -> Dict[str, Any]:
        """On-demand deep diagnostic: startup check + integrity + orphans."""
        report = self.startup_check()
        try:
            report["db_integrity"] = self.db.verify_database()
        except Exception as exc:
            report["db_integrity"] = {"healthy": False, "error": str(exc)}
        try:
            report["orphans"] = self.db.find_orphans(limit=50)
        except Exception as exc:
            report["orphans_error"] = str(exc)
        try:
            report["learning"] = {
                "feedback": self.feedback.summary(),
                "meta": self.meta_learner.summary(),
                "predictions": len(self.preload.active_plans()),
            }
        except Exception as exc:
            report["learning_error"] = str(exc)
        return report

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

    @staticmethod
    def _timing_key(tool_name: str, tool_call_id: Any = None) -> str:
        """Identity-safe key for pairing pre/post timing.

        Prefers a non-empty tool_call_id so concurrent calls to the same
        tool do not overwrite each other's start timestamp; falls back to
        the normalized tool name for callers without a call identity
        (sequential behavior unchanged).
        """
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            return "call:" + tool_call_id.strip()
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
        key = self._timing_key(tool_name, kwargs.get("tool_call_id"))
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
        key = self._timing_key(tool_name, kwargs.get("tool_call_id"))
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
        # Plans are observable, persisted (predictions table), and consumed
        # by the decision engine. Planning never executes anything.
        # Prediction LOGGING is throttled (same pair, 60 s) so the hot
        # hook path stays lightweight; plans themselves are built always.
        try:
            plans = self.preload.plan_next(tool_name)
            if plans:
                now = time.time()
                for p in plans:
                    log_key = (tool_name, p["predicted_tool"])
                    last = self._pred_log_at.get(log_key, 0.0)
                    if now - last >= 60.0:
                        try:
                            self.db.log_prediction(
                                tool_name, p["predicted_tool"], p["confidence"])
                            self._pred_log_at[log_key] = now
                        except Exception:
                            pass
                self.decision.recommend([p["predicted_tool"] for p in plans],
                                        context_tool=tool_name)
        except Exception as exc:
            logger.debug("Preload planning failed: %s", exc)
        self.meta_learner.ingest({
            "tool": tool_name,
            "success": is_success,
            "delta_us": delta_us,
            "anomalous": 1 if anomalous else 0,
        })
        # Amortized learner durability: persist every N post hooks in the
        # background so fsync latency never blocks tool execution.
        # Telemetry itself is already queued above; this only affects how
        # quickly learned state becomes restart-safe. Explicit
        # persist_learners() stays synchronous for shutdown/flush paths.
        self._post_count += 1
        if self._post_count % LEARNER_PERSIST_EVERY == 0:
            try:
                _IO_EXECUTOR.submit(self._persist_learners_guarded)
            except Exception as exc:
                logger.debug("Periodic learner persist submit failed: %s", exc)

        logger.debug(
            "Post tool hook: %s success=%s dt=%dms anomalous=%s trace=%s event=%s",
            tool_name, is_success, duration_ms, anomalous, trace_id, event_id,
        )

    def _persist_learners_guarded(self) -> None:
        try:
            self.persist_learners()
        except Exception as exc:
            logger.debug("Periodic learner persist failed: %s", exc)

    def run_self_test(self) -> Dict[str, Any]:
        battery = self.self_test.run_battery()
        self.db.enqueue_event({"event": "self_test", "result": battery}, node_id=self.node_id)
        return battery

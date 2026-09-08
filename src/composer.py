"""SkillComposer - safe workflow engine with validation and audit.

v0.20.7 enterprise: Adds conditional branches, fallbacks, and retry policy support
for resilient tool execution pipelines.
v0.23: safe execution model. Steps reference SKILL NAMES resolved through
a trusted in-process handler registry -- never callables, eval, exec, or
pickle payloads from storage. Bounded steps/retries/timeouts, full audit
trail, deterministic ordering, cancellation support.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any, Callable, Dict, List, Optional

try:
    from .skill_graph import validate_skill_name
except ImportError:
    from skill_graph import validate_skill_name

MAX_STEPS = 50
MAX_RETRIES = 5
DEFAULT_STEP_TIMEOUT_S = 30.0

_EXECUTOR = ThreadPoolExecutor(max_workers=2)


class WorkflowValidationError(ValueError):
    """Raised when a workflow plan fails static validation."""


class SkillComposer:
    """Safe workflow engine over a trusted skill-handler registry."""

    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []
        self.results: List[Dict[str, Any]] = []
        self.audit: List[Dict[str, Any]] = []
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
        self._cancelled = False

    # ------------------------------------------------------------------ #
    # Trusted registry (in-process only; never loaded from DB/payloads)
    # ------------------------------------------------------------------ #
    def register_handler(self, name: str,
                         fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        """Register a skill implementation. Name-validated, callable-only."""
        if not validate_skill_name(name):
            raise ValueError("invalid skill name: %r" % (name,))
        if not callable(fn):
            raise ValueError("handler must be callable")
        self._handlers[name] = fn

    def registered_skills(self) -> List[str]:
        return sorted(self._handlers)

    def cancel(self) -> None:
        """Request cooperative cancellation of a running workflow."""
        self._cancelled = True

    # ------------------------------------------------------------------ #
    # Plan building (data only -- no execution)
    # ------------------------------------------------------------------ #
    def add_step(self, name: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Add a workflow step referencing a registered skill name."""
        try:
            from .redaction import sanitize
        except ImportError:
            from redaction import sanitize  # type: ignore[no-redef]

        if not validate_skill_name(name):
            raise ValueError("invalid skill name: %r" % (name,))
        if len(self.steps) >= MAX_STEPS:
            raise WorkflowValidationError(
                "step limit exceeded (%d)" % MAX_STEPS)
        clean_payload = sanitize(payload or {})
        if not isinstance(clean_payload, dict):
            clean_payload = {}
        self.steps.append({"name": name, "payload": clean_payload,
                           "ts": time.time()})

    def add_conditional_branch(self, step_name: str, condition: Callable[[Dict[str, Any]], bool], true_next: str, false_next: Optional[str] = None) -> None:
        """Register a conditional execution branch for dynamic routing.

        *condition* must be a trusted in-process callable (never
        deserialized). Branch targets must be valid skill names.
        """
        if not validate_skill_name(step_name):
            raise ValueError("invalid skill name: %r" % (step_name,))
        for target in [true_next] + ([false_next] if false_next else []):
            if not validate_skill_name(target):
                raise ValueError("invalid branch target: %r" % (target,))
        if not callable(condition):
            raise ValueError("condition must be callable")
        if len(self.steps) >= MAX_STEPS:
            raise WorkflowValidationError(
                "step limit exceeded (%d)" % MAX_STEPS)
        self.steps.append({
            "name": step_name,
            "type": "conditional",
            "condition": condition,
            "true_next": true_next,
            "false_next": false_next,
            "ts": time.time(),
        })

    def add_retry_step(self, name: str, retries: int = 3, backoff_base: float = 0.5) -> None:
        """Add retry metadata to the most recent step (bounded)."""
        if not self.steps:
            return
        self.steps[-1].update({
            "retries": max(0, min(int(retries), MAX_RETRIES)),
            "backoff_base": max(0.0, min(float(backoff_base), 60.0)),
        })

    def validate_plan(self, graph=None) -> Dict[str, Any]:
        """Static validation: bounds, known handlers, graph consistency."""
        issues = []
        if len(self.steps) > MAX_STEPS:
            issues.append("too many steps: %d" % len(self.steps))
        names = [s["name"] for s in self.steps]
        unknown = sorted({n for n in names if n not in self._handlers})
        if unknown:
            issues.append("unregistered skills: %s" % ", ".join(unknown))
        graph_status: Optional[Dict[str, Any]] = None
        if graph is not None:
            try:
                graph_status = graph.validate()
                if graph_status["cycles"]:
                    issues.append("graph cycles: %s" % graph_status["cycles"])
            except Exception as exc:
                issues.append("graph validation failed: %s" % exc)
        return {"valid": not issues, "issues": issues,
                "steps": len(self.steps), "graph": graph_status}

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def _exec_with_retry(self, step: Dict[str, Any], run_fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        """Execute a step with retry/backoff on transient failure."""
        retries = int(step.get("retries") or 0)
        backoff = float(step.get("backoff_base") or 0.0)
        timeout = float(step.get("timeout_s") or DEFAULT_STEP_TIMEOUT_S)
        last: Dict[str, Any] = {"step": step["name"], "status": "error", "executed_at": time.time(), "payload": step.get("payload", {})}
        for attempt in range(1 + retries):
            if self._cancelled:
                last = {"step": step["name"], "status": "cancelled",
                        "executed_at": time.time(),
                        "payload": step.get("payload", {})}
                break
            future = _EXECUTOR.submit(run_fn, step)
            try:
                last = future.result(timeout=timeout)
            except FuturesTimeout:
                future.cancel()
                last = {"step": step["name"], "status": "timeout",
                        "executed_at": time.time(),
                        "payload": step.get("payload", {})}
            except Exception as exc:
                last = {"step": step["name"], "status": "error",
                        "executed_at": time.time(),
                        "payload": step.get("payload", {}),
                        "error": "%s: %s" % (type(exc).__name__, exc)}
            last["attempt"] = attempt + 1
            if last.get("status") == "ok":
                break
            if attempt < retries:
                time.sleep(min(backoff * (2 ** attempt), 60.0))
        return last

    def run(self, context: Optional[Dict[str, Any]] = None,
            graph=None) -> List[Dict[str, Any]]:
        """Validate, execute in order, audit every step. Deterministic."""
        try:
            from .redaction import sanitize
        except ImportError:
            from redaction import sanitize  # type: ignore[no-redef]

        validation = self.validate_plan(graph=graph)
        if not validation["valid"]:
            raise WorkflowValidationError(
                "invalid plan: %s" % "; ".join(validation["issues"]))
        results: List[Dict[str, Any]] = []
        context = dict(sanitize(context or {}))
        steps = list(self.steps)

        def run_step(step: Dict[str, Any]) -> Dict[str, Any]:
            if self._cancelled:
                return {"step": step["name"], "status": "cancelled",
                        "executed_at": time.time(),
                        "payload": step.get("payload", {})}
            if step.get("type") == "conditional":
                fn = step.get("condition")
                chosen = step.get("true_next")
                try:
                    if fn is not None and not fn(context):
                        chosen = step.get("false_next")
                except Exception as exc:
                    return {"step": step["name"], "status": "error",
                            "executed_at": time.time(),
                            "payload": step.get("payload", {}),
                            "error": "condition failed: %s" % exc}
                return {"step": step["name"], "status": "ok",
                        "executed_at": time.time(), "next": chosen,
                        "payload": step.get("payload", {})}
            handler = self._handlers.get(step["name"])
            if handler is None:
                return {"step": step["name"], "status": "error",
                        "executed_at": time.time(),
                        "payload": step.get("payload", {}),
                        "error": "no handler registered"}
            try:
                out = handler({"payload": step.get("payload", {}),
                               "context": context})
                if not isinstance(out, dict):
                    out = {"result": out}
            except Exception as exc:
                return {"step": step["name"], "status": "error",
                        "executed_at": time.time(),
                        "payload": step.get("payload", {}),
                        "error": "%s: %s" % (type(exc).__name__, exc)}
            out.setdefault("step", step["name"])
            out.setdefault("status", "ok")
            out.setdefault("executed_at", time.time())
            return out

        idx = 0
        while idx < len(steps):
            step = steps[idx]
            if "retries" in step or "timeout_s" in step:
                result = self._exec_with_retry(step, run_step)
            else:
                result = run_step(step)
                if self._cancelled and result.get("status") != "cancelled":
                    result["status"] = "cancelled"
            results.append(result)
            self.audit.append({"step": step["name"],
                               "status": result.get("status"),
                               "at": time.time(),
                               "attempt": result.get("attempt", 1)})
            context[step["name"]] = result
            if result.get("status") in ("error", "timeout", "cancelled"):
                break  # fail-fast: never blindly continue past failures
            nxt = result.get("next")
            if nxt:
                for i, s in enumerate(steps):
                    if s["name"] == nxt:
                        idx = i
                        break
                else:
                    idx += 1
            else:
                idx += 1
        self.results.extend(results)
        self._cancelled = False
        return results

    def clear(self) -> None:
        """Reset workflow state (handlers survive; audit is kept)."""
        self.steps.clear()
        self.results.clear()

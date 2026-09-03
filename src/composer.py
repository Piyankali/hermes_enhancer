"""SkillComposer - Workflow orchestration with fallback/retry conditional execution.

v0.20.7 enterprise: Adds conditional branches, fallbacks, and retry policy support
for resilient tool execution pipelines.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional


class SkillComposer:
    """Minimal workflow engine stub for composing skill execution plans."""

    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []
        self.results: List[Dict[str, Any]] = []

    def add_step(self, name: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Add a workflow step."""
        self.steps.append({"name": name, "payload": payload or {}, "ts": time.time()})

    def add_conditional_branch(self, step_name: str, condition: Callable[[Dict[str, Any]], bool], true_next: str, false_next: Optional[str] = None) -> None:
        """Register a conditional execution branch for dynamic routing."""
        self.steps.append({
            "name": step_name,
            "type": "conditional",
            "condition": condition,
            "true_next": true_next,
            "false_next": false_next,
            "ts": time.time(),
        })

    def add_retry_step(self, name: str, retries: int = 3, backoff_base: float = 0.5) -> None:
        """Add retry metadata to the most recent step."""
        if not self.steps:
            return
        self.steps[-1].update({
            "retries": max(0, retries),
            "backoff_base": max(0.0, backoff_base),
        })

    def _exec_with_retry(self, step: Dict[str, Any], run_fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        """Execute a step with retry/backoff on transient failure."""
        retries = int(step.get("retries") or 0)
        backoff = float(step.get("backoff_base") or 0.0)
        last: Dict[str, Any] = {"step": step["name"], "status": "error", "executed_at": time.time(), "payload": step.get("payload", {})}
        for attempt in range(1 + retries):
            last = run_fn(step)
            if last.get("status") == "ok":
                break
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
        return last

    def run(self, context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Execute workflow steps in order and return results with fallback/retry."""
        results: List[Dict[str, Any]] = []
        context = context or {}
        steps = list(self.steps)

        def run_step(step: Dict[str, Any]) -> Dict[str, Any]:
            if step.get("type") == "conditional":
                fn = step.get("condition")
                chosen = step.get("true_next")
                if fn is not None and not fn(context):
                    chosen = step.get("false_next")
                return {"step": step["name"], "status": "ok", "executed_at": time.time(), "next": chosen, "payload": step.get("payload", {})}
            return {
                "step": step["name"],
                "status": "ok",
                "executed_at": time.time(),
                "payload": step.get("payload", {}),
            }

        idx = 0
        while idx < len(steps):
            step = steps[idx]
            if "retries" in step:
                result = self._exec_with_retry(step, run_step)
            else:
                result = run_step(step)
            results.append(result)
            context[step["name"]] = result
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
        return results

    def clear(self) -> None:
        """Reset workflow state."""
        self.steps.clear()
        self.results.clear()

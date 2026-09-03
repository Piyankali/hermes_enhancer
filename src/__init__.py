"""Hermes Enhancer - Self-healing and telemetry plugin package."""

import time
from typing import Any, Callable, Dict, Optional

from .enhancer import HermesEnhancer
from .federated_db import get_db, FederatedDB
from .self_test import SelfTestEngine
from .feedback_optimizer import FeedbackOptimizer
from .predictive_preload import PredictivePreload
from .meta_learner import MetaLearner
from .skill_graph import SkillGraph
from .composer import SkillComposer

_instance: Optional[HermesEnhancer] = None
_pending_pre: Dict[str, float] = {}


def register(ctx) -> None:
    """Register the enhancer plugin and bind pre/post tool hooks into Hermes runtime."""
    global _instance
    enhancer = HermesEnhancer(node_id=getattr(ctx, "node_id", None) or "local")
    _instance = enhancer

    def _on_pre_tool_call(
        tool_name: str = "",
        args: Optional[Dict[str, Any]] = None,
        task_id: str = "",
        session_id: str = "",
        tool_call_id: str = "",
        turn_id: str = "",
        api_request_id: str = "",
        middleware_trace: Optional[list] = None,
        **_: Any,
    ) -> None:
        tool_name = tool_name or "unknown"
        key = tool_call_id or tool_name
        _pending_pre[key] = time.perf_counter()
        enhancer.pre_tool_call(
            args=(tool_name,),
            kwargs={
                "task_id": task_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
                "turn_id": turn_id,
                "api_request_id": api_request_id,
                "middleware_trace": middleware_trace or [],
            },
        )

    def _on_post_tool_call(
        tool_name: str = "",
        args: Optional[Dict[str, Any]] = None,
        result: Any = None,
        status: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_ms: int = 0,
        task_id: str = "",
        session_id: str = "",
        tool_call_id: str = "",
        turn_id: str = "",
        api_request_id: str = "",
        middleware_trace: Optional[list] = None,
        **_: Any,
    ) -> None:
        tool_name = tool_name or "unknown"
        key = tool_call_id or tool_name
        t0 = _pending_pre.pop(key, None)
        if t0 is None:
            t0 = time.perf_counter()
        pre_state = {"_enhancer_t0": t0, "_enhancer_tool": tool_name}
        enhancer.on_post_tool_call(
            tool_name=tool_name,
            pre_state=pre_state,
            result=result,
            error=RuntimeError(error_message) if error_message else None,
        )

    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)


def get_instance() -> HermesEnhancer:
    """Get or create the HermesEnhancer singleton instance."""
    global _instance
    if _instance is None:
        _instance = HermesEnhancer()
    return _instance


def hooks() -> Dict[str, Callable]:
    """Return active hook bindings for Hermes runtime."""
    enhancer = get_instance()
    return {
        "pre_tool_call": enhancer.pre_tool_call,
        "post_tool_call": enhancer.post_tool_call,
    }

"""PredictivePreload - Markov chain tool sequencer with memory-leak protection.

v0.20.7 enterprise: Monitors memory footprint and auto-clears cache/gc
to maintain 24/7 uptime.
v0.23: restart-safe SQLite persistence (tool_transitions), bounded
preload plans with TTL + invalidation, and prediction logging consumed
by the decision engine. Counting statistics -- no ML.
"""

from __future__ import annotations

import gc
import time
try:
    import resource
except ImportError:  # Windows / non-POSIX: memory guard degrades to no-op.
    resource = None  # type: ignore[assignment]
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("hermes.enhancer.preload")

MAX_TRANSITION_KEYS = 2000
MAX_TARGETS_PER_KEY = 50
MAX_SEQUENCE_LEN = 500
PLAN_TTL_S = 300.0


class PredictivePreload:
    """Tracks tool call sequences and predicts next likely tool."""

    def __init__(self, order: int = 1, memory_limit_mb: int = 256) -> None:
        if order < 1:
            raise ValueError("order must be >= 1")
        self.order = order
        self.memory_limit_bytes = memory_limit_mb * 1024 * 1024
        self.transitions: Dict[Tuple[str, ...], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.last_sequence: List[str] = []
        self._plans: Dict[int, Dict[str, Any]] = {}
        self._plan_counter = 0

    def _current_rss(self) -> int:
        """Return current RSS in bytes (0 when unmeasurable)."""
        try:
            if resource is None:
                return 0
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        except Exception:
            return 0

    def maybe_cleanup(self) -> None:
        """Auto-trigger cache purge and gc.collect() when memory limit is reached."""
        try:
            rss = self._current_rss()
            if rss > self.memory_limit_bytes:
                logger.warning("Memory limit exceeded: %s bytes > %s; clearing preload cache", rss, self.memory_limit_bytes)
                self.clear()
                gc.collect()
        except Exception as exc:
            logger.debug("Memory cleanup check failed: %s", exc)

    def record(self, tool_name: str) -> None:
        """Record a tool call event and update transition counts (bounded)."""
        self.maybe_cleanup()
        history = tuple(self.last_sequence[-self.order :])
        if history:
            targets = self.transitions[history]
            targets[tool_name] += 1
            if len(targets) > MAX_TARGETS_PER_KEY:
                # Drop the coldest target to stay bounded.
                coldest = min(targets, key=lambda k: targets[k])
                del targets[coldest]
            if len(self.transitions) > MAX_TRANSITION_KEYS:
                oldest = next(iter(self.transitions))
                del self.transitions[oldest]
        self.last_sequence.append(tool_name)
        if len(self.last_sequence) > MAX_SEQUENCE_LEN:
            del self.last_sequence[: len(self.last_sequence) - MAX_SEQUENCE_LEN]

    def predict(self, current_tool: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """Predict the next likely tools given the current tool."""
        if self.order == 1:
            history = (current_tool,)
        else:
            history = tuple(self.last_sequence[-self.order :])
        counts = self.transitions.get(history, {})
        if not counts:
            return []
        total = sum(counts.values())
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [(tool, count / total) for tool, count in ranked]

    def clear(self) -> None:
        """Reset state."""
        self.transitions.clear()
        self.last_sequence.clear()
        self._plans.clear()

    # ------------------------------------------------------------------ #
    # v0.23 persistence + plans
    # ------------------------------------------------------------------ #
    @staticmethod
    def _key_to_str(history: Tuple[str, ...]) -> str:
        return "\x1f".join(history)

    @staticmethod
    def _str_to_key(prev_key: str) -> Tuple[str, ...]:
        return tuple(prev_key.split("\x1f")) if prev_key else ()

    def to_rows(self) -> List[Dict[str, Any]]:
        rows = []
        for history, targets in self.transitions.items():
            prev_key = self._key_to_str(history)
            for next_tool, count in targets.items():
                rows.append({"prev_key": prev_key, "next_tool": next_tool,
                             "count": count})
        return rows

    def load_rows(self, rows: List[Dict[str, Any]]) -> int:
        count = 0
        for r in rows:
            try:
                history = self._str_to_key(str(r["prev_key"]))
                if not history or len(history) > 8:
                    continue
                next_tool = str(r["next_tool"])
                if not next_tool or len(next_tool) > 128:
                    continue
                self.transitions[history][next_tool] = int(r["count"])
                count += 1
            except (KeyError, TypeError, ValueError):
                continue
        return count

    def save_to_db(self, db) -> int:
        """Persist transition counts. Returns rows written."""
        n = db.save_transitions(self.to_rows())
        try:
            db.prune_transitions()
        except Exception:
            pass
        return n

    def load_from_db(self, db) -> int:
        """Restore transition counts. Returns rows restored."""
        return self.load_rows(db.load_transitions())

    def plan_next(self, current_tool: str, threshold: float = 0.5,
                  top_k: int = 3, db=None) -> List[Dict[str, Any]]:
        """Build bounded preload plans for likely next tools.

        Each plan is a real, observable object: {plan_id, current_tool,
        predicted_tool, confidence, created_at, status}. Plans expire
        after PLAN_TTL_S and can be invalidated/consumed explicitly.
        Predictions at/above *threshold* are also logged to the
        predictions table when *db* is supplied (persisted, restart-safe,
        observable). Nothing is executed by planning.
        """
        self._expire_plans()
        plans = []
        for tool, prob in self.predict(current_tool, top_k=top_k):
            if prob < threshold:
                continue
            self._plan_counter += 1
            plan_id = self._plan_counter
            plan = {"plan_id": plan_id, "current_tool": current_tool,
                    "predicted_tool": tool, "confidence": round(prob, 4),
                    "created_at": time.time(), "status": "proposed"}
            self._plans[plan_id] = plan
            plans.append(dict(plan))
            if db is not None:
                try:
                    db.log_prediction(current_tool, tool, prob)
                except Exception:
                    pass
        # Bound live plans.
        while len(self._plans) > 100:
            oldest = min(self._plans, key=lambda k: self._plans[k]["created_at"])
            del self._plans[oldest]
        return plans

    def _expire_plans(self) -> int:
        now = time.time()
        expired = [k for k, p in self._plans.items()
                   if p["status"] == "proposed"
                   and now - p["created_at"] > PLAN_TTL_S]
        for k in expired:
            self._plans[k]["status"] = "expired"
        return len(expired)

    def consume_plan(self, plan_id: int) -> Optional[Dict[str, Any]]:
        """Mark a plan consumed (a candidate was actually prepared/used)."""
        plan = self._plans.get(plan_id)
        if plan is None or plan["status"] != "proposed":
            return None
        plan["status"] = "consumed"
        return dict(plan)

    def invalidate_plan(self, plan_id: int) -> bool:
        """Invalidate a stale/wrong plan. Returns True when found."""
        plan = self._plans.get(plan_id)
        if plan is None:
            return False
        plan["status"] = "invalidated"
        return True

    def active_plans(self) -> List[Dict[str, Any]]:
        self._expire_plans()
        return [dict(p) for p in self._plans.values()
                if p["status"] == "proposed"]

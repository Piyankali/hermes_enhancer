"""PredictivePreload - Markov chain tool sequencer with memory-leak protection.

v0.20.7 enterprise: Monitors memory footprint and auto-clears cache/gc
to maintain 24/7 uptime.
"""

from __future__ import annotations

import gc
import time
import resource
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


logger = logging.getLogger("hermes.enhancer.preload")


class PredictivePreload:
    """Tracks tool call sequences and predicts next likely tool."""

    def __init__(self, order: int = 1, memory_limit_mb: int = 256) -> None:
        if order < 1:
            raise ValueError("order must be >= 1")
        self.order = order
        self.memory_limit_bytes = memory_limit_mb * 1024 * 1024
        self.transitions: Dict[Tuple[str, ...], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.last_sequence: List[str] = []

    def _current_rss(self) -> int:
        """Return current RSS in bytes."""
        try:
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
        """Record a tool call event and update transition counts."""
        self.maybe_cleanup()
        history = tuple(self.last_sequence[-self.order :])
        if history:
            self.transitions[history][tool_name] += 1
        self.last_sequence.append(tool_name)

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

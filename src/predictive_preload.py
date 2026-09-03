"""PredictivePreload - Markov chain tool sequencer for prefetch hints."""

import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


class PredictivePreload:
    """Tracks tool call sequences and predicts next likely tool."""

    def __init__(self, order: int = 1) -> None:
        if order < 1:
            raise ValueError("order must be >= 1")
        self.order = order
        self.transitions: Dict[Tuple[str, ...], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.last_sequence: List[str] = []

    def record(self, tool_name: str) -> None:
        """Record a tool call event and update transition counts."""
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

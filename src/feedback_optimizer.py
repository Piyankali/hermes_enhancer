"""FeedbackOptimizer - EMA success scoring for tool/skill outcomes.

v0.20.7: Ignores anomalous zero/negative/slow durations (>300s) from EMA scoring.
"""

import time
import math
from typing import Any, Dict, List, Optional


class FeedbackOptimizer:
    """Exponential Moving Average (EMA) scorer for telemetry outcomes."""

    def __init__(self, alpha: float = 0.2, default_score: float = 0.5) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = alpha
        self.default_score = default_score
        self.scores: Dict[str, float] = {}

    def _is_anomalous_duration(self, duration_s: float) -> bool:
        """Flag zero, negative, or extreme durations as anomalous."""
        if duration_s <= 0:
            return True
        if duration_s > 300:
            return True
        return False

    def record_outcome(self, key: str, success: bool, weight: float = 1.0,
                       duration_s: float = 0.0, anomalous: bool = False) -> None:
        """Record an outcome and update the EMA score for a given key.

        Anomalous durations are not used to update EMA to avoid skewed metrics.
        """
        current = self.scores.get(key, self.default_score)
        value = 1.0 if success else 0.0

        if anomalous or self._is_anomalous_duration(duration_s):
            updated = current
        else:
            updated = self.alpha * value * weight + (1.0 - self.alpha) * current

        self.scores[key] = updated

    def get_score(self, key: str) -> float:
        """Get the current EMA score for a key."""
        return self.scores.get(key, self.default_score)

    def top_n(self, n: int = 10) -> List[tuple[str, float]]:
        """Return the top N scored keys, sorted descending."""
        return sorted(self.scores.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def summary(self) -> Dict[str, Any]:
        """Return a summary of scores and distribution."""
        if not self.scores:
            return {"count": 0, "mean": self.default_score, "top": []}
        values = list(self.scores.values())
        return {
            "count": len(values),
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "top": self.top_n(),
        }

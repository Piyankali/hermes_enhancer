"""FeedbackOptimizer - EMA success scoring for tool/skill outcomes.

v0.20.7: Ignores anomalous zero/negative/slow durations (>300s) from EMA scoring.
v0.23: per-tool sample/confidence tracking, restart-safe SQLite
persistence (tool_feedback), and a safe candidate-ranking API consumed
by the decision engine. Statistics only -- no ML.
"""

import time
import math
from typing import Any, Dict, List, Optional, Tuple


class FeedbackOptimizer:
    """Exponential Moving Average (EMA) scorer for telemetry outcomes."""

    # Confidence smoothing constant: confidence = samples / (samples + K).
    CONFIDENCE_K = 5.0

    def __init__(self, alpha: float = 0.2, default_score: float = 0.5) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = alpha
        self.default_score = default_score
        self.scores: Dict[str, float] = {}
        self.samples: Dict[str, int] = {}
        self.successes: Dict[str, int] = {}
        self.failures: Dict[str, int] = {}

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
            self.samples[key] = self.samples.get(key, 0) + 1
            if success:
                self.successes[key] = self.successes.get(key, 0) + 1
            else:
                self.failures[key] = self.failures.get(key, 0) + 1

        self.scores[key] = updated

    def get_score(self, key: str) -> float:
        """Get the current EMA score for a key."""
        return self.scores.get(key, self.default_score)

    def score(self, key: str) -> Dict[str, Any]:
        """Decision-engine view: score + sample-based confidence."""
        samples = self.samples.get(key, 0)
        confidence = samples / (samples + self.CONFIDENCE_K)
        return {
            "tool": key,
            "score": self.scores.get(key, self.default_score),
            "samples": samples,
            "successes": self.successes.get(key, 0),
            "failures": self.failures.get(key, 0),
            "confidence": round(confidence, 4),
        }

    def rank_candidates(
        self, candidates: List[str], min_samples: int = 3
    ) -> List[Dict[str, Any]]:
        """Rank candidate tools by score with confidence gating.

        Candidates below *min_samples* keep the default score and are
        flagged low-confidence so the decision layer falls back instead
        of trusting thin history. The input list is never shortened here
        -- callers decide; in particular the only available tool is never
        removed by ranking.
        """
        ranked = []
        for tool in candidates:
            info = self.score(tool)
            info["trusted"] = info["samples"] >= min_samples
            ranked.append(info)
        ranked.sort(key=lambda r: (r["trusted"], r["score"]), reverse=True)
        return ranked

    def to_rows(self) -> List[Dict[str, Any]]:
        return [
            {"tool": tool, "score": score,
             "samples": self.samples.get(tool, 0),
             "successes": self.successes.get(tool, 0),
             "failures": self.failures.get(tool, 0)}
            for tool, score in self.scores.items()
        ]

    def load_rows(self, rows: List[Dict[str, Any]]) -> int:
        count = 0
        for r in rows:
            try:
                tool = str(r["tool"])
                self.scores[tool] = float(r["score"])
                self.samples[tool] = int(r["samples"])
                self.successes[tool] = int(r.get("successes", 0))
                self.failures[tool] = int(r.get("failures", 0))
                count += 1
            except (KeyError, TypeError, ValueError):
                # Corrupted learning row: quarantine by skipping.
                continue
        return count

    def save_to_db(self, db) -> int:
        """Persist EMA state via FederatedDB. Returns rows written."""
        return db.save_feedback(self.to_rows())

    def load_from_db(self, db) -> int:
        """Restore EMA state via FederatedDB. Returns rows restored."""
        return self.load_rows(db.load_feedback())

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

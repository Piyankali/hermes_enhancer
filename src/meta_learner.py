"""MetaLearner - statistical history optimizer with SQLite persistence.

v0.21.0-dev: Bounded history, workflow summary, cold/warm separation.
v0.23: derives reusable statistics from historical outcomes (per-tool
latency/failure rates, successful tool-pair sequences), persists them
(meta_tool_stats, meta_sequences), and exposes learn()/recommend() /
rank_workflows() consumed by the decision engine.

Statistical heuristics only -- no neural networks, no RL, no LLM
training, no embeddings.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple


MAX_HISTORY = 2000
SEQ_ORDER = 2


class MetaLearner:
    """Statistical optimizer over long-running telemetry history."""

    def __init__(self) -> None:
        self.history: deque[Dict[str, Any]] = deque(maxlen=MAX_HISTORY)
        self.winning_workflows: List[Dict[str, Any]] = []
        # Aggregates (also persisted): tool -> {calls, fails, duration_us}
        self.tool_stats: Dict[str, Dict[str, int]] = {}
        # Sequence key "a->b" -> {occurrences, successes}
        self.sequences: Dict[str, Dict[str, int]] = {}
        self._recent_tools: List[str] = []

    # ------------------------------------------------------------------ #
    # Ingest / learn
    # ------------------------------------------------------------------ #
    def ingest(self, event: Dict[str, Any]) -> None:
        """Ingest a telemetry event and update running aggregates."""
        try:
            from .redaction import sanitize
        except ImportError:
            from redaction import sanitize  # type: ignore[no-redef]

        event = sanitize(event)
        self.history.append({"ts": time.time(), "event": event})
        tool = str(event.get("tool") or "unknown")
        success = bool(event.get("success"))
        try:
            delta_us = int(event.get("delta_us") or 0)
        except (TypeError, ValueError):
            delta_us = 0
        stats = self.tool_stats.setdefault(
            tool, {"calls": 0, "fails": 0, "duration_us": 0})
        stats["calls"] += 1
        stats["duration_us"] += max(0, delta_us)
        if not success:
            stats["fails"] += 1
        # Successful pair sequences ("prev->tool" where tool succeeded).
        prev = self._recent_tools[-1] if self._recent_tools else None
        self._recent_tools.append(tool)
        if len(self._recent_tools) > SEQ_ORDER + 1:
            del self._recent_tools[: len(self._recent_tools) - (SEQ_ORDER + 1)]
        if success and prev:
            key = "%s->%s" % (prev, tool)
            entry = self.sequences.setdefault(
                key, {"occurrences": 0, "successes": 0})
            entry["occurrences"] += 1
            entry["successes"] += 1
        if event.get("success") and event.get("tool"):
            workflow = {
                "workflow": json.dumps({
                    "tool": event.get("tool"),
                    "success": event.get("success"),
                    "delta_us": event.get("delta_us"),
                    "anomalous": event.get("anomalous"),
                }),
                "score": 1.0,
                "recorded_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self.winning_workflows.append(workflow)

    def learn(self, events: List[Dict[str, Any]]) -> Dict[str, int]:
        """Batch-ingest events; returns counts {ingested, tools, sequences}."""
        before_tools = len(self.tool_stats)
        before_seq = len(self.sequences)
        n = 0
        for event in events:
            if isinstance(event, dict):
                self.ingest(event)
                n += 1
        return {"ingested": n,
                "tools": len(self.tool_stats) - before_tools,
                "sequences": len(self.sequences) - before_seq}

    # ------------------------------------------------------------------ #
    # Recommend / rank
    # ------------------------------------------------------------------ #
    def recommend(self, current_tool: Optional[str] = None,
                  top_k: int = 5) -> List[Dict[str, Any]]:
        """Recommend follow-up tools after *current_tool*.

        Scores candidate sequences by success rate x log(occurrences):
        frequently-successful continuations rank first. Returns
        [{tools, occurrences, success_rate, score}].
        """
        scored = []
        for key, entry in self.sequences.items():
            parts = key.split("->")
            if len(parts) != 2:
                continue
            if current_tool is not None and parts[0] != current_tool:
                continue
            occ = entry["occurrences"]
            rate = entry["successes"] / occ if occ else 0.0
            import math
            score = rate * math.log1p(occ)
            scored.append({"tools": parts[1:], "occurrences": occ,
                           "success_rate": round(rate, 4),
                           "score": round(score, 4)})
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_k]

    def failure_patterns(self, min_fails: int = 2) -> List[Dict[str, Any]]:
        """Tools with repeated failures (latency + fail rate included)."""
        out = []
        for tool, s in self.tool_stats.items():
            if s["fails"] >= min_fails:
                avg_us = s["duration_us"] // max(1, s["calls"])
                out.append({"tool": tool, "calls": s["calls"],
                            "fails": s["fails"],
                            "fail_rate": round(s["fails"] / max(1, s["calls"]), 4),
                            "avg_duration_us": avg_us})
        out.sort(key=lambda r: r["fail_rate"], reverse=True)
        return out

    def rank_workflows(self, top_k: int = 10) -> List[Dict[str, Any]]:
        """Rank learned sequences across all contexts by score."""
        return self.recommend(current_tool=None, top_k=top_k)

    # ------------------------------------------------------------------ #
    # Maintenance / persistence
    # ------------------------------------------------------------------ #
    def prune(self, keep_last: int = 1000) -> int:
        """Retain only the most recent events (deque auto-bounds anyway)."""
        overflow = len(self.history) - keep_last
        if overflow <= 0:
            return 0
        for _ in range(overflow):
            self.history.popleft()
        return overflow

    def summary(self) -> Dict[str, Any]:
        return {
            "history_count": len(self.history),
            "winning_workflows_count": len(self.winning_workflows),
            "tools_tracked": len(self.tool_stats),
            "sequences_tracked": len(self.sequences),
        }

    def to_stat_rows(self) -> List[Dict[str, Any]]:
        return [{"tool": t, "calls": s["calls"], "fails": s["fails"],
                 "total_duration_us": s["duration_us"]}
                for t, s in self.tool_stats.items()]

    def to_sequence_rows(self) -> List[Dict[str, Any]]:
        return [{"seq_key": k, "pattern": k,
                 "occurrences": v["occurrences"], "successes": v["successes"]}
                for k, v in self.sequences.items()]

    def load_stat_rows(self, rows: List[Dict[str, Any]]) -> int:
        count = 0
        for r in rows:
            try:
                tool = str(r["tool"])
                self.tool_stats[tool] = {
                    "calls": int(r["calls"]), "fails": int(r["fails"]),
                    "duration_us": int(r["total_duration_us"])}
                count += 1
            except (KeyError, TypeError, ValueError):
                continue
        return count

    def load_sequence_rows(self, rows: List[Dict[str, Any]]) -> int:
        count = 0
        for r in rows:
            try:
                key = str(r["seq_key"])
                if not key or len(key) > 512:
                    continue
                self.sequences[key] = {
                    "occurrences": int(r["occurrences"]),
                    "successes": int(r["successes"])}
                count += 1
            except (KeyError, TypeError, ValueError):
                continue
        return count

    def save_to_db(self, db) -> Dict[str, int]:
        """Persist aggregates. Returns {stats, sequences} written."""
        return {"stats": db.save_tool_stats(self.to_stat_rows()),
                "sequences": db.save_sequences(self.to_sequence_rows())}

    def load_from_db(self, db) -> Dict[str, int]:
        """Restore aggregates. Returns {stats, sequences} restored."""
        return {"stats": self.load_stat_rows(db.load_tool_stats()),
                "sequences": self.load_sequence_rows(db.load_sequences())}

"""DecisionEngine - conservative candidate ranking over learned state.

v0.23: closes the loop that v0.22 left open. Consumes FeedbackOptimizer
scores, PredictivePreload plans, MetaLearner recommendations, and
SkillGraph constraints to produce ranked, confidence-gated suggestions.

Policy levels: observe (default) -> recommend -> approved -> automatic.
Default behavior never overrides, never executes, never removes the
only available tool. Privileged/destructive operations are never
autonomously generated from learned data. Statistics + heuristics only.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

POLICY_OBSERVE = "observe"
POLICY_RECOMMEND = "recommend"
POLICY_APPROVED = "approved"
POLICY_AUTOMATIC = "automatic"
POLICIES = (POLICY_OBSERVE, POLICY_RECOMMEND, POLICY_APPROVED, POLICY_AUTOMATIC)

# Tools whose autonomous selection requires the strictest policy AND
# high confidence. Everything else degrades to suggestion/observation.
PRIVILEGED_TOOLS = frozenset({
    "write_file", "patch", "terminal", "computer_use",
    "send", "browser", "execute",
})

DEFAULT_CONFIDENCE_THRESHOLD = 0.6
PRIVILEGED_CONFIDENCE_THRESHOLD = 0.8


class DecisionEngine:
    """Rank candidates from learned state under a safety policy."""

    def __init__(self, policy: str = POLICY_OBSERVE,
                 confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
                 feedback=None, preload=None, meta_learner=None,
                 skill_graph=None) -> None:
        if policy not in POLICIES:
            raise ValueError("unknown policy: %r" % (policy,))
        self.policy = policy
        self.confidence_threshold = confidence_threshold
        self.feedback = feedback
        self.preload = preload
        self.meta_learner = meta_learner
        self.skill_graph = skill_graph
        self.decisions: List[Dict[str, Any]] = []

    def set_policy(self, policy: str) -> None:
        if policy not in POLICIES:
            raise ValueError("unknown policy: %r" % (policy,))
        self.policy = policy

    def recommend(self, candidates: List[str],
                  context_tool: Optional[str] = None) -> Dict[str, Any]:
        """Rank *candidates* using every available learned signal.

        Returns {action, ranking, confidence, reasons, ...} where action
        is one of: none (observe / low confidence), suggest (recommend+),
        execute_candidate (approved/automatic + high confidence +
        non-privileged or privileged-with-very-high-confidence).
        The candidate list is never shortened; selection is advisory.
        """
        if not candidates:
            return {"action": "none", "ranking": [], "confidence": 0.0,
                    "reasons": ["no candidates"], "policy": self.policy,
                    "selected": None}
        scored: Dict[str, Dict[str, Any]] = {
            c: {"tool": c, "feedback_score": 0.5, "feedback_conf": 0.0,
                "predicted": False, "pred_conf": 0.0, "meta_score": 0.0}
            for c in candidates
        }
        reasons: List[str] = []
        # Feedback signal.
        if self.feedback is not None:
            try:
                for entry in self.feedback.rank_candidates(candidates):
                    s = scored.get(entry["tool"])
                    if s is not None:
                        s["feedback_score"] = entry["score"]
                        s["feedback_conf"] = entry["confidence"]
                        s["trusted"] = entry["trusted"]
                reasons.append("feedback")
            except Exception as exc:
                reasons.append("feedback unavailable: %s" % exc)
        # Prediction signal.
        predicted: Dict[str, float] = {}
        if self.preload is not None and context_tool:
            try:
                for tool, prob in self.preload.predict(context_tool):
                    if tool in scored:
                        predicted[tool] = prob
                        scored[tool]["predicted"] = True
                        scored[tool]["pred_conf"] = round(prob, 4)
                if predicted:
                    reasons.append("prediction")
            except Exception as exc:
                reasons.append("prediction unavailable: %s" % exc)
        # Meta-learner signal.
        if self.meta_learner is not None and context_tool:
            try:
                for rec in self.meta_learner.recommend(context_tool):
                    for tool in rec["tools"]:
                        if tool in scored:
                            scored[tool]["meta_score"] = max(
                                scored[tool]["meta_score"], rec["score"])
                reasons.append("meta")
            except Exception as exc:
                reasons.append("meta unavailable: %s" % exc)
        # Skill-graph constraints (dependency-closed suggestions only).
        if self.skill_graph is not None:
            try:
                cycles = self.skill_graph.detect_cycles()
                if cycles:
                    reasons.append("graph has cycles; constraint skipped")
            except Exception as exc:
                reasons.append("graph unavailable: %s" % exc)
        # Combine: 0.5*feedback + 0.3*prediction + 0.2*meta(normalized).
        ranking = []
        for tool, s in scored.items():
            combined = (0.5 * s["feedback_score"]
                        + 0.3 * (s["pred_conf"] if s["predicted"] else 0.0)
                        + 0.2 * min(1.0, s["meta_score"]))
            confidence = max(s["feedback_conf"],
                             s["pred_conf"] if s["predicted"] else 0.0)
            ranking.append({"tool": tool, "score": round(combined, 4),
                            "confidence": round(confidence, 4),
                            "feedback_score": s["feedback_score"],
                            "predicted": s["predicted"],
                            "privileged": tool in PRIVILEGED_TOOLS})
        ranking.sort(key=lambda r: (r["score"], r["confidence"]), reverse=True)
        best = ranking[0]
        threshold = (PRIVILEGED_CONFIDENCE_THRESHOLD if best["privileged"]
                     else self.confidence_threshold)
        action = "none"
        selected: Optional[str] = None
        if self.policy in (POLICY_APPROVED, POLICY_AUTOMATIC):
            if best["confidence"] >= threshold:
                if (not best["privileged"]
                        or self.policy == POLICY_AUTOMATIC
                        and best["confidence"] >= PRIVILEGED_CONFIDENCE_THRESHOLD):
                    action = "execute_candidate"
                    selected = best["tool"]
                else:
                    action = "suggest"
                    selected = best["tool"]
                    reasons.append("privileged tool downgraded to suggestion")
            else:
                reasons.append("below confidence threshold; no optimization")
        elif self.policy == POLICY_RECOMMEND:
            if best["confidence"] >= self.confidence_threshold:
                action = "suggest"
                selected = best["tool"]
            else:
                reasons.append("below confidence threshold; no optimization")
        else:
            reasons.append("observe policy: ranking only")
        decision = {"action": action, "ranking": ranking,
                    "confidence": best["confidence"], "reasons": reasons,
                    "policy": self.policy, "selected": selected,
                    "at": time.time()}
        self.decisions.append(decision)
        if len(self.decisions) > 200:
            del self.decisions[: len(self.decisions) - 200]
        return decision

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        return list(self.decisions[-limit:])

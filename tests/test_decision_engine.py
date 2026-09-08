"""v0.23 decision-engine tests: conservative, never destructive."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decision_engine import (DecisionEngine, POLICY_OBSERVE, POLICY_RECOMMEND,
                             POLICY_APPROVED, POLICY_AUTOMATIC)
from feedback_optimizer import FeedbackOptimizer
from predictive_preload import PredictivePreload


def _wired():
    fo = FeedbackOptimizer()
    for _ in range(10):
        fo.record_outcome("read_file", success=True, duration_s=1.0)
    for _ in range(10):
        fo.record_outcome("terminal", success=False, duration_s=1.0)
    pp = PredictivePreload()
    for _ in range(3):
        pp.record("terminal")
        pp.record("read_file")
    return DecisionEngine(feedback=fo, preload=pp)


def test_observe_never_acts():
    d = _wired()
    assert d.policy == POLICY_OBSERVE
    out = d.recommend(["terminal", "read_file"], context_tool="terminal")
    assert out["action"] == "none"
    assert out["selected"] is None
    assert out["ranking"][0]["tool"] == "read_file"


def test_recommend_suggests_above_threshold():
    d = _wired()
    d.set_policy(POLICY_RECOMMEND)
    out = d.recommend(["terminal", "read_file"], context_tool="terminal")
    assert out["action"] == "suggest"
    assert out["selected"] == "read_file"


def test_low_confidence_no_optimization():
    d = DecisionEngine(feedback=FeedbackOptimizer())
    d.set_policy(POLICY_AUTOMATIC)
    out = d.recommend(["a", "b"])
    assert out["action"] == "none"
    assert out["selected"] is None


def test_only_candidate_never_removed():
    d = _wired()
    d.set_policy(POLICY_AUTOMATIC)
    out = d.recommend(["terminal"])
    assert len(out["ranking"]) == 1


def test_privileged_needs_high_confidence():
    fo = FeedbackOptimizer()
    for _ in range(4):
        fo.record_outcome("write_file", success=True, duration_s=1.0)
    d = DecisionEngine(feedback=fo)
    d.set_policy(POLICY_APPROVED)
    out = d.recommend(["write_file", "read_file"])
    assert out["action"] in ("none", "suggest")
    assert out["selected"] != "write_file" or out["action"] == "suggest"


def test_empty_candidates_safe():
    d = DecisionEngine()
    out = d.recommend([])
    assert out == {"action": "none", "ranking": [], "confidence": 0.0,
                   "reasons": ["no candidates"], "policy": POLICY_OBSERVE,
                   "selected": None}


def test_unknown_policy_rejected():
    try:
        DecisionEngine(policy="yolo")
        assert False
    except ValueError:
        pass

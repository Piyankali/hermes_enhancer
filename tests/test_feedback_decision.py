"""v0.23 feedback-decision tests: scores actually drive ranking."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from feedback_optimizer import FeedbackOptimizer


def test_ranking_orders_by_score_with_confidence():
    fo = FeedbackOptimizer()
    for _ in range(10):
        fo.record_outcome("good", success=True, duration_s=1.0)
    for _ in range(10):
        fo.record_outcome("bad", success=False, duration_s=1.0)
    ranked = fo.rank_candidates(["bad", "good", "new_tool"])
    assert [r["tool"] for r in ranked] == ["good", "bad", "new_tool"]
    assert ranked[0]["trusted"] is True
    assert ranked[2]["trusted"] is False
    assert ranked[2]["score"] == fo.default_score


def test_only_tool_never_removed_and_low_score_kept():
    fo = FeedbackOptimizer()
    for _ in range(5):
        fo.record_outcome("only", success=False, duration_s=1.0)
    ranked = fo.rank_candidates(["only"])
    assert len(ranked) == 1 and ranked[0]["tool"] == "only"


def test_confidence_grows_with_samples():
    fo = FeedbackOptimizer()
    assert fo.score("t")["confidence"] == 0.0
    for _ in range(20):
        fo.record_outcome("t", success=True, duration_s=1.0)
    c = fo.score("t")["confidence"]
    assert 0.5 < c <= 1.0, c


def test_anomalous_outcomes_do_not_move_score_or_samples():
    fo = FeedbackOptimizer()
    fo.record_outcome("t", success=True, duration_s=1.0)
    before = fo.score("t")
    fo.record_outcome("t", success=False, duration_s=-5.0)
    fo.record_outcome("t", success=False, duration_s=9999.0)
    after = fo.score("t")
    assert after["score"] == before["score"]
    assert after["samples"] == before["samples"]

"""v0.23 meta-learning tests: statistics, recommendations, honesty."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meta_learner import MetaLearner, MAX_HISTORY


def test_learn_sequences_and_recommend():
    ml = MetaLearner()
    for _ in range(3):
        ml.ingest({"tool": "terminal", "success": True, "delta_us": 1000})
        ml.ingest({"tool": "write_file", "success": True, "delta_us": 2000})
    recs = ml.recommend("terminal")
    assert recs and recs[0]["tools"] == ["write_file"]
    assert recs[0]["success_rate"] == 1.0


def test_failure_patterns():
    ml = MetaLearner()
    for _ in range(3):
        ml.ingest({"tool": "flaky", "success": False, "delta_us": 100})
    ml.ingest({"tool": "fine", "success": True, "delta_us": 100})
    pats = ml.failure_patterns()
    assert [p["tool"] for p in pats] == ["flaky"]
    assert pats[0]["fail_rate"] == 1.0


def test_history_bounded():
    ml = MetaLearner()
    for i in range(MAX_HISTORY + 500):
        ml.ingest({"tool": "t%d" % (i % 5), "success": True, "delta_us": 10})
    assert len(ml.history) <= MAX_HISTORY


def test_rank_workflows_orders_by_score():
    ml = MetaLearner()
    for _ in range(5):
        ml.ingest({"tool": "a", "success": True, "delta_us": 10})
        ml.ingest({"tool": "b", "success": True, "delta_us": 10})
    ml.ingest({"tool": "a", "success": True, "delta_us": 10})
    ml.ingest({"tool": "c", "success": True, "delta_us": 10})
    ranked = ml.rank_workflows()
    assert ranked[0]["score"] >= ranked[-1]["score"]


def test_no_ml_claims_in_api():
    ml = MetaLearner()
    assert ml.summary()["tools_tracked"] == 0
    assert ml.recommend("nothing") == []
    assert ml.failure_patterns() == []

"""v0.23 learning-persistence tests: learner state survives restart."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from federated_db import FederatedDB
from feedback_optimizer import FeedbackOptimizer
from predictive_preload import PredictivePreload
from meta_learner import MetaLearner


def test_feedback_roundtrip(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "fb.db"))
    try:
        fo = FeedbackOptimizer()
        for _ in range(5):
            fo.record_outcome("terminal", success=True, duration_s=1.0)
        fo.record_outcome("terminal", success=False, duration_s=1.0)
        assert fo.save_to_db(db) == 1
        fo2 = FeedbackOptimizer()
        assert fo2.load_from_db(db) == 1
        assert fo2.get_score("terminal") == fo.get_score("terminal")
        assert fo2.score("terminal")["samples"] == 6
    finally:
        db.shutdown()


def test_feedback_quarantines_corrupt_rows(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "fbc.db"))
    try:
        fo = FeedbackOptimizer()
        assert fo.load_rows([{"tool": "ok", "score": 0.9, "samples": 3},
                             {"tool": "bad", "score": "nan-x", "samples": 1},
                             {"nope": 1}]) == 1
        assert fo.get_score("ok") == 0.9
    finally:
        db.shutdown()


def test_transitions_roundtrip_and_predict(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "tr.db"))
    try:
        pp = PredictivePreload()
        for tool in ["a", "b", "a", "b", "a", "c"]:
            pp.record(tool)
        assert pp.save_to_db(db) >= 2
        pp2 = PredictivePreload()
        assert pp2.load_from_db(db) >= 2
        preds = dict(pp2.predict("a"))
        assert preds.get("b", 0) > preds.get("c", 0)
    finally:
        db.shutdown()


def test_meta_stats_sequences_roundtrip(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "meta.db"))
    try:
        ml = MetaLearner()
        ml.learn([{"tool": "t1", "success": True, "delta_us": 1000},
                  {"tool": "t2", "success": True, "delta_us": 2000},
                  {"tool": "t1", "success": False, "delta_us": 500}])
        rep = ml.save_to_db(db)
        assert rep["stats"] == 2 and rep["sequences"] >= 1
        ml2 = MetaLearner()
        rep2 = ml2.load_from_db(db)
        assert rep2 == rep
        assert ml2.tool_stats["t1"]["fails"] == 1
        assert ml2.recommend("t1"), "expected follow-up recommendation"
    finally:
        db.shutdown()


def test_schema_version_stamped(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "v.db"))
    try:
        assert db.get_schema_version() == "2"
    finally:
        db.shutdown()


def test_persist_idempotent_rewrite(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "idem.db"))
    try:
        fo = FeedbackOptimizer()
        fo.record_outcome("t", success=True, duration_s=1.0)
        assert fo.save_to_db(db) == 1
        assert fo.save_to_db(db) == 1
        assert len(db.load_feedback()) == 1
    finally:
        db.shutdown()

"""v0.23 prediction-runtime tests: plans are real, bounded, persisted."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from federated_db import FederatedDB
from predictive_preload import PredictivePreload, PLAN_TTL_S


def _trained():
    pp = PredictivePreload()
    for _ in range(4):
        pp.record("terminal")
        pp.record("write_file")
    pp.record("terminal")
    pp.record("other")
    pp.record("terminal")
    return pp


def test_plan_threshold_and_confidence():
    pp = _trained()
    plans = pp.plan_next("terminal", threshold=0.5)
    by_tool = {p["predicted_tool"]: p for p in plans}
    assert by_tool == {"write_file": by_tool["write_file"]}
    assert by_tool["write_file"]["confidence"] == 0.8
    assert by_tool["write_file"]["status"] == "proposed"
    assert pp.plan_next("terminal", threshold=0.9) == []


def test_plan_lifecycle_consume_invalidate():
    pp = _trained()
    (plan,) = pp.plan_next("terminal", threshold=0.5)
    assert pp.consume_plan(plan["plan_id"])["status"] == "consumed"
    assert pp.consume_plan(plan["plan_id"]) is None
    (plan2,) = pp.plan_next("terminal", threshold=0.5)
    assert pp.invalidate_plan(plan2["plan_id"]) is True
    assert pp.invalidate_plan(999999) is False
    assert plan2["plan_id"] not in [p["plan_id"] for p in pp.active_plans()]


def test_plan_expiry():
    import time
    pp = _trained()
    (plan,) = pp.plan_next("terminal", threshold=0.5)
    pp._plans[plan["plan_id"]]["created_at"] -= (PLAN_TTL_S + 1)
    assert pp.active_plans() == []
    assert pp._plans[plan["plan_id"]]["status"] == "expired"


def test_predictions_persisted_and_status_flow(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "pred.db"))
    try:
        pp = _trained()
        pp.plan_next("terminal", threshold=0.5, db=db)
        rows = db.get_predictions("terminal")
        assert len(rows) == 1
        assert rows[0]["predicted_tool"] == "write_file"
        pid = rows[0]["id"]
        assert db.set_prediction_status(pid, "consumed") is True
        assert db.get_predictions("terminal") == []
        assert len(db.get_predictions("terminal", status="consumed")) == 1
        try:
            db.set_prediction_status(pid, "bogus")
            assert False, "expected ValueError"
        except ValueError:
            pass
    finally:
        db.shutdown()


def test_predictions_bounded(tmp_path):
    db = FederatedDB(db_path=str(tmp_path / "predb.db"))
    try:
        for i in range(520):
            db.log_prediction("t", "u%d" % (i % 10), 0.9)
        import sqlite3
        con = sqlite3.connect(db.db_path)
        try:
            n = con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        finally:
            con.close()
        assert n <= 500, n
    finally:
        db.shutdown()


def test_transitions_bounded_in_memory():
    pp = PredictivePreload()
    pp.record("seed")
    for i in range(3000):
        pp2key = "tool_%d" % i
        pp.transitions[("seed",)][pp2key] = 1
    pp.record("seed2")
    assert len(pp.transitions) <= 2001

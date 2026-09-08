"""v0.23 self-test tests: startup check PASS/WARN/FAIL, no repairs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from federated_db import FederatedDB
from enhancer import HermesEnhancer
import enhancer as _enh_mod


def _make(tmp_path, monkeypatch):
    db = FederatedDB(db_path=str(tmp_path / "st.db"))
    monkeypatch.setattr(_enh_mod, "get_db", lambda: db)
    enh = HermesEnhancer(node_id="st")
    return enh, db


def test_startup_check_pass(tmp_path, monkeypatch):
    enh, db = _make(tmp_path, monkeypatch)
    try:
        rep = enh.startup_check()
        assert rep["status"] == "PASS", rep
        assert set(rep["checks"]) >= {"config", "db_available",
                                      "schema_version", "event_buffer",
                                      "queue", "redaction", "skill_graph",
                                      "learners"}
    finally:
        db.shutdown()


def test_full_diagnostics_shape(tmp_path, monkeypatch):
    enh, db = _make(tmp_path, monkeypatch)
    try:
        rep = enh.run_full_diagnostics()
        assert rep["db_integrity"]["healthy"] is True
        assert rep["orphans"] == []
        assert "learning" in rep
    finally:
        db.shutdown()


def test_startup_check_never_raises(tmp_path, monkeypatch):
    enh, db = _make(tmp_path, monkeypatch)
    db.shutdown()
    rep = enh.startup_check()
    assert rep["status"] in ("PASS", "WARN", "FAIL")

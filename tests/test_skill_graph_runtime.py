"""v0.23 skill-graph runtime tests: registration, ordering, persistence."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skill_graph import SkillGraph


def test_register_and_order():
    g = SkillGraph()
    g.register_skill("deploy", depends_on=["test", "build"])
    g.register_skill("test", depends_on=["build"])
    g.register_skill("build")
    order = g.topological_order()
    assert order.index("build") < order.index("test") < order.index("deploy")
    assert g.dependencies("deploy") == ["test", "build"]
    assert g.dependents("build") == ["deploy", "test"]


def test_invalid_names_rejected():
    g = SkillGraph()
    for bad in ["", "a" * 65, "evil;DROP", "x/y", None, 123]:
        try:
            g.register_skill(bad)
            assert False, bad
        except ValueError:
            pass


def test_validate_report():
    g = SkillGraph()
    g.register_skill("a", depends_on=["ghost"])
    rep = g.validate()
    assert rep["status"] == "PASS"  # dangling is reported, not fatal
    assert rep["dangling"] == ["ghost"]


def test_save_load_roundtrip(tmp_path):
    g = SkillGraph()
    g.register_skill("lint", meta={"kind": "check"})
    g.register_skill("ci", depends_on=["lint"])
    dbp = str(tmp_path / "g.db")
    con = sqlite3.connect(dbp)
    try:
        con.execute("CREATE TABLE skill_graph_nodes (skill TEXT PRIMARY KEY, meta TEXT, updated_at TEXT NOT NULL)")
        con.execute("CREATE TABLE skill_graph_edges (src TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY (src, dst))")
        g.save_to_db(con)
    finally:
        con.close()
    g2 = SkillGraph()
    con = sqlite3.connect(dbp)
    try:
        rep = g2.load_from_db(con)
    finally:
        con.close()
    assert rep == {"loaded_skills": 2, "loaded_edges": 1, "quarantined": 0}
    assert g2.topological_order().index("lint") < g2.topological_order().index("ci")


def test_quarantine_malicious_rows(tmp_path):
    dbp = str(tmp_path / "gm.db")
    con = sqlite3.connect(dbp)
    try:
        con.execute("CREATE TABLE skill_graph_nodes (skill TEXT PRIMARY KEY, meta TEXT, updated_at TEXT NOT NULL)")
        con.execute("CREATE TABLE skill_graph_edges (src TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY (src, dst))")
        con.execute("INSERT INTO skill_graph_nodes VALUES ('ok', '{}', 't')")
        con.execute("INSERT INTO skill_graph_nodes VALUES ('evil;DROP TABLE x', '{}', 't')")
        con.execute("INSERT INTO skill_graph_nodes VALUES ('badmeta', '[1,2]', 't')")
        con.execute("INSERT INTO skill_graph_edges VALUES ('ok', 'ok')")
        con.execute("INSERT INTO skill_graph_edges VALUES ('ok', '__import__(\"os\").system(1)')")
        con.commit()
    finally:
        con.close()
    g = SkillGraph()
    con = sqlite3.connect(dbp)
    try:
        rep = g.load_from_db(con)
    finally:
        con.close()
    assert rep["quarantined"] == 3, rep
    assert '__import__' not in str(g.edges)
    assert set(g.edges) == {"ok", "badmeta"}

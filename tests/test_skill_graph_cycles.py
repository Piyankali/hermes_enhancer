"""v0.23 skill-graph cycle tests: A->B->C->A is controlled, never hangs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skill_graph import SkillGraph, CycleError


def test_cycle_raises_with_path():
    g = SkillGraph()
    g.register_skill("A", depends_on=["B"])
    g.register_skill("B", depends_on=["C"])
    g.register_skill("C", depends_on=["A"])
    try:
        g.topological_order()
        assert False, "expected CycleError"
    except CycleError as exc:
        assert set(exc.cycle) == {"A", "B", "C"}
    cycles = g.detect_cycles()
    assert len(cycles) == 1 and set(cycles[0]) == {"A", "B", "C"}


def test_self_loop_detected():
    g = SkillGraph()
    g.register_skill("solo", depends_on=["solo"])
    assert g.detect_cycles() != []
    assert g.validate()["status"] == "FAIL"


def test_acyclic_graph_clean():
    g = SkillGraph()
    g.register_skill("x")
    g.register_skill("y", depends_on=["x"])
    assert g.detect_cycles() == []
    assert g.validate()["status"] == "PASS"
    assert g.topological_order() == ["x", "y"]


def test_multiple_cycles_all_reported():
    g = SkillGraph()
    g.register_skill("a", depends_on=["b"])
    g.register_skill("b", depends_on=["a"])
    g.register_skill("c", depends_on=["d"])
    g.register_skill("d", depends_on=["c"])
    assert len(g.detect_cycles()) == 2

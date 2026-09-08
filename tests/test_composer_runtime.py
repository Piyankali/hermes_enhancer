"""v0.23 composer-runtime tests: safe plans, no code-from-data."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composer import SkillComposer, WorkflowValidationError, MAX_STEPS


def _composer():
    c = SkillComposer()
    c.register_handler("fetch", lambda req: {"status": "ok", "data": [1, 2]})
    c.register_handler("build", lambda req: {"status": "ok"})
    return c


def test_ordered_execution_with_audit():
    c = _composer()
    c.add_step("fetch")
    c.add_step("build")
    results = c.run()
    assert [r["step"] for r in results] == ["fetch", "build"]
    assert all(r["status"] == "ok" for r in results)
    assert len(c.audit) == 2


def test_unknown_skill_rejected_at_validate():
    c = SkillComposer()
    c.add_step("ghost")
    try:
        c.run()
        assert False, "expected WorkflowValidationError"
    except WorkflowValidationError:
        pass


def test_no_callable_smuggling_from_payload():
    c = _composer()
    c.add_step("fetch", payload={"cb": "not-a-callable"})
    results = c.run()
    assert results[0]["status"] == "ok"


def test_retry_eventual_success():
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"status": "error"}
        return {"status": "ok"}

    c = SkillComposer()
    c.register_handler("flaky", flaky)
    c.add_step("flaky")
    c.add_retry_step("flaky", retries=3, backoff_base=0.0)
    results = c.run()
    assert results[0]["status"] == "ok"
    assert results[0]["attempt"] == 3


def test_fail_fast_and_timeout():
    c = SkillComposer()
    c.register_handler("bad", lambda req: {"status": "error"})
    c.register_handler("never", lambda req: {"status": "ok"})
    c.add_step("bad")
    c.add_step("never")
    results = c.run()
    assert [r["step"] for r in results] == ["bad"]

    import time as _t
    c2 = SkillComposer()

    def slow(req):
        _t.sleep(5)
        return {"status": "ok"}

    c2.register_handler("slow", slow)
    c2.add_step("slow")
    c2.steps[-1]["timeout_s"] = 0.2
    results = c2.run()
    assert results[0]["status"] == "timeout"


def test_step_limit_and_branching():
    c = SkillComposer()
    c.register_handler("s", lambda req: {"status": "ok"})
    for _ in range(MAX_STEPS):
        c.add_step("s")
    try:
        c.add_step("s")
        assert False
    except WorkflowValidationError:
        pass
    c2 = _composer()
    c2.register_handler("route", lambda req: {"status": "ok"})
    c2.add_step("fetch")
    c2.add_conditional_branch("route", lambda ctx: True, "build", "fetch")
    c2.add_step("build")
    results = c2.run()
    assert results[1]["next"] == "build"


def test_cancellation():
    c = _composer()
    c.add_step("fetch")
    c.add_step("build")
    c.cancel()
    results = c.run()
    assert results[0]["status"] == "cancelled"

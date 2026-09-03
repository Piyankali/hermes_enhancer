"""SelfTestEngine - Parameter validation and path expansion for Hermes runtime."""

import os
import json
import time
import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple


class SelfTestEngine:
    """Validates runtime parameters, expands paths, and checks component health."""

    def __init__(self) -> None:
        self.test_results: List[Dict[str, Any]] = []

    def expand_path(self, path: str) -> str:
        """Expand user home directory and environment variables in a path."""
        return os.path.expandvars(os.path.expanduser(path))

    def validate_config(self, config: Any, required_keys: List[str]) -> Tuple[bool, List[str]]:
        """Validate that a configuration dictionary contains all required keys."""
        if not isinstance(config, dict):
            return False, ["config is not a dict"]
        missing = [key for key in required_keys if key not in config]
        return len(missing) == 0, missing

    def check_file_exists(self, path: str) -> bool:
        """Check if a file exists at the given path (supports ~ expansion)."""
        expanded = self.expand_path(path)
        return os.path.isfile(expanded)

    def check_directory_writable(self, path: str) -> bool:
        """Check if a directory exists and is writable."""
        expanded = self.expand_path(path)
        if not os.path.isdir(expanded):
            return False
        return os.access(expanded, os.W_OK)

    def run_function_signature_test(self, func: Callable, expected_args: List[str]) -> Tuple[bool, str]:
        """Verify that a callable accepts the expected argument names."""
        try:
            sig = inspect.signature(func)
            param_names = [p.name for p in sig.parameters.values() if p.default == inspect.Parameter.empty]
            for arg in expected_args:
                if arg not in param_names:
                    return False, f"Missing required parameter: {arg}"
            return True, "Signature valid"
        except Exception as e:
            return False, f"Signature inspection failed: {e}"

    def record_result(self, test_name: str, passed: bool, duration_ms: float, details: str = "") -> None:
        """Record a test result for later reporting."""
        self.test_results.append({
            "test": test_name,
            "passed": passed,
            "duration_ms": duration_ms,
            "details": details,
            "timestamp": time.time(),
        })

    def run_battery(self) -> Dict[str, Any]:
        """Run a standard self-test battery and return summary."""
        tests = []
        # Test path expansion
        start = time.perf_counter()
        expanded = self.expand_path("~/.hermes")
        ok = expanded.startswith(os.path.expanduser("~"))
        duration = (time.perf_counter() - start) * 1000
        self.record_result("path_expansion", ok, duration)
        tests.append(ok)

        # Test config validation
        start = time.perf_counter()
        valid, missing = self.validate_config({"db_path": "x", "wal": True}, ["db_path", "wal"])
        ok = valid and len(missing) == 0
        duration = (time.perf_counter() - start) * 1000
        self.record_result("config_validation", ok, duration, f"missing={missing}")
        tests.append(ok)

        # Test directory writability
        start = time.perf_counter()
        ok = self.check_directory_writable("~/.hermes/plugins")
        duration = (time.perf_counter() - start) * 1000
        self.record_result("directory_writable", ok, duration)
        tests.append(ok)

        passed = sum(tests)
        total = len(tests)
        return {
            "passed": passed,
            "total": total,
            "results": self.test_results,
            "all_passed": passed == total,
        }

    def summary(self) -> str:
        """Return a human-readable test summary."""
        battery = self.run_battery()
        status = "PASS" if battery["all_passed"] else "FAIL"
        lines = [f"SelfTestEngine: {battery['passed']}/{battery['total']} tests passed ({status})"]
        for r in battery["results"]:
            mark = "✓" if r["passed"] else "✗"
            lines.append(f"  {mark} {r['test']} ({r['duration_ms']:.3f}ms) {r['details']}")
        return "\n".join(lines)

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_benchmark_smoke(tmp_path):
    out = tmp_path / "benchmark.json"
    p = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmarks" / "benchmark.py"),
            "--iterations", "20",
            "--warmup", "5",
            "--memory-calls", "20",
            "--output", str(out),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert p.returncode == 0, p.stderr
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["target_version"] == "0.20.7"
    assert "enhancer_hooks" in data["results"]
    assert "preload" in data["results"]
    assert "db" in data["results"]
    assert data["results"]["preload"]["top1_correct"] is True
    assert data["results"]["feedback"]["anomalous_score_unchanged"] is True


def test_no_network_dependency():
    text = (ROOT / "benchmarks" / "benchmark.py").read_text()
    assert "requests" not in text
    assert "httpx" not in text

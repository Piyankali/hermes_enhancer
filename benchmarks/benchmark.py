#!/usr/bin/env python3
"""
Hermes Enhancer v0.20.7 benchmark suite.

Measures the actual v0.20.7 components from src/ without network/API calls.
Results are written as JSON, CSV and Markdown.

Usage:
    python3 benchmarks/benchmark.py
    python3 benchmarks/benchmark.py --iterations 1000 --warmup 100
    python3 benchmarks/benchmark.py --output results/run.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gc
import importlib
import inspect
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def now_ns() -> int:
    return time.perf_counter_ns()


def stats(samples_ns: list[int]) -> dict[str, float]:
    s = sorted(samples_ns)
    return {
        "n": len(s),
        "min_us": s[0] / 1_000,
        "mean_us": statistics.mean(s) / 1_000,
        "median_us": statistics.median(s) / 1_000,
        "p95_us": s[max(0, int(len(s) * 0.95) - 1)] / 1_000,
        "p99_us": s[max(0, int(len(s) * 0.99) - 1)] / 1_000,
        "max_us": s[-1] / 1_000,
    }


def bench(fn: Callable[[], Any], iterations: int, warmup: int) -> dict[str, float]:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(iterations):
        t0 = now_ns()
        fn()
        samples.append(now_ns() - t0)
    return stats(samples)


def percent_change(baseline: float, enhanced: float) -> float | None:
    if baseline == 0:
        return None
    return ((enhanced - baseline) / baseline) * 100.0


def load_modules():
    return {
        "db": importlib.import_module("src.federated_db"),
        "feedback": importlib.import_module("src.feedback_optimizer"),
        "preload": importlib.import_module("src.predictive_preload"),
        "meta": importlib.import_module("src.meta_learner"),
        "graph": importlib.import_module("src.skill_graph"),
        "composer": importlib.import_module("src.composer"),
        "enhancer": importlib.import_module("src.enhancer"),
        "self_test": importlib.import_module("src.self_test"),
    }


def db_bench(mod, iterations: int, warmup: int, tmp: Path) -> dict[str, Any]:
    db_path = tmp / "benchmark.db"
    db = mod.FederatedDB(str(db_path), prune_threshold=1000000, retention_days=14)
    payload = {
        "hook": "post_tool_call",
        "tool": "benchmark_tool",
        "status": "success",
        "success": True,
        "duration": 0.001,
        "delta_us": 1000,
        "anomalous": 0,
    }

    sync = bench(lambda: db.push(payload), iterations, warmup)

    async def async_one():
        await db.async_push(payload)

    async def async_batch():
        await asyncio.gather(*(db.async_push(payload) for _ in range(min(iterations, 100))))

    async_t0 = now_ns()
    asyncio.run(async_one())
    async_one_us = (now_ns() - async_t0) / 1_000

    batch_n = min(iterations, 100)
    async_t0 = now_ns()
    asyncio.run(async_batch())
    async_batch_us = (now_ns() - async_t0) / 1_000

    return {
        "sync_push": sync,
        "async_single_us": async_one_us,
        "async_batch_n": batch_n,
        "async_batch_total_us": async_batch_us,
        "async_batch_mean_us": async_batch_us / batch_n,
        "rows": db.count(),
        "db_bytes": db_path.stat().st_size if db_path.exists() else 0,
    }


def feedback_bench(mod, iterations: int, warmup: int) -> dict[str, Any]:
    opt = mod.FeedbackOptimizer(alpha=0.2)
    fn = lambda: opt.record_outcome("benchmark_tool", True, duration_s=0.010)
    timing = bench(fn, iterations, warmup)

    # Verify anomaly exclusion: score must not change on anomalous input.
    before = opt.get_score("benchmark_tool")
    opt.record_outcome("benchmark_tool", False, duration_s=0.0)
    after = opt.get_score("benchmark_tool")

    return {
        "record_outcome": timing,
        "score_after_training": before,
        "anomalous_score_unchanged": before == after,
    }


def preload_bench(mod, iterations: int, warmup: int) -> dict[str, Any]:
    p = mod.PredictivePreload(order=1)
    sequences = [
        ("search", "read", "analyze", "report"),
        ("search", "read", "analyze", "report"),
        ("search", "read", "analyze", "report"),
        ("browser", "read", "analyze", "report"),
    ]
    for seq in sequences:
        p.clear()
        for tool in seq:
            p.record(tool)

    # Re-train without clearing between sequences so transition counts accumulate.
    p.clear()
    for seq in sequences:
        for tool in seq:
            p.record(tool)

    predict_timing = bench(lambda: p.predict("search", top_k=5), iterations, warmup)
    predictions = p.predict("search", top_k=5)

    expected = "read"
    top1 = predictions[0][0] if predictions else None
    return {
        "predict": predict_timing,
        "predictions": predictions,
        "top1": top1,
        "top1_correct": top1 == expected,
        "top1_probability": predictions[0][1] if predictions else 0.0,
        "transition_count": sum(sum(v.values()) for v in p.transitions.values()),
    }


def meta_bench(mod, iterations: int, warmup: int) -> dict[str, Any]:
    m = mod.MetaLearner()
    event = {"tool": "benchmark_tool", "success": True, "delta_us": 1000, "anomalous": 0}
    ingest = bench(lambda: m.ingest(event), iterations, warmup)
    before = len(m.history)
    removed = m.prune(max(1, before // 2))
    return {
        "ingest": ingest,
        "history_before_prune": before,
        "pruned": removed,
        "history_after_prune": len(m.history),
    }


def graph_bench(mod, iterations: int, warmup: int) -> dict[str, Any]:
    g = mod.SkillGraph()
    g.register_skill("report")
    g.register_skill("analyze", ["report"])
    g.register_skill("read", ["analyze"])
    g.register_skill("search", ["read"])

    dep = bench(lambda: g.dependencies("search"), iterations, warmup)
    topo = bench(lambda: g.topological_order(), iterations, warmup)
    return {
        "dependencies": dep,
        "topological_order": topo,
        "summary": g.summary(),
        "topological_result": g.topological_order(),
    }


def enhancer_hook_bench(mod, iterations: int, warmup: int, tmp: Path) -> dict[str, Any]:
    # The real HermesEnhancer constructor uses the singleton DB. Replace the
    # instance DB with an isolated temporary DB so this benchmark cannot touch
    # the user's ~/.hermes/federated.db.
    e = mod.HermesEnhancer(node_id="benchmark")
    e.db = mod.FederatedDB(str(tmp / "hook.db"), prune_threshold=1000000)
    e._enabled = True

    def baseline():
        # Equivalent minimal tool-call bookkeeping without the enhancer.
        t0 = time.perf_counter()
        _ = t0

    def enhanced():
        metadata = e.pre_tool_call(("benchmark_tool",), {})
        e.on_post_tool_call(
            tool_name="benchmark_tool",
            result={"ok": True},
            status="success",
            duration_ms=1,
        )
        return metadata

    base = bench(baseline, iterations, warmup)
    enh = bench(enhanced, iterations, warmup)

    # Give background executor work a chance to finish.
    time.sleep(0.05)
    summary = e.db.summary_counts()

    return {
        "baseline_noop_hook": base,
        "enhanced_pre_post_hooks": enh,
        "mean_overhead_us": enh["mean_us"] - base["mean_us"],
        "p95_overhead_us": enh["p95_us"] - base["p95_us"],
        "mean_change_percent": percent_change(base["mean_us"], enh["mean_us"]),
        "telemetry_summary": summary,
        "feedback_score": e.feedback.get_score("benchmark_tool"),
        "preload_history_len": len(e.preload.last_sequence),
        "meta_history_len": len(e.meta_learner.history),
    }


def self_test_bench(mod, iterations: int, warmup: int) -> dict[str, Any]:
    engine = mod.SelfTestEngine()
    timing = bench(lambda: engine.run_battery(), iterations=max(1, min(iterations, 100)), warmup=min(warmup, 10))
    return {"run_battery": timing, "sample": engine.run_battery()}


def memory_bench(mod, tmp: Path, calls: int) -> dict[str, Any]:
    e = mod.HermesEnhancer(node_id="memory-benchmark")
    e.db = mod.FederatedDB(str(tmp / "memory.db"), prune_threshold=1000000)

    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()

    for i in range(calls):
        e.pre_tool_call((f"tool_{i % 8}",), {})
        e.on_post_tool_call(
            tool_name=f"tool_{i % 8}",
            result={"ok": True},
            status="success",
            duration_ms=1,
        )

    time.sleep(0.05)
    gc.collect()
    after = tracemalloc.take_snapshot()
    diff = after.compare_to(before, "lineno")
    positive = [x for x in diff if x.size_diff > 0][:10]
    total = sum(max(0, x.size_diff) for x in diff)

    return {
        "calls": calls,
        "positive_allocated_bytes": total,
        "top_deltas": [
            {
                "file": str(x.traceback[0].filename),
                "line": x.traceback[0].lineno,
                "delta_bytes": x.size_diff,
            }
            for x in positive
        ],
    }


def flatten_results(results: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    def walk(prefix: str, value: Any):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}.{k}" if prefix else k, v)
        elif isinstance(value, (int, float, str, bool)) or value is None:
            rows.append({"metric": prefix, "value": value})
    walk("", results)
    return rows


def markdown_report(doc: dict[str, Any]) -> str:
    r = doc["results"]
    lines = [
        "# Hermes Enhancer v0.20.7 Benchmark Report",
        "",
        f"- Timestamp (UTC): `{doc['environment']['timestamp_utc']}`",
        f"- Python: `{doc['environment']['python']}`",
        f"- Platform: `{doc['environment']['platform']}`",
        f"- Iterations: `{doc['config']['iterations']}`",
        f"- Warmup: `{doc['config']['warmup']}`",
        "",
        "## Headline measurements",
        "",
        "| Area | Metric | Result |",
        "|---|---|---:|",
        f"| Hook path | Baseline mean | {r['enhancer_hooks']['baseline_noop_hook']['mean_us']:.3f} µs |",
        f"| Hook path | Enhanced mean | {r['enhancer_hooks']['enhanced_pre_post_hooks']['mean_us']:.3f} µs |",
        f"| Hook path | Mean overhead | {r['enhancer_hooks']['mean_overhead_us']:.3f} µs |",
        f"| Hook path | P95 overhead | {r['enhancer_hooks']['p95_overhead_us']:.3f} µs |",
        f"| DB | Sync push mean | {r['db']['sync_push']['mean_us']:.3f} µs |",
        f"| Preload | Top-1 correct | {r['preload']['top1_correct']} |",
        f"| Preload | Top-1 probability | {r['preload']['top1_probability']:.3f} |",
        f"| Feedback | Anomaly excluded | {r['feedback']['anomalous_score_unchanged']} |",
        f"| Memory | Positive allocated bytes | {r['memory']['positive_allocated_bytes']} |",
        "",
        "## Important interpretation",
        "",
        "These are measurements of the implementation in the checked-out repository, not claims about every Hermes workload. "
        "The hook benchmark compares a minimal no-op baseline with the actual v0.20.7 pre/post hook path; it does not measure "
        "LLM inference time or the complete Hermes runtime.",
        "",
        "## Detailed results",
        "",
        "See the JSON file for the complete machine-readable result set.",
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=1000)
    ap.add_argument("--warmup", type=int, default=100)
    ap.add_argument("--memory-calls", type=int, default=500)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    if args.iterations < 10:
        raise SystemExit("--iterations must be >= 10")

    mods = load_modules()

    with tempfile.TemporaryDirectory(prefix="hermes-enhancer-bench-", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        results = {
            "db": db_bench(mods["db"], args.iterations, args.warmup, tmp),
            "feedback": feedback_bench(mods["feedback"], args.iterations, args.warmup),
            "preload": preload_bench(mods["preload"], args.iterations, args.warmup),
            "meta": meta_bench(mods["meta"], args.iterations, args.warmup),
            "graph": graph_bench(mods["graph"], args.iterations, args.warmup),
            "enhancer_hooks": enhancer_hook_bench(mods["enhancer"], args.iterations, args.warmup, tmp),
            "self_test": self_test_bench(mods["self_test"], args.iterations, args.warmup),
            "memory": memory_bench(mods["enhancer"], tmp, args.memory_calls),
        }

        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        doc = {
            "suite": "hermes-enhancer-benchmark",
            "suite_version": "1.0.0",
            "target_version": "0.20.7",
            "environment": {
                "timestamp_utc": stamp,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "cpu_count": os.cpu_count(),
            },
            "config": vars(args),
            "results": results,
        }

        out = Path(args.output) if args.output else ROOT / "results" / "benchmark.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")

        csv_path = out.with_suffix(".csv")
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["metric", "value"])
            writer.writeheader()
            writer.writerows(flatten_results(results))

        md_path = out.with_suffix(".md")
        md_path.write_text(markdown_report(doc), encoding="utf-8")

        print(f"JSON: {out}")
        print(f"CSV:  {csv_path}")
        print(f"MD:   {md_path}")
        print()
        print("Hook mean overhead: "
              f"{results['enhancer_hooks']['mean_overhead_us']:.3f} us")
        print("Preload top-1: "
              f"{results['preload']['top1']} "
              f"(correct={results['preload']['top1_correct']})")


if __name__ == "__main__":
    main()

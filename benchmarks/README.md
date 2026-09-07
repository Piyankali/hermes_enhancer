# Hermes Enhancer v0.20.7 — Automated Benchmark Suite

This directory provides a reproducible, local benchmark suite for the actual
Hermes Enhancer v0.20.7 implementation.

## What it measures

- **Hook overhead:** minimal no-op baseline vs real `HermesEnhancer.pre_tool_call()` + `on_post_tool_call()`
- **SQLite persistence:** synchronous `push()` latency and async push throughput
- **FeedbackOptimizer:** EMA update throughput and anomaly exclusion
- **PredictivePreload:** prediction latency and top-1 prediction correctness
- **MetaLearner:** ingest throughput and pruning behavior
- **SkillGraph:** dependency lookup and topological ordering
- **SelfTestEngine:** diagnostic battery latency
- **Memory:** positive allocations observed while exercising the enhancer hooks
- **Telemetry:** records produced by the real v0.20.7 hook path

The suite does **not** call an LLM or make network requests. This keeps results
repeatable and isolates the enhancer implementation.

## Run

From the repository root:

```bash
python3 benchmarks/benchmark.py
```

Custom run:

```bash
python3 benchmarks/benchmark.py \
  --iterations 5000 \
  --warmup 500 \
  --memory-calls 2000
```

Output:

```text
results/benchmark.json
results/benchmark.csv
results/benchmark.md
```

Or choose an explicit JSON path:

```bash
python3 benchmarks/benchmark.py \
  --output results/v0.20.7-my-device.json
```

## Test the suite

The legacy `tests/test_benchmark_suite.py` wrapper was retired; run the
benchmark directly (see above). This suite is historical v0.20.7-era
material and is not part of v0.22 validation.

## Benchmark protocol

For credible README claims:

1. Run baseline and enhanced measurements on the same machine.
2. Keep Python and Hermes versions fixed.
3. Use the same workload and iteration count.
4. Use warmup iterations before recording measurements.
5. Prefer median/P95/P99 over a single timing.
6. Repeat the full benchmark several times.
7. Record device/OS/Python information with the result.
8. Do not report a percentage improvement unless it comes from measured output.

## Interpreting hook overhead

The hook benchmark intentionally compares:

```text
Baseline
  minimal bookkeeping
       │
       ▼
  tool invocation boundary

Enhanced
  pre_tool_call
       ↓
  telemetry scheduling
       ↓
  post_tool_call
       ↓
  feedback + preload + meta-learning
       ↓
  telemetry scheduling
```

The difference is **enhancer instrumentation overhead**, not total Hermes
agent latency.

LLM inference, network calls, tool process startup, and external APIs are
therefore not included.

## Isolated database

The benchmark replaces the `HermesEnhancer` instance database with a temporary
SQLite database. It does not intentionally benchmark against or modify:

```text
~/.hermes/federated.db
```

This is important when running the benchmark on a real Hermes installation.

## Metrics to publish later

Once you have repeated measurements, the repository README can publish:

- median hook overhead
- P95/P99 hook overhead
- synchronous DB push latency
- async batch throughput
- preload top-1 accuracy
- preload hit rate, once hit/miss telemetry exists
- prediction confidence
- self-test latency
- memory allocation delta
- CPU/RSS measurements from a controlled environment
- repeated-workflow warm-vs-cold results

Do not publish placeholder numbers as production benchmark results.

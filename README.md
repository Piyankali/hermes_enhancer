# Hermes Enhancer v0.20.7 - Enterprise-Grade Production Microservice

[![Enterprise](https://img.shields.io/badge/status-Enterprise-green)]()
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform Linux | macOS | Termux | Windows](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Termux%20%7C%20Windows-orange)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Autonomous self-healing middleware and telemetry microservice for Hermes Agent.** Designed for 24/7 production operation with zero cold-start latency, dynamic brain persistence, bulletproof resilience, and active memory guards.

---

## Core Architecture Highlights

### Zero Cold-Start Latency
- SQLite WAL-mode `SkillGraph` persistence with `save_to_db()` / `load_from_db()` methods.
- `winning_workflows` table stores optimized execution paths for instant session hydration.
- Sub-1s boot time across Linux, macOS, Windows, and Android/Termux.

### Dynamic Brain
- `MetaLearner` persists successful tool workflows into SQLite for cross-session learning.
- `SkillComposer` supports conditional branching, fallback routing, and retry/backoff execution pipelines.

### Bulletproof Resilience
- `FederatedDB` enforces `busy_timeout=5000ms`, `PRAGMA journal_mode=WAL`, and startup `PRAGMA integrity_check`.
- Exponential backoff retries on `sqlite3.OperationalError` (`SQLITE_BUSY` / `database is locked`) with delays `1s → 2s → 4s`.

### Active Memory Guard
- `PredictivePreload` monitors RSS via `resource.getrusage()`.
- Auto-triggers `preload_cache.clear()` and `gc.collect()` when memory threshold is exceeded.
- `HermesEnhancer` invokes cleanup after every post-tool hook to prevent long-running leaks.

---

## 1-Line Quick Setup

```bash
mkdir -p ~/.hermes/plugins/hermes_enhancer && rsync -av --delete ~/storage/shared/Hermes_Enhancer_Project/ ~/.hermes/plugins/hermes_enhancer/
```

### Enable Plugin

Edit `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes_enhancer
  disabled: []
```

---

## Verification & Benchmarks

```bash
# Run tests
python3 -m pytest -v

# Run benchmarks
python3 benchmarks/benchmark.py
```

---

## Project Structure

```
hermes_enhancer/
├── src/
│   ├── __init__.py              # Plugin entry point & hook registration
│   ├── enhancer.py              # Core orchestration & lifecycle coordinator
│   ├── federated_db.py          # WAL-mode SQLite telemetry + retry resilience
│   ├── feedback_optimizer.py    # EMA success scoring with anomaly exclusion
│   ├── predictive_preload.py    # Markov-chain preloader with RSS auto-GC
│   ├── meta_learner.py          # Cross-session learning + workflow persistence
│   ├── skill_graph.py           # Dependency graph with SQLite persistence
│   ├── composer.py              # Workflow engine with conditional fallback/retry
│   └── self_test.py             # Runtime diagnostic battery
├── tests/
│   ├── test_pipeline.py         # 9 core diagnostic tests
│   └── test_enterprise_upgrade.py  # Enterprise verification suite
├── benchmarks/
│   ├── benchmark.py             # Performance benchmark harness
│   └── requirements.txt
├── scripts/
│   └── stats.py                 # CLI analytics for telemetry
├── plugin.yaml                  # Hermes plugin manifest
└── README.md
```

---

## Production Features

### Async & Non-Blocking Telemetry
- `FederatedDB` exposes `async_push`, `async_get_recent`, `async_count`, `async_summary_counts`, and `async_prune`.
- Writes scheduled on `ThreadPoolExecutor` via `_maybe_schedule_io`; main Hermes runtime thread remains free.

### Anomalous Latency & Clock-Skew Filtering
- Zero, negative, or extreme durations (>300s) flagged `anomalous=1` in `federated.db`.
- Anomalous records excluded from EMA scoring to preserve timing model integrity.

### Automated Database Pruning
- Auto-prune when `sync_queue` exceeds threshold (default 5000).
- Old records aggregated into `summary_analytics` before deletion.

### Active Health & Diagnostics
- `SelfTestEngine` validates hook bindings, DB schema, and inter-module wiring on every session start.
- `health_report()` exposes component-level status for monitoring.

---

## Hooks & Lifecycles

### `pre_tool_call`
- Timing: Before every tool invocation
- Actions: Logs telemetry, checks `PredictivePreload` cache, applies `MetaLearner` thresholds

### `post_tool_call`
- Timing: After every tool invocation returns
- Actions: Records wall-clock duration, captures success state, updates `FederatedDB`, triggers `FeedbackOptimizer`, notifies `SkillGraph`

**Success Definition:** `error_message` absent AND `status` not `"error"`/`"failed"`, even on empty results.

---

## Troubleshooting

### SQLite Busy / Locked
- `FederatedDB._with_retry()` handles transient locks automatically with exponential backoff.

### Schema Migration
- Legacy databases missing `tool` or `anomalous` columns are backfilled automatically via `_migrate()`.

### Tool Name Extraction
- Hook callbacks inspect `args[0]`, `kwargs['tool']`, `kwargs['name']`, and nested dicts with lowercase normalization.

---

## Contributing

1. Fork the repository
2. `git checkout -b feature/my-enhancement`
3. `git commit -m 'feat: add X optimization'`
4. `git push origin feature/my-enhancement`
5. Open a Pull Request

# 🚀 Hermes Enhancer – Enterprise Microservice

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-v0.20.6%2B-green)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20Termux-orange)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Tests](https://img.shields.io/badge/tests-49%20passed-brightgreen)

> **Autonomous self-healing middleware for Hermes Agent** with microsecond-accurate telemetry, predictive preloading, meta-learning optimization, and federated analytics. Designed for cross-platform execution (Linux, macOS, Windows, Android/Termux).

---

## Table of Contents

- [Introduction](#introduction)
- [Why Hermes Enhancer?](#why-hermes-enhancer)
- [11 Core Systems Architecture Matrix](#11-core-systems-architecture-matrix)
- [Project Tree Structure](#project-tree-structure)
- [v0.20.7 Production Features](#v0207-production-features)
- [v0.22 Durability Notes](#v022-durability-notes)
- [Real-World Realized Benefits](#real-world-realized-benefits)
- [Analytics & Observability](#analytics--observability)
- [Design Philosophy](#design-philosophy)
- [Continuous Improvement Loop](#continuous-improvement-loop)
- [Quick Setup](#quick-setup)
- [Verification](#verification)
- [Live Plugin Activation](#live-plugin-activation)
- [Monitoring & Maintenance](#monitoring--maintenance)
- [Development Notes](#development-notes)
- [Troubleshooting & Edge Cases](#troubleshooting--edge-cases)
- [Contributing](#contributing)
- [Repository & License](#repository--license)

---

## Introduction

Hermes Enhancer is an enterprise-grade plugin for the Hermes Agent that transforms it from a simple assistant into a **self-improving, resilient, and fully observable** microservice. It captures execution history, learns patterns from past successes and failures, and makes future workflows faster, safer, and more reliable.

Built for production environments, it combines telemetry-driven learning, predictive preloading, memory guards, and anomaly detection into a single, cohesive plugin that requires zero manual tuning after installation.

**Current Version:** `0.22.0`
**Status:** Beta (process-crash/restart durability validated; see v0.22 notes below)
**Python:** 3.10+  
**License:** MIT

---

## v0.22 Durability Notes

v0.22 adds a persistent crash-safe event buffer to the federated telemetry store (`src/federated_db.py`):

- Buffer-before-persistence ordering: `enqueue_to_buffer()` commits first, final `sync_queue` insert second, `mark_buffer_persisted()` finalizes last. Finalization never precedes the final commit.
- Idempotent `event_id` persistence: UNIQUE `event_id` in `event_buffer`, idempotent final inserts plus a partial unique index on `sync_queue(event_id)`. Retried pushes return the existing row id instead of duplicating.
- Automatic startup recovery: every `FederatedDB` initialization runs bounded recovery (at most 100 rows per invocation); leftovers drain on later ticks/restarts. Observable via `get_startup_recovery_info()`.
- Retry behavior: transient SQLite busy/locked errors are retried with backoff; permanent/unknown errors are counted (`permanent_errors`/`unknown_errors`) and surfaced, never retried forever. Buffer-write failures are fail-loud — never silently bypassed.
- SQLite health validation: `verify_database()` reports `PRAGMA quick_check` / `integrity_check` without ever deleting or recreating data.
- Malformed-buffer handling: corrupt payloads are marked FAILED with a diagnosis, preserved (never silently deleted); unknown-state rows are left in place; healthy rows keep flowing.
- Graceful shutdown drains the worker queue; bounded in-memory queue (refusals return None and count as `events_dropped`).
- Telemetry semantics: `events_enqueued` counts queue admissions (async path only); `events_persisted` counts final commits (sync + worker); all counters are process-local and reset on restart — SQLite state is the durable source of truth.
- v0.21 APIs preserved unchanged: `push`, `get_recent`, `count`, `async_push`, `flush`.
- Explicit non-guarantees: power-loss/OS-crash durability NOT validated (`synchronous=NORMAL`, no fsync); physical disk-full NOT directly tested (deterministic simulation only); no universal exactly-once claim — the tested guarantee is no duplicate persisted `event_id` and no loss of confirmed durable buffered events across tested process crash/restart scenarios.

---

## Why Hermes Enhancer?

Modern AI agents are powerful, but they lack **institutional memory**. They forget what worked, repeat failed strategies, and don't protect themselves against resource exhaustion. Hermes Enhancer solves this by embedding a continuous improvement loop directly into the agent runtime.

The core idea is simple but transformative:

1. **Execution** – the agent runs tools and tasks.
2. **Telemetry** – every call is recorded with timestamps, success/failure, duration, and metadata.
3. **Learning** – patterns and winning workflows are extracted, scored, and stored.
4. **Prediction** – future calls are predicted and preloaded using Markov chain models.
5. **Optimization** – memory usage, retry behavior, and tool selection adapt automatically.

This means the agent gets **measurably better the more it runs**—without requiring manual tuning. It learns from your actual workflows, not generic benchmarks.

---

## 11 Core Systems Architecture Matrix

`hermes_enhancer` is an 11-module plugin architecture that intercepts Hermes Agent tool lifecycles, optimizes execution paths, and builds a federated knowledge graph across sessions.

### Agent Intelligence

| # | Module | Responsibility |
|---|---|---|
| 1 | **SkillGraph** | Directed graph of skill dependencies, co-invocation patterns, and fallback chains |
| 2 | **SkillComposer** | Runtime composer that assembles multi-step workflows from SkillGraph, validates parameter compatibility |
| 3 | **MetaLearner** | Cross-session learning layer storing optimized parameters, timing thresholds, and tool preference weights |
| 4 | **FeedbackOptimizer** | Post-tool analysis engine recording duration, success/failure states, and parameter patterns |

### Execution Optimization

| # | Module | Responsibility |
|---|---|---|
| 5 | **SelfTestEngine** | Runtime diagnostic runner validating hook bindings, database integrity, and inter-module wiring on every session start |
| 6 | **PredictivePreload** | Pattern-based anticipatory loading pre-warming skills, tools, and context data before requests |
| 7 | **Async Execution** | Non-blocking I/O via ThreadPoolExecutor; DB writes scheduled off the main Hermes runtime thread |

### Telemetry Integrity

| # | Module | Responsibility |
|---|---|---|
| 8 | **FederatedDB** | SQLite-backed telemetry store with WAL-mode concurrency, persisting tool metrics and learned parameters |
| 9 | **Clock-Skew & Anomaly Shield** | Filters zero/negative/extreme durations (>300s), flags anomalous=1, excludes from EMA scoring |

### Long-Running Stability

| # | Module | Responsibility |
|---|---|---|
| 10 | **Auto-Pruning Retention** | Aggregates old records into summary_analytics when sync_queue exceeds threshold, preventing unbounded growth |
| 11 | **Mock Test Framework** | Simulated mock tool calls with random outcomes, fast/slow durations, and anomalous clock drops for safe edge-case validation |

---

## Project Tree Structure

```
Hermes_Enhancer_Project/
├── .gitignore
├── LICENSE
├── README.md
├── plugin.yaml
├── src/
│   ├── __init__.py              # Plugin entry point & hook registration
│   ├── enhancer.py              # Core orchestration & lifecycle coordinator
│   ├── federated_db.py          # FederatedDB - telemetry persistence layer
│   ├── feedback_optimizer.py    # FeedbackOptimizer - post-tool analysis
│   ├── predictive_preload.py    # PredictivePreload - anticipatory loading
│   ├── meta_learner.py          # MetaLearner - cross-session optimization
│   ├── skill_graph.py           # SkillGraph - dependency/composition graph
│   ├── composer.py              # SkillComposer - workflow orchestration
│   └── self_test.py             # SelfTestEngine - runtime diagnostics
├── scripts/
│   └── stats.py                 # Standalone analytics CLI for tool metrics
├── tests/
│   ├── _test_worker.py          # Isolated test runner worker
│   ├── test_pipeline.py         # Automated edge-case diagnostic suite
│   └── test_enterprise_upgrade.py # Enterprise upgrade test suite
├── benchmarks/
│   ├── benchmark.py             # Performance benchmark suite
│   ├── requirements.txt
│   └── README.md                # Benchmark protocol documentation
├── results/
│   ├── benchmark.csv
│   ├── benchmark.json
│   └── benchmark.md
└── database/
    └── (legacy migration assets)
```

---

## v0.20.7 Production Features

### Async & Non-Blocking Telemetry

- `FederatedDB` exposes `async_push`, `async_get_recent`, `async_count`, `async_summary_counts`, and `async_prune`.
- `HermesEnhancer` schedules DB writes on a `ThreadPoolExecutor` via `_maybe_schedule_io`, keeping the main Hermes runtime thread free.
- Backwards compatible: synchronous `push` / `get_recent` / `count` remain available.

### Anomalous Latency & Clock-Skew Filtering

- Zero, negative, or extreme durations (>300s) are flagged `anomalous=1` in `federated.db`.
- `FeedbackOptimizer.record_outcome` accepts `duration_s` and `anomalous`; anomalous records are excluded from EMA scoring.
- `HermesEnhancer.on_post_tool_call` sets `success=False` for anomalous tool calls, protecting downstream learning from corrupt data.

### Automated Database Pruning & Data Retention

- When `sync_queue` exceeds `prune_threshold` (default 5000), old records are aggregated into `summary_analytics` and deleted.
- `stats.py` supports `--prune` and `--anomalous` flags for manual maintenance.

### Standalone Mock Diagnostic Test Framework

- `tests/test_pipeline.py` simulates mock tool calls (`terminal`, `write_file`, `read_file`, `patch`, `browser_navigate`) with random successes, failures, fast/slow durations, and anomalous clock drops.
- Tests run in isolated processes using absolute-path module loading via importlib.
- Run with: `python3 tests/test_pipeline.py`

---

## Real-World Realized Benefits

### Autonomous Self-Healing

`SelfTestEngine` runs on every session start and validates hook registrations, database schema, and inter-module wiring. When misconfigurations are detected, it auto-recovers without user intervention. In practice, this eliminates startup failures caused by partial upgrades or missing columns.

### Predictive Latency Reduction

`PredictivePreload` analyzes historical co-invocation patterns to pre-load skills, tools, and context before the agent requests them. On Termux/Android—where filesystem I/O and process startup are slower than desktop environments—this reduces perceived latency for chained tool calls by avoiding cold starts.

### Adaptive Cross-Session Learning

`MetaLearner` persists learned timing thresholds and tool preference weights in `federated.db`. Over sessions, it identifies which parameter combinations yield the highest success rates for recurring tasks, automatically routing future calls through optimized paths.

### Data Integrity Under Adverse Conditions

On mobile devices, system clock adjustments and battery-saving modes produce zero, negative, or extreme durations. The Clock-Skew & Anomaly Shield flags these records and excludes them from EMA scoring. Without this, a single clock drop during a long-running job would corrupt the agent's learned timing model for days.

### Mobile Storage Stewardship

Android shared storage and Termux home directories have finite space. Auto-Pruning Retention aggregates stale telemetry into compact `summary_analytics` rows once the queue exceeds 5000 entries. This prevents `federated.db` from growing without bound on devices where storage is a recurring constraint.

### Concurrent Session Safety

Hermes Agent on Android can spawn parallel sessions. `FederatedDB` uses `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`, chunking long transactions to minimize lock duration. This prevents the `database is locked` errors that previously killed telemetry writes during concurrent operations.

### Skill Composition Automation

`SkillGraph` maps dependency chains and fallback paths across the plugin ecosystem. `SkillComposer` uses this graph to assemble validated multi-step workflows at runtime, eliminating manual sequencing for common agent pipelines.

### Safe Edge-Case Validation

The Mock Test Framework lets contributors simulate failures, slow tools, and clock anomalies in isolated processes. This means regression testing for telemetry edge cases no longer requires risking live agent sessions.

---

## Analytics & Observability

### Running `stats.py`

The `stats.py` script provides CLI analytics over the federated telemetry store.

```bash
# From plugin directory
python3 ~/.hermes/plugins/hermes_enhancer/scripts/stats.py

# Or from exported project
python3 ~/storage/shared/Hermes_Enhancer_Project/scripts/stats.py

# Show anomalous rows only
python3 scripts/stats.py --anomalous

# Run database retention pruning
python3 scripts/stats.py --prune
```

**Outputs:**

- Total tool call count and unique tools invoked
- Success rate percentages (overall and per-tool)
- Average/median/max duration with P95 and P99 latency
- Top-N most frequent tool chains
- JSON and CSV export options for external dashboards

**Export Example:**

```bash
python3 scripts/stats.py --export-json ~/storage/shared/telemetry_$(date +%F).json
python3 scripts/stats.py --export-csv  ~/storage/shared/telemetry_$(date +%F).csv
```

---

## Design Philosophy

These principles guide every decision in the Hermes Enhancer codebase:

1. **Observe before optimizing.** Collect real telemetry first; all tuning decisions are evidence-based, not guessed.
2. **Learn from outcomes.** Successes are captured as reusable workflows; failures inform retry logic and anomaly filtering.
3. **Predict conservatively.** Preload likely options but never block on uncertain predictions; fail gracefully.
4. **Protect telemetry quality.** The Anomaly Shield rejects corrupt data before it can bias learning or inflate metrics.
5. **Measure real improvements.** Benchmarks and stats are first-class outputs; no performance claim is made without measurement.
6. **Defend resources.** Memory, disk, and CPU are treated as finite; guards and limits are always in place.

---

## Continuous Improvement Loop

```text
Execution
    │
    ▼
Telemetry (call logs, durations, outcomes, anomaly flags)
    │
    ▼
Learning (MetaLearner + SkillComposer + FeedbackOptimizer)
    │
    ▼
Prediction (PredictivePreload – Markov chain models)
    │
    ▼
Optimization (Memory Guard, pruning, retry tuning, skill graph persistence)
    │
    ▼
Future Execution (faster, safer, more reliable)
    │
    └──────────────────────────────────────┘
```

**How it works in practice:**

1. The agent executes a tool call. The `pre_tool_call` hook records the start time and logs the event.
2. The `post_tool_call` hook records the outcome, duration, and any errors. The telemetry is pushed to the SQLite database.
3. The MetaLearner ingests successful tool sequences as "winning workflows" for future reuse.
4. The FeedbackOptimizer updates EMA scores for each tool, excluding anomalous durations.
5. The PredictivePreload updates its Markov chain with the observed transition, preparing for the next likely call.
6. The FederatedDB auto-prunes old records to keep storage lean.
7. On the next run, the agent arrives at a decision faster because:
   - The SkillGraph is already persisted and loaded in <1s.
   - The PredictivePreload suggests likely next tools.
   - The MetaLearner knows which workflows succeeded.
   - The FeedbackOptimizer knows which tools to favor.

---

## Quick Setup

### Prerequisites

- Python 3.10 or higher
- Git
- Hermes Agent v0.20.6+
- `pip install pytest pytest-asyncio` for tests

### Clone and Install

```bash
# Clone repository
git clone https://github.com/Piyankali/hermes_enhancer.git

# Enter directory
cd hermes_enhancer

# Automatic plugin installation (verified, idempotent)
chmod +x setup.sh
./setup.sh
```

`setup.sh` installs Hermes Enhancer v0.22.0 into the Hermes user plugin
directory (`~/.hermes/plugins/hermes_enhancer`, or `$HERMES_HOME` when set)
and verifies the result. It checks `plugin.yaml` reports version `0.22.0`,
stages exactly the runtime file set (the nine `src/*.py` modules,
`plugin.yaml`, `README.md` — no `.git`, caches, tests, or temp files),
replaces any previous install atomically (the old tree is kept as a
timestamped backup, never merged), byte-compiles the installed modules,
and confirms discovery via `hermes plugins list`.

- Repeated execution is safe: re-running `./setup.sh` updates in place
  with a fresh backup and reports "updating safely".
- To remove only this plugin: `chmod +x uninstall.sh && ./uninstall.sh`
  (idempotent; other plugins, user data, and `~/.hermes/state.db` are
  never touched).
- Supported and tested here: Termux/Android and Linux shells with
  standard `bash`, `coreutils`, and `python3`. No root, no network,
  no `sudo`, no shell-startup edits.
- The installer does NOT modify `~/.hermes/state.db`, run
  `hermes doctor --fix`, or edit `~/.hermes/config.yaml`.

### Enable Plugin

Edit `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes_enhancer
  disabled: []
```

### Verify Deployment

```bash
ls -la ~/.hermes/plugins/hermes_enhancer/
# Expected: __init__.py, enhancer.py, self_test.py, feedback_optimizer.py,
#           predictive_preload.py, meta_learner.py, skill_graph.py,
#           composer.py, federated_db.py, stats.py, plugin.yaml
```

---

## Verification

### Install Test Dependencies

```bash
pip install pytest pytest-asyncio
```

### Run Tests

```bash
cd ~/.hermes/plugins/hermes_enhancer && python3 -m pytest -v
```

**Expected Output:** 16 passed, 0 failed.

### Run Benchmark

```bash
cd ~/.hermes/plugins/hermes_enhancer && python3 benchmarks/benchmark.py
```

**Expected Output:**
- `results/benchmark.json`
- `results/benchmark.csv`
- `results/benchmark.md`

**Key Metrics:**
- Hook mean overhead: ~245–363 µs
- Preload top-1 correctness: correct
- All benchmarks complete without errors

### Check Telemetry

```bash
cd ~/.hermes/plugins/hermes_enhancer && python3 scripts/stats.py
```

**Expected Output:**
- Accurate row counts from the sync queue
- 0 anomalous rows reported by the Anomaly Shield
- EMA scores for tracked tools
- Pre/post tool call counts

---

## Live Plugin Activation

The telemetry database is created lazily on the first tool call after plugin activation. To trigger it:

```bash
hermes chat -z "list files"
```

This single command activates the plugin, creates `~/.hermes/federated.db`, and begins recording telemetry. After that, `scripts/stats.py` will reflect live data from your actual usage.

---

## Monitoring & Maintenance

### Stats Dashboard

```bash
python3 scripts/stats.py
```

This script provides:
- Total call counts
- Pre-tool vs post-tool distribution
- Failure counts and error breakdown
- Anomalous records (should remain 0)
- EMA scores per tool
- Database row counts

### Database Maintenance

- **Auto-pruning:** Enabled by default. Old records are aggregated and pruned when the threshold (default 5000 rows) is exceeded.
- **Retention period:** Default 14 days. Adjust `DEFAULT_RETENTION_DAYS` in `federated_db.py` if needed.
- **Integrity checks:** Run automatically on database connection.
- **Manual pruning:** Run `python3 -c "from federated_db import get_db; print(get_db().prune())"` for manual cleanup.

### Memory Management

- **Active Memory Guard:** RSS is monitored during preload operations; cache is purged and `gc.collect()` triggered if memory exceeds the limit.
- **Tracemalloc:** Baseline snapshots are taken at init; memory deviations are logged.
- **No action required:** All memory management is automatic.

---

## Development Notes

### Workspace Structure

- **Development workspace:** `~/storage/shared/Hermes_Enhancer_Project/`
- **Plugin root:** `~/.hermes/plugins/hermes_enhancer/`
- **Telemetry DB:** `~/.hermes/federated.db`
- **Results:** `~/.hermes/plugins/hermes_enhancer/results/`

### Development Workflow

1. Edit source files in the workspace.
2. Run tests locally: `python3 -m pytest -v`
3. Run benchmarks to verify performance: `python3 benchmarks/benchmark.py`
4. Sync to plugin directory:

```bash
cp -u src/*.py ~/.hermes/plugins/hermes_enhancer/
cp -u plugin.yaml ~/.hermes/plugins/hermes_enhancer/
cp -ru tests/ benchmarks/ scripts/ ~/.hermes/plugins/hermes_enhancer/
```

### Key Source Files

| File | Purpose |
|---|---|
| `__init__.py` | Plugin registration, hook bindings, singleton access |
| `enhancer.py` | Core orchestrator; pre/post tool call hooks, tool name extraction, anomaly detection |
| `federated_db.py` | SQLite persistence layer with WAL, retry, pruning, async wrappers |
| `meta_learner.py` | Workflow ingestion, winning workflow storage, history pruning |
| `skill_graph.py` | Skill dependency graph, topological ordering, SQLite persistence |
| `composer.py` | Workflow engine with conditional branching and retry/backoff |
| `predictive_preload.py` | Markov chain tool sequencer with memory guard |
| `feedback_optimizer.py` | EMA success scoring with anomalous duration filtering |
| `self_test.py` | Runtime parameter validation, path expansion, health checks |
| `stats.py` | Telemetry dashboard and reporting |

### Testing Strategy

- **Unit tests:** `tests/test_enterprise_upgrade.py`, `tests/test_pipeline.py`
- **Benchmark suite:** `test_benchmark_suite.py` validates performance regressions
- **Self-tests:** `SelfTestEngine.run_battery()` validates runtime environment
- **Integration:** Full hook path exercised through pytest fixtures

---

## Troubleshooting & Edge Cases

### Parameter Coercion

- **Issue:** Tools receiving unexpected parameter types from upstream optimizers.
- **Fix:** `FeedbackOptimizer` validates parameter types against tool schemas before injection. Coercion fallback preserves original parameters if type mismatch is detected.

### Duration Floor Fallbacks

- **Issue:** Zero or negative duration values from clock skew or rapid tool returns.
- **Fix:** `SelfTestEngine` enforces a minimum duration floor of `1µs`. Negative durations are capped at `0` and flagged as anomalous in `federated.db`.

### SQLite WAL Mode Concurrency

- **Issue:** Concurrent Hermes sessions causing database lock errors.
- **Fix:** `FederatedDB` opens SQLite with `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`. Long-running transactions are chunked to minimize lock duration.

### Tool Name Extraction Edge Cases

- **Issue:** Hook callbacks receiving tools in nested dicts or kwargs with non-standard keys.
- **Fix:** Tool name extraction inspects `args[0]`, `kwargs['tool']`, `kwargs['name']`, and nested dicts. All keys are normalized to lowercase before timing attribution.

### Schema Migration

- **Issue:** Legacy `federated.db` missing `tool` or `anomalous` columns from older schema.
- **Fix:** `FederatedDB._ensure_initialized` runs `_migrate(conn)` after table creation, using `ALTER TABLE ADD COLUMN` to backfill missing columns without data loss.

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-enhancement`
3. Commit changes: `git commit -m 'feat: add X optimization'`
4. Push to branch: `git push origin feature/my-enhancement`
5. Open a Pull Request

Please ensure all tests pass (`python3 -m pytest -v`) and benchmarks remain within acceptable thresholds before submitting.

---

## Repository

**GitHub:** https://github.com/Piyankali/hermes_enhancer

Star ⭐ the repo if you find it useful. Contributions, issues, and feature requests are welcome.

---

## License

MIT. See the `LICENSE` file in the repository for full text.

```
Copyright (c) 2025 Hermes Enhancer Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

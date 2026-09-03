# Hermes Enhancer v0.20.7 - Autonomous Self-Healing & Telemetry Middleware

[![Python 3.x](https://img.shields.io/badge/python-3.x-blue)](https://www.python.org/)
[![Hermes Agent v0.20.6+](https://img.shields.io/badge/Hermes_Agent-v0.20.6%2B-green)](https://github.com/NousResearch/hermes-agent)
[![platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20Termux-orange)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Autonomous self-healing middleware for Hermes Agent** with microsecond-accurate telemetry, predictive preloading, meta-learning optimization, and federated analytics. Designed for cross-platform execution (Linux, macOS, Windows, Android/Termux).

---

## 11 Core Systems Architecture Matrix

`hermes_enhancer` is an 11-module plugin architecture that intercepts Hermes Agent tool lifecycles, optimizes execution paths, and builds a federated knowledge graph across sessions.

### Agent Intelligence
| # | Module | Responsibility |
|---|--------|---------------|
| 1 | **SkillGraph** | Directed graph of skill dependencies, co-invocation patterns, and fallback chains |
| 2 | **SkillComposer** | Runtime composer that assembles multi-step workflows from SkillGraph, validates parameter compatibility |
| 3 | **MetaLearner** | Cross-session learning layer storing optimized parameters, timing thresholds, and tool preference weights |
| 4 | **FeedbackOptimizer** | Post-tool analysis engine recording duration, success/failure states, and parameter patterns |

### Execution Optimization
| # | Module | Responsibility |
|---|--------|---------------|
| 5 | **SelfTestEngine** | Runtime diagnostic runner validating hook bindings, database integrity, and inter-module wiring on every session start |
| 6 | **PredictivePreload** | Pattern-based anticipatory loading pre-warming skills, tools, and context data before requests |
| 7 | **Async Execution** | Non-blocking I/O via ThreadPoolExecutor; DB writes scheduled off the main Hermes runtime thread |

### Telemetry Integrity
| # | Module | Responsibility |
|---|--------|---------------|
| 8 | **FederatedDB** | SQLite-backed telemetry store with WAL-mode concurrency, persisting tool metrics and learned parameters |
| 9 | **Clock-Skew & Anomaly Shield** | Filters zero/negative/extreme durations (>300s), flags anomalous=1, excludes from EMA scoring |

### Long-Running Stability
| # | Module | Responsibility |
|---|--------|---------------|
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
└── tests/
    ├── _test_worker.py          # Isolated test runner worker
    └── test_pipeline.py         # Automated edge-case diagnostic suite
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
- `HermesEnhancer.on_post_tool_call` sets `success=False` for anomalous tool calls.

### Automated Database Pruning & Data Retention
- When `sync_queue` exceeds `prune_threshold` (default 5000), old records are aggregated into `summary_analytics` and deleted.
- `stats.py` supports `--prune` and `--anomalous` flags for manual maintenance.

### Standalone Mock Diagnostic Test Framework
- `tests/test_pipeline.py` simulates mock tool calls (`terminal`, `write_file`, `read_file`, `patch`, `browser_navigate`) with random successes, failures, fast/slow durations, and anomalous clock drops.
- Tests run in isolated processes using absolute-path module loading via importlib.
- Run with: `python3 tests/test_pipeline.py`

---

## Quick Install

```bash
# Clone repository
git clone https://github.com/Piyankali/hermes_enhancer.git

# Enter directory
cd hermes_enhancer

# Create plugin directory and install
mkdir -p ~/.hermes/plugins/hermes_enhancer/
cp -r * ~/.hermes/plugins/hermes_enhancer/
```

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

## CLI Usage

```bash
# General metrics
python3 scripts/stats.py

# Anomalous latency/clock skew tracking
python3 scripts/stats.py --anomalous

# Data retention cleanup
python3 scripts/stats.py --prune
```

## Diagnostics

```bash
# Run test suite
python3 tests/test_pipeline.py
```

---

## Hooks & Lifecycles

The plugin intercepts two Hermes Agent lifecycle hooks:

### `pre_tool_call`
- **Timing:** Before every tool invocation
- **Actions:**
  - Logs tool name and parameters for telemetry
  - Checks `PredictivePreload` cache for predicted next-tools
  - Applies `MetaLearner` learned timing thresholds
  - Injects optimized parameters from `FeedbackOptimizer`

### `post_tool_call`
- **Timing:** After every tool invocation returns
- **Actions:**
  - Records wall-clock duration with microsecond precision
  - Captures success/error state and result metadata
  - Updates `FederatedDB` with tool execution metrics
  - Triggers `FeedbackOptimizer` to update learning weights
  - Notifies `SkillGraph` of actual co-invocation patterns

**Success Definition:** A tool call is considered successful when `error_message` is absent AND `status` is not `"error"`/`"failed"`, even on empty results.

---

## Real-World Realized Benefits for Hermes Agent

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
python3 ~/.hermes/plugins/hermes_enhancer/stats.py

# Or from exported project
python3 ~/storage/shared/Hermes_Enhancer_Project/scripts/stats.py
```

**Outputs:**
- Total tool call count and unique tools invoked
- Success rate percentages (overall and per-tool)
- Average/median/max duration with P95 and P99 latency
- Top-N most frequent tool chains
- JSON and CSV export options for external dashboards

**Export Example:**
```bash
python3 stats.py --export-json ~/storage/shared/telemetry_$(date +%F).json
python3 stats.py --export-csv  ~/storage/shared/telemetry_$(date +%F).csv
```

### Database Maintenance

```bash
# Manual prune of old telemetry
python3 stats.py --prune

# Show anomalous rows
python3 stats.py --anomalous
```

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

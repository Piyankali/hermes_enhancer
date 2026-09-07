# Hermes Enhancer

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-v0.21.0-tested-green)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Termux-orange)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Tests](https://img.shields.io/badge/tests-67_tracked_(59_core_%2B_8_installer)-brightgreen)

> **Self-healing telemetry middleware for Hermes Agent** — it records tool
> executions, learns from outcomes, predicts likely next tools, and persists
> telemetry through a crash-safe event buffer with automatic startup recovery.

**Version:** `0.22.2`
**Status:** Beta (process-crash/restart durability validated; power-loss
durability is not claimed — see [Failure boundaries](#failure-boundaries))
**Python:** 3.10+
**License:** MIT

---

## Table of Contents

- [Project overview](#project-overview)
- [Version and compatibility](#version-and-compatibility)
- [Feature overview](#feature-overview)
- [Architecture](#architecture)
- [Execution lifecycle](#execution-lifecycle)
- [Runtime components](#runtime-components)
- [Public API reference](#public-api-reference)
- [Persistent telemetry](#persistent-telemetry)
- [Crash-safe durability](#crash-safe-durability)
- [Recovery model](#recovery-model)
- [Duplicate protection](#duplicate-protection)
- [Failure handling](#failure-handling)
- [Database health and integrity](#database-health-and-integrity)
- [Observability and counters](#observability-and-counters)
- [Sync and async APIs](#sync-and-async-apis)
- [Learning systems](#learning-systems)
- [Predictive preload](#predictive-preload)
- [Skill graph](#skill-graph)
- [Workflow composition](#workflow-composition)
- [Self-test and diagnostics](#self-test-and-diagnostics)
- [Retention and pruning](#retention-and-pruning)
- [Installation](#installation)
- [Android / Termux](#android--termux)
- [Upgrade](#upgrade)
- [Verification](#verification)
- [Usage examples](#usage-examples)
- [Configuration](#configuration)
- [Testing](#testing)
- [Validation evidence](#validation-evidence)
- [Performance and benchmarks](#performance-and-benchmarks)
- [Security and privacy](#security-and-privacy)
- [Failure boundaries](#failure-boundaries)
- [Troubleshooting](#troubleshooting)
- [Uninstallation](#uninstallation)
- [Repository structure](#repository-structure)
- [Development](#development)
- [Roadmap](#roadmap)
- [License](#license)

---

## Project overview

AI agents forget what worked, repeat failed strategies, and lose
telemetry when processes die. Hermes Enhancer embeds a continuous
improvement loop in the agent runtime:

1. **Execution** — the agent runs tools.
2. **Telemetry** — every call is recorded with timestamps, outcome,
   duration, and IDs.
3. **Durable storage** — records go through a crash-safe buffer into
   SQLite (v0.22.0).
4. **Learning** — outcome scores, skill relationships, and transition
   statistics accumulate across sessions.
5. **Prediction** — likely next tools are preloaded from historical
   transitions.

The agent gets measurably better the more it runs — from its own
workflows, not generic benchmarks.

---

## Version and compatibility

| Item | Value | Source |
|---|---|---|
| Plugin version | `0.22.2` | `plugin.yaml` |
| Installer gate | `0.22.2` (`EXPECTED_VERSION`) | `setup.sh` |
| Status | Beta | this release |
| Python | 3.10+ | `setup.sh` byte-compile / test runs |
| Hermes | Tested against Hermes Agent **v0.21.0** CLI | `results/v0.22_installer_validation.md` |

No broader Hermes-version compatibility is claimed: the only
evidence-backed statement is that installation and read-only plugin
discovery (`hermes plugins list --plain`, `hermes plugins show`) were
validated against the v0.21.0 CLI. Behavior on other Hermes versions is
not verified.

---

## Feature overview

- `pre_tool_call` / `post_tool_call` hooks into Hermes tool execution
  (registered via `register(ctx)`)
- Persistent SQLite telemetry (`sync_queue`, WAL mode, 5 s busy timeout)
- v0.22 crash-safe event buffer with automatic bounded startup recovery
  (≤100 rows per invocation)
- Idempotent `event_id` persistence (retries return the existing row)
- Transient/permanent/unknown failure classification with bounded retries
- SQLite health checks (`quick_check` + `integrity_check`) that never
  delete data
- Malformed-record quarantine (diagnosed, preserved, never silently
  dropped)
- Markov-order-1 next-tool prediction with a 256 MB memory guard
- Skill graph, workflow composer, cross-session meta-learner
- EMA outcome scoring (`alpha = 0.2`), anomalous durations excluded
- Automatic retention pruning (5000-row threshold, 14-day retention)
- Bounded in-memory queue (5000) + background batch worker
  (100 rows / 0.25 s)
- Automatic installer with verification (`setup.sh`), safe scoped
  uninstaller (`uninstall.sh`)

---

## Architecture

```text
Hermes Agent
     │
     ├── pre_tool_call
     │
     ▼
Hermes Enhancer (HermesEnhancer, src/enhancer.py)
     │
     ├── PredictivePreload ── transition prediction
     ├── SkillGraph ───────── skill dependencies
     ├── SkillComposer ────── workflow plans
     ├── FeedbackOptimizer ── EMA outcome scores
     ├── MetaLearner ──────── cross-session history
     ├── SelfTestEngine ───── diagnostics battery
     │
     └── Telemetry
             │
             ▼
        FederatedDB (src/federated_db.py)
             │
      ┌──────┴──────┐
      ▼             ▼
 Event Buffer    Worker Queue (bounded 5000,
 (event_buffer,   batch 100 / 0.25 s)
  durable commit
  first)
      │             │
      └──────┬──────┘
             ▼
        SQLite Storage
        (sync_queue, WAL,
         synchronous=NORMAL)
```

All components and relationships above are verified in source:
`HermesEnhancer.__init__` owns exactly `db`, `self_test`, `feedback`,
`preload`, `meta_learner`, `skill_graph`, `composer`; `post_tool_call`
fans out to `enqueue_event` + `record_outcome` + `preload.record` +
`meta_learner.ingest`; `push`/`_enqueue_durable` commit to
`event_buffer` before final persistence.

---

## Execution lifecycle

```text
tool execution
      ↓
telemetry capture (IDs assigned: event_id, trace_id, tool_call_id)
      ↓
validation / anomaly handling (durations <= 0 or > 300 s flagged)
      ↓
persistent storage (queue → worker batch → SQLite)
      ↓
feedback / learning (scores, graph edges, transitions)
      ↓
prediction / relationships
      ↓
future execution
```

`pre_tool_call` captures state, starts timing, assigns IDs, and enqueues
a hook event. `on_post_tool_call` records outcome and duration, flags
anomalies, updates the optimizer/learner/graph, and persists telemetry.

---

## Runtime components

| Module | Class | Role |
|---|---|---|
| `__init__.py` | — | Plugin entrypoints: `register`, `get_instance` |
| `enhancer.py` | `HermesEnhancer` | Hooks, timing, anomaly flags, orchestration |
| `federated_db.py` | `FederatedDB` | SQLite telemetry + v0.22 buffer/recovery |
| `feedback_optimizer.py` | `FeedbackOptimizer` | EMA outcome scoring per tool |
| `predictive_preload.py` | `PredictivePreload` | Markov-order-1 next-tool prediction |
| `meta_learner.py` | `MetaLearner` | Cross-session event ingest and summaries |
| `skill_graph.py` | `SkillGraph` | Skill dependencies, ordering, DB persistence |
| `composer.py` | `SkillComposer` | Workflow steps, branches, retry steps |
| `self_test.py` | `SelfTestEngine` | Config/file/signature diagnostics battery |

Nine runtime modules. The installer copies exactly these nine
`src/*.py` files plus `plugin.yaml` and `README.md` into the plugin
directory — nothing else (`scripts/stats.py` stays repository-only).

---

## Public API reference

### Plugin entrypoints (`src/__init__.py`)

### `register(ctx)`

Purpose: bind the enhancer's pre/post tool hooks into the Hermes runtime.

Parameters:
- `ctx`: Hermes plugin context (uses `ctx.node_id`, `ctx.register_hook`).

Behavior: creates a `HermesEnhancer`, wraps it in `_on_pre_tool_call` /
`_on_post_tool_call` closures (which translate the Hermes hook signature
into `pre_tool_call(args, kwargs)` / `on_post_tool_call(...)` calls and
track start times per `tool_call_id`), and registers both hooks. This is
the primary integration path.

### `get_instance()`

Purpose: return the singleton `HermesEnhancer`, creating it on first call.

### Enhancer (`src/enhancer.py`)

`HermesEnhancer(node_id="local")` owns the seven components listed in
[Architecture](#architecture) and takes a `tracemalloc` baseline snapshot
at construction.

### `pre_tool_call(args, kwargs)`

Purpose: pre-tool hook — capture state and start timing.

Parameters:
- `args`: positional tuple; tool name extracted from `args[0]`.
- `kwargs`: may carry `trace_id`, `tool_call_id` (reused when present).

Returns: dict with resume markers `_enhancer_t0`, `_enhancer_tool`,
`_enhancer_trace_id`, `_enhancer_event_id` (empty dict when disabled).

Side effects: enqueues a `pre_tool_call` hook event via
`db.enqueue_event(...)` (in-memory queue path, not the durable buffer).

### `on_post_tool_call(*args, **kwargs)`

Purpose: post-tool hook — capture duration, outcome, and push telemetry.

Returns: `None`.

Behavior: resolves timing from `_start_times` (5 ms fallback), derives
success from `status` / `error_message` / `error_type` / result text,
flags anomalous durations (which force `success=False`), enqueues a
`post_tool_call` event, then updates feedback scores, preload
transitions, and the meta-learner. All durations below 1 ms are clamped
to 1 ms.

### `cleanup_completed_tasks()`

Removes `__completed__`-prefixed entries from `_start_times`.
Returns `{"removed_start_times": N}`.

### `cleanup_old_history(keep_last=1000)`

Prunes the meta-learner history.
Returns `{"pruned_meta_learner": N}`.

### `cleanup_preload_cache()`

Clears preload transitions/history when non-empty.
Returns `{"preload_cleared": 0|1}`.

### `self_heal_memory()`

Compares current `tracemalloc` growth against the construction baseline;
above 50 MB it runs the three cleanups plus `gc.collect()`.
Returns a report dict (`triggered`, `steps`, byte counts); never raises.

### `health_report()`

Aggregates node id, DB availability/count/counters, self-test battery,
feedback/preload/meta-learner/skill-graph/composer summaries, and the
memory report. Calls `self_heal_memory()` (possible GC side effect).

### `check_memory_leak()`

Top-20 positive `tracemalloc` deltas (`file`, `line`, `delta_bytes`).
Read-only.

### `run_self_test()`

Runs the `SelfTestEngine` battery and persists the result as a
`self_test` telemetry event. Returns the battery dict.

---

## Persistent telemetry

`FederatedDB` (`src/federated_db.py`) is a thread-safe SQLite store. The
`sync_queue` table holds one row per telemetry event (payload, timestamp,
node, tool, anomaly flag, `event_id`, `trace_id`, `tool_call_id`).

Connection defaults (constructor): `PRAGMA journal_mode=WAL`,
`PRAGMA busy_timeout=5000`, `PRAGMA synchronous=NORMAL`. Default database
path: `~/.hermes/federated.db` (`DB_PATH`; overridable per instance).

Write paths:

- **Hook path** (`pre_tool_call` / `on_post_tool_call`): bounded
  in-memory queue (default 5000) → background daemon worker batch
  (default 100 rows / 0.25 s) → SQLite. `flush()` blocks until the queue
  drains; `shutdown()` drains on exit.
- **Direct API path** (`push` / `async_push`): crash-safe buffered path
  ([Crash-safe durability](#crash-safe-durability)). Synchronous `push`
  commits inline; `async_push` buffers durably, then queues for the
  worker.

`FederatedDB(...)` parameters: `db_path`, `prune_threshold` (5000),
`retention_days` (14), `db_batch_size` (100), `db_flush_interval`
(0.25), `db_max_queue` (5000).

---

## Crash-safe durability

Direct `push()` / `async_push()` calls follow strict ordering:

```text
Event
  ↓
Durable buffer commit (event_buffer, INSERT OR IGNORE)
  ↓
Final SQLite persistence (sync_queue commit)
  ↓
Buffer finalization (state → PERSISTED + persisted_at)
```

Critical invariant, proven by crash tests: **the buffer is never
finalized before final persistence is committed.** A crash between any
two commits leaves either a pending buffer row (recovered later) or a
committed final row (deduplicated later) — never a finalized buffer
with a missing final event.

Buffer schema (`event_buffer`, `event_id` UNIQUE NOT NULL PRIMARY KEY):
`event_id`, `trace_id`, `tool_call_id`, `event_type`, `payload`,
`created_at`, `state`, `attempt_count`, `last_error`, `next_retry_at`,
`buffered_at`, `persisted_at`, `schema_version`, `checksum`.
States observed: `CREATED`, `QUEUED`, `PERSISTING`, `PERSISTED`,
`RETRY_WAIT`, `FAILED`.

What is actually durable: only rows committed to SQLite (buffer or
final) survive a process crash. Hook telemetry sitting only in the
in-memory queue at crash time is not covered — call `flush()` /
`shutdown()` on graceful exit, or use the `push` API for records that
must survive.

---

## Recovery model

```text
Startup
  ↓
Database initialization (schema + indexes)
  ↓
Automatic startup recovery (bounded batch, ≤100 rows)
  ↓
Persist missing finals / finalize already-persisted buffers
  ↓
Continue normal operation
```

Every `FederatedDB` initialization runs recovery automatically after the
schema is ready — no manual call needed. Each invocation processes **at
most 100 rows**; leftovers drain on later scheduler ticks or restarts
(a 500-row backlog drains in exactly five invocations). Recovery is
idempotent: startup, scheduler, and manual invocations share the same
`event_id` guards, so repeated recovery never duplicates. Progress is
observable via `get_startup_recovery_info()`, which reports whether the
automatic pass executed and what it did.

---

## Duplicate protection

No duplicate persisted `event_id` and no loss of confirmed durable
buffered events across the tested normal process crash/restart scenarios.

Mechanisms: UNIQUE `event_id` in `event_buffer` (duplicates return
`False` instead of raising); idempotent final inserts plus a partial
unique index on `sync_queue(event_id)` (legacy empty IDs exempt), so a
retried `push` returns the existing row id; recovery checks
`get_event()` first and only finalizes when the final row exists
(counted as `skipped`, never re-inserted).

---

## Failure handling

Tested failure classes: database locked, transaction failure, buffer
write failure, corrupted buffer payload, duplicate event, worker
interruption, simulated storage exhaustion.

Classification (`_with_retry` / `_flush_buffer`): transient SQLite
busy/locked `OperationalError`s are retried with backoff (`0 s, 0.5 s,
1.5 s`, up to 3 attempts); permanent and unknown errors increment
`permanent_errors` / `unknown_errors` and surface instead of retrying
forever. Buffer-write failures are fail-loud — `push`/`async_push` raise
rather than silently bypassing durability. Corrupt payloads fail JSON
validation and are marked `FAILED` with a `corrupt_payload:` diagnosis;
the row is kept, unknown-state rows are left untouched, and healthy rows
keep flowing.

Retryable vs permanent vs unknown is decided by the exception type at
the persistence boundary; anything unrecognized counts as unknown and is
surfaced, never silently dropped.

---

## Database health and integrity

### `verify_database()`

Purpose: report SQLite health without modifying data.

Returns: `{healthy, quick_check, integrity_check, error, database}` —
`healthy` is true only when both `PRAGMA quick_check` and the deeper
`PRAGMA integrity_check` return `ok`. Inaccessible paths report
`OPERATIONAL_ERROR` without crashing the caller.

Corruption is detected and surfaced; the database is never silently
deleted, recreated, or repaired.

---

## Observability and counters

Every event carries `event_id`, `trace_id`, `tool_call_id`, an event
type, and a JSON payload. `get_recent(limit)` / `count()` /
`summary_counts()` query final state; `get_buffer_event()` /
`get_buffer_summary()` (`{total, per-state counts}`) inspect the buffer.

Counters (`get_counters()` / `get_event_counters()`): `events_enqueued`
counts queue admissions (async path only); `events_persisted` counts
final commits (sync + worker); `events_dropped` counts queue refusals
(`enqueue_event` returns `None`); `events_failed` counts failed worker
batches; `events_retried` and `transient/permanent/unknown_errors` count
operations, not events. All counters are **process-local**: they reset
on restart. SQLite state is the durable source of truth. Recovery
reports per-invocation dicts (`recovered`, `skipped`, `states_seen`)
instead of inventing persistent counters.

---

## Sync and async APIs

v0.21 APIs, signatures unchanged:

### `push(payload, node_id="local") -> int`

Crash-safe synchronous persist. Returns the row id (existing id when the
`event_id` was already persisted). Raises loudly on buffer-write failure.

### `get_recent(limit=100) -> list[dict]`, `count() -> int`

Read final persisted state (newest-first for `get_recent`).

### `async_push(payload, node_id="local") -> Optional[str]`

Durable buffer commit on a worker thread, then worker queue. Returns the
`event_id`, or `None` if the bounded queue (or shutdown) refused it.

### `flush(timeout=5.0) -> dict`, `async_flush(timeout=5.0) -> dict`

Block until the queue drains. `shutdown(timeout=5.0)` drains and stops
the worker; `enqueue_event` after shutdown returns `None`.

v0.22 buffer/recovery APIs:

- `enqueue_to_buffer(event_id, trace_id, tool_call_id, event_type, payload) -> bool`
- `mark_buffer_persisted(event_id) -> bool`
- `get_buffer_event(event_id) -> dict | None`
- `get_buffer_summary() -> dict` (`total` plus per-state counts)
- `get_event(event_id) -> dict | None` (final-state lookup)
- `recover_buffered_events() -> dict` (`recovered`, `skipped`,
  `states_seen`; ≤100 rows)
- `verify_database() -> dict`
- `get_startup_recovery_info() -> dict`
- `enqueue_event(payload, node_id="local", event_id=None, trace_id=None, tool_call_id=None) -> Optional[str]`
- `prune() -> dict` / `async_prune()`, `summary_counts()` /
  `async_summary_counts()`, `async_get_recent()`, `async_count()`

---

## Learning systems

### Feedback optimizer (`src/feedback_optimizer.py`)

EMA outcome scorer. `__init__(alpha=0.2, default_score=0.5)` (alpha must
be in `(0, 1]`).

- `record_outcome(key, success, weight=1.0, duration_s=0.0, anomalous=False) -> None` —
  updates `scores[key]` toward 1.0/0.0; anomalous durations (explicit
  flag or `duration_s <= 0` / `> 300`) leave the score unchanged.
- `get_score(key) -> float` — current score or `default_score`.
- `top_n(n=10)` — keys sorted by score, descending.
- `summary()` — `{count, mean, top}` (`{count: 0, mean: default, top: []}`
  when empty).

### Meta-learner (`src/meta_learner.py`)

Cross-session event store (in-memory list, not a neural model).

- `ingest(event) -> None` — appends `{ts, event}`; successful events with
  a `tool` key also append a `winning_workflows` record.
- `prune(keep_last=1000) -> int` — retains the newest N history entries;
  returns the removed count (0 when already within bounds). Note:
  `winning_workflows` is not pruned.
- `summary()` — `{history_count, winning_workflows_count}`.

### Skill graph

- `register_skill(skill, depends_on=None, meta=None) -> None` —
  registers a skill; dependencies append (callers must avoid duplicates).
- `dependencies(skill) -> list[str]` — direct dependencies ([] unknown).
- `topological_order() -> list[str]` — DFS ordering, dependencies first.
  Note: no cycle detection — cyclic graphs recurse until
  `RecursionError`.
- `summary()` — `{skills, dependency_count}`.
- `save_to_db(db_conn)` / `load_from_db(db_conn)` — persist/restore via
  `skill_graph_nodes(skill, meta, updated_at)` and
  `skill_graph_edges(src, dst)` tables (created by `FederatedDB`
  migrations). Malformed stored metadata loads as `{}`.

### Workflow composition

- `add_step(name, payload=None) -> None` — append an ordered step.
- `add_conditional_branch(step_name, condition, true_next, false_next=None)` —
  append a routing step evaluated against the run context.
- `add_retry_step(name, retries=3, backoff_base=0.5)` — attaches retry
  metadata to the **most recently added** step (no-op when empty; the
  `name` argument is informational only).
- `run(context=None) -> list[dict]` — executes steps in order; results
  accumulate in `self.results`. Conditional steps jump to the named step
  (unknown names fall through to the next step). Retry steps re-execute
  with exponential backoff (`backoff_base * 2**attempt`). Steps always
  report `status: "ok"` — `run_fn` failures surface as raised exceptions
  or via custom `run_step` overrides, not as error results.
- `clear() -> None` — empties steps and results.

---

## Predictive preload

`PredictivePreload(order=1, memory_limit_mb=256)` records tool-call
transitions (`record(tool_name)`) and predicts successors
(`predict(current_tool, top_k=5) -> [(tool, probability)]`, probabilities
normalized over the observed transitions for that history key; `[]` when
unseen). Order must be `>= 1` (default 1: pure bigram statistics, not a
neural model). `maybe_cleanup()` clears state plus `gc.collect()` when
RSS exceeds the limit; `clear()` resets unconditionally.

---

## Self-test and diagnostics

`SelfTestEngine` battery (`run_battery()`): path expansion (`~/.hermes`
resolves under home), config validation (required-keys check), directory
writability (`~/.hermes/plugins`). Returns
`{passed, total, results, all_passed}` (3 checks); `summary()` renders a
one-line `PASS`/`FAIL` string. `run_function_signature_test(func,
expected_args)` compares a function's parameter names. Exposed via
`HermesEnhancer.run_self_test()`; `scripts/stats.py` prints telemetry
summaries from the local `~/.hermes/federated.db` for development
inspection (repository-only helper, not installed).

---

## Retention and pruning

Defaults (constructor-configurable): prune threshold 5000 rows,
retention 14 days. When the threshold is exceeded, sub-cutoff rows are
aggregated per tool/day into `summary_analytics`, then deleted. Pruning
runs automatically on the write path (`_maybe_prune`) and on demand via
`prune()` / `async_prune()`, which report before/after counts.

---

## Installation

Requirements: Python 3.10+, git. No root, no network access during
install, no `sudo`, no shell-startup edits.

```bash
git clone https://github.com/Piyankali/hermes_enhancer.git
cd hermes_enhancer
bash setup.sh
```

(`chmod +x setup.sh` first on filesystems that support exec bits, then
`./setup.sh` works too.) What happens:

```text
Clone repository
      ↓
Run setup.sh
      ↓
Detect Hermes installation (~/.hermes, or $HERMES_HOME)
      ↓
Stage plugin files (exact runtime set, temp staging dir)
      ↓
Install hermes_enhancer (atomic replace; old tree backed up, never merged)
      ↓
Verify installation (files, version 0.22.2, byte-compile out of tree)
      ↓
Verify Hermes plugin discovery (hermes plugins list)
      ↓
Plugin ready
```

Step by step (verified against `setup.sh` source):

1. **Project check** — all 11 required files must exist; `plugin.yaml`
   must report `0.22.2` or the installer aborts.
2. **Hermes home detection** — `$HERMES_HOME`, default `$HOME/.hermes`;
   plugin destination `$HERMES_HOME/plugins/hermes_enhancer`.
3. **Staging** — the nine `src/*.py` modules plus `plugin.yaml` and
   `README.md` are copied to a temp staging dir (same filesystem for an
   atomic move) and re-checked for version `0.22.2`. `scripts/stats.py`,
   tests, benchmarks, caches, and VCS metadata are never staged.
4. **Atomic install** — an existing tree is moved to a timestamped backup
   (`.hermes_enhancer.bak.YYYYMMDD_HHMMSS`, never merged, never deleted),
   then the stage is moved into place. Any failure aborts with `ERROR`
   and a nonzero exit — no half-installed tree.
5. **Verification from disk** — every installed file re-checked, version
   re-read, and `federated_db.py` + `enhancer.py` byte-compiled in an
   isolated temp dir.
6. **Discovery** — read-only `hermes plugins list --plain` with retries
   (~50 s); prints `PASS`, a lag warning, or `SKIPPED` when the CLI is
   absent (`SKIP_DISCOVERY=1` forces the skip, used by tests).

The installer never touches `~/.hermes/state.db`, never runs
`hermes doctor --fix`, never edits `~/.hermes/config.yaml`, uses no
network and no `sudo` — no manual configuration is required.

---

## Android / Termux

```bash
pkg update
pkg install git python
git clone https://github.com/Piyankali/hermes_enhancer.git
cd hermes_enhancer
bash setup.sh
```

`pytest` / `pytest-asyncio` are needed only for running the test suite
(see [Testing](#testing)), not for installation. Validated on
Termux/Android with Termux `bash`, coreutils, and `python3`.

---

## Upgrade

```bash
cd hermes_enhancer
git pull
bash setup.sh
```

Re-running is safe by design: the installer detects the existing tree,
moves it to a timestamped backup (never merged, never deleted), and
installs fresh — the script prints `(Already installed: updating
safely)`. There is no automatic rollback; restore manually from the
backup directory printed during installation if needed.

---

## Verification

```bash
hermes plugins list --plain | grep hermes_enhancer
hermes plugins show hermes_enhancer
cat ~/.hermes/plugins/hermes_enhancer/plugin.yaml   # version: 0.22.2
ls ~/.hermes/plugins/hermes_enhancer/                # 9 .py modules + plugin.yaml + README.md
```

`setup.sh` already performs all of these checks and prints
`Plugin installation: PASS` / `Plugin discovery: PASS` /
`Plugin version: 0.22.2` — only those lines count as verified success.

---

## Usage examples

### Hook integration (installed plugin — flat layout)

```python
from enhancer import HermesEnhancer

enh = HermesEnhancer(node_id="local")
pre = enh.pre_tool_call(args=("my_tool",), kwargs={"tool_call_id": "tc1"})
# ... run the tool ...
enh.on_post_tool_call(
    tool_name="my_tool",
    pre_state=pre,          # accepted but timing resolves via _start_times
    result="ok",
    status="success",
    duration_ms=12,
    tool_call_id="tc1",
)
```

Note: `on_post_tool_call` resolves start time from the earlier
`pre_tool_call` keyed by normalized tool name, not from the passed
`pre_state` dict — always call both hooks for accurate durations.
Hermes-runtime registration is done by `register(ctx)`; see
`src/__init__.py`.

### Running from the source repository

The modules use flat absolute imports (`from enhancer import ...`), so
the `src/` directory itself must be on `sys.path`:

```bash
cd hermes_enhancer
PYTHONPATH=src python3 scripts/stats.py
PYTHONPATH=src python3 -m pytest tests -q
```

### Crash-safe direct persistence

```python
from federated_db import FederatedDB

db = FederatedDB()  # automatic bounded startup recovery runs here
row_id = db.push({"tool": "my_tool", "success": True})
db.flush()
info = db.get_startup_recovery_info()  # {"executed": True, ...}
db.shutdown()  # drains the worker queue
```

### Async API (valid `asyncio` form)

```python
import asyncio
from federated_db import FederatedDB

async def main():
    db = FederatedDB(db_path="/tmp/demo.db")
    event_id = await db.async_push({"tool": "my_tool"})
    recent = await db.async_get_recent(10)
    n = await db.async_count()
    await db.async_flush()
    db.shutdown()
    return event_id, len(recent), n

asyncio.run(main())
```

---

## Configuration

No manual configuration is required for normal installation: the
installer places files where Hermes discovers them
(`$HERMES_HOME/plugins/hermes_enhancer`) and Hermes needs no config
edits for file-based discovery. Runtime tunables are constructor
arguments (`db_path`, `prune_threshold`, `retention_days`,
`db_batch_size`, `db_flush_interval`, `db_max_queue`), not config files.
Environment overrides exist only for the installer/tests: `HERMES_HOME`
(custom Hermes home) and `SKIP_DISCOVERY=1` (skip the live CLI check).

---

## Testing

```bash
pip install pytest pytest-asyncio   # test dependencies only
python3 -m pytest tests -q
```

67 tracked tests across 13 test files: **59 core validation + 8 installer**
(the 8 installer tests execute the real scripts against fake
`HERMES_HOME`s and take ~10 s; they were run separately at release, not
in the same command as the 59).

| Suite | Tests | Proves |
|---|---|---|
| `test_pipeline.py` | 9 | sync/async insert, anomaly flags, prune, EMA |
| `test_v022_corruption.py` | 6 | health-check paths |
| `test_v022_malformed_buffer.py` | 6 | quarantine without loss |
| `test_v022_async.py` | 4 | async buffer integration |
| `test_v022_async_hook_regression.py` | 4 | asyncio import + timing/failure lock-in |
| `test_v022_concurrent_timing.py` | 6 | same/different-tool overlap, failure path, fallback |
| `test_v022_startup_recovery.py` | 5 | automatic bounded recovery |
| `test_v022_crash_recovery.py` | 4 | `os._exit` crash durability |
| `test_v022_failure_injection.py` | 6 | lock/tx/buffer/corrupt/dupe/worker |
| `test_v022_stress.py` | 5 | 10K seq + 10K conc + backlog + telemetry |
| `test_v022_storage_failure.py` | 3 | simulated ENOSPC + unwritable dir |
| `test_v022_multiprocess.py` | 1 | 4 processes x 1500 writes |
| `test_v022_installer.py` | 8 | setup/uninstall end-to-end (fake homes) |
| **Total** | **67** | **59 core + 8 installer** |

`tests/crash_child.py` is a crash/multiprocess child driver, not a test.
Note: bare root `pytest` also collects `src/self_test.py`, which fails
on a pre-existing absolute-import style issue — run `pytest tests`
instead. Two enterprise-loader tests require an installed plugin path.
Crash/stress suites take several minutes (real subprocesses, 10K+-row
workloads).

---

## Validation evidence

Source: `results/v0.22_*.md` (8 tracked files).

- Startup recovery: PASS (automatic, ≤100/invocation, idempotent)
- Crash durability: PASS (buffer-crash, post-commit crash, 120-event,
  repeated restart; 0 loss, 0 duplicates)
- Failure injection: PASS (all six classes + simulated storage)
- Stress: PASS (10K sequential, 10K concurrent, 500-backlog exact
  100-batches, burst, telemetry reconciliation)
- Multi-process: PASS (6000/6000 unique, integrity PASS)
- Storage simulation: PASS (labeled simulated; physical disk-full not
  tested)
- SQLite integrity: PASS (`quick_check` + `integrity_check` on all
  final states)
- Installer: PASS (8/8 incl. live install + CLI discovery)

Validated versus not validated: everything above was executed against
isolated databases. Power-loss/OS-crash durability was NOT validated;
physical disk-full was NOT directly tested — see [Failure
boundaries](#failure-boundaries).

---

## Performance and benchmarks

Measured on-device during v0.22 validation (observations, not
guarantees): 10K sequential pushes ≈ 39 events/s; 10K threaded pushes ≈
72 events/s; 4-process × 1500 ≈ 15 s wall; 10K-row database ≈ 10.8 MB;
RSS growth ≈ +1.5 MB across 10K events (bounded worker + bounded
recovery). No throughput targets are defined; durability was never
traded for speed.

`benchmarks/` holds the historical v0.20.7-era micro-benchmark suite
(`benchmark.py`, its own `README.md`, `requirements.txt`): hook
overhead, push latency/throughput, EMA/prediction/ingest/graph/self-test
timings, and memory observations. It measures the old component set and
was not re-run for v0.22 — the v0.22 numbers above come from the stress
suites and `results/v0.22_*.md`. See `benchmarks/README.md` for its own
usage (`python3 benchmarks/benchmark.py`).

---

## Security and privacy

Execution telemetry records tool names, arguments, timings, and outcomes
— treat the database (`~/.hermes/federated.db`) as sensitive operational
data: it may contain things you typed or referenced. Protect it with
filesystem permissions (single-user `~/.hermes`), be careful with
backups (installer backups retain old trees), shared/multi-user
machines, and anything you paste from logs. No telemetry leaves the
machine: the installer uses no network, the plugin makes no network
calls, and there is no credential handling. No encryption,
authentication, sandboxing, or network isolation is implemented — the
security model is local-file permissions only.

«Use Hermes Agent and its plugins only with systems/data you are
authorized to access.» Hermes Enhancer is not itself a security-testing
authorization mechanism.

---

## Failure boundaries

Tested (evidence in `results/v0.22_*.md`, isolated databases only):
normal process crash, restart recovery, buffered-event recovery,
duplicate protection, malformed-buffer handling, corruption detection,
simulated storage failure, multiprocess writes, 10K stress workloads.

Not validated / not guaranteed:

- **Power-loss / OS-crash durability NOT validated**
  (`synchronous=NORMAL`, no fsync). The tested boundary is normal
  process crash/restart only.
- **Physical disk-full NOT directly tested.** Deterministic injected
  storage failures verify fail-loud + retain-and-recover behavior.
- **Filesystem-level durability beyond tested process-crash scenarios**
  is not claimed; SQLite configuration is not converted into a stronger
  guarantee than the tests support.
- **Process-local telemetry.** Counters reset on restart; database
  state is the durable source of truth.
- **No universal exactly-once guarantee.** See [Duplicate
  protection](#duplicate-protection) for the tested wording.
- **Compatibility.** Tested against Hermes Agent CLI v0.21.0 only.
- **Arbitrary hardware/storage failure** is out of scope.

---

## Troubleshooting

**Plugin not discovered.** Check the files, the manifest, and live CLI
state (commands verified against Hermes v0.21.0):

```bash
ls ~/.hermes/plugins/hermes_enhancer/
cat ~/.hermes/plugins/hermes_enhancer/plugin.yaml
hermes plugins list --plain | grep hermes_enhancer
hermes plugins show hermes_enhancer
```

If the list lags a fresh install, wait ~1 minute and retry (the plugin
index refreshes asynchronously); file presence is authoritative.

**Installation failure.** Re-run `bash setup.sh` and read the step
output — it fails loudly (`ERROR`, nonzero exit) on missing files,
version mismatch, or verification failure, without leaving a
half-installed tree. Never copy files manually around it.

**Import failure.** From the plugin directory (flat layout, directory on
`sys.path`):

```python
import enhancer, federated_db, feedback_optimizer, predictive_preload
import meta_learner, skill_graph, composer, self_test
```

These are absolute intra-package imports resolving against the plugin
directory itself — not `src.*` submodules.

**Version mismatch.** Compare `cat plugin.yaml` (repo) with
`cat ~/.hermes/plugins/hermes_enhancer/plugin.yaml` (installed); both
must read `0.22.2`. Re-run `bash setup.sh` to reconcile.

---

## Uninstallation

```bash
bash uninstall.sh
```

Removes only `$HERMES_HOME/plugins/hermes_enhancer` (default
`~/.hermes/plugins/hermes_enhancer`) via `rm -rf` after a directory
check, then verifies removal. Timestamped install backups
(`.hermes_enhancer.bak.*`) are kept and listed for manual removal.
Other plugins, user data, configuration, Hermes itself, and
`~/.hermes/state.db` are never touched. Running it twice is safe
(second run reports `NOTHING TO REMOVE`).

---

## Repository structure

Tracked files only (`git ls-files`):

```text
hermes_enhancer/
├── plugin.yaml                  # name, version 0.22.2, hooks
├── README.md                    # this file
├── LICENSE
├── .gitignore
├── setup.sh                     # automatic installer (idempotent, verified)
├── uninstall.sh                 # scoped, idempotent remover
├── src/                         # 9 runtime modules (flat layout installed)
│   ├── __init__.py              # register(), get_instance()
│   ├── enhancer.py              # hooks + orchestration
│   ├── federated_db.py          # SQLite telemetry + v0.22 buffer/recovery
│   ├── feedback_optimizer.py
│   ├── predictive_preload.py
│   ├── meta_learner.py
│   ├── skill_graph.py
│   ├── composer.py
│   └── self_test.py
├── scripts/
│   └── stats.py                 # repo-only telemetry inspector (not installed)
├── tests/                       # 14 tracked files (13 test files + driver)
│   ├── crash_child.py           # crash/multiprocess child driver (not a test)
│   ├── test_pipeline.py         # 9 core regression
│   └── test_v022_*.py           # 12 suites, 58 tests
├── benchmarks/                  # v0.20.7-era micro-benchmark suite
│   ├── benchmark.py
│   ├── README.md
│   └── requirements.txt
└── results/                     # 8 tracked v0.22 validation documents
    ├── v0.22_architecture.md
    ├── v0.22_durability_validation.md
    ├── v0.22_recovery_matrix.md
    ├── v0.22_final_gap_audit.md
    ├── v0.22_release_checklist.md
    ├── v0.22_release_notes.md
    ├── v0.22_installer_validation.md
    └── v0.22_release_manifest.md
```

Local-only historical reports (intentionally untracked, not part of the
repository): `results/FINAL_VALIDATION_REPORT.md`, `results/REPORT.md`,
`results/V0.21.0_RELEASE_AUDIT.md`,
`results/comparison_v0.20.7_vs_v0.21.0.md`,
`results/v0.21.0_performance_refinement.md`.

Retired artifacts (removed during final cleanup; backups kept outside the
repository): `tests/test_enterprise_upgrade.py`, `tests/_test_worker.py`,
`test_benchmark_suite.py`, `setup_plugin.sh`, the root-level
`federated_db.py` bytecode copy, and the accidental `./~/` directory.

Other local-only files (untracked): `database/`, local
`results/baseline_*` / `benchmark.*` outputs, caches.

---

## Development

```bash
git clone https://github.com/Piyankali/hermes_enhancer.git
cd hermes_enhancer
pip install pytest pytest-asyncio
PYTHONPATH=src python3 -m pytest tests/test_pipeline.py -q
```

Conventions: deterministic edits, isolated `tmp_path` databases for
anything involving failure/corruption/recovery (never touch
`~/.hermes/state.db`), compile before test
(`python3 -m py_compile src/federated_db.py`), no overclaims without
automated evidence. This mission is documentation-only: `src/`,
`tests/`, scripts, and result artifacts must not be modified to fit the
docs.

---

## Roadmap

Genuinely future work (none of this is implemented or promised):

- Power-loss / OS-crash durability validation (fsync profiling, kill -9
  at sync points)
- Crash-persistent telemetry (surviving counters where correctness needs
  them)
- Configurable durability levels (e.g. NORMAL vs FULL synchronous modes)
- Broader Hermes-version compatibility matrix

v0.22 durability, recovery, idempotency, health checks, stress,
multi-process, and installer work are complete and validated — they are
not roadmap items.

---

## License

MIT — see [LICENSE](LICENSE).

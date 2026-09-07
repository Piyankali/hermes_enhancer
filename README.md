# Hermes Enhancer

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-v0.20.6%2B-green)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Termux-orange)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Tests](https://img.shields.io/badge/tests-49%20passed-brightgreen)

> **Self-healing telemetry middleware for Hermes Agent** — it records tool
> executions, learns from outcomes, predicts likely next tools, and (since
> v0.22.0) persists telemetry through a crash-safe event buffer with
> automatic startup recovery.

**Version:** `0.22.0`
**Status:** Beta (process-crash/restart durability validated; power-loss
durability is not claimed — see [Failure boundaries](#failure-boundaries--limitations))
**Python:** 3.10+
**License:** MIT

---

## Table of Contents

- [Why Hermes Enhancer?](#why-hermes-enhancer)
- [Feature overview](#feature-overview)
- [Architecture](#architecture)
- [Execution lifecycle](#execution-lifecycle)
- [Core components](#core-components)
- [Persistent telemetry (FederatedDB)](#persistent-telemetry-federateddb)
- [v0.22 crash-safe durability](#v022-crash-safe-durability)
- [Recovery model](#recovery-model)
- [Duplicate protection](#duplicate-protection)
- [Failure handling](#failure-handling)
- [Database health](#database-health--corruption-detection)
- [Telemetry and observability](#telemetry-and-observability)
- [Sync and async APIs](#sync-and-async-apis)
- [Learning systems](#learning-systems)
- [Anomaly protection](#anomaly-protection)
- [Retention and pruning](#retention-and-pruning)
- [Self-test and diagnostics](#self-test--diagnostics)
- [Installation](#installation)
- [Android / Termux installation](#android--termux-installation)
- [Upgrade](#upgrade-existing-installation)
- [Verify installation](#verify-installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Testing](#testing)
- [Validation evidence (v0.22.0 Beta)](#validation-evidence-v0220-beta)
- [Performance observations](#performance-observations)
- [Security and privacy](#security-and-privacy)
- [Failure boundaries / limitations](#failure-boundaries--limitations)
- [Troubleshooting](#troubleshooting)
- [Uninstallation](#uninstallation)
- [Repository structure](#repository-structure)
- [Development](#development)
- [Roadmap](#roadmap)
- [License](#license)

---

## Why Hermes Enhancer?

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

## Feature overview

- `pre_tool_call` / `post_tool_call` hooks into Hermes tool execution
- Persistent SQLite telemetry (`sync_queue`, WAL mode)
- v0.22 crash-safe event buffer with automatic bounded startup recovery
- Idempotent `event_id` persistence (retries return the existing row)
- Transient/permanent failure classification with bounded retries
- SQLite health checks that never delete data
- Malformed-record quarantine (diagnosed, preserved, never silently dropped)
- Markov-order-1 next-tool prediction with a 256 MB memory guard
- Skill graph, workflow composer, cross-session meta-learner
- EMA outcome scoring (`alpha = 0.2`), anomalous durations excluded
- Automatic retention pruning (5000-row threshold, 14-day retention)
- Bounded in-memory queue (5000) + background batch worker
- Automatic installer with verification (`setup.sh`), safe uninstaller

---

## Architecture

```text
                    Hermes Agent
                         |
                         v
                  Hermes Enhancer
                         |
        +----------------+----------------+
        |                                 |
  pre_tool_call                    post_tool_call
        |                                 |
        v                                 v
  PredictivePreload                  Telemetry capture
  MetaLearner                        FeedbackOptimizer
  SkillGraph                         FederatedDB
  SkillComposer                      Async batch worker
        |                            Retention pruning
        v                                 |
  Execution planning  <---  learning loop +
```

Planning (left) uses accumulated knowledge before a tool runs;
telemetry (right) records the outcome durably after it finishes, and the
learning loop feeds scores, relationships, and transitions back.

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
a hook event. `post_tool_call` records outcome and duration, flags
anomalies, updates the optimizer/learner/graph, and persists telemetry.

---

## Core components

| Component | Role |
|---|---|
| `enhancer.py` | Main integration: hooks, timing, anomaly flags, orchestration |
| `federated_db.py` | Persistent telemetry and execution storage (SQLite, WAL) |
| `feedback_optimizer.py` | EMA outcome scoring per tool (`alpha = 0.2`) |
| `predictive_preload.py` | Markov-order-1 next-tool prediction, 256 MB guard |
| `meta_learner.py` | Cross-session event ingest, pruning, summaries |
| `skill_graph.py` | Skill dependencies, topological order, DB persistence |
| `composer.py` | Workflow steps, conditional branches, retry steps |
| `self_test.py` | Config/file/signature diagnostics battery |

---

## Persistent telemetry (FederatedDB)

`FederatedDB` (`src/federated_db.py`) is a thread-safe SQLite store
(WAL mode, 5 s busy timeout). The `sync_queue` table holds one row per
telemetry event (payload, timestamp, node, tool, anomaly flag,
`event_id`, `trace_id`, `tool_call_id`).

Write paths:

- **Hook path** (`pre/post_tool_call`): in-memory bounded queue (default
  5000) → background worker batch (default 100 rows / 0.25 s) → SQLite.
  `flush()` blocks until the queue drains; `shutdown()` drains on exit.
- **Direct API path** (`push` / `async_push`): crash-safe buffered path
  (next section). Synchronous `push` commits inline; `async_push`
  buffers durably, then queues for the worker.

Retention: when `sync_queue` exceeds 5000 rows, rows older than 14 days
are aggregated into `summary_analytics` and deleted, automatically on
writes; `prune()` does the same on demand. Both threshold and retention
are constructor-configurable.

---

## v0.22 crash-safe durability

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
States observed: `CREATED`, `PERSISTED`, `FAILED` (plus `QUEUED`,
`PERSISTING`, `RETRY_WAIT` handled by recovery).

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
Automatic startup recovery (bounded batch)
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
observable via `get_startup_recovery_info()`.

---

## Duplicate protection

Recovery is designed and tested so that a confirmed durably buffered
event is not permanently lost across the tested normal process
crash/restart scenarios, while already-persisted events are protected
against duplicate final persistence through event identity/idempotency.

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

Classification (see `_with_retry` / `_flush_buffer`): transient SQLite
busy/locked `OperationalError`s are retried with backoff (`0 s, 0.5 s,
1.5 s`); permanent and unknown errors increment `permanent_errors` /
`unknown_errors` and surface instead of retrying forever. Buffer-write
failures are fail-loud — `push`/`async_push` raise rather than silently
bypassing durability. Corrupt payloads fail JSON validation and are
marked `FAILED` with a `corrupt_payload:` diagnosis; the row is kept,
unknown-state rows are left untouched, and healthy rows keep flowing.

---

## Database health / corruption detection

`verify_database()` returns
`{healthy, quick_check, integrity_check, error, database}` using
`PRAGMA quick_check` plus the deeper `PRAGMA integrity_check`.
Corruption is detected and surfaced; the database is never silently
deleted, recreated, or repaired. Inaccessible paths report
`OPERATIONAL_ERROR` without crashing the caller.

---

## Telemetry and observability

Every event carries `event_id`, `trace_id`, `tool_call_id`, an event
type, and a JSON payload. `get_recent(limit)` / `count()` /
`summary_counts()` query final state; `get_buffer_event()` /
`get_buffer_summary()` (`{total, per-state counts}`) inspect the buffer.

Counters (`get_counters()`): `events_enqueued` counts queue admissions
(async path only); `events_persisted` counts final commits (sync +
worker); `events_dropped` counts queue refusals (`enqueue` returns
`None`); `events_failed` counts failed worker batches; `events_retried`
and `transient/permanent/unknown_errors` count operations, not events.
All counters are **process-local**: they reset on restart. SQLite state
is the durable source of truth. Recovery reports per-invocation dicts
`{recovered, skipped, states_seen}` instead of inventing persistent
counters.

---

## Sync and async APIs

v0.21 APIs, signatures unchanged:

- `push(payload, node_id="local") -> int` — crash-safe sync persist;
  returns the row id (existing id when the `event_id` was already
  persisted). Raises loudly on buffer-write failure.
- `get_recent(limit=100) -> list[dict]`, `count() -> int`
- `async_push(payload, node_id="local") -> Optional[str]` — durable
  buffer commit, then worker queue; returns the `event_id`, or `None`
  if the bounded queue (or shutdown) refused it.
- `flush(timeout=5.0) -> dict` — block until the queue drains.

v0.22 buffer/recovery APIs:

- `enqueue_to_buffer(event_id, trace_id, tool_call_id, event_type, payload) -> bool`
- `mark_buffer_persisted(event_id) -> bool`
- `get_buffer_event(event_id) -> dict | None`
- `get_buffer_summary() -> dict` (`total` plus per-state counts)
- `recover_buffered_events() -> dict` (`recovered`, `skipped`, `states_seen`; ≤100 rows)
- `verify_database() -> dict`
- `get_startup_recovery_info() -> dict`
- Helpers: `get_event()`, `get_counters()` / `get_event_counters()`,
  `prune()`, `summary_counts()`, `shutdown()`, plus `async_*` variants.

---

## Learning systems

- **SkillGraph** (`skill_graph.py`): skill registration with
  dependencies and metadata, dependency lookup, topological ordering,
  summaries, and SQLite persistence (`save_to_db` / `load_from_db`).
- **SkillComposer** (`composer.py`): ordered steps with payloads,
  conditional branches, and retry steps (configurable retries/backoff).
- **MetaLearner** (`meta_learner.py`): cross-session event ingest with
  bounded history (`prune(keep_last=1000)`) and summaries.
- **FeedbackOptimizer** (`feedback_optimizer.py`): EMA outcome scoring
  with `alpha = 0.2`; anomalous durations are excluded from scoring.
- **PredictivePreload** (`predictive_preload.py`): Markov-order-1
  transition recording and top-k next-tool prediction, with RSS-based
  auto-cleanup (256 MB default). Transition statistics, not neural
  models.

---

## Anomaly protection

Durations `<= 0` and `> 300` seconds are flagged anomalous
(`enhancer._flag_anomalous`, `feedback_optimizer._is_anomalous_duration`)
and excluded from EMA optimization while still being persisted with
their anomaly flag for later analysis.

---

## Retention and pruning

Defaults (constructor-configurable): prune threshold 5000 rows,
retention 14 days. When the threshold is exceeded, sub-cutoff rows are
aggregated per tool/day into `summary_analytics`, then deleted. Pruning
runs automatically on the write path and on demand via `prune()`, which
reports before/after counts.

---

## Self-test / diagnostics

`SelfTestEngine` (`src/self_test.py`) runs a diagnostics battery:
config validation, file/directory checks, and function-signature tests,
with per-check timing and a summary. `HermesEnhancer.run_self_test()`
exposes it; `scripts/stats.py` prints telemetry summaries from a local
database for development inspection.

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
Verify installation (files, version 0.22.0, byte-compile out of tree)
      ↓
Verify Hermes plugin discovery (hermes plugins list)
      ↓
Plugin ready
```

The installer checks `plugin.yaml` reports `0.22.0`, stages the nine
`src/*.py` modules plus `plugin.yaml` and `README.md` (no `.git`,
caches, tests, or temp files), and fails loudly (`ERROR`, nonzero exit,
no half-installed tree) when any check fails. It never touches
`~/.hermes/state.db`, never runs `hermes doctor --fix`, and never edits
`~/.hermes/config.yaml` — no manual configuration is required.

---

## Android / Termux installation

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

## Upgrade (existing installation)

```bash
cd hermes_enhancer
git pull
bash setup.sh
```

Re-running is safe by design: the installer detects the existing tree,
moves it to a timestamped backup (never merged, never deleted), and
installs fresh. There is no automatic rollback; restore manually from
the backup directory printed during installation if needed.

---

## Verify installation

```bash
hermes plugins list --plain | grep hermes_enhancer
hermes plugins show hermes_enhancer
cat ~/.hermes/plugins/hermes_enhancer/plugin.yaml   # version: 0.22.0
ls ~/.hermes/plugins/hermes_enhancer/                # 9 .py modules + plugin.yaml + README.md
```

`setup.sh` already performs all of these checks and prints
`Plugin installation: PASS` / `Plugin discovery: PASS` /
`Plugin version: 0.22.0` — only those lines count as verified success.

---

## Usage

Attach the hooks in your Hermes tool flow (see `src/enhancer.py`):

- `pre_tool_call(args, kwargs)` captures state/timing and returns
  resume markers (`_enhancer_t0`, `_enhancer_tool`, …).
- `on_post_tool_call(...)` records outcome, duration, and anomaly flags.

Or persist records directly with crash-safe durability:

```python
from federated_db import FederatedDB
db = FederatedDB()  # automatic bounded startup recovery runs here
db.push({"tool": "my_tool", "hook": "post_tool_call", "success": 1})
await db.async_push({"tool": "my_tool", "hook": "pre_tool_call"})
db.flush()
info = db.get_startup_recovery_info()  # {"executed": True, ...}
db.shutdown()  # drains the worker queue
```

---

## Configuration

No manual configuration is required for normal installation: the
installer places files where Hermes discovers them
(`$HERMES_HOME/plugins/hermes_enhancer`) and Hermes needs no config
edits for file-based discovery. Runtime tunables are constructor
arguments (`db_path`, `prune_threshold`, `retention_days`,
`db_batch_size`, `db_flush_interval`, `db_max_queue`), not config files.

---

## Testing

```bash
pip install pytest pytest-asyncio   # test dependencies only
python3 -m pytest tests -q
```

Focused v0.22 suites (49/49 PASS at release):

| Suite | Tests | Proves |
|---|---|---|
| `test_v022_corruption.py` | 6 | health-check paths |
| `test_v022_malformed_buffer.py` | 6 | quarantine without loss |
| `test_v022_async.py` | 4 | async buffer integration |
| `test_v022_startup_recovery.py` | 5 | automatic bounded recovery |
| `test_v022_crash_recovery.py` | 4 | os._exit crash durability |
| `test_v022_failure_injection.py` | 6 | lock/tx/buffer/corrupt/dupe/worker |
| `test_v022_stress.py` | 5 | 10K seq + 10K conc + backlog + telemetry |
| `test_v022_storage_failure.py` | 3 | simulated ENOSPC + unwritable dir |
| `test_v022_multiprocess.py` | 1 | 4 processes x 1500 writes |
| `test_pipeline.py` | 9 | core regression |
| `test_v022_installer.py` | 8 | setup/uninstall end-to-end (fake homes) |

Note: run from `tests/` scope; bare root `pytest` also collects
`src/self_test.py`, which fails on a pre-existing absolute-import style
issue, and two enterprise-loader tests require an installed plugin path.
Crash/stress suites take several minutes (they use real subprocesses
and 10K+-row workloads).

---

## Validation evidence (v0.22.0 Beta)

- Startup recovery: PASS (automatic, ≤100/invocation, idempotent)
- Crash durability: PASS (buffer-crash, post-commit crash, 120-event,
  repeated restart; 0 loss, 0 duplicates)
- Failure injection: PASS (all six classes + simulated storage)
- Stress: PASS (10K sequential, 10K concurrent, 500-backlog exact
  100-batches, burst, telemetry reconciliation)
- Multi-process: PASS (6000/6000 unique, integrity PASS)
- Storage simulation: PASS (labeled simulated; physical disk-full not tested)
- SQLite integrity: PASS (`quick_check` + `integrity_check` on all final states)
- Installer: PASS (8/8 incl. live install + CLI discovery)

Validated versus not validated: everything above was executed against
isolated databases. Power-loss/OS-crash durability was NOT validated;
physical disk-full was NOT directly tested — see limitations.

---

## Performance observations

Measured on-device during validation (observations, not guarantees):
10K sequential pushes ≈ 39 events/s; 10K threaded pushes ≈ 72 events/s;
4-process × 1500 ≈ 15 s wall; 10K-row database ≈ 10.8 MB; RSS growth
≈ +1.5 MB across 10K events (bounded worker + bounded recovery).
No throughput targets are defined; durability was never traded for speed.

---

## Security and privacy

Execution telemetry records tool names, arguments, timings, and outcomes
— treat the database as sensitive operational data: it may contain
things you typed or referenced. Protect it with filesystem permissions
(single-user `~/.hermes`), be careful with backups (installer backups
retain old trees), shared/multi-user machines, and anything you paste
from logs. The installer uses no network, no `sudo`, no remote code, and
no credential handling.

«Use Hermes Agent and its plugins only with systems/data you are
authorized to access.» Hermes Enhancer is not itself a security-testing
authorization mechanism.

---

## Failure boundaries / limitations

- **Beta status.** v0.22.0 validates process-crash/restart durability,
  not production hardening in every environment.
- **Workload-dependent performance.** Throughput varies with storage
  speed and contention; SQLite busy-timeout plus retry absorbs bursts.
- **Power-loss / OS-crash durability NOT validated**
  (`synchronous=NORMAL`, no fsync). The tested boundary is normal
  process crash/restart only.
- **Physical disk-full NOT directly tested.** Deterministic injected
  storage failures verify fail-loud + retain-and-recover behavior.
- **Process-local telemetry.** Counters reset on restart; database
  state is the durable source of truth.
- **No universal exactly-once guarantee.** See [Duplicate
  protection](#duplicate-protection) for the tested wording.
- **Compatibility.** Tested against Hermes Agent CLI v0.21.0
  (`pre_tool_call` / `post_tool_call` hooks, `$HERMES_HOME/plugins`
  file discovery). Behavior on other Hermes versions is not verified.
- **Test environment notes.** Root-collected pytest includes a
  pre-existing `src/self_test.py` import-style failure; run
  `pytest tests` instead.

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

These use absolute intra-package imports, so they resolve when the
plugin directory itself is importable — not as `src.*` submodules.

**Version mismatch.** Compare `cat plugin.yaml` (repo) with
`cat ~/.hermes/plugins/hermes_enhancer/plugin.yaml` (installed); both
must read `0.22.0`. Re-run `bash setup.sh` to reconcile.

---

## Uninstallation

```bash
bash uninstall.sh
```

Removes only `$HERMES_HOME/plugins/hermes_enhancer` (default
`~/.hermes/plugins/hermes_enhancer`). Timestamped install backups are
kept and listed for manual removal. Other plugins, user data,
configuration, and `~/.hermes/state.db` are never touched. Running it
twice is safe (second run reports nothing to remove).

---

## Repository structure

```text
hermes_enhancer/
├── plugin.yaml                  # name, version 0.22.0, hooks
├── README.md                    # this file
├── setup.sh                     # automatic installer (idempotent, verified)
├── uninstall.sh                 # scoped, idempotent remover
├── src/                         # 9 runtime modules (flat layout installed)
│   ├── __init__.py
│   ├── enhancer.py              # hooks + orchestration
│   ├── federated_db.py          # SQLite telemetry + v0.22 buffer/recovery
│   ├── feedback_optimizer.py
│   ├── predictive_preload.py
│   ├── meta_learner.py
│   ├── skill_graph.py
│   ├── composer.py
│   └── self_test.py
├── scripts/
│   └── stats.py                 # dev telemetry inspection helper
├── tests/                       # pipeline + enterprise + v0.22 suites
│   ├── crash_child.py           # crash/multiprocess child driver (not a test)
│   └── test_v022_*.py
├── benchmarks/                  # benchmark.py + notes
└── results/
    ├── v0.22_architecture.md
    ├── v0.22_durability_validation.md
    ├── v0.22_recovery_matrix.md
    ├── v0.22_final_gap_audit.md
    ├── v0.22_release_checklist.md
    ├── v0.22_release_notes.md
    ├── v0.22_installer_validation.md
    └── v0.22_release_manifest.md
```

---

## Development

```bash
git clone https://github.com/Piyankali/hermes_enhancer.git
cd hermes_enhancer
pip install pytest pytest-asyncio
python3 -m pytest tests/test_pipeline.py -q
```

Conventions: deterministic edits, isolated `tmp_path` databases for
anything involving failure/corruption/recovery (never touch
`~/.hermes/state.db`), compile before test
(`python3 -m py_compile src/federated_db.py`), no overclaims without
automated evidence.

---

## Roadmap

Genuinely future work (none of this is implemented or promised):

- Power-loss / OS-crash durability validation (fsync profiling, kill -9
  at sync points)
- Crash-persistent telemetry (surviving counters where correctness needs them)
- Configurable durability levels (e.g. NORMAL vs FULL synchronous modes)
- Broader Hermes-version compatibility matrix

v0.22 durability, recovery, idempotency, health checks, stress,
multi-process, and installer work are complete and validated — they are
not roadmap items.

---

## License

MIT — see [LICENSE](LICENSE).

# Hermes Enhancer v0.20.7 Benchmark Report

- Timestamp (UTC): `2026-09-03T18:28:17Z`
- Python: `3.13.13`
- Platform: `Android-16-aarch64-64bit-ELF`
- Iterations: `100`
- Warmup: `10`

## Headline measurements

| Area | Metric | Result |
|---|---|---:|
| Hook path | Baseline mean | 1.780 µs |
| Hook path | Enhanced mean | 378.852 µs |
| Hook path | Mean overhead | 377.071 µs |
| Hook path | P95 overhead | 753.593 µs |
| DB | Sync push mean | 5888.541 µs |
| Preload | Top-1 correct | True |
| Preload | Top-1 probability | 1.000 |
| Feedback | Anomaly excluded | True |
| Memory | Positive allocated bytes | 57851 |

## Important interpretation

These are measurements of the implementation in the checked-out repository, not claims about every Hermes workload. The hook benchmark compares a minimal no-op baseline with the actual v0.20.7 pre/post hook path; it does not measure LLM inference time or the complete Hermes runtime.

## Detailed results

See the JSON file for the complete machine-readable result set.

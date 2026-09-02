# RepoPilot benchmark suite

This intentionally buggy Python repository contains ten isolated maintenance tasks covering
arithmetic, text normalization, boundary handling, pagination, stable deduplication, identity
normalization, retry policy, date ranges, configuration parsing, and tenant-safe cache keys.

Each evaluation case runs only its named test module, so cases remain independent while sharing a
single realistic repository tree. The deterministic mock model provides an offline regression
baseline; results from a real model must be reported separately with the provider and model name.

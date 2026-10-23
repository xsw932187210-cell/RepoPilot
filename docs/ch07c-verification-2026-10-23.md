# CH-07C verification — 2026-10-23

Verified source: `3615d6b3a23b8121612a12fb6cbcf88fe5707826`, implementation
`7341a2e`, based on PR #13 merge `58d581bdc26868e1e7eda752d2df971ad98fce61`.
Status is VERIFIED, with no CH-07C push or PR at the freeze checkpoint.

| Check | Observed result |
| --- | --- |
| Full host Python 3.13.5 | 470 passed / 42 service-conditional skips, 86.23%, 61.79 s |
| Isolated Linux Python 3.12.15 | 512 passed / zero failures, errors or skips, 87.56%, 186.18 s |
| Mock evaluation | 10/10, with all human approval gates reached |
| API smoke | Completed, 3 Mock requests, zero unknown requests |
| Worker restart at approval | Same approval, candidate, complete manifest and test evidence; completed with 3 requests total and zero unknown requests |
| Installed source identity | 104 source/test files match the fixed commit; test and API packages each contain 54 matching Python files |
| Dedicated resource cleanup | Containers, volumes and network removed; images retained |
| Real model inference | Zero for this card; CH-01C has not started |

The final isolated command was:

```sh
CH07C_COMPOSE_PROJECT=repopilot_ch07c_20261023 \
CH07C_API_PORT=58120 \
CH07C_DEPENDENCY_IMAGE=repopilot-model-deps:20261023 \
sh scripts/ch07c_integration.sh
```

Do not rerun this command merely to resume development. It is the historical
acceptance command; a future deliberate rerun should choose a fresh project and
report directory. The first wrapper invocation rejected the CH-07C card before
creating resources. Commit `3615d6b` adds the explicit card/project allowlist and
required test database; the successful final run uses that source.

The isolated suite covers independent repeats, immutable configuration identity,
rotating order, global and per-unit ceilings, retry reservation, concurrent-writer
rejection, missing/corrupt ledgers, and real subprocess exits before/after request
start. Statistics cases include all-success, all-failure, pending/missing units,
incomplete usage, orphan reservations, mixed identities and a one-project sample.
The synthetic [arithmetic fixture](evidence/2026-10-23-ch07c-hand-computable.json)
is separate from Mock service acceptance and from real-model experiments.

Contract and assumptions: [repeated evaluations](repeated-evaluations.md).
The [machine summary](evidence/2026-10-23-ch07c-verification.json) records immutable
image IDs, source hashes and artifact checksums. Raw local evidence is ignored by
Git in `reports/repopilot_ch07c_20261023/`, `reports/ch07c-container-final.log`,
`reports/ch07c-host.*` and `reports/ch07c-hand-computable.md`. Earlier failed
wrapper output remains in `reports/ch07c-container.log`.

The 2026-09-09 real report, failure analysis/ledger and Mock dataset remain
byte-identical to `b7cf1c9`. No new repair success rate, model gain or exact dollar
cost is claimed. The three-case/two-project holdout remains too small for broad
quality conclusions. CH-01B routing and CH-01C real comparison are not implemented
by this card; the separately approved 108-attempt budget is unconsumed.

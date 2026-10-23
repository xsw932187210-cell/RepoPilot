# CH-01B verification — 2026-10-23

Routing implementation: `c0f8de0`, fixture-default correction: `06f656b`.
Consolidated installed-image acceptance source:
`4b6f7c93f21d14979179023d48dc44c99aa14272` (CH-01B plus CH-01C offline protocol
preparation). The routing, graph, API, DB, Worker, model/control and public-data
files are identical to the CH-01B implementation. The later CH-01C execution-limit
binding changes only its protocol guard/configuration and is checked separately;
it is not presented as a rerun of this service suite.

| Check | Observed result |
| --- | --- |
| Isolated Linux Python 3.12.15 / PostgreSQL 16.15 | 538 passed / zero failures, errors or skips, 87.20%, 191.31 s |
| API → queue → Worker → Docker | oneshot/auto/workflow persist their strategy, test candidates, wait for bound human approval; rejection cancels without publication |
| Upgrade boundaries | visible empty/policy-rejected first candidate upgrades once on the same ledger; insufficient budget stops after one request; explicit one-shot does not retry |
| Migration | `0007 → 0008` preserves old workflow rows and round-trips auto; prior legacy migrations/recovery pass in the same suite |
| Mock benchmark | 10/10 |
| API smoke | Completed; default workflow, 3 requests |
| Approval-checkpoint Worker restart | workflow 3→3, oneshot 1→1; same candidate/manifest/evidence/approval, unknown=0 |
| Source identity | 111 source/test Python files and test/API packages each with 57 Python files match the installed-image acceptance commit |
| Cleanup | Dedicated containers, volumes and network removed; images retained |
| Real model requests for CH-01B | 0 |

Command:

```sh
CH01B_COMPOSE_PROJECT=repopilot_ch01b_20261023 \
CH01B_API_PORT=58130 \
CH01B_DEPENDENCY_IMAGE=repopilot-model-deps:20261023 \
sh scripts/ch01b_integration.sh
```

The final run reused installed dependencies and reinstalled the current package;
there was no new third-party dependency for routing. Tests and services use Mock,
with real provider credentials absent and GitHub publication disabled. Docker
execution boundaries, cancellation, policy correction, bound approvals and
publication HTTP mocks remain covered. Admission checks do not add Worker fencing
or guarantee an entire multi-call upgrade can complete after transport retries.

The first focused host run was 57 passed / one fixture assertion failure (a missing
optional zero-valued counter). The assertion was corrected and all 538 cases
passed in the consolidated Linux run; the earlier log remains retained. An initial
sandbox-denied Docker invocation did not start services and is kept separately.
No full host suite was repeated, following the user's request to finish code and
concentrate end-to-end validation.

Contract: [task routing](task-routing.md). Machine evidence:
[2026-10-23-ch01b-verification.json](evidence/2026-10-23-ch01b-verification.json).
Raw local logs are ignored in `reports/repopilot_ch01b_20261023/`,
`reports/ch01b-container.log` and `reports/ch01-routing-protocol-offline.log`.
These establish engineering behavior, not a real-model repair-rate improvement.
Historical 2026-09-09 results and failure records remain unchanged. CH-01C has its
own predeclared model/configuration identity and 108-attempt authorization.

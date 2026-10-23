# CH-01C interrupted pilot — 2026-10-23

This is an incomplete diagnostic run, not a completed strategy comparison.
The exact source was `93079302b7f6ed4dfa24eb89028bfa8f5c77826b` (implementation
SHA-256 `271bafdd373e12431465ef706b09de306ada30be647c2f58e4fa4bf6655ef3de`),
with the predeclared `ch01c-pilot-v1.json` and plan fingerprint
`25a007cc711e54ed554d5b7d6b716eb079f7553654cf10ec8723745cf3a8fd15`.

The three cases all passed the separate buggy/fixed reproduction before inference.
Of 18 selected defect/strategy/repeat units, six completed generation/acceptance,
three failed before inference, and nine remain pending after provider rate limiting.
No accepted repair was observed among the six completed units. Incomplete pairs
and an infrastructure-incompatible case prevent a meaningful strategy winner.

The durable ledger records **30/108 reservations and starts**, including five
transport retries: Google Cloud 22 (17 Coder, 5 Planner), ChatGPT 8 Reviewer.
There are no orphan reservations. The original counters/results are retained;
the remaining total authorization is 78 requests, not a new 108-request budget.
The pending second-repeat tqdm/workflow unit already consumed its six-call unit
ceiling and cannot gain free retries through resume.

The zero-inference failures for Cookiecutter case 3 exposed a mismatch between the
exported benchmark tree and artifact initialization. The upstream commit contains
a Git submodule entry at `docs/HelloCookieCutter1` (mode 160000). The history-free
`git archive` export does not include its checkout, while the general artifact base
reader correctly refuses non-blob Git entries. Reproduction alone did not exercise
that graph path. It is an evaluator preparation failure, not evidence that the
model failed to fix this case, and does not invalidate the observed buggy/fixed
focused test reproduction. The corpus documentation must distinguish these claims.

A corrected evaluator will construct its offline artifact baseline from the exact
already exported buggy files, outside the Agent workspace, with no submodule fetch,
history or hidden overlays. Online raw-Git publication and artifact link rejection
remain strict. This runtime correction requires a new experiment identity; the
old results cannot be relabeled or silently combined with a new implementation.
No repair prompt, routing threshold, retrieval budget or expected answer is tuned
using these results.

Public partial evidence is
[2026-10-23-ch01c-v1-interrupted.json](evidence/2026-10-23-ch01c-v1-interrupted.json).
Private/raw records remain in `reports/ch01c-holdout-20261023-v1/` and
`.local/evaluation-authorization/ch01c.json`. Unknown token usage and dollar cost
remain unknown. The 2026-09-09 historical quality results are unchanged.

This interrupted v1 experiment remains incomplete and is never resumed or mixed
with corrected results. On 2026-10-23 the user explicitly approved a separate v2
experiment using only the remaining 78 attempts, with the original 30 retained and
each new unit capped at six. Its separate authorization supersedes the old grant
without clearing its reservation IDs; see the [corrected plan](ch01c-corrected-plan-2026-10-23.md).

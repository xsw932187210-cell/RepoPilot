# Repeated evaluation contract

CH-07C adds `repeat-evaluation-v1` and aggregate report schema 3. Legacy evaluation
records and report schema 2 remain readable; historical results are not rewritten.
The real evaluator is version 12. `auto` is reserved for CH-01B and is not accepted
by the current CLI.

Each cohort fixes its run ID, strategy version, selected cases, repeat count,
model/runtime policy and rotating order in its fingerprint. Each repeat has its
own records under `runs/<run_id>/repeat-NNN/<mode>/cases/`; incompatible resumes
fail before another request. The entire cohort has a nonblocking OS file lock.
The example in `evals/real/experiments/repeated-template.json` is a two-strategy
configuration illustration, not an authorization or the final CH-01C plan.

Every transport attempt, including retries, reserves the shared cohort budget
durably before transport. Per-case/strategy/repeat limits also apply. A crash
between the shared reservation and local record update conservatively consumes
budget; an orphan does not become a free retry. Missing/truncated ledgers and
identity mismatches stop execution. Completed records are reused, quota-pending
units can resume, and earlier usage is retained. Reservation and transport-start
counts differ when a crash interrupts their handoff.

Reports distinguish unique defects/projects from execution units and display
selected, completed, failed and pending denominators. Executor attempts, API
reservations/starts, scheduled transport retries, Coder iterations and approval
gates have separate columns. Missing usage is unknown; observed counts remain
available. Orphan reservations invalidate exact token totals, including affected
strata. Money remains null. Duration uses cumulative observed execution history;
an unobserved crashed duration cannot be reconstructed.

Paired effects are **second strategy minus first strategy**, with repetitions
averaged within each defect. A pair requires terminal records for both strategies
in every repeat; incomplete pairs are excluded explicitly and remain visible in
the selected denominator. Projects are resampling clusters: at most six projects
use exact ordered cluster resampling, otherwise 5,000 resamples use statistical
seed 7. The 95% interval uses outward order statistics. Fewer than two projects
produce no interval; equal endpoints are flagged as degenerate. These are
descriptive intervals for a purposive sample, not evidence of generalization.
The statistics seed is not a provider seed; no provider sampling seed is sent.

Project and difficulty strata are explicit. Absent trusted difficulty labels are
`unlabeled`; answer files and hidden outcomes cannot supply labels or routing
features. Public case summaries omit hidden identities, raw patches and model
text. Frozen-corpus selection rejects cases outside the declared partition.

The [hand-computable fixture](evidence/2026-10-23-ch07c-hand-computable.json)
contains two synthetic defects in two projects, two strategies and two repeats:
eight execution units, four successes, mean paired effect 0 and interval [-1, 1].
It validates arithmetic only. The [verification record](ch07c-verification-2026-10-23.md)
records runtime and recovery checks; neither artifact measures real-model repair
quality.

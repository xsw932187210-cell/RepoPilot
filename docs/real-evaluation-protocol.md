# Real-defect evaluation protocol

The original protocol is a small, one-project pilot, **not** SWE-bench or evidence of
general repair performance: 20 pinned BugsInPy defects from The Fuck.
Corpus metadata and third-party test licenses are under `evals/real/`.

CH-07B separately freezes `python-pilot-v1`: 23 reproduced cases / three projects,
with the original 20 as development and three project-disjoint holdout cases.
See [freeze and contamination rules](real-task-corpus.md#frozen-cross-project-pilot-v1).
This adds no model-quality result or replacement for the historical experiment identity.

The current implementation uses manifest schema **2**, evaluator configuration **14**,
acceptance protocol **4** and `withheld-junit-v2`. Trusted project adapters declare source
roots, read-only/test paths and immutable runtime images. The original corpus permits
only existing `thefuck/**/*.py` files; the cross-project pilot uses each trusted adapter's
source capabilities. The separate `tinyledger` fixture is
synthetic and contributes no real repair results. See the [adapter and migration
contract](project-adapters.md). New experiments require a new output directory.

Historical version 8 changed only evidence persistence: raw patches, commands, process output and per-test
identities are removed before checkpoints are written, secret redaction is centralized, and
latency excludes unattempted quota placeholders. It does not change prompts, retrieval, candidate
execution or the acceptance decision. The published 2026-09-09 comparison was captured under
version 7 immediately before this storage hardening. Version 9 adds explicit project/environment
binding and refuses to write old directories; it does not rerun or replace that historical report.

Version 11 binds native Cloud and ChatGPT role profiles, version 12 adds independent
repeats, and version 13 sends all three strategies through the same graph gates.
Version 14 initializes private artifact baselines from the exact exported buggy
files and preflights every selected case before inference. This handles omitted
upstream gitlinks without fetching submodules or relaxing online raw-Git checks.
Baselines live outside Agent/test workspaces; benchmark publication stays disabled.

CH-10 separately versions the model-call checkpoint envelope as evaluation schema `4` with
`evaluation-call-budget-v2`. It preserves reserved/started/outcome counters across process restart,
classifies a possibly sent request without a persisted response as unknown, and fingerprints the
timeout, output, retry, and cumulative-backoff rules. Schema-3 directories and the published
evaluator-v7 results remain historical records; do not resume or aggregate them as schema 4.

## Reproduce and resume

From the repository root (Docker required):

```bash
docker build --target runtime -t repopilot-api:latest .
docker build -f Dockerfile.eval-tools -t repopilot-eval-tools:local .
docker build -f Dockerfile.real-tasks -t repopilot-real-tasks:local .
git clone https://github.com/nvbn/thefuck.git reports/real-corpus-cache/thefuck
make eval-real-reproduce
# After configuring the OpenAI-compatible provider in the ignored .env:
make eval-real
```

Repeat the identical command to resume. Completed pairs are reused; pending quota
pairs are attempted again. Configuration changes require a separate output directory
(the Python CLI supports `--output`). `--case thefuck-001` can select a pilot, but
changing the selection in the same model-record directory is deliberately rejected.
Use `--model <exact-provider-model-id>` with a new output directory to compare a
different available model without editing the credential-bearing `.env` file.
No dependency installation occurs in model-controlled workspaces. Each adapter pins an immutable
image; an optional image alias must resolve to that same local ID. The ID and adapter fingerprint
are included in experiment identity. Rebuilding the recipe requires an explicit manifest image
update and a new output directory. The base
Python digest and direct Python dependency versions are pinned in the Dockerfile;
OS/transitive dependency rebuilding can still produce a different image ID.
Rate-limit and retryable HTTP/transport failures use bounded per-request backoff; every
attempt consumes the same explicit call budget, request timeout and cumulative wait are bounded,
and SDK-level hidden retries are disabled. Exact provider/model and adapter/request-schema identity
are recorded per attempt. No automatic fallback is configured; a deliberate fallback must be
explicitly marked and evaluated under a separate experiment identity.

## Leakage boundary

- The trusted orchestrator can read upstream history and benchmark metadata.
  Agent context receives only an archive of the buggy commit, without `.git`,
  reference patches, fixed source, or the hidden overlay.
- All selected strategies initially retrieve from the same issue text, using the same 12-file /
  70,000-character limits. Planner search terms are deliberately disabled in this
  experiment to hold initial retrieval constant. Thus this comparison tests role
  planning/review/retry, **not retrieval gain**. A retrieval ablation remains separate.
- Only existing source files permitted by the trusted project adapter can be edited;
  for the original The Fuck corpus this is `thefuck/**/*.py`. Tests/config remain read-only.
  Readable context and edit capabilities are separate: selected tests may be shown for
  reasoning, but only allowed source paths appear in the model's edit-path contract.
  A generated edit batch is completely preflighted before any write. The workflow may
  return one policy denial to Coder as a bounded Observation; a second violation stops
  the pair. The one-shot baseline remains a single generation with no correction loop.
  After generation, a fresh buggy export receives only these source changes, followed
  by the evaluator-owned fixed-version tests. Hidden results never feed retry prompts.
- `expected_fix_files` is metadata, not an oracle path restriction or success metric.
  A valid alternative implementation can modify a different source file.

## Comparison and measurement

Since evaluator v13, `oneshot` calls the same coder once, with a fixed minimal plan,
then uses deterministic permissions, visible-test and human-approval gates without
model planning/review. `workflow` uses the
actual LangGraph planner, coder, visible-test runner and reviewer, with up to two coder
iterations. Deterministic rejection triggers a retry. After tests pass, a concrete
medium/high-risk reviewer finding may trigger one bounded remediation, while low-risk or
test-coverage-only concerns go directly to HITL; the second candidate is always escalated.
`auto` selects one of these paths from first visible retrieval, with at most one
budgeted upgrade after a visible one-shot failure; see [task routing](task-routing.md).
All strategies share primary/Reviewer profiles where those roles are present, corpus,
context limits, project Docker images and acceptance suites. The frozen CH-01C plan
caps each defect/strategy/repeat at six requests, including failures and retries. Their
**actual costs need not be equal**. Report those costs alongside outcomes; do not
describe this as an equal-token experiment. Failed provider attempts consume the cap.

The [2026-10-23 corrected CH-01C result](real-evaluation-results-2026-10-23.md) uses
the opt-in `repopilot.eval_protocol` wrapper and `ch01c-pilot-v2.json`, with a separately
confirmed 78-attempt grant after preserving the original 30. It finished 18 units,
each strategy observed 0/6 accepted repairs, and combined use was 101/108. The wrapper's
default is read-only planning; `--run` requires an existing bound private grant.
A completed same-directory resume was checked to send zero new model requests.
These results do not replace or merge with historical evaluator-v7 quality metrics.

Before model execution, every selected case must fail in the buggy version and pass
in the fixed version, with the same JUnit test identities. Collection/environment
errors do not count as successfully reproduced defects. The entire selected focused
test file/selector is evaluated; this is not a full-project regression guarantee.

- `resolved` / `success`: non-empty source change, independent suite exits zero,
  all fail-to-pass targets repaired, all pass-to-pass checks preserved, no missing or
  unexpected test identities. Skipped/error tests do not count as passing.
- `regressions`: originally passing selected tests that no longer pass. Denominator
  is the selected pass-to-pass set, not the entire upstream test suite.
- `workflow_gate_passed`: whether the graph reached the human-approval interrupt.
  Independent patch correctness and workflow approval are reported separately. The model
  reviewer is advisory: it cannot publish, and its risk classification can request at most
  one remediation before the human boundary. The experiment never approves or publishes PRs.
- Calls are exact counted API attempts. Tokens are provider-reported or `null`, never
  estimates. Financial cost is not invented. Per-pair duration, aggregate acceptance counts,
  node timings and failure type are saved. Quota-pending cases are not counted as repairs.

The harness saves sanitized resumable records plus `evaluation-summary.json` and
`evaluation-summary.md`. All persisted forms omit diffs, commands, process output and test
identities; comparison arrays become counts. Reports show success rates against both selected
and completed denominators and separate failed and quota-pending pairs. Duration, calls, retry
classes and provider-reported token completeness remain visible; no price estimate is added.

## Security scope

The orchestrator's Docker socket is an administrative trust boundary. The child
container has no network or host mounts, runs as UID 10001, drops capabilities and
limits memory/processes. It still shares a kernel; it is not ActPlane/eBPF or VM
isolation. Public benchmark tests and model-modified code execute only in this child.
The reproduction validator may execute fixed upstream code but never sends it to
the model. Local reports are ignored by Git; publish only deliberately reviewed data.

CH-06C adds public_data_version=public-data-v1 to the runtime fingerprint. New configuration
metadata rejects known credentials; structured evidence masks credential fields and drops headers.
Failures keep code/type only. Use a new records directory for this policy; historical quality
reports and datasets are unchanged and were not rerun. See [data boundaries](data-boundaries.md).

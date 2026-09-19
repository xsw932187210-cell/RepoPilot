# Real-defect evaluation protocol

This is a small, one-project pilot, **not** SWE-bench and not evidence of general
repository repair performance. The corpus is 20 pinned BugsInPy defects from The Fuck.
Corpus metadata and third-party test licenses are under `evals/real/`.

The current implementation records this contract as protocol/evaluator configuration version 8:
the model sees issue text plus bounded retrieved context, may edit only existing
`thefuck/**/*.py` source files, and is evaluated by the withheld JUnit acceptance suite
(`withheld-junit-v1`). The version is part of the resumable experiment identity, so changing
these rules requires a new output directory.

Version 8 changes only evidence persistence: raw patches, commands, process output and per-test
identities are removed before checkpoints are written, secret redaction is centralized, and
latency excludes unattempted quota placeholders. It does not change prompts, retrieval, candidate
execution or the acceptance decision. The published 2026-09-09 comparison was captured under
version 7 immediately before this storage hardening; future runs use version 8.

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
No dependency installation occurs in model-controlled workspaces. Image tags are
resolved to immutable local image IDs and included in experiment identity. The base
Python digest and direct Python dependency versions are pinned in the Dockerfile;
OS/transitive dependency rebuilding can still produce a different image ID.
Rate-limit and retryable HTTP/transport failures use bounded per-request backoff; every
attempt consumes the same explicit call budget, and SDK-level hidden retries are disabled.

## Leakage boundary

- The trusted orchestrator can read upstream history and benchmark metadata.
  Agent context receives only an archive of the buggy commit, without `.git`,
  reference patches, fixed source, or the hidden overlay.
- Both arms initially retrieve from the same issue text, using the same 12-file /
  70,000-character limits. Planner search terms are deliberately disabled in this
  experiment to hold initial retrieval constant. Thus this comparison tests role
  planning/review/retry, **not retrieval gain**. A retrieval ablation remains separate.
- Only existing `thefuck/**/*.py` source can be edited. Tests/config cannot be edited.
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

`oneshot` calls the same coder once, with a fixed minimal plan. `workflow` uses the
actual LangGraph planner, coder, visible-test runner and reviewer, with up to two coder
iterations. Deterministic rejection triggers a retry. After tests pass, a concrete
medium/high-risk reviewer finding may trigger one bounded remediation, while low-risk or
test-coverage-only concerns go directly to HITL; the second candidate is always escalated.
Both share the model, temperature, corpus, context limits, Docker image,
acceptance suite and a maximum of six API attempts per pair by default. Their
**actual costs need not be equal**. Report those costs alongside outcomes; do not
describe this as an equal-token experiment. Failed provider attempts consume the cap.

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

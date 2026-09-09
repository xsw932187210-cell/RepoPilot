# Real-model evaluation results — 2026-09-09

## Executive summary

RepoPilot completed a paired real-model evaluation on 20 pinned historical Python defects from
BugsInPy's The Fuck project. All 20 buggy/fixed pairs first passed deterministic Docker
reproduction. The same model, issue text, initial retrieval policy, acceptance suite and maximum
six-call ceiling were then used for a one-shot baseline and the full multi-agent workflow.

The result is useful but does **not** show a multi-agent quality lift: one-shot repaired 15/20
cases, while the workflow repaired 13/20. The workflow used about 3.1 times as many model calls and
had about 3.0 times the median latency. This report therefore treats orchestration, recovery and
policy enforcement as demonstrated engineering capabilities, while recording repair-rate
improvement as an open problem.

## Experiment identity

| Field | Value |
| --- | --- |
| Run date | 2026-09-09 |
| Corpus | 20 pinned BugsInPy defects from The Fuck |
| Reproduction | 20/20 buggy versions fail and fixed versions pass the evaluator-owned suite |
| Model | `gemini-3.1-flash-lite` through an OpenAI-compatible adapter |
| Temperature | `0.0` |
| Context ceiling | 12 files / 70,000 characters; issue-only initial retrieval |
| Call ceiling | 6 API attempts per case/mode pair |
| Acceptance | network-disabled Docker, withheld JUnit protocol v3 |
| Dataset SHA-256 | `6b54dc349ba8f493e9cfb2702bf6c227c6b5c3da781e3246620779fdc9984471` |
| Evaluation image | `sha256:4488c6ee019a76b875dcbd99737dbeb5163c5bc5221c4ab491494a7890664e58` |
| Behavior implementation SHA-256 | `86b09c4267f213dcf518e72f68d2a4b70595f187eaca5b32ae7288cffe97570a` |
| Evaluator config version | 7; version 8 subsequently hardened persistence only |

An independent, model-free retrieval diagnostic on the same 20 buggy commits selected every
expected fix file within the 12-file budget (20/20 cases, macro target recall 100%). Recall@3 and
Recall@5 were both 95%, and mean reciprocal rank was 0.874. Expected paths were used only after
retrieval for scoring; they were not passed into the query or model context. Run
`make eval-real-retrieval` to reproduce `reports/real-retrieval.json` locally.

## Results

| Mode | Selected | Completed | Runtime failed | Repaired | Repair rate | Median | P95 | Model calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| One-shot | 20 | 20 | 0 | 15 | 75% | 5.6 s | 8.8 s | 22 |
| Workflow | 20 | 18 | 2 | 13 | 65% of selected / 72.2% of completed | 16.8 s | 26.3 s | 69 |
| Overall | 40 | 38 | 2 | 28 | 70% of selected / 73.7% of completed | 12.5 s | 23.6 s | 91 |

Paired by defect, both modes repaired 12 cases, only one-shot repaired 3, only workflow repaired 1,
and neither repaired 4. There were no quota-pending pairs. Provider usage metadata was incomplete
for retry responses, so exact total tokens and financial cost are deliberately reported as
`null`; successful responses exposed 682,126 observed tokens, which is not presented as a complete
total.

## Failure and governance evidence

- Nine completed candidates missed the evaluator behavior without introducing a recorded
  pass-to-pass regression.
- One workflow candidate prevented JUnit report creation and introduced one pass-to-pass
  regression. It did not pass the release gate.
- Two workflow pairs ended in `SecurityError` after attempting to edit tests outside the exact
  source-file capability. The complete edit batch was rejected before any partial write.
- In a separate workflow case, the first proposal also violated the write policy; one sanitized
  denial plus refreshed context was returned to Coder, the corrected source-only proposal passed
  hidden acceptance, and the graph reached HITL. This exercises the bounded recovery path rather
  than merely documenting it.
- All workflow publication remained disabled. A successful candidate could reach the checkpointed
  human-approval interrupt, but the benchmark never approved or created a GitHub PR.

## Interpretation

The current workflow demonstrates single-writer coordination, read-only parallel analysis,
capability-checked edits, isolated execution, bounded correction and human gating. It does not yet
demonstrate that Planner/Reviewer roles improve coding accuracy for these small defects. The weak
model often solved them directly, while extra roles added calls and sometimes proposed unnecessary
edits or test changes.

The next iteration should be evaluated on a new or held-out multi-project set rather than tuned
against these 20 cases. Priorities are:

1. route simple issues to the one-shot path and reserve full orchestration for ambiguous or
   cross-file tasks;
2. calibrate Reviewer actions so evidence-only concerns escalate to HITL without speculative
   rewrites;
3. improve constrained edit generation and policy-denial recovery without weakening the test-write
   boundary;
4. add broader project-level regression suites and additional repositories/languages;
5. add Redis lease renewal plus fencing before claiming strong multi-worker exclusivity, and treat
   ActPlane/eBPF or VM isolation as future work rather than a current capability.

## Reproduction and disclosure notes

The corpus manifest, pinned commits, fixture hashes, Dockerfiles and runner are versioned in the
repository. Local raw reports remain ignored. Evaluator version 8 now removes diffs, commands,
process output and per-test identities before checkpoint persistence, centralizes credential
redaction and excludes unattempted quota placeholders from latency. Those storage-only changes do
not alter the v7 prompts, generated candidates or acceptance decisions summarized above.

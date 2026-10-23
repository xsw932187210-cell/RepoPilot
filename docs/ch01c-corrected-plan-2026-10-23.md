# CH-01C corrected experiment plan — 2026-10-23

Status: user explicitly approved the corrected experiment on **2026-10-23**;
the private grant was created and the frozen v2 run started after that approval.
It has now finished all 18 units: 16 completed, two call-layer failures, no pending
units, and no independently accepted repairs. It used 71/78 attempts; with the
preserved original 30, the combined consumption is 101/108. Full results are in the
[dated report](real-evaluation-results-2026-10-23.md), including three unknown
response outcomes and the verified zero-request completed resume.
The [original incomplete run](ch01c-interrupted-2026-10-23.md)
and its 30 requests remain preserved. The corrected experiment uses only the
remaining **78 requests**, including failures/retries, with the original combined
ceiling of 108. Each new defect/strategy/repeat unit is still capped at six.
Original exhausted units are not reset under their old identities.

Protocol: `evals/real/experiments/ch01c-pilot-v2.json`; run/output identities end in
`v2`. It keeps the same three holdout defects, three strategies, two repeats,
rotating order, Gemini/ChatGPT models, SDK versions, output/timeout/context limits,
Coder/Reviewer prompts, router thresholds, permissions and hidden acceptance as
v1. It changes evaluator preparation only, using `exported-buggy-tree-v1` and
evaluator v14. No old generation/result is reused in the corrected comparison.

For offline evaluation only, artifact initialization now reads a deterministic
private Git object baseline built from the exact history-free buggy export. It
contains only captured regular files/directories; no submodule is fetched and no
upstream attribute, filter or hook executes. The source revision remains recorded
separately; `candidate.base_sha` names this exported baseline rather than pretending
to be an upstream publication commit. The object repository is outside model/test
workspaces, and benchmark GitHub publication is disabled. Online publication uses
its original raw-Git base and strict link/submodule rejection.

All selected cases must pass baseline creation, artifact capture and an empty
initial diff check before any paid request. The three real holdout cases have
passed this preflight plus the complete Mock graph and Docker visible/hidden test
path with an intentionally empty candidate: one Mock call each, original buggy
acceptance exit 1, no edits, no approval/publication. Related host boundaries passed
29 tests. These checks required zero additional real-model requests. Initial
diagnostic failures remain in local logs.

The CI unit test now supplies declared SDK metadata deterministically and separately
asserts that mismatched metadata is rejected; runtime SDK pin checks remain strict.
The first CI failure was caused by the runner installing newer allowed SDKs, not a
real provider request or a weakening of the experiment's version requirements.

The private corrected grant was explicitly created after user confirmation as
`.local/evaluation-authorization/ch01c-v2.json`. It names prior consumption 30,
new maximum 78 and total ceiling 108. Startup validates the old reservation history,
retains its IDs and marks the old authorization superseded before allowing the
new one. Both grants share one OS lock; new config/directories cannot reset their
respective ledgers. The runner does not manufacture grants.

Primary/secondary outcomes and interpretation remain as in the
[original predeclared plan](ch01c-experiment-plan-2026-10-23.md). Three purposive
defects across two projects are a small pilot; repeats are not new defects,
difficulty remains unlabeled, incomplete pairs establish no winner, and missing
usage/money remains unknown. The curation ledger records runtime debugging after
v1; no prompts, routing or retrieval parameters were tuned from its results.

Verification summary:
[2026-10-23-ch01c-baseline-correction.json](evidence/2026-10-23-ch01c-baseline-correction.json).
Default planning remains read-only: `python -m repopilot.eval_protocol`.
After the recorded confirmation, `--run` uses the same trusted source mappings as v1 and
the separately bound v2 grant/directory. Existing real results from 2026-09-09 are
unchanged.

# Budgeted task routing

CH-01B adds `execution_strategy=oneshot|workflow|auto` to task creation and task
views. The value is persisted by migration `20261023_0008`; old rows and omitted
fields use `workflow`. Stop old Workers and migrate before upgrading. Existing
bound approval checkpoints can resume; a new routed checkpoint requires the same
trusted routing-policy fingerprint. No destructive downgrade is provided.

All routes use the same immutable candidates, modification-intent containment,
Docker test path in the deployed application, evidence binding and human approval.
`workflow` retains Planner/Coder/Reviewer and bounded remediation. `oneshot` uses
one Coder generation with the same static plan as the former real-eval baseline;
Planner/Reviewer are not called. A deterministic node requires passing visible
tests and a nonempty diff. Its summary explicitly says LLM review was not run,
`review_performed=false`, `review.approved=false`; the human decides publication.
Policy rejection, empty changes or failed tests stop explicit one-shot without a
second Coder generation. Transport retries still consume the existing call budget.

`auto` uses `visible-rules-v1`, with policy fields included in its fingerprint:

- A simple candidate has 40–1,500 issue characters, at most two selected editable
  files and at most 20,000 context characters, with no declared broad/ambiguous
  terms. It starts with one-shot; otherwise it selects workflow.
- Terms are the literal case-insensitive English words refactor/redesign/
  architecture/unclear/intermittent and Chinese 重构/架构/不清楚/偶尔. These are
  conservative heuristics, not learned difficulty or semantic understanding.
- Lightweight routes with no editable retrieval hit stop before inference.
  A selected workflow needs at least three remaining per-task reservations; a
  selected one-shot needs one. Explicit legacy workflow keeps its prior controlled
  generation/error behavior even if no useful context or insufficient budget exists.
- After a failed one-shot, auto may upgrade once to Planner/Coder/Reviewer if a
  candidate iteration remains and the same ledger has at least three calls left.
  It uses only visible policy/test failure, never hidden acceptance. Credentials,
  token-budget failures and provider failures do not cause a model-switch fallback.
  Provider quota remains classifiable as pending by the evaluation runtime.

The upgrade reuses candidate history, policy-retry count and the exact model-call
ledger. Its upfront check is advisory admission against remaining task calls;
the existing ledger and experiment-wide reservation hook remain authoritative for
every transport, including retries. It does not reserve a guaranteed three-call
block across concurrent Workers. Unknown provider effects consume budget as before.

Route decision/history store requested/selected strategy, reasons, numeric visible
features, policy identity, upgrade count and remaining calls. Task metrics expose
selected route, policy version, upgrades and whether model review ran. Public data
projection omits unrecognized routing/feature fields. No hidden metadata is an
argument to the router; fields come from issue-only first retrieval of the buggy
tree. Later workflow retrieval can still use Planner terms in the application;
real evaluation keeps issue-only retrieval for all arms.

Since real evaluator v13, all three strategies use the same permission/test/HITL
graph, then runs withheld acceptance after generation stops. No benchmark approval
is synthesized and no benchmark PR is published. Historical v12 and earlier
one-shot results retain their old identity/meaning; the added visible gate does not
retroactively turn historical one-shot runs into approval-reaching runs. This card
adds engineering behavior, with repair quality measured separately by CH-01C.
Evaluator v14 retains these strategy rules and constructs its private artifact
baseline from the exact exported buggy files; it does not change online raw-Git
publication checks or send hidden acceptance feedback into routing.

The [2026-10-23 corrected pilot](real-evaluation-results-2026-10-23.md) finished
18 units on three defects/two projects. Each strategy observed 0/6 accepted repairs;
one-shot/workflow/auto used 14/27/30 requests. All four reported auto routes selected
workflow; two auto call failures had no route result. This pilot did not establish
a routing or quality improvement. Keep workflow as the default and auto explicit.

The [consolidated Docker verification](ch01b-verification-2026-10-23.md) passed
538 cases, Mock 10/10, smoke and both approval-restart paths. The harness is
`sh scripts/ch01b_integration.sh`; it includes API/Worker/Docker strategy tests and
separate workflow and one-shot approval-checkpoint restart probes. Cached dependency
images can be reused while the current package is reinstalled and hashed.

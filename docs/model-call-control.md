# Model-call budgets and provider failures

RepoPilot routes every online Planner, Coder, policy-correction, and Reviewer request through the
provider-neutral `ControlledModelCaller`. The online policy is named
`provider-call-control-v1`; real-evaluation records use the same state machine and classifiers
under the compatibility identity `evaluation-call-budget-v2` and evaluation record schema `4`.

The control layer is a cost and recovery boundary, not a claim that a provider request and a
database transaction can be made atomic.

## Configuration

| Environment variable | Default | Meaning |
| --- | ---: | --- |
| `MODEL_MAX_CALLS` | `12` | Total transport attempts reserved for one online task |
| `MODEL_REQUEST_TIMEOUT_SECONDS` | `90` | Wall-clock timeout around one transport attempt |
| `MODEL_MAX_RATE_LIMIT_RETRIES` | `2` | Additional retries for retryable `429` responses |
| `MODEL_MAX_TRANSIENT_RETRIES` | `2` | Additional retries for transient HTTP/transport failures |
| `MODEL_RETRY_BASE_SECONDS` | `1` | Base for exponential delay when no valid `Retry-After` exists |
| `MODEL_MAX_RETRY_WAIT_SECONDS` | `30` | Maximum permitted delay for one retry |
| `MODEL_MAX_TOTAL_BACKOFF_SECONDS` | `60` | Persisted cumulative wait ceiling for the task |
| `MODEL_MAX_TOTAL_TOKENS` | `0` | Trusted provider-token ceiling; `0` disables it |
| `MODEL_MAX_OUTPUT_TOKENS` | `4096` | Per-request provider output ceiling |

The call limit includes the initial Planner/Coder/Reviewer calls, policy-correction calls, graph
remediation calls, and every provider transport retry. Retry limits do not grant calls beyond the
task-wide limit. A trusted token limit is checked before the next reservation only while every
previous outcome has complete provider-reported usage. One response can therefore cross the token
threshold before later calls are stopped. Missing usage disables that exact token gate for the
task; the call cap and provider output cap continue to bound requests. RepoPilot does not estimate
tokens, provider-specific billing units, or monetary cost.

## Durable ledger and state transitions

Alembic revision `20260921_0003` adds only the two CH-10 structures:

- `model_call_budgets` stores the immutable policy fingerprint, limits, counters, cumulative
  backoff, observed provider usage, and whether usage is complete for a task.
- `model_call_attempts` stores the task sequence, logical call, role, retry index, exact provider
  and model, adapter/request-schema versions, fallback origin, timestamps, outcome, stable error
  code, wait, and trusted usage.

One task has one budget. Concurrent first-use initialization is resolved by the task primary key;
the losing initializer reloads the winner and accepts it only when the policy fingerprint matches.
Reservation uses a conditional database update, so concurrent contenders cannot exceed the stored
call or trusted-token threshold.

The attempt lifecycle is:

```text
RESERVED -> STARTED -> SUCCEEDED
                    -> FAILED
                    -> UNKNOWN
```

The reservation transaction commits before `STARTED`, and `STARTED` commits immediately before
calling the provider transport. No request is made when reservation fails. Successful responses
are recorded only after optional schema validation, so malformed structured output becomes a
stable failed attempt instead of being passed to the graph.

There is deliberately no release/refund path. The recovery rules are conservative:

- A crash after reservation but before `STARTED` leaves `RESERVED`. It never reached the controlled
  transport, but still consumes the task cap and is exposed as a pending reservation.
- A crash after `STARTED` and before a persisted outcome may have sent bytes. On Worker recovery,
  every such attempt becomes `UNKNOWN` with `model_response_unknown_after_recovery`; it continues
  to consume the cap and is never retried for free.
- A provider success followed by a process/database failure before outcome persistence is the same
  unknown-result case. RepoPilot cannot safely infer whether it was billed or completed.

These rules survive Worker/checkpoint restarts because the ledger is separate from graph state.
They do not supply queue recovery, Worker ownership renewal, fencing, or general exactly-once
execution. CH-09 status/version CAS protects task-row updates only; CH-04B remains responsible for
stronger Worker ownership.

## Provider error and retry policy

The SDK adapter sets `max_retries=0`. The controlled layer is the only component that may retry:

| Signal | Ledger outcome | Retry behavior |
| --- | --- | --- |
| `429` rate limit | `FAILED` / `model_provider_rate_limit` | Valid numeric or HTTP-date `Retry-After`; otherwise bounded exponential delay |
| Daily/insufficient quota | `FAILED` / `model_provider_quota_exhausted` | Never retry; evaluation records the pair as quota-pending |
| HTTP `408`, `425`, `500`, `502`, `503`, `504` or recognized temporary outage | `FAILED` / `model_provider_transient_failure` | Bounded transient retry |
| Connection reset/refusal/closure, transport timeout, request timeout | `UNKNOWN` / `model_provider_response_unknown` | Bounded retry, but both the unknown attempt and retry consume calls |
| Invalid structured response | `FAILED` / `model_invalid_response` | Never retry |
| Other provider `4xx` | `FAILED` / `model_provider_rejected_request` | Never retry |
| Unclassified exception | `FAILED` / `model_provider_non_retryable_failure` | Never retry |

A delay above `MODEL_MAX_RETRY_WAIT_SECONDS` fails with
`model_retry_wait_limit_exceeded`. A wait that would exceed the persisted cumulative backoff
ceiling fails with `model_backoff_budget_exhausted`. Exhausting a retry class raises its stable
normalized provider error. In every case, the failed/unknown attempt has already consumed the
single task budget before another transport can be considered.

RepoPilot does not automatically switch providers or models. If a trusted adapter deliberately
does so, the call identity must set `is_fallback` and name the original provider or model. The
ledger and metrics count those attempts separately. Evaluation identity includes the exact
provider/model and runtime-policy fingerprint; a fallback run must use a new experiment identity
and cannot be combined with the original model group.

## Metrics and evaluation compatibility

`GET /api/v1/tasks/{task_id}/metrics` exposes the policy version and separate maximum, reserved,
started, succeeded, failed, unknown, pending-reservation, pending-response, retry, fallback,
backoff, observed-token, and token-completeness fields. Observed token counters may be non-zero
while exact aggregate token fields remain unavailable because another attempt had no trusted
usage.

Real evaluation persists the same transitions into each case/mode checkpoint after reservation,
start, retry scheduling, and outcome. Resuming a schema-4 record preserves counts and reconciles a
pending started attempt to unknown. The runtime manifest includes
`evaluation-call-budget-v2`, timeout/output/retry/backoff policy, provider, and exact model. Older
schema-3 record directories keep their historical meaning and fail strict resume matching; start
a new output directory rather than mixing them with schema 4. The published 2026-09-09 evaluator-v7
results remain historical evidence and are not relabelled as CH-10 runs.

## Deployment and verification

Stop old API/Worker processes, confirm there is no active work, back up the application database,
run one forward migration to `20260921_0003`, verify the head, then start only the new image. Old
Workers do not reserve this ledger and must not run alongside CH-10 Workers. Destructive downgrade
is unsupported; restore the backup or deploy a reviewed forward fix.

Useful checks are:

```bash
ruff check .
pytest -q tests/test_model_calls.py tests/test_eval_runtime.py tests/test_llm.py \
  tests/test_migrations.py tests/test_metrics.py
make ch10-migration-test
make eval
make smoke
make smoke-recovery
```

`make ch10-migration-test` creates a dedicated Compose project, PostgreSQL database, host port, and
volume; it refuses a non-dedicated database name and removes only its own resources. The provider
fault suite uses deterministic transports. It proves control-layer behavior, not live-provider
integration or billing accuracy.

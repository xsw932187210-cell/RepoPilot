# Evaluation contract

RepoPilot separates two kinds of evidence that should not be mixed in a resume or interview.

## Deterministic workflow regression

`evals/cases.jsonl` contains ten small defects across arithmetic, text normalization, boundary
handling, pagination, collection semantics, identity normalization, retries, dates, configuration,
and cache isolation. The mock provider applies deterministic patches so this suite tests the
software-delivery workflow itself:

- the task reaches completion;
- the sandboxed test command passes;
- the changed-file set exactly matches the case contract;
- the expected implementation file appears in the ranked retrieval context;
- the graph pauses for human approval;
- iteration and latency fields are recorded.

Run it with:

```bash
make eval
```

The CI job also saves `reports/mock-evaluation.json` as a downloadable workflow artifact. A failure
in any case fails the quality gate. These results are a regression baseline for orchestration,
state, tools, and policy—not a measurement of LLM coding quality.

The report also exposes retrieval target recall, Recall@3, Recall@5, mean reciprocal rank, selected
file count, and context characters. These use the initial retrieval snapshot, before any code edits,
and record retrieval limits in report metadata. Recall is the fraction of expected files found,
macro-averaged over cases; all-target hit rate is reported separately. These numbers use the small
bundled fixture and prove ranking regression behavior only; they must not be presented as
large-codebase retrieval quality.

## Real-model evaluation

Real-model patches must run through the isolated real-task Docker harness. The legacy
`repopilot.evaluation` command intentionally refuses a real provider because it uses `LocalSandbox`;
it remains the backwards-compatible mock workflow regression command.

The real harness uses the provider-neutral runtime in `repopilot.eval_runtime`:

```python
config = EvaluationConfig(
    dataset_sha256=dataset_sha256(dataset_bytes),  # full 64-character digest
    provider="openai",
    model="the-exact-model-name",
    temperature=0.0,
    max_model_calls=6,
    modes=("oneshot", "workflow"),
    context_budget={"files": 12, "chars": 70_000, "initial_retrieval": "issue-only"},
    test_evaluator="withheld-junit-v1",
    evaluator_config={"sandbox": "docker", "timeout_seconds": 120},
)
runtime = EvaluationRuntime(config, Path("reports/real-run.records"))
report = await runtime.run(cases, execute_case)
```

`execute_case(case, mode, budget)` is an injected async callback. It prepares an isolated checkout,
uses the supplied `ModelCallBudget`, runs the named evaluator, and returns a JSON-compatible result
including `success`. Build workflow models with `build_agent_model(settings, budget=budget)`; direct
model invocations must go through `await budget.call(model.ainvoke, messages)`. The runtime does not
clone repositories, run tests, call a model, or write to GitHub by itself.

Each case/mode pair is atomically checkpointed. A process crash leaves only that pair as `running`;
the next invocation reuses terminal siblings and reruns the unfinished pair. Ordinary case failures
are recorded as `failed` and do not abort other cases. Unresolved provider rate limits are recorded
as `pending_quota`, never as case failures, and stop new paid work for that invocation. A later run
can resume those pending pairs.

Checkpoint and model-report records are evidence summaries, not debug dumps. Before persistence the
runtime recursively redacts secrets, drops candidate diffs, commands, stdout/stderr/logs and raw
JUnit outcomes, and replaces test-identity lists with counts. Full acceptance outcomes exist only
in evaluator memory while deciding the result. Unattempted quota placeholders have no duration and
are excluded from latency percentiles. Keep `reports/` private even with these controls; only the
sanitized aggregate report is intended for publication.

Resume is deliberately strict. The record directory is rejected if any experiment-identity input
changes: the full dataset hash, provider/model, temperature, modes, maximum model-call budget,
context limits, test evaluator, evaluator configuration, or provider retry/backoff policy. Start
a new record directory for a changed experiment.

Rate-limit and retryable transport/HTTP 5xx retries happen around the individual model request, not
around the whole case. Numeric or HTTP-date `Retry-After` values are honored only within the
configured retry count, delay ceiling, and model-call cap. Daily/insufficient quota stops
immediately; an exhausted transient-outage retry is isolated as a case failure. SDK retries are
disabled for budgeted OpenAI-compatible models so hidden requests cannot bypass the cap.

The two modes receive the same named model, context budget, test evaluator, and maximum call cap.
This is a common ceiling, not a claim that their actual usage is identical: a one-shot baseline may
use one call while the workflow can use several up to the same cap. Reports preserve actual calls
per case and aggregate them per mode.

Token totals are copied only when every request exposes exact provider usage. Otherwise
`input_tokens`, `output_tokens`, and `total_tokens` are `null`; observed counts from successful
responses remain separately labelled `observed_*`. No token estimate or invented USD cost is
reported.

Set required provider credentials only in the runtime environment used by the real harness. Never
place credentials or values from ignored environment files in reports. Run the harness with GitHub
writes disabled. Record the model name, dataset hash, run date, per-case failures, success rate,
scope-match rate, test-pass rate, median duration, and p95 duration. Do not compare two model runs
unless they use the same dataset revision and runtime configuration. Do not place a real-model
success rate on a resume until the report has been saved and manually checked.

## Runtime evidence

With the Compose stack running, use:

```bash
make smoke
make smoke-recovery
```

The first command validates the API-to-worker-to-approval path and its metrics endpoint. The second
restarts the worker at a durable LangGraph interrupt and verifies completion from the persisted
checkpoint. Neither command enables GitHub writes.

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

Set `MODEL_NAME`, `OPENAI_API_KEY`, and optionally `OPENAI_BASE_URL` in the ignored `.env`, then run:

```bash
make eval-real
```

Record the model name, dataset hash, run date, per-case failures, success rate, scope-match rate,
test-pass rate, median duration, and p95 duration. Do not compare two model runs unless they use the
same dataset revision and runtime configuration. Do not place a real-model success rate on a resume
until the report has been saved and manually checked.

## Runtime evidence

With the Compose stack running, use:

```bash
make smoke
make smoke-recovery
```

The first command validates the API-to-worker-to-approval path and its metrics endpoint. The second
restarts the worker at a durable LangGraph interrupt and verifies completion from the persisted
checkpoint. Neither command enables GitHub writes.

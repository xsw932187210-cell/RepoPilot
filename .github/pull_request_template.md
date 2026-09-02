## What changed

-

## Evidence

- [ ] `ruff check .`
- [ ] `pytest --cov=repopilot --cov-fail-under=70 -q`
- [ ] `make eval`
- [ ] `make smoke` when runtime behavior changed
- [ ] `make smoke-recovery` when persistence or orchestration changed

## Safety review

- [ ] Model-generated paths remain inside the task workspace.
- [ ] Test execution remains shell-free, network-disabled, and resource-limited.
- [ ] No token, credential, private repository content, or `.env` value is logged or committed.
- [ ] GitHub writes remain disabled by default and gated by reviewer plus human approval.

## Resume claim impact

Describe which measurable claim this PR adds or changes, and link the test, benchmark, or runtime
evidence that supports it. Write `none` if the change should not affect a portfolio claim.

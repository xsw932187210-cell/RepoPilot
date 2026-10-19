# Workspace capability policy

RepoPilot treats repository text and model output as untrusted requests. The service selects one
immutable `WorkspaceCapabilityPolicy`; issue text, repository files, and model responses cannot
add permissions to it. The current schema version is `workspace-capabilities-v1`.

## Capability model

The policy has separate rules for:

- `read`: files eligible for repository inspection and model context;
- `write`: existing regular files that may be replaced after stale-content checks;
- `create`: trusted directory prefixes, extensions, and a byte limit for new files.

`delete` and `rename` are not supported in v1. Requests for either action fail with
`action_not_supported`. The application default does not authorize any creation. A trusted project
adapter may opt into creation, but the caller must also provide the same normalized path through
`expected_absent`; the write uses exclusive creation so an already-created target is not silently
overwritten.

The default application policy behaves as follows:

| Path category | Read | Modify existing | Create |
| --- | --- | --- | --- |
| Supported source and documentation files | yes | yes | no |
| Tests | yes | no | no |
| Build and CI configuration | yes | no | no |
| Credential-like files | no | no | no |

Test writes can be enabled only by constructing a trusted policy with
`default_workspace_policy(..., allow_test_writes=True)` or by injecting another service-owned
policy. There is no task, issue, repository, or model field that enables this override.

The real-defect evaluator injects a stricter policy. It may modify only files already exported in
its original `thefuck/**/*.py` source set; it still cannot add paths, alter the independent hidden
acceptance overlay, approve, or publish.

## Path and batch enforcement

The same portable normalized path is used for policy matching, case-folded identity checks,
duplicate detection, context lookup, and filesystem access. Absolute paths, traversal, `.git`,
platform-ambiguous components, case aliases, symbolic links, hard links, and special files are
rejected at the repository boundary. Unsafe files are also omitted from readable context.

`WorkspaceManager.apply_edits()` validates the complete batch before its first write. Its stable
`CapabilityError` carries `reason`, `action`, `path`, and `policy_version`; current reasons include
read/write/create denial, unsupported actions, invalid or aliased paths, duplicate targets, unsafe
links, hard links, special files, size violations, stale/out-of-context content, and failed
expected-absent conditions.

## Structured Coder correction

The online Coder returns one `CodeChangeOutput-v2` modification intent in its normal model call.
Each existing-file edit names `action=write`, an exact editable path, complete replacement content,
and `expected_content_sha256` for the bytes it read. The graph checks the intent against its latest
retrieval snapshot before `apply_edits()` performs the complete trusted-policy and filesystem
preflight. The optional digest field in `FileEdit` keeps direct tool callers and older serialized
artifacts readable; it is mandatory at the graph proposal boundary. The graph currently exposes no
create intent, even when a trusted tool policy could authorize creation. No extra planning model
call is made for these checks. A malformed proposal schema stops with the stable
`invalid_modification_intent` error before any write.

A denial produces `policy-feedback-v1`: a stable `reason_code`, a target path only if it exactly
matches a non-sensitive path in the readable context, `allowed_actions=["write"]`, a bounded list
of exact editable paths, and fixed guidance. The exception message and rejected arbitrary path
are never copied to the model, node event, or task error summary. A rejected batch is discarded in
full; legal edits in that batch are not applied. The graph re-inspects the workspace before the
next proposal, and a digest from the previous snapshot fails as `stale_content`.

`policy_retry_count` allows one correction for the entire task, not one per Coder iteration. It
is checkpointed by the separate `apply_policy` node before the next `coder` call. A second denial
ends as `policy_retry_exhausted`; a missing digest yields structured
`expected_content_required` feedback under the same bounded allowance. All model attempts,
including the correction, use the existing CH-10 task-wide call ledger. If that budget is empty,
the controlled caller makes no transport request and the graph stops with
`model_call_budget_exhausted`. The count is distinct from Coder iterations and provider retries.

This is recovery from a policy denial, not a filesystem transaction: an external writer can still
race after preflight, and an I/O failure during sequential write can leave a partial result.
The checkpoint and workspace are also not one transaction. A crash after a successful write but
before the `apply_policy` checkpoint may re-run preflight against changed bytes; CH-03A/CH-05A
remain responsible for immutable candidate identity and atomic visible generations. CH-04B must
add Worker ownership fencing before concurrent writers can be treated as safe.

## Verification

Focused coverage lives in `tests/test_capabilities.py`, `tests/test_runtime_boundaries.py`,
`tests/test_reliability.py`, and `tests/test_real_evaluation.py`. The deterministic evaluator also
checks that readable tests remain available for diagnosis while only expected source files change.

`make ch02b-integration` runs the isolated PostgreSQL/Redis/API/Worker and Docker probe. It
hard-exits an execution process after the persisted policy denial, explicitly resumes the same
thread in a new process, and checks correction limits, durable calls, actual files and terminal
states. It also checks a pinned pre-CH-02B approval checkpoint and a real Worker-service restart.
See the [2026-10-19 verification record](ch02b-verification-2026-10-19.md) for the six passing
scenarios, environment and cleanup. Mid-task continuation is test-driven; automatic queue reclaim
and Worker ownership fencing remain separate work.

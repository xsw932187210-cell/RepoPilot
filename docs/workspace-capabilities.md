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

The Coder may refresh context and retry once after a rejected batch. No legal edit from the rejected
batch is retained. This is recovery from a policy denial, not a filesystem transaction: an external
writer can still race after preflight, and an I/O failure during the sequential write phase can
leave a partial result. Candidate staging and atomic visible generations remain CH-05A work.

## Verification

Focused coverage lives in `tests/test_capabilities.py`, `tests/test_runtime_boundaries.py`,
`tests/test_reliability.py`, and `tests/test_real_evaluation.py`. The deterministic evaluator also
checks that readable tests remain available for diagnosis while only expected source files change.

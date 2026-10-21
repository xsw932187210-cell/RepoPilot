# Staged candidates and complete visible generations (CH-05A)

The graph reads immutable candidates and writes each proposal into a new private staging
tree. It never applies generated edits to the original checkout. A successful database CAS
selects a complete artifact; readers keep their explicit candidate reference through a switch.
This is atomic visibility for controlled readers, not a filesystem/database transaction.

## Storage and interfaces

`GenerationCoordinator.initialize(task_id, workspace, base_sha)` freezes generation 0.
`apply(parent, edits, expected_contents, base_repository)` creates generation N+1. The existing
`candidate-v1`, manifest, artifact reference and Diff identity remain unchanged. The graph state's
`workspace` is now only the original checkout/base-blob source; `candidate` is its read view.
History-free evaluation adapters still obtain base blobs from their separate trusted repository.

`SQLGenerationStore` persists two tables through Alembic `20261021_0005`:

- `candidate_generations`: append-only task/generation, immutable candidate ID, parent ID,
  operation ID. `(task_id, generation)` and `(task_id, operation_id)` are unique.
- `generation_heads`: one task's currently selected candidate/generation. It is a pointer,
  never an alias that changes the contents of an old approval or test reference.

`commit(expected_parent, operation_id, candidate)` locks the task row, requires a running task,
compares the head to the expected parent, inserts the candidate/generation and CAS-updates the
head in one transaction. A stale parent fails with `stale_candidate_generation`. A repeated
operation resolves to its committed candidate only while that candidate remains current.
Operation identity includes parent, complete edits and context digest; there is no fuzzy match
to a later candidate. Offline graphs use `MemoryGenerationStore` with the same contract, without
a database/process-restart guarantee.

## Write and durability order

1. Acquire a task-scoped advisory OS `flock` under the service-owned `.staging/<task-hash>/`.
   Independent supported processes on the shared volume use the same lock. Lock acquisition
   does not block the event loop; it has a bounded wait. Kernel process exit releases the lock.
2. Resolve a previously committed identical operation, or verify the current parent. Allocate
   a fresh mode-0700 stage, then persist/fsync `journal.json` with phase `writing` and parent ID.
3. Materialize the verified parent into `tree/`. Validate the whole edit batch and its context
   before model writes. Descriptor-relative writes use `O_NOFOLLOW`, regular/single-link checks,
   expected bytes and exclusive creation where the trusted low-level API permits creation.
   Context and preflight reads preserve UTF-8 newline bytes, including CRLF, so intent hashes
   and raw-file freshness checks refer to the same content.
   The online generation protocol currently permits existing-file writes only.
4. Verify the entire resulting tree against the parent plus the complete authorized edit set,
   including every unedited byte, path and mode. Build `tree.tar` and full Diff, then write the
   manifest last; fsync all bundle files and directories. Publish a content-addressed complete
   bundle by a single directory rename, verify it, persist the full candidate in phase `sealed`.
   Recheck the staged tree before committing. Original checkout bytes are never rolled back.
5. Commit the database generation/head transaction. Finally persist journal phase `committed`.
   A checkpoint can follow later. If it did not commit, exact operation replay reuses the SQL
   generation instead of applying edits or calling the Coder again.

Each file write can still fail. Such a failure only leaves a private incomplete stage; it does
not change the selected generation. Raw stages and artifacts are retained, including on failure.
This uses additional disk space; retention/garbage collection belongs to CH-16.

## Readers and stale results

Research and retry inspection use verified disposable materializations of the bound candidate;
unexpected modification during inspection stops execution. Test Runner uses the candidate already
committed by `apply_policy`, rather than freezing a mutable directory after writes. Tests can
mutate their own disposable sandbox copy without changing stored inputs.

SQL evidence insertion, opening an approval, consuming its decision and final task-result writes
check the current candidate while holding the same task-row serialization boundary as generation
commits. Old graphs cannot insert evidence, open an approval or complete a task for a superseded
candidate. Existing evidence and approval objects keep their original immutable references.
The publisher still receives its old edits/base-branch interface; approved-base construction and
remote publication idempotency remain CH-03B.

The original checkout can change after the initial capture without changing the candidate.
Such user changes are left intact. Changes to a staging or inspection tree, including link
replacement, fail containment/digest checks. The service does not perform a blind rollback or
overwrite newly observed external bytes. An arbitrary process with the same OS identity can
ignore advisory locks or tamper with service storage; this is detection and controlled-reader
isolation, not protection against an administrator or a fully hostile shared filesystem.

## Restart reconciliation and operator action

On Worker startup, `reconcile_staging()` scans running tasks. Before executing a task the Worker
also reconciles its stages. `GenerationCoordinator.recover(task_id)` takes the same writer lock,
compares each retained journal to the committed operation and verifies complete artifacts. It
persists a metadata-only `recovery.json` beside the journals and logs stage IDs/states, not source.

| Observation | Classification | Action |
| --- | --- | --- |
| Stage directory without journal, or interrupted writing | `incomplete` | Keep files; never select it |
| Complete sealed candidate without matching SQL commit | `orphan` | Verify and keep it; do not adopt automatically |
| SQL operation and full journal candidate match, including exit before final journal update | `committed` | Retain it; explicit checkpoint replay may reuse the existing operation |
| Invalid journal, mismatched manifest, missing/corrupt artifact | `corrupt` | Keep evidence; never infer success from directory existence |

Reconciliation does not alter the task state, enqueue a job, reclaim a Redis delivery, or delete
data. An interrupted running task can still need operator attention. Until CH-04A/04C provide
general queue/graph recovery, the supported public recovery action is to inspect retained
metadata, cancel a stranded task and create a new task; retain its old stages for diagnosis.
The service tests additionally resume the same saved graph explicitly to verify operation replay.
Do not hand-edit a pointer, copy partial files into an accepted artifact, or delete suspected
orphans before checking SQL/checkpoint/approval references and taking a backup.

## Upgrade compatibility

Stop old API/Worker processes and back up both PostgreSQL and the shared artifact volume. Upgrade
through `repopilot-migrate upgrade`; the new head is `20261021_0005`. The migration preserves the
latest explicit CH-03A approval candidate for each task as its initial head/generation record.
It keeps that candidate's original generation number, ID, evidence, decision and checkpoint.
This compatibility seed is not a new test run. Existing checkout edits from CH-03A are preserved.

An old running checkpoint without a committed candidate/head is not reconstructed from workspace
bytes. Cancel/recreate it for revalidation. Pre-CH-03A unbound approvals remain explicitly refused.
Mixed old/new Workers are unsupported. Downgrade is intentionally rejected; restore the matching
SQL/volume backup or apply a forward fix.

## Verification entry points

`tests/test_generations.py` covers second-file failure, complete reader views, operation replay,
old parents/operations, external byte changes, post-preflight link swaps, escape/readonly batch
rejection, writer serialization, orphan/corrupt retention, stale evidence/approval/result writes.
`make ch05a-integration` uses a dedicated mock-only Compose project and real PostgreSQL, Redis,
Docker tests, hard process exits, exact graph replay, an actual pinned CH-03A approval upgrade,
API smoke and Worker restart. Its cleanup touches only that project's labelled resources.

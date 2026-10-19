# Frozen candidates and bound approval decisions (CH-03A)

The graph freezes repository contents before each test run. Tests receive a disposable
materialization of that artifact, and approval identifies the same candidate and evidence.
The mutable workspace path is not an approval credential.

## Artifact and evidence contract

`ArtifactStore.freeze(workspace, task_id, base_sha, generation, policy_version)` returns
`candidate-v1`. `base_sha` is the prepared Git HEAD recorded before edits.
`materialize(candidate)` verifies and creates a temporary test tree; `read_diff(candidate)`
returns the complete stored Diff. CH-05A staging will reuse this interface.

- Canonical `manifest-v1` contains the base SHA and sorted paths, file permission bits,
  byte counts and SHA-256 content digests. Directory modes normalize to 0755. Root `.git`
  metadata is excluded; other regular files, including untracked and binary files, are included.
  Links, special files, aliases and special permission bits are rejected. Each snapshot/base
  tree is limited to 100 MiB and 10,000 entries.
- `artifact_ref=sha256:<manifest_digest>` identifies a bundle under the shared volume's
  `.artifacts/`. Candidate identity additionally binds task, generation, actual base SHA,
  policy version and full-Diff digest. Display previews never determine identity.
- The interface never overwrites an existing bundle. Reads verify its manifest/Diff, and
  materialization verifies every byte/mode. This is application immutability and tamper
  detection, not immutable storage against the service administrator. Back up the artifact
  volume with PostgreSQL. Garbage collection is not implemented.
- Full Diff is a binary-capable Git `--no-index` diff using stable `base/` and `tree/` prefixes.
  Base blobs are read directly from the commit tree; export attributes cannot omit/rewrite them.
  Additions and deletions are included without truncation. Git's textual mode output describes
  the executable bit; the manifest records all supported file permission bits and empty directories.

`test-evidence-v1` binds candidate/artifact IDs, resolved Docker image ID, parsed command,
sandbox/command/workspace policy versions, resource settings, result and observation time.
Docker creates the container from the resolved image ID. Local execution is labelled
`local-test-only`, without an isolation claim. Scope is `configured-tests`, separate from hidden
acceptance. Failed runs also retain candidate/evidence rows. Test mutations affect only the copy.

## API and compatibility

Every endpoint uses the existing optional `X-API-Key` guard. This is a single-operator API,
without tenant authorization. Server-assigned actor is `local-operator` or `api-key-operator`.

1. `GET /api/v1/tasks/{id}/approval` returns `approval-v2`: ID, request version, status, expiry,
   frozen candidate/manifest, test evidence, Reviewer risk/summary and saved decision, if any.
   Default approval lifetime is 24 hours. There is no separate approval frontend.
2. `GET /api/v1/tasks/{id}/candidates/{candidate_id}` returns the full manifest/identity;
   `/diff` returns the untruncated Diff with its digest as ETag. Lookups are task scoped.
3. `POST /api/v1/tasks/{id}/approval` requires `schema_version=approval-v2`, `approval_id`,
   `request_version`, `candidate_id`, `evidence_digest`, strict boolean `approved`, optional
   feedback. Copy bindings from GET after reviewing them. Legacy boolean-only requests return
   422; missing fields are never filled with the newest candidate.
4. POST returns `ApprovalReceipt`: `saved=true`, task status, and durable decision with ID,
   actor, time, bindings and `dispatch_state`.
5. `POST /api/v1/tasks/{id}/approval/dispatch` with `{"decision_id":"..."}` explicitly retries
   a saved pending/enqueued intent while the task is queued. It creates no second decision.

| Condition | Response |
| --- | --- |
| Decision committed and Redis accepts delivery | 200, `enqueued`, or `consumed` if Worker won the race |
| Decision committed, Redis delivery raises, including an unknown transport outcome | 202, `saved=true`, `pending`, `queue_delivery_failed` |
| Same bindings, approval, feedback and actor submitted again | Original decision ID |
| Opposite/conflicting repeat, stale binding/version, stopped/non-waiting task, expired request | 409 with a stable reason |
| Legacy boolean request or invalid schema | 422 |
| Missing/corrupt artifact before approval | 503 `approval_artifact_unavailable`, no decision |
| Old awaiting checkpoint without frozen candidate/evidence | GET 409 `legacy_approval_requires_revalidation` |

Migration retains old task/event JSON and checkpoints. Hashing today's mutable workspace cannot
invent test evidence for a legacy approval. Cancel the old task and create a new task to rerun
preparation/testing. Stop old API/Worker processes before upgrading; mixed versions are unsupported.
The historical CH-02B legacy-resume result describes that earlier release, not this stricter API.

## Transactions and recovery boundaries

Alembic `20261019_0004` creates `candidates`, `test_evidence`, `approval_requests`,
`approval_decisions`. Opening a request commits bindings with `running → awaiting_approval`.
Deciding locks the task on PostgreSQL and uses status/version CAS. Decision, `awaiting_approval
→ queued`, pending intent and audit event commit together. Approval ID is unique in decisions;
opposite concurrent decisions have one winner. Request version equals the task's waiting version
(for example waiting v3, queued v4, running v5).

Redis enqueue follows commit. A crash before/during enqueue leaves a recoverable pending intent.
`enqueued` means an accepted delivery attempt, not consumption acknowledgement. Worker resolves
the committed decision from PostgreSQL, claims the queued task version, marks intent consumed,
and resumes the same graph thread. Redis booleans cannot replace the saved decision. Cancellation
blocks pending/enqueued intents.

CH-04A still owns automatic outbox dispatch, queue acknowledgements and recovery after dequeue.
CH-04B owns fencing. A crash after task claim can strand a running task; there is no new watchdog.
Artifact storage and SQL are not one transaction: freezing can leave an unreferenced bundle,
but approval never references a partially published bundle.

`finalize` checks the artifact and that state edits match its bytes. Publisher still has its
original edits/base-branch interface. Approved-base construction, remote reconciliation and
publication intent idempotency belong to CH-03B after CH-05A. Tests keep GitHub writes disabled;
this card does not establish end-to-end remote publication binding.

## Reproduction

`make ch03a-integration` uses a fresh dedicated Compose project/database/volume, mock model,
blank provider/GitHub credentials, real PostgreSQL/Redis/Docker and scoped cleanup. It runs
approval migration/concurrency/commit-exit/Redis-failure/corruption probes, policy-correction
process tests and explicit legacy refusal, ten-case mock evaluation, API smoke and actual Worker
restart with byte-for-byte comparison of the approval view. Local ignored evidence is under
`reports/<project>/`; sanitized results are in the dated delivery note.

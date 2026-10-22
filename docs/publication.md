# Approved publication and remote reconciliation (CH-03B)

The publisher accepts a persisted intent ID, not model edits. An intent binds the SQL task,
consumed affirmative decision, approval version, evidence, immutable candidate, target repository,
base branch and approved base SHA. Its branch includes the complete task and intent IDs.
Git objects are derived from all approved file bytes and executable modes. Empty directories
have no Git representation; other permissions than 0644/0755 are refused rather than normalized.
The commit has a deterministic message, author/committer and date. Its parent is the approved SHA.

`publish_intents`, installed by migration `20261022_0006`, stores the immutable intent and a
versioned receipt. A receipt saves each blob/tree/commit/ref/PR request before transport, then
confirms the target by reading GitHub. Object IDs are calculated before transport. Ref creation
uses a unique full identity and never updates or force pushes an existing ref. PR lookup includes
closed PRs and verifies the marker, repository, head commit, base branch, draft and open state.

Transport failure or process death is not evidence of absence. Replay first reads the same
target. Content-addressed Git objects and create-only refs can safely reuse their exact targets.
An ambiguous PR request followed by an empty lookup stays unknown: it never issues a second PR
creation request. Lookup errors and incomplete/paginated results stop writes. Later reconciliation
can discover the original PR. Conflicts require operator attention, without destructive repair.

Each write requires the original approval and active SQL task/version/current generation, explicit
runtime enablement, token and allowlisted owner. The base is checked before writes and after
responses. Movement stops publication and permanently marks this intent as requiring new tests
and approval, including when the PR already exists. These observations cannot make a GitHub
branch update atomic with the checks.

The publisher accepts an async cancellation predicate. Cancellation before a request prevents
it. Cancellation during transport allows that request to finish or fail, reads its target, saves
the observed effects and stops subsequent writes. Cancellation is sticky for this intent. It
does not delete objects, branches or PRs. [CH-06A execution control](execution-control.md) now supplies the production stop predicate,
interrupts local request waits and permits bounded read-only reconciliation. A confirmed local
stop retains the independent remote receipt, including unknown or already-published effects.

This protocol assumes one executing publisher per intent. SQL receipt CAS detects stale saves;
it does not fence an already dispatched HTTP request. Multi-Worker ownership, automatic queue
recovery and general exactly-once execution remain CH-04B/C. Tests use HTTP transports, never
real GitHub writes. Upgrade requires stopping old processes and backing up SQL and artifact
storage; no intent is fabricated for historical completed tasks. Destructive downgrade is refused.

## Runtime outcomes and explicit recovery

`pending/publishing` describes progress; `unknown` describes an unresolved request or lookup.
`published` means the expected open Draft PR was verified. `blocked` records an identity or authority
conflict; `revalidation_required` records observed base movement. `cancelled` means no further
requests will be issued by this intent, not that prior remote effects were removed. The receipt's
`remote_ids`, `steps`, `cancel_requested` and `requires_revalidation` keep these facts independent.
A PR can exist while the phase requires revalidation or records cancellation.

The graph stores the receipt in its result and emits it at finalize. Unresolved/conflicting or
revalidation-required publication ends the graph as failed; a cancellation signal returns cancelled.
The existing task CAS may already have accepted a cancellation, so the independent receipt remains
the source for remote facts. `GET /api/v1/tasks/{id}/publication` uses the existing API Key protection
and reads only SQL. It works for terminal tasks and does not send HTTP requests.

An operator can stop the executing Worker, inspect the receipt and explicitly reconcile the same
intent. Default CLI mode sends only GET requests; it never changes a terminal task status:

```bash
repopilot-publish FULL_INTENT_ID
# Only for an original task that is still running with its original approval/version:
repopilot-publish FULL_INTENT_ID --continue-active
```

Continuation still requires runtime enablement, token, allowlisted owner, valid approval, current
candidate and unchanged base. A failed/cancelled task cannot authorize new writes. Its already sent
requests can still be reconciled in read-only mode. An ambiguous PR that remains absent in lookup
requires operator investigation; neither CLI mode invents certainty or repeats its create request.
There is no background publisher dispatcher or automatic requeue. Retain SQL and artifacts for
pending reconciliation. New tests/approval for a changed base require a fresh task in the current
task API; do not reset task versions or edit receipts by hand.

## Upgrade boundary

Stop old API/Workers, back up SQL and artifacts, migrate to `20261022_0006`, then start only the new
code. Waiting bound CH-03A/CH-05A approvals may resume; the new approval validation records the
publication protocol marker on the consumed SQL decision and in the graph checkpoint. Old
already-approved/finalizing checkpoints have no trustworthy publication intent and are refused as
`legacy_publication_requires_reconciliation`/`invalid_publication_approval`. Inspect the old branch
and PR manually before cancelling/recreating such a task. Migration does not infer success/absence
from an old task result, and does not automatically reauthorize a possibly published candidate.

## Protocol references and verification

The implementation uses GitHub's [blob](https://docs.github.com/en/rest/git/blobs),
[tree](https://docs.github.com/en/rest/git/trees),
[commit](https://docs.github.com/en/rest/git/commits),
[create-only ref](https://docs.github.com/en/rest/git/refs) and
[Draft PR/list](https://docs.github.com/en/rest/pulls/pulls) APIs. Full tree creation deliberately
omits `base_tree`; author/committer dates are explicit. SHA-1 identifies Git objects, while the
candidate/evidence and SQL payload integrity digests remain SHA-256. Returned IDs and content must
match the precomputed identities; unsupported serialization fails closed.

`tests/test_github.py` injects response loss at all five stage types, lookup failures, conflicts,
cancellation and base movement. `tests/test_publication.py` checks native Git identities, SQL/API
bindings and graph routing. `make ch03b-integration` runs the full suite with isolated PostgreSQL,
Redis and Docker, including actual process exits after the test transport applies each stage.
The remote GitHub service is always simulated; this does not claim a real GitHub integration run.

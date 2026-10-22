# Public diagnostics and private execution data

CH-06C uses public-data-v1 for online output. API responses include the
X-RepoPilot-Public-Data header. This projection version is separate from stored
result/event versions and immutable candidate/evidence identities.

## Exit inventory

| Exit | Allowed data and enforcement |
| --- | --- |
| Worker/API/library logging | Bounded, single-line diagnostics. Existing handlers receive the shared filter. Exception objects become stable code/type; traceback, exception text and stack output are discarded. Worker reconciliation logs counts instead of private reports. |
| New SQL events / Redis notifications | Message redaction and an explicit payload field allowlist. ORM insertion also covers approval/control events within their authority transaction. Notifications apply the projection again. |
| Event history / live SSE | Both paths project old SQL/Redis entries; kind/node cannot inject SSE lines. This does not repair CH-06B delivery or cursor gaps. |
| Task API / result | Explicit result, review, retrieval, metric and receipt fields. No edits, raw context, code Diff, headers or arbitrary future result fields. Legacy exception text becomes a stable code. A GET does not rewrite SQL history. |
| Validation / unhandled HTTP errors | Validation keeps loc/type/message, omits input/context. Unexpected errors return internal_error; logs retain classification only. |
| Candidate / approval metadata | Canonical identities remain intact. Known credentials in legacy authority data cause 409 sensitive_legacy_data_requires_revalidation; objects are not silently redacted while keeping their digest. Metadata retains the existing optional API Key boundary. |
| Complete candidate Diff | Private, byte-exact access requires a configured API Key and matching request key. Missing configuration returns 503 private_artifact_requires_api_key; missing/wrong key returns 401. Responses use private, no-store and nosniff. |
| Issue / human feedback | Known credentials are redacted before new task/decision persistence and graph input. Retries compare the same persisted, redacted feedback. Sensitive execution commands/bindings are rejected rather than changed. |
| Model / checkpoint | Files matching known credential patterns are omitted whole from retrieval. Sensitive proposals stop with sensitive_execution_data before checkpoint/write. Async graph input is guarded before persistence; sensitive legacy checkpoints are refused on resume. Provider failures are replaced before LangGraph stores error writes. |
| External tracing | Automatic LangSmith tracing is disabled around async graph execution and model transport. App entry points do not install third-party raw-data callbacks. |
| Evaluation output | Shared recursive projection masks credential fields and omits headers/raw data. Failures retain code/type only. Runtime identity includes public_data_version to reject mixed old/new run directories. Historical reports remain unchanged. |

Public strings are scanned before truncation, normally at 2,000 characters. Public
collections are capped at 200 items and nesting at 12 levels. Canonical manifests,
evidence and private code do not use this lossy projection. New public fields need
an allowlist entry and a regression test.

## Private storage, access and retention

Service-owned workspace/artifact roots use mode 0700. Materialization stays
private; archived source bytes, file modes, manifests, edits and digests are
unchanged. Evaluation record directories use 0700; atomic temporary files use 0600.
Checkpoint access uses the service SQL role. No HTTP endpoint exposes checkpoints,
raw SDK responses or workspace archives. Settings representations omit credentials
and credential-bearing URL fields.

Private execution data is retained for approval/recovery and operator diagnosis.
Retention is manual in this version:

1. Restrict service UID, SQL role and backup access. API Key holders are trusted
   operators. Keep the optional no-key demo inside a trusted machine/network;
   complete Diff access remains disabled.
2. Keep waiting/running/cancelling task data while recovery or cleanup needs it.
   Do not delete a shared content-addressed artifact solely because one task ended.
3. Before disposal, stop/drain executors, preserve required evidence, identify all
   tasks/approvals/intents referencing an artifact, then remove checkpoints and
   workspace data under the operator's retention decision. Apply the same expiry
   to backups. There is no automatic purge or artifact TTL in this card.
4. Old private checkpoints containing credentials need operator review and fresh
   validated execution. Do not mass-replace recoverable code or reuse old evidence
   identities for edited bytes.

This is single-service access control. Production encryption, tenant authorization,
automatic retention/GC and stronger isolation remain CH-12/CH-16. Direct Python
saver/filesystem callers are trusted operators.

## Known patterns and compatibility

Patterns cover GitHub/OpenAI/Google token forms, private-key blocks, credential
field names, bearer/basic headers, URL userinfo and common credential query
parameters. Credential URLs are checked through at most two decoding rounds.
A match can omit a whole source file or reject a proposal; it never replaces
executable code. False positives are possible. This is not arbitrary secret
discovery; unknown secrets, custom encodings and deliberate exfiltration need
additional policy and review.

Public result/event projections may contain fewer fields; event Diff previews are
removed. Approval bindings stay approval-v2. Canonical source still comes from the
exact private artifact. A token-bearing invalid-path proposal now stops before
the write-policy retry because that proposal cannot be checkpointed.

Run the offline suite and make ch06c-integration. The latter reuses the dedicated
CH-06A harness with a distinct repopilot_ch06c_ project, port 58090 and three new
PostgreSQL/API/Worker probes, plus all earlier migration, cancellation, publication,
Mock, smoke and Worker restart checks. It uses no real model or application GitHub
publication calls.

# Trusted evaluation project adapters (CH-07A)

The current evaluator uses manifest schema **2**, `python-source-v1` adapters,
evaluator configuration **9**, acceptance protocol **4**, and `withheld-junit-v2`.
These are evaluation contracts; online task APIs and SQL revision `0007` are unchanged.

The real corpus remains the original 20 BugsInPy/The Fuck cases. The second project,
`tinyledger`, is an owned **synthetic fixture**, not a new historical defect or a
repair-quality measurement. Cross-project development/holdout curation is CH-07B.

## Configuration authority

`evals/real/manifest.json` is operator-owned configuration. It is not loaded from
the Issue, a candidate, a model response, or the upstream repository. Do not point
the evaluator at a repository-supplied manifest. The trusted orchestrator may access
source caches, manifests and the Docker daemon; child tests receive tar snapshots only.

Each `projects.<project>` entry declares:

| Field | Contract |
| --- | --- |
| `version` | `python-source-v1` |
| `source_roots` | Literal, contained directory paths; only existing Python files below them can be edited |
| `readonly_paths` | Literal paths/subtrees that remain read-only, even inside source roots |
| `test_roots` | Explicit read-only test subtrees; overlay destinations must belong to them |
| `visible_test_policy` | `buggy-tree-only-v1`: visible tests come from the buggy export |
| `acceptance_test_policy` | `withheld-overlay-v1`: fixed-version overlays run only in evaluator exports |
| `runtime.image` | Immutable local `sha256:<id>` or registry `name@sha256:<digest>`; no implicit image pulls |
| `runtime.python_version` | Explicit major.minor, equal to every task in that project |
| `runtime.pythonpath` | Contained paths relative to `/workspace`; `.` means `/workspace` |
| Runtime preparation | Pinned base image, evaluator-owned recipe path and SHA-256, install argv and a preparation description |
| `fingerprint` | SHA-256 of the complete canonical adapter, excluding this field |

Credential paths remain protected; create/delete/rename are not enabled. Source
roots are not reference-fix paths: an alternative fix can modify any permitted,
retrieved existing source file. `expected_fix_files` remains evaluator-only analysis.

The existing task records retain project, repository, full buggy/fixed Git IDs,
license SPDX/file, issue, distinct visible/acceptance argv, overlay source/replacement
hashes, reference paths and verification status. IDs and paths are portable and
contained. The loader verifies project/task fingerprints, recipe and overlay bytes,
test-root protection, command selectors, source policy and runtime version before
execution. The exported source must contain its declared non-empty license file.
Known credential-bearing configuration is rejected before diagnostic output.

## Three separate environments

1. **Agent:** `prepare_agent_workspace` exports the buggy Git tree with no history
   or benchmark files. `OfflineWorkspace` derives allowed files from the trusted
   adapter and the exported files. Only issue text, visible argv and bounded buggy
   context enter model calls/graph state. Planner hints remain disabled for initial
   retrieval in both experiment arms.
2. **Reproducer:** separate buggy/fixed exports run the same withheld suite in the
   project's pinned environment. Visible buggy tests must pass, buggy acceptance must
   fail by behavior, and fixed acceptance must pass with matching JUnit identities.
3. **Candidate acceptance:** permitted source changes are replayed into another
   fresh buggy export; verified overlays are then installed there. Hidden outcomes
   never return to Coder/Reviewer or their retry feedback.

Visible/acceptance commands select explicit test files within declared test roots.
The evaluator controls JUnit output. Directory-wide selection, shell commands,
configuration overrides and arbitrary pytest plugins are not supported by schema 2.

Both visible and acceptance Docker containers use the adapter's Python path, no
network or host mounts, UID 10001, dropped capabilities, and bounded memory/processes.
Dependencies are installed by the trusted operator while preparing the image.
`runtime.install_argv` is preparation metadata; evaluation never executes it or
installs dependencies from a candidate. Images use their resolved immutable IDs
before any reproduction or generation begins. Recipe hashes identify curated
recipes; they are not an attestation of the Docker build or a lock of every OS/
transitive dependency. Rebuilding may produce a different image and requires an
explicit manifest update and new experiment.

## Project bindings and experiment migration

The CLI accepts repeated mappings:

```bash
python -m repopilot.real_evaluation \
  --manifest /trusted/corpus/manifest.json \
  --source project_a=/trusted/cache/project_a \
  --source project_b=/trusted/cache/project_b \
  --output reports/new-experiment --reproduce-only
```

A bare `--source /path` remains valid for one selected project. Multiple projects
require complete explicit mappings; duplicates/unknown projects are rejected.
An optional `--image PROJECT=ALIAS` must resolve to the image pinned by that adapter;
it cannot silently replace the environment. The shell wrapper mounts the complete
trusted cache at `/sources` and evaluation assets at `/app/evals`. Its default
source mapping is The Fuck for the default 20-case manifest; custom manifests pass
their own mappings. The host upstream cache is never mounted into child containers.

`evals/real/manifest-v1.json` preserves the original manifest byte-for-byte
(SHA-256 `6b54dc349ba8f493e9cfb2702bf6c227c6b5c3da781e3246620779fdc9984471`).
All 20 task records/fingerprints are unchanged in schema 2; their adapter is new
top-level metadata. Schema 1 is rejected by normal execution. Explicit
`allow_legacy_read=True` loads a read-only metadata view with no executable adapter.

New runs default to `reports/real-v2`. `experiment-identity.json` binds manifest
digest, selected cases, project adapters/images/Python paths, evaluator and current
implementation, model/provider/temperature and call cap. A non-empty old directory
without this marker, or any different identity, is rejected before evidence is
rewritten. A matching directory can reuse exact reproduction records. Model-call
records continue using schema 4 and the CH-10/CH-06C budget/data contracts; their
new evaluator identity prevents mixing with v7/v8 runs. Original quality reports,
failure ledgers and old run directories are not migrated or overwritten.

## Validation and limits

```bash
python -m pytest -q tests/test_project_adapters.py tests/test_real_tasks.py
python evals/fixtures/tinyledger/prepare.py /new/owned/synthetic-cache
REPOPILOT_CH07A_DOCKER_TEST=1 python -m pytest -q \
  tests/test_project_adapters.py -k full_docker
python scripts/check_ch07a_compatibility.py \
  --source reports/real-corpus-cache/thefuck --output reports/ch07a-compatibility.json
make ch07a-integration
```

The deterministic fixture fixes `src/tinyledger/totals.py`, protects `checks/` and
an explicitly read-only Python file, uses `/workspace/src`, verifies fail-to-pass
and pass-to-pass outcomes, and stops workflow at approval without publication.
The real-model runner rejects `corpus.kind=synthetic` when generation is requested.
No fixture success is added to the 20 real cases or described as model ability.

CH-07A does not collect additional real defects, tune strategies, implement repeats/
statistical inference, or promise improved repair rates. Those remain CH-07B/C and
CH-01B/C. Read-only source inspection is safe on the host; upstream tests/code run
only in bounded Docker children. Docker still shares the host kernel and is not a VM.

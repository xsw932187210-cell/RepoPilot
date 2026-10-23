# Real-task corpus

The unchanged default `evals/real/manifest.json` defines the historical pilot: 20
Python bugs from **one** open-source project, The Fuck. It is useful for testing the
end-to-end evaluator with a cached dependency image; it is not representative of the
Python ecosystem and must not be described as SWE-bench or broad repair performance.
The separate frozen pilot below adds three reproduced cases without rewriting that
historical experiment identity.

## Provenance and license

The task identities and revision mappings come from the official
[BugsInPy repository](https://github.com/soarsmu/BugsInPy/tree/11c5f1eea954a42132cfd06bf257766a7963e0fd),
pinned at commit `11c5f1eea954a42132cfd06bf257766a7963e0fd`. BugsInPy describes
the benchmark as a database of existing bugs in real Python programs and records a
buggy commit, fixed commit, Python version, relevant test file and test selector for
each case. The upstream source is
[nvbn/thefuck](https://github.com/nvbn/thefuck), and each task pins full 40-character
buggy and fixed commit IDs. The source and test files retain The Fuck's
[MIT license](https://github.com/nvbn/thefuck/blob/444908ce1c17767ef4aaf9e0b4950497914f7f63/LICENSE.md).

The selected BugsInPy IDs are `1, 2, 5, 6, 7, 9, 10, 11, 12, 13, 15, 18, 19,
21, 22, 25, 26, 27, 31, 32`. They are focused unit-level regressions with a small
source patch in the upstream fix. Case 27 replaced an earlier candidate, case 8,
because 27 has a smaller, more isolated opener-rule test and avoids broad DNF help
parsing. This selection choice does not turn the cases into synthetic mutations:
every task still names an upstream buggy revision and its later upstream fix.

## Manifest contract

The manifest is evaluator-owned metadata and is never copied into a model workspace.
Schema 2 adds a top-level `projects` map: literal source/read-only/test roots, visible/hidden
test policies, pinned runtime image/Python path, dependency preparation and verified recipe
hashes. The original 20 task records and their fingerprints are unchanged. The byte-identical
schema-1 manifest is archived as `manifest-v1.json` and can only be loaded explicitly for reading.
See [project adapters and migration](project-adapters.md) for the complete contract.
Each task contains:

- a stable task ID and pinned BugsInPy case;
- upstream repository, buggy commit, fixed commit and license metadata;
- a user-facing behavioral problem statement that contains no reference patch;
- separate visible and acceptance pytest commands represented as argument vectors,
  never arbitrary shell scripts;
- one fixed-version test-file overlay with its destination, content SHA-256 and the
  SHA-256 of the exact buggy test file it may replace;
- expected upstream fix paths for analysis only;
- an `assembled` or `reproduction_verified` status; and
- a SHA-256 fingerprint over the canonical task record, excluding the fingerprint
  field itself.

`assembled` means the pinned metadata, commits and overlay were curated. It does not
mean the fail/pass pair has run successfully. A case may be labeled
`reproduction_verified` only when the same immutable evaluator image observes the
buggy revision fail and the fixed revision pass with matching JUnit test identities.
Reproduction evidence belongs in ignored `reports/`, tied to both the task fingerprint
and immutable image ID. At initial assembly all 20 manifest entries remain
`assembled`; run evidence, rather than prose, is authoritative.

## Leakage boundary

`repopilot.real_tasks.prepare_agent_workspace` uses `git archive` to export only the
buggy commit. The resulting tree has no `.git` directory, fixed commit object,
BugsInPy metadata, reference patch, or evaluator overlay. The agent receives only
this history-free tree and `issue.title` / `issue.body`.

The workflow receives only `test.visible_argv`, currently a common pre-existing
`tests/rules/test_cd_mkdir.py` smoke suite from the buggy tree. This path is present in
all 20 pinned revisions and is not an overlay destination. The loader rejects a
visible command that names any hidden overlay. The focused selector and its fixed
test contents remain in `test.acceptance_argv` and `hidden_overlay`; neither is put in
the graph state, prompts, visible test output or retry feedback.

After the agent has finished, the trusted evaluator replays allowed source edits into
a fresh buggy export and calls `materialize_hidden_overlay`. That function restricts
destinations to existing regular files below the adapter's read-only test roots (`tests/`
for this real corpus), checks source and target path
containment, rejects symlink escapes, verifies both the expected buggy-file SHA-256
and fixed overlay SHA-256, and then replaces the relevant test file. Requiring the
original hash prevents an overlay from silently overwriting a candidate-created or
wrong-revision file. The fixed commit is exported separately by
`prepare_reference_workspace` for evaluator-only validation. Neither
`source.fixed_commit`, `expected_fix_files`, acceptance selectors, overlay
paths/content, nor fixed workspace paths belong in prompts, retrieval input, tool
results, or retry feedback.

The overlay is the relevant test file taken from the task's fixed commit. This mirrors
BugsInPy's checkout procedure: it checks out the buggy source while installing the
test file from the fixed revision. It is independent of a candidate patch, but it is
not a full-project regression suite.

## Deterministic environment

All 20 cases share the same environment family. `Dockerfile.real-tasks` pins the
`python:3.8-slim` base image digest, pip/setuptools/wheel and the direct runtime/test
dependencies. The project is imported from `PYTHONPATH=/workspace`; it is not
installed from a network VCS requirement. Child test containers run without network
or model credentials.

The historical tests use pytest's removed `Node.get_marker` method. The image loads
`evals/runtime/sitecustomize.py`, which aliases that call to `get_closest_marker`.
This is an environment-compatibility shim, not a defect fix: the same shim is present
for buggy, fixed and candidate executions, and neither the upstream source nor hidden
assertions are rewritten. Image identity is recorded so a shim or dependency change
cannot silently reuse prior reproduction evidence.

## Local validation

The fast, offline corpus checks are:

```bash
python -m pytest -q tests/test_real_tasks.py
```

They validate schema version, task fingerprints, unique IDs, overlay checksums, path
containment, symlink rejection and history-free Git export. With a trusted upstream
cache at `reports/real-corpus-cache/thefuck`, the reproduction-only evaluation is:

```bash
bash scripts/eval_real.sh --reproduce-only
```

Upstream code and tests are never executed directly on the host. The reproduction
runner exports disposable trees and runs them in the bounded Docker acceptance
environment. No model call is needed for corpus reproduction.

## Frozen cross-project pilot v1

`freeze-pilot-v1.json` freezes `manifest-pilot-v1.json`, reproduction counts,
provenance, exclusions, license copies and evaluator-only public-symptom probes.
Fingerprint: `c616e16419a9313b46ff733dc201a8f0a8bd86de313301f1b9fae266099c2adf`.
There are 23 real defects, not synthetic mutations or repeated executions.

Reproduction verifies the focused buggy/fixed behavior, not every future graph
integration. The first CH-01C run exposed Cookiecutter case 3's upstream documentation
gitlink: its archive export was valid, but the raw artifact-base reader correctly
refused that non-blob entry. Evaluator v14 adds an exported-file baseline and a
mandatory all-case artifact preflight; see the
[runtime correction](ch01c-corrected-plan-2026-10-23.md). No submodule is fetched.

| Partition | Project | Cases | Selection |
| --- | --- | --- | --- |
| development | The Fuck | 20 | Original records/fingerprints unchanged; previously analyzed |
| holdout | tqdm | 1 | BugsInPy 3; remaining no-total boolean failure |
| holdout | Cookiecutter | 2 | BugsInPy 3/4; duplicate choice prompt and failed pre-hook |

Whole projects stay in one partition. The same coding assistant inspected reference
fixes/tests for provenance, environment preparation and QA; no holdout answers or
results were used for retrieval/prompt/router tuning. This is not a blind-curator or
unseen-training-data claim. Public training contamination is unknown, and three
holdout cases across two projects cannot provide precise generalization estimates.

The [exclusion list](../evals/real/exclusions-pilot-v1.json) records eight inspected
candidates: missing pre-existing fixture destinations, single-test-file leakage
conflicts, a feature request, a near-duplicate, an oversized source file and an
insufficiently matched Issue. This is not an exhaustive survey; about 50 cases remains
an expansion target. Primary new sources are [tqdm #353](https://github.com/tqdm/tqdm/issues/353),
[Cookiecutter #1167](https://github.com/cookiecutter/cookiecutter/issues/1167) /
[PR #1168](https://github.com/cookiecutter/cookiecutter/pull/1168), and
[Cookiecutter #464](https://github.com/cookiecutter/cookiecutter/issues/464) /
[PR #549](https://github.com/cookiecutter/cookiecutter/pull/549). Pinned benchmark
metadata is retained in `provenance/pilot-v1.json`; full OIDs/overlay hashes are in
the manifest. `licenses/` preserves The Fuck MIT, tqdm MIT/MPL-2.0 and Cookiecutter
BSD-3-Clause notices with the copyright years of the selected revisions.

All 23 cases passed fixed-image Docker fail/pass reproduction and visible checks.
The three new cases also have direct public-symptom checks, including executing the
failing hook instead of relying solely on the upstream assertion's missing exception
class. These are focused tests, not upstream full suites; six failing test instances
in the new cases are still three defects. See [verification](ch07b-verification-2026-10-23.md).

The historical base is `sha256:4488c6ee019a76b875dcbd99737dbeb5163c5bc5221c4ab491494a7890664e58`.
The tqdm extension is `sha256:30ed8955d2abaccd55551edc84c03e7a0ec0140806eb3062d6289a56c12f37a2`;
Cookiecutter is `sha256:71636a241413be383bd592c2cdcbd0609a86e68e5432c23bafc117851d59910a`.
Both use Python 3.8.20 / pytest 6.2.5 rather than historical Python 3.6.9. All ports
are documented in the recipe/provenance. The tqdm environment retains the previously
prepared python-toolbox package (unused here) and nose. Cookiecutter adds its pinned
dependencies and coverage plugin. No upstream VCS package, tox or setup hook executes
on the host. Trusted preparation verifies the local base alias against the pinned
ID before building. Recipes/versions are recorded, but builds are not bit-for-bit
reproducible: a different image ID requires a new adapter/freeze and rerun.

Metadata checks require no cache, Docker or model:

```bash
python scripts/verify_frozen_corpus.py --metadata-only
python -m pytest -q tests/test_frozen_corpus.py tests/test_real_tasks.py
```

With trusted no-checkout Git caches and the frozen images available:

```bash
python scripts/verify_frozen_corpus.py \
  --source thefuck=reports/real-corpus-cache/thefuck \
  --source tqdm=reports/real-corpus-cache/tqdm \
  --source cookiecutter=reports/real-corpus-cache/cookiecutter \
  --output reports/pilot-v1-reproduction
```

The orchestrator reads Git objects as data and launches snapshot-only Docker children.
Private test output is separate from count-only summaries. Probes, overlays and fixed
trees never enter Agent exports. The legacy real-model entry still uses the original
manifest until an experiment explicitly selects the new freeze.

`contamination-pilot-v1.json` is an append-only operator record outside the immutable
asset map; its current digest is returned for experiment binding. Reference-example,
prompt-tuning or router-tuning events make `select("holdout")` fail. Reclassify the
whole project in a new freeze and choose a new holdout; do not edit the old split or
erase exposure events. A predeclared issue-only evaluation is not itself tuning;
using its answers/hidden feedback/outcomes to revise strategy is exposure for future runs.

# Real-task corpus

`evals/real/manifest.json` defines a deliberately narrow pilot corpus: 20 historical
Python bugs from **one** open-source project, The Fuck. It is useful for testing the
end-to-end evaluator with a cached dependency image; it is not representative of the
Python ecosystem and must not be described as SWE-bench or broad repair performance.

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
destinations to existing regular files below `tests/`, checks source and target path
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

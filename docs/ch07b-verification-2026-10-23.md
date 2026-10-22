# CH-07B verification — 2026-10-23

Verified source: `1c5e61b4baa74d38eb8124829a9e3aa4b8a46e60`; base
`d7e9d21e7aa8f42b53fca73c0846d229edcd4810` (model integration PR #12 merged into
the CH-07A feature branch). Main still needs the stacked dependency integration.

The frozen `python-pilot-v1` contains 23 real defects across three projects: the
unchanged historical The Fuck 20 form development/regression, while tqdm 1 and
Cookiecutter 2 form a project-disjoint holdout. Freeze fingerprint:
`c616e16419a9313b46ff733dc201a8f0a8bd86de313301f1b9fae266099c2adf`.
This delivers a small pilot; about 50 cases remains an expansion target.

| Check | Observed result |
| --- | --- |
| Full host Python 3.13.5 | 445 passed / 42 service-conditional skips, 85.92%, 60.04 s |
| Linux Python 3.12 corpus/freeze boundary tests | 29 passed, zero skips, 1.02 s |
| Fixed-image Docker corpus reproduction | 23/23; buggy exit 1, fixed/visible exit 0, matching JUnit identities |
| Extra public-symptom checks | 3/3 fail on buggy, pass on fixed; no reference-patch application |
| Agent export/context checks | 5,399 file instances, history-free buggy exports, original tests/read-only roots, no reference identity in context |
| Provenance | Full BugsInPy revision, upstream Issues/commits, fixed overlays and license copies bound by hashes |
| Secrets | Known current credentials: zero matches in all reachable Git objects |
| Real model calls | 0 for this card |

The Linux boundary run reused immutable test image
`sha256:952e06f59889273d528f430f4db4023684033a342a82b59f1b45f68f1e74ee11`
with current source/tests/corpus mounted read-only, no network, credentials, socket or
database. It is not a new installed-image/full-service acceptance run. The data and
freeze reader do not change API/Worker behavior; the separate mixed-model 463-test
service acceptance remains its own evidence. The new tests cover asset mutation,
split overlap/missing/project crossing, wrong reproduction identities/environments,
exposure invalidation and path containment.

New runtime extensions use Python 3.8.20. Cookiecutter keeps Click 7.1.2, Jinja2
2.11.2, pytest 6.2.5 and fixed supporting dependencies. MarkupSafe 2.0.1, future
0.18.3 and six 1.16.0 are explicit environment ports from the pinned benchmark
metadata; tox is replaced by direct focused pytest. The existing historical base
and its compatibility shim are shared by buggy/fixed/candidate. Upstream source,
tests and setup hooks were never executed on the host. All child containers have
no network, host mounts, credentials or Docker socket and are removed after use;
dependency images are retained.

The first Cookiecutter attempt lacked the repository's configured pytest-cov
plugin and selected a nested helper rather than top-level `test_workin`; neither
was counted as a defect failure. The environment/selector were corrected and all
23 cases rerun. The first bare-ID Docker FROM was rejected before installation;
the successful build used a verified local base alias and recorded its immutable
identity. The initial host run had 444 passes and one loopback-test timeout inside
the execution sandbox (85.37%); the unchanged source passed the complete rerun
with local loopback access. These intermediate logs are retained in `reports/`.

For tqdm the selected buggy commit already handles an explicit `total`. The
remaining public symptom is boolean conversion of an unknown-length iterable
without `total`; the initial explicit-total check passed both versions and was
not counted as evidence. Cookiecutter case 4's upstream failing assertion first
references a missing exception class, so a separate check actually executes an
exit-1 hook and confirms that the buggy API returns normally while the fixed API
raises. Case 3 also checks real Click prompt output separately from upstream
mock-call assertions. These corroborating checks are evaluator-only assets, not
additional defects and never copied into an Agent workspace.

Raw evidence: `reports/ch07b-reproduction-final/`, `reports/ch07b-frozen-qa/`,
`reports/ch07b-symptoms/`, `reports/ch07b-host-final.*`,
`reports/ch07b-linux-boundaries.log`. The [machine summary](evidence/2026-10-23-ch07b-verification.json)
binds 97 source/test/script hashes to the verified source and omits hidden test identities.

The original 20 task records/fingerprints and 2026-09-09 quality report, failure
analysis/ledger and Mock dataset remain unchanged. Curators read reference changes
for QA; no repair prompt/router tuning used holdout cases. Public training-data
contamination is unknown. Focused tests and a three-case holdout cannot establish
broad repair quality. CH-07C, CH-01B and CH-01C remain separate work. The user's
new 108-attempt authorization for CH-01C has not been consumed by this card.

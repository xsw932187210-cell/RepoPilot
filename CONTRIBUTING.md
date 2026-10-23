# Contributing

Read [AGENTS.md](AGENTS.md) and the current card in [Openspec.md](Openspec.md). Use one focused
`codex/` feature branch and PR per verifiable goal. Mark implementation, verification and merge
separately; preserve local work and historical evaluation identities.

For Docker-based checks:

```sh
make check   # locked install, lint, fast tests, deterministic Mock benchmark
make verify  # isolated full services, faults, smoke and Worker approval restart
```

See [reproducible delivery](docs/reproducible-delivery.md) for local virtual environments,
environment checks and CI triggers. Tests use Mock without external model credentials. Add
boundary tests for routing, tools or API changes; service faults require actual dedicated
services rather than a unit-test substitute. A skipped probe is not a passed acceptance test.

Never commit `.env`, `.local`, raw reports, credentials, authorization URLs or private patches.
Builds exclude private directories. Public evidence uses reviewed fields and includes exact
dataset/implementation identities. Keep independent acceptance data out of model inputs and
leave all upstream fixture bytes and hashes unchanged.

Real-model experiments require an explicit protocol, independent output directory and confirmed
request budget, including failures and retries. Preserve negative, pending and unknown results;
do not reset consumed budgets or use a different denominator to improve a result.

Use the [PR template](.github/pull_request_template.md). Describe the observable change, checks
actually run, unresolved limitations and evidence links. Document compatible upgrades and
recovery when configuration or persistent data changes. Follow
[third-party attribution](THIRD_PARTY_NOTICES.md) for copied material.

To report a bug, include the source commit, doctor check names/statuses, reproduction steps and
sanitized aggregate evidence. Do not attach complete local configuration, tokens, model text or
the entire reports directory.

# RepoPilot contribution guide

- Keep the LangGraph workflow explicit: deterministic checks belong in graph nodes, not prompts.
- Never log API keys, GitHub tokens, authorization headers, or credential-bearing URLs.
- All file paths produced by a model must pass containment checks before reads or writes.
- Side-effecting GitHub actions require both a LangGraph human interrupt and explicit runtime enablement.
- Tests must run without external model or network access by using the mock model and demo repository.
- Add or update tests whenever graph routing, tool boundaries, or API contracts change.

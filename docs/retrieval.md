# Code retrieval contract

RepoPilot retrieves a bounded, explainable code context before the coder model runs. This iteration
uses an offline hybrid strategy named `hybrid-bm25-symbol-v1`; it requires no embedding service or
external network call.

## Ranking pipeline

1. Enumerate text source files while excluding `.git`, virtual environments, build output,
   dependencies, binary files, and files above the configured safety limit.
2. Tokenize prose, paths, `snake_case`, and `camelCase` identifiers.
3. Score document content with BM25 (`k1=1.5`, `b=0.75`).
4. Add explicit path-match and Python AST class/function symbol scores. Apply an
   implementation-first prior for code-change tasks so repeated assertions do not rank a test
   above the source symbol they exercise.
5. Build one-hop Python import and source/test counterpart edges, then boost neighbors of the top
   lexical/symbol seeds.
6. Select positive-score whole files under both `MAX_CONTEXT_FILES` and
   `MAX_CONTEXT_CHARS`; when nothing matches, use at most three deterministic fallback files.

The strategy deliberately keeps score components separate. Each selected file records lexical,
path, symbol, role-prior, and dependency scores; matched terms and symbols; related seed paths; and
content size. Researcher events, checkpoint state, final task results, and evaluation reports expose
this evidence without storing credentials or file bodies in event payloads.

## Context safety

`MAX_CONTEXT_CHARS` defaults to 70,000 characters and reserves part of the budget for the repository
tree and ranking evidence. A file that cannot fit is skipped and counted in
`skipped_for_budget`; selected file bodies are never silently truncated. Model-generated output
still passes the existing containment and file-size checks before it can modify a workspace.

## Evaluation

The deterministic benchmark measures the first retrieval, before any generated edit. The initial
snapshot is checkpointed separately from the context refreshed during retries; final task results
expose both `initial_retrieval` and the latest `retrieval`. It reports:

- target recall across the full selected context;
- the fraction of cases where all expected files are selected;
- Recall@3 and Recall@5;
- mean reciprocal rank (MRR);
- candidate and selected file counts;
- selected context characters.

Recall is the fraction of expected files retrieved, not an all-or-nothing per-case flag. Aggregate
recall is a macro-average over cases; MRR uses the first relevant file's rank. Reports record the
context-file, character, and file-size limits alongside the dataset hash.

Run `make eval` to reproduce the report. The bundled fixture is intentionally small, so a perfect
score is only a workflow regression signal. Defensible large-repository claims require a separate,
versioned corpus with real repositories and issue-level ground truth.

## Known limits

- AST symbols and import resolution are currently Python-focused.
- Ranking is per task and in memory; no persistent incremental index exists.
- There are no embeddings, cross-encoder reranking, call-graph extraction, or semantic chunks yet.
- Oversized files are skipped rather than retrieved as safe editable fragments.

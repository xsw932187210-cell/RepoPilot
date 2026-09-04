from __future__ import annotations

import ast
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_GENERIC_SYMBOL = re.compile(
    r"\b(?:class|def|function|fn|func|interface|struct|type)\s+([A-Za-z_]\w*)"
)
_GENERIC_IMPORT = re.compile(
    r"(?:\bfrom\s+['\"]([^'\"]+)['\"]|\brequire\(['\"]([^'\"]+)['\"]\))"
)
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}


def tokenize(value: str) -> tuple[str, ...]:
    """Tokenize prose, paths, snake_case, and camelCase identifiers deterministically."""

    expanded = _CAMEL_BOUNDARY.sub(" ", value).replace("_", " ")
    return tuple(
        token
        for match in _IDENTIFIER.finditer(expanded)
        if (token := match.group(0).casefold()) not in _STOP_WORDS
        and (len(token) >= 2 or token.isdigit())
    )


@dataclass(frozen=True, slots=True)
class CodeDocument:
    path: str
    content: str
    tokens: tuple[str, ...]
    path_tokens: frozenset[str]
    symbols: tuple[str, ...]
    imports: tuple[str, ...]
    is_test: bool


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    path: str
    content: str
    score: float
    lexical_score: float
    path_score: float
    symbol_score: float
    role_score: float
    dependency_score: float
    matched_terms: tuple[str, ...]
    matched_symbols: tuple[str, ...]
    related_paths: tuple[str, ...]

    def evidence(self) -> dict[str, object]:
        return {
            "path": self.path,
            "score": round(self.score, 4),
            "lexical_score": round(self.lexical_score, 4),
            "path_score": round(self.path_score, 4),
            "symbol_score": round(self.symbol_score, 4),
            "role_score": round(self.role_score, 4),
            "dependency_score": round(self.dependency_score, 4),
            "matched_terms": list(self.matched_terms),
            "matched_symbols": list(self.matched_symbols),
            "related_paths": list(self.related_paths),
            "content_chars": len(self.content),
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    hits: tuple[RetrievalHit, ...]
    query_terms: tuple[str, ...]
    candidate_count: int
    selected_chars: int
    skipped_for_budget: int
    strategy: str = "hybrid-bm25-symbol-v1"


def _python_metadata(content: str) -> tuple[set[str], set[str]]:
    symbols: set[str] = set()
    imports: set[str] = set()
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return symbols, imports

    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return symbols, imports


def _document(path: str, content: str) -> CodeDocument:
    symbols = set(_GENERIC_SYMBOL.findall(content))
    imports = {left or right for left, right in _GENERIC_IMPORT.findall(content)}
    if PurePosixPath(path).suffix.casefold() == ".py":
        python_symbols, python_imports = _python_metadata(content)
        symbols.update(python_symbols)
        imports.update(python_imports)
    path_tokens = frozenset(tokenize(path))
    lowered_parts = {part.casefold() for part in PurePosixPath(path).parts}
    filename = PurePosixPath(path).name.casefold()
    return CodeDocument(
        path=path,
        content=content,
        tokens=tokenize(content),
        path_tokens=path_tokens,
        symbols=tuple(sorted(symbols, key=str.casefold)),
        imports=tuple(sorted(imports, key=str.casefold)),
        is_test=(
            "tests" in lowered_parts
            or filename.startswith("test_")
            or filename.endswith("_test.py")
        ),
    )


def _module_aliases(path: str) -> set[str]:
    pure = PurePosixPath(path)
    if pure.suffix.casefold() != ".py":
        return set()
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    aliases = {".".join(parts[index:]) for index in range(len(parts))}
    return {alias for alias in aliases if alias}


def _normalized_stem(path: str) -> str:
    stem = PurePosixPath(path).stem.casefold()
    if stem.startswith("test_"):
        stem = stem[5:]
    if stem.endswith("_test"):
        stem = stem[:-5]
    return stem


def _dependency_graph(documents: list[CodeDocument]) -> dict[str, set[str]]:
    graph: defaultdict[str, set[str]] = defaultdict(set)
    aliases: defaultdict[str, set[str]] = defaultdict(set)
    for document in documents:
        for alias in _module_aliases(document.path):
            aliases[alias].add(document.path)

    for document in documents:
        for imported in document.imports:
            normalized = imported.lstrip(".").replace("/", ".")
            candidates = aliases.get(normalized, set())
            if not candidates:
                matching_aliases = [
                    alias
                    for alias in aliases
                    if alias.endswith(f".{normalized}") or normalized.endswith(f".{alias}")
                ]
                if matching_aliases:
                    longest = max(matching_aliases, key=lambda alias: (len(alias), alias))
                    candidates = aliases[longest]
            for candidate in candidates:
                if candidate != document.path:
                    graph[document.path].add(candidate)
                    graph[candidate].add(document.path)

    by_stem: defaultdict[str, list[CodeDocument]] = defaultdict(list)
    for document in documents:
        by_stem[_normalized_stem(document.path)].append(document)
    for matches in by_stem.values():
        tests = [document for document in matches if document.is_test]
        sources = [document for document in matches if not document.is_test]
        for test in tests:
            for source in sources:
                graph[test.path].add(source.path)
                graph[source.path].add(test.path)
    return dict(graph)


class HybridCodeRetriever:
    """Explainable offline retrieval for code, symbols, and one-hop dependencies."""

    def __init__(self, *, max_files: int, max_context_chars: int):
        self.max_files = max_files
        self.max_context_chars = max_context_chars

    def retrieve(
        self,
        files: dict[str, str],
        *,
        issue_text: str,
        search_terms: list[str],
    ) -> RetrievalResult:
        documents = [_document(path, content) for path, content in sorted(files.items())]
        query_terms = tuple(sorted(set(tokenize("\n".join([issue_text, *search_terms])))))
        query_set = set(query_terms)
        if not documents:
            return RetrievalResult(
                hits=(),
                query_terms=query_terms,
                candidate_count=0,
                selected_chars=0,
                skipped_for_budget=0,
            )

        counters = {document.path: Counter(document.tokens) for document in documents}
        lengths = {path: sum(counter.values()) for path, counter in counters.items()}
        average_length = sum(lengths.values()) / len(documents) or 1.0
        frequencies = {
            term: sum(counter.get(term, 0) > 0 for counter in counters.values())
            for term in query_terms
        }

        base_scores: dict[str, dict[str, object]] = {}
        for document in documents:
            counter = counters[document.path]
            lexical = 0.0
            matched_terms: list[str] = []
            for term in query_terms:
                frequency = counter.get(term, 0)
                if not frequency:
                    continue
                matched_terms.append(term)
                document_frequency = frequencies[term]
                inverse_frequency = math.log(
                    1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + 1.5 * (
                    1 - 0.75 + 0.75 * lengths[document.path] / average_length
                )
                lexical += inverse_frequency * (frequency * 2.5) / denominator

            matched_path_terms = sorted(query_set & document.path_tokens)
            matched_symbols = tuple(
                symbol
                for symbol in document.symbols
                if query_set.intersection(tokenize(symbol))
            )
            path_score = 1.75 * len(matched_path_terms)
            matched_symbol_terms = {
                term
                for symbol in matched_symbols
                for term in tokenize(symbol)
                if term in query_set
            }
            symbol_score = 2.5 * len(matched_symbol_terms)
            relevance_score = lexical + path_score + symbol_score
            role_score = (
                -0.5 * relevance_score
                if document.is_test
                else 3.0 if matched_path_terms or matched_symbols else 0.0
            )
            base_scores[document.path] = {
                "lexical": lexical,
                "path": path_score,
                "symbol": symbol_score,
                "role": role_score,
                "matched_terms": tuple(sorted(set(matched_terms) | set(matched_path_terms))),
                "matched_symbols": matched_symbols,
            }

        seeds = sorted(
            documents,
            key=lambda document: (
                -sum(
                    float(base_scores[document.path][key])
                    for key in ("lexical", "path", "symbol", "role")
                ),
                document.path,
            ),
        )
        positive_seeds = [
            document
            for document in seeds
            if sum(
                float(base_scores[document.path][key])
                for key in ("lexical", "path", "symbol", "role")
            )
            > 0
        ][: max(2, min(5, self.max_files // 2 or 1))]
        dependency_graph = _dependency_graph(documents)
        dependency_scores: defaultdict[str, float] = defaultdict(float)
        related_paths: defaultdict[str, set[str]] = defaultdict(set)
        for seed in positive_seeds:
            for related in sorted(dependency_graph.get(seed.path, set())):
                dependency_scores[related] = min(4.5, dependency_scores[related] + 1.5)
                related_paths[related].add(seed.path)

        ranked: list[RetrievalHit] = []
        for document in documents:
            components = base_scores[document.path]
            dependency_score = dependency_scores[document.path]
            score = sum(
                float(components[key]) for key in ("lexical", "path", "symbol", "role")
            ) + dependency_score
            ranked.append(
                RetrievalHit(
                    path=document.path,
                    content=document.content,
                    score=score,
                    lexical_score=float(components["lexical"]),
                    path_score=float(components["path"]),
                    symbol_score=float(components["symbol"]),
                    role_score=float(components["role"]),
                    dependency_score=dependency_score,
                    matched_terms=components["matched_terms"],  # type: ignore[arg-type]
                    matched_symbols=components["matched_symbols"],  # type: ignore[arg-type]
                    related_paths=tuple(sorted(related_paths[document.path])),
                )
            )
        ranked.sort(key=lambda hit: (-hit.score, hit.path))

        reserve = min(12_000, self.max_context_chars // 4)
        content_budget = max(1_000, self.max_context_chars - reserve)
        eligible_hits = [hit for hit in ranked if hit.score > 0]
        if not eligible_hits:
            eligible_hits = ranked[: min(3, self.max_files)]
        selected: list[RetrievalHit] = []
        selected_chars = 0
        skipped_for_budget = 0
        for hit in eligible_hits:
            section_chars = len(hit.content) + len(hit.path) + 24
            if selected_chars + section_chars > content_budget:
                skipped_for_budget += 1
                continue
            selected.append(hit)
            selected_chars += section_chars
            if len(selected) >= self.max_files:
                break

        return RetrievalResult(
            hits=tuple(selected),
            query_terms=query_terms,
            candidate_count=len(documents),
            selected_chars=selected_chars,
            skipped_for_budget=skipped_for_budget,
        )

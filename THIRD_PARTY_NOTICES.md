# Third-party material and attribution

RepoPilot's original code is under the root [MIT license](LICENSE). Dependencies and copied
evaluation fixtures retain their own licenses; the root license does not replace those notices.

| Material | Origin and identity | Retained notices |
| --- | --- | --- |
| Real-defect identities and revision mappings | BugsInPy, commit `11c5f1eea954a42132cfd06bf257766a7963e0fd` | [Corpus provenance](docs/real-task-corpus.md#provenance-and-license), [pilot provenance](evals/real/provenance/pilot-v1.json) |
| The Fuck test overlays | Revisions pinned per task in the manifest; source and tests licensed MIT | [MIT notice](evals/real/licenses/thefuck.txt) |
| tqdm test overlay | tqdm-003 selected upstream revisions; MIT/MPL-2.0 notices | [Upstream notice](evals/real/licenses/tqdm-003.txt) |
| Cookiecutter test overlays | cookiecutter-003/004 selected upstream revisions; BSD-3-Clause | [003 notice](evals/real/licenses/cookiecutter-003.txt), [004 notice](evals/real/licenses/cookiecutter-004.txt) |
| Python dependencies | Exact distributions and artifact hashes in [uv.lock](uv.lock) | Installed distributions retain their package license files and metadata |

The manifest, freeze and provenance files bind selected upstream commits and fixture hashes.
Keep copied fixture bytes and copyright notices intact. Reference revisions, evaluator overlays
and independent acceptance outcomes remain evaluator-side data, not model context or prompt
tuning material. Their publication in this repository does not make an observed case an unseen
quality benchmark.

The project is an independent learning/portfolio implementation inspired by the architectural
ideas of LangChain Open-SWE, as described in [README](README.md). It is not an official LangChain,
OpenAI or Google product. New third-party snippets must record their exact source and license
before inclusion; dependency updates and fixture updates are separate changes.

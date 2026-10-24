# Initial market validation — 24 October 2026

This is a preliminary public-web scan, not proof that no competing project exists. Search indexing is incomplete and private/internal tools are not visible.

## The problem is real

- Splink's blocking guide describes blocking rules as a primary determinant of runtime and explains the trade-off between avoiding comparisons and retaining true matches: <https://moj-analytical-services.github.io/splink/demos/tutorials/03_Blocking.html>
- Splink has first-party helpers to count comparisons created by blocking rules and inspect large blocks: <https://moj-analytical-services.github.io/splink/topic_guides/blocking/blocking_rules.html>
- The Python Record Linkage Toolkit exposes reduction ratio and true-positive / false-positive evaluation functions: <https://recordlinkage.readthedocs.io/en/latest/ref-evaluation.html>
- ER-Evaluation focuses on entity-level evaluation, summary statistics, uncertainty estimates, and error analysis: <https://pypi.org/project/ER-Evaluation/>
- A recent public entity-resolution benchmark explicitly reports pair completeness and reduction ratio together, demonstrating why recall and compute cost need to be examined side by side: <https://github.com/alex-hahn/catalog-forge>

## Existing adjacent solutions

| Project | Existing capability | How CandGuard would differ |
|---|---|---|
| Splink | Blocking-rule diagnostics within the Splink workflow | CandGuard aims to audit exported candidate-pair files from any framework, without adopting Splink |
| Python Record Linkage Toolkit | Pair-level classification metrics and reduction ratio utilities | CandGuard wraps candidate-file auditing and threshold exit codes into a CLI |
| ER-Evaluation | Entity-centric metrics and error analysis | CandGuard focuses narrowly on the candidate-generation stage before scoring/clustering |
| BlockingPy | Candidate-generation algorithms using approximate-nearest-neighbor methods | CandGuard is an evaluator/gate, not a candidate generator |

## Differentiation hypothesis

A small CLI that (1) accepts generic CSV/TSV output, (2) computes pair completeness against labeled matches, (3) quantifies candidate explosion and duplicate rows, (4) optionally exports missed pairs, and (5) enforces thresholds in CI may save teams from custom per-project scripts.

This is a **hypothesis to validate with users**, not a claim of global uniqueness. Existing systems can implement similar functionality, and some unindexed project may already do the full combination.

## Validation plan before wider promotion

1. Run a fresh search on GitHub, PyPI, and npm for candidate-pair audit CLIs and CI gates.
2. Ask 5–10 people who work on record linkage, data quality, or ML competitions whether they would use this outside a one-off benchmark.
3. Benchmark against a known reference implementation and synthetic files at 1M, 10M, and 50M candidate rows.
4. Only claim scale support after recording hardware, input layout, runtime, peak memory, and temporary disk usage.
5. If a mature existing tool already provides this exact generic workflow, prefer contributing the missing feature upstream over maintaining a duplicate.

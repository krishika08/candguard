# CandGuard

**A quality gate for entity-resolution candidate generation.**

Candidate generation (also called *blocking*) decides which record pairs a matching model is allowed to inspect. If a true pair is filtered out here, a later model cannot recover it. But evaluating blocking often means writing one-off scripts, and the best trade-off depends on both candidate recall and the number of comparisons produced.

CandGuard is a small, local-first CLI that audits CSV/TSV candidate-pair files and can fail a command when quality thresholds are missed. It is framework-agnostic: use it with output from your own blocker, a competition pipeline, or an existing record-linkage package.

> **Status:** experimental MVP. CSV/TSV only. SQLite provides disk-backed processing validated up to 10,000,000 candidate pairs (measured at 34.9s–49.7s elapsed wall time and ~419 MB peak resident memory in isolated processes on an Intel Core Ultra 5 125H with 16 GiB RAM). Peak memory increases sub-linearly with dataset size and is an empirical measurement, not a constant-memory guarantee; larger workloads (>10M rows) have not been validated. See [`docs/benchmark-report.md`](docs/benchmark-report.md) for full benchmarks and hardware details.

## Quick start

Requires Python 3.10+.

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e .

candguard audit \
  --candidates examples/candidates.tsv \
  --truth examples/truth.tsv \
  --left-id source_id \
  --right-id candidate_id \
  --min-recall 0.60
```

To audit your own files, each file must have a header. The candidate file contains the pairs passed to your matching model; the optional truth file contains known matching pairs. IDs are compared as trimmed strings. In cross-linkage mode, pair direction is preserved; in deduplication mode, pairs are treated as logically unordered and canonicalized.

### Optional Parquet input

CSV/TSV support has no dependencies. To also read `.parquet` candidate or ground-truth files, install the optional PyArrow extra:

```bash
pip install "candguard[parquet]"        # or, from a checkout: pip install -e ".[parquet]"

candguard audit --candidates candidates.parquet --truth truth.parquet   --left-id source_id --right-id candidate_id
```

Identifier columns must be string or integer typed. Strings are trimmed like CSV/TSV values, integers become decimal text, and nulls or other types are rejected. See [`docs/parquet-guide.md`](docs/parquet-guide.md) for type rules, error messages, Python-version compatibility, and measured CSV/TSV/Parquet comparisons.

## Example output

The bundled data is intentionally tiny and synthetic:

```text
CandGuard — Candidate Generation Audit
==========================================
Candidate rows:                 5
Unique candidate pairs:         4
Duplicate candidate rows:       1
Source records represented:     3
Target records represented:     3

Candidates per represented source record
-----------------------------------------
Min / median / mean:             1 / 1 / 1.3333
P95 / P99 / max:                 2 / 2 / 2

Ground-truth audit
------------------
Unique known matching pairs:     3
Recovered by candidate set:      2
Missing before model scoring:    1
Candidate recall:                66.6667%

Quality gate: PASS
```

Numbers above are illustrative; run the command to see the exact output.

## Useful options

Measure recall against labeled pairs and save the pairs the blocker missed:

```bash
candguard audit \
  --candidates out/candidates.tsv \
  --truth data/known_matches.tsv \
  --left-id source_id --right-id target_id \
  --missed-pairs-out out/missed_pairs.tsv \
  --format json --output out/audit.json
```

Estimate how aggressively the candidate generator reduces the search space for two datasets:

```bash
candguard audit \
  --candidates out/candidates.tsv \
  --left-id source_id --right-id target_id \
  --left-records 100000 --right-records 80000
```

For within-dataset deduplication, use `--linkage-type dedupe --left-records N` instead. In dedupe mode, candidate pairs and truth pairs are treated as unordered, self-pairs are disallowed, and candidate counts represent comparisons per represented record. Reduction ratio is reported only when record counts are supplied.

Use the CLI as a lightweight CI gate:

```bash
candguard audit \
  --candidates out/candidates.tsv \
  --truth data/known_matches.tsv \
  --left-id source_id --right-id target_id \
  --min-recall 0.95 \
  --max-pairs 25000000 \
  --max-duplicate-rows 0
```

Exit codes: `0` means all configured gates passed, `1` means a metric gate failed, and `2` means invalid input or configuration. A `--min-recall` gate requires a truth file.

## Regression testing with `candguard compare`

Candidate generation changes (new blocking keys, indexing rules, or nearest-neighbor thresholds) frequently trade recall against comparison volume. `candguard compare` enables automated CI checks between a baseline audit (e.g., from `main`) and a candidate PR branch to prevent silent recall regressions or comparison volume explosions.

### Example comparison

Save audit reports as JSON from both branches:

```bash
# Baseline run (e.g. on main branch)
candguard audit \
  --candidates base_candidates.tsv --truth truth.tsv \
  --left-id source_id --right-id candidate_id \
  --format json --output reports/baseline.json

# Current run (e.g. on candidate PR branch)
candguard audit \
  --candidates pr_candidates.tsv --truth truth.tsv \
  --left-id source_id --right-id candidate_id \
  --format json --output reports/current.json
```

Run regression checks against thresholds:

```bash
candguard compare \
  --baseline reports/baseline.json \
  --current reports/current.json \
  --max-recall-drop 0.01 \
  --max-pair-growth 0.20 \
  --max-duplicate-growth 0.10
```

### Example compare output

```text
CandGuard — Regression Comparison Audit
==========================================
Baseline report:                reports/baseline.json
Current report:                 reports/current.json
Linkage type:                   cross

Candidate rows:                 500,000 -> 650,000 (+30.00%)
Unique candidate pairs:         480,000 -> 610,000 (+27.08%)
Duplicate candidate rows:       20,000 -> 40,000 (+100.00%)
Candidate recall:               96.5000% -> 94.2000% (drop: 0.0230)

Quality gate: FAIL
  - candidate recall drop 0.0230 (2.30%) exceeds limit 0.0100 (1.00%)
  - candidate row growth 30.00% exceeds limit 20.00%
  - duplicate row growth 100.00% exceeds limit 10.00%
```

### Threshold and metric semantics

- **Raw candidate rows vs. unique candidate pairs:** Raw candidate rows reflect the total output volume generated by the blocker before deduplication. Pair growth compares raw rows (`(current - baseline) / baseline`) to guard downstream pipelines against processing explosions. Unique candidate pairs deduplicate repeated occurrences.
- **Recall drop (`--max-recall-drop`):** Absolute recall decrease, defined as `max(0.0, baseline_recall - current_recall)`. A threshold of `0.02` allows up to two percentage points of drop (e.g., from 95% to 93%). If current recall improves, drop is `0.0000` and passes.
- **Pair growth (`--max-pair-growth`):** Fractional raw row growth: `(current - baseline) / baseline`. A threshold of `0.20` permits up to 20% growth.
- **Duplicate growth (`--max-duplicate-growth`):** Fractional duplicate candidate row growth: `(current - baseline) / baseline`.
- **Ground-truth comparability:** Recall comparisons verify that both reports used the exact same ground-truth dataset via an unambiguous length-prefixed serialization and SHA-256 fingerprint (`sha256:...`). Reports record an explicit `fingerprint_version` (current: `v2`). If ground-truth fingerprints differ, fingerprint versions are incompatible (e.g. `v1` vs `v2`), or fingerprints are missing, recall comparison fails safely with exit code 2. Pair-count-only and duplicate-growth-only comparisons do not require ground-truth comparability.

### Compare exit codes

- `0`: Valid comparison; all configured regression gates passed (or no gates were configured).
- `1`: One or more regression gates failed.
- `2`: Invalid input, malformed JSON, unsupported schema versions, incompatible linkage types, mismatched ground-truth datasets, or I/O errors.

### Minimal CI example (GitHub Actions)

```yaml
      - name: Compare candidate generation against baseline
        run: |
          candguard compare \
            --baseline baseline-report.json \
            --current current-report.json \
            --max-recall-drop 0.01 \
            --max-pair-growth 0.20
```

## Metrics

- Candidate rows and unique ordered pairs.
- Duplicate candidate rows.
- Unique left and right IDs represented in the candidates.
- Per-source candidate-count distribution (min, median, mean, p95, p99, max).
- Candidate recall / pair completeness when ground truth is provided.
- Missing true pairs, optionally written to a TSV.
- Reduction ratio when source dataset sizes are known.
- Exit-code quality gates for recall, maximum pair count, and duplicate rows.

Candidate recall is a **ceiling on pair recall for downstream models**, not the final entity-resolution score. Reduction ratio and candidate recall should be interpreted together. High recall alone can be achieved by generating nearly every possible pair, which may be too expensive.

## Limitations of v0.1

- Inputs are CSV/TSV files with headers, or single `.parquet` files with the optional `candguard[parquet]` extra. Partitioned Parquet datasets, remote storage, and other formats are not supported.
- SQLite provides disk-backed processing validated up to 10,000,000 candidate pairs on the documented host machine (see [`docs/benchmark-report.md`](docs/benchmark-report.md)). Larger workloads (>10M rows) have not been validated. Measured throughput spans 200k–361k rows/s across all scales (201k–287k rows/s at 10M rows) on that host and is not a performance SLA for arbitrary customer datasets.
- Memory usage is disk-backed but peak resident memory increases with dataset size (from ~45 MB at 100k rows to ~419 MB at 10M rows in isolated child processes); it is not an $\mathcal{O}(1)$ constant-memory guarantee.
- The truth file must represent known matching pairs in the same left/right orientation as the candidate file for cross-dataset linkage (deduplication mode handles unordered pairs automatically).
- This tool audits output pairs; it does not generate blocking rules or infer missing ground truth.
- Missing left records with zero candidates cannot be enumerated without a separate full source-ID file; v0.1 reports only IDs represented in candidate output.
- Candidate identifiers are compared as strings. Normalize identifier formatting consistently before running an audit.

## Development

```bash
pip install -e ".[dev]"
pytest

# include the optional Parquet tests (skipped when PyArrow is not installed)
pip install -e ".[dev,parquet]"
pytest
```

## Why this tool exists

Existing record-linkage libraries and entity-resolution frameworks provide pieces of the workflow. CandGuard's proposed niche is to make *candidate-output auditing and CI thresholds* usable on CSV/TSV output from any framework without requiring a team to adopt a particular matching stack.

See [`docs/market-validation.md`](docs/market-validation.md) for the initial competitor scan and the limits of the novelty claim.

## License

MIT

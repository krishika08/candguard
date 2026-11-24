# CandGuard

**A quality gate for entity-resolution candidate generation.**

Candidate generation (also called *blocking*) decides which record pairs a matching model is allowed to inspect. If a true pair is filtered out here, a later model cannot recover it. But evaluating blocking often means writing one-off scripts, and the best trade-off depends on both candidate recall and the number of comparisons produced.

CandGuard is a small, local-first CLI that audits candidate-pair files and fails a command when quality thresholds are missed. It is framework-agnostic: use it with output from your own blocker, a competition pipeline, or an existing record-linkage package.

## Project status

CandGuard is experimental. The published package and the development branch currently have **different capabilities**:

| | Version | What it includes |
|:--|:--|:--|
| **Latest PyPI release** | `0.1.0` | CSV/TSV candidate audits, candidate recall against ground truth, missed-pair export, reduction ratio, audit quality gates, and `candguard compare` with recall-drop, pair-growth and duplicate-growth gates. |
| **Development branch (`main`)** | unreleased, planned for `0.2.0` | Everything in 0.1.0, plus optional Parquet input, record-coverage and candidate-degree diagnostics, candidate-set drift analysis with an optional Jaccard gate, output-collision protections, and stricter validation of compare reports. |

Features on `main` are **not** available in the published `0.1.0` package, and should not be assumed to exist in a `pip install candguard` installation. Version 0.2.0 has not been released yet. Until the release version bump, a development checkout still reports `candguard 0.1.0` from `candguard --version`, even though it contains the unreleased changes. See [`CHANGELOG.md`](CHANGELOG.md) for what is new on `main`.

## Installation

Requires Python 3.10 or newer. Neither install path has any default runtime dependencies.

### Published release (0.1.0)

```bash
pip install candguard==0.1.0
```

This installs the published v0.1.0 functionality: CSV/TSV input, the audit command and the original `compare` gates. The published 0.1.0 distribution has **no** `parquet` extra and none of the unreleased features listed above.

### Development checkout (unreleased)

```bash
git clone https://github.com/krishika08/candguard.git
cd candguard
python -m venv .venv
# macOS/Linux:          source .venv/bin/activate
# Windows PowerShell:   .venv\Scripts\Activate.ps1
pip install -e .
```

This installs the current development source in editable mode, including the unreleased features.

To read `.parquet` files from the development source, install the optional extra:

```bash
pip install -e ".[parquet]"
```

The extra requires `pyarrow>=25.0.1`; on Python 3.10 this resolves to PyArrow 25.x. PyArrow is imported only when a `.parquet` file is read, so CSV/TSV audits never load it. The error shown when PyArrow is missing suggests `pip install "candguard[parquet]"`. That command only works from PyPI once a release containing the extra is published, so from a checkout use the command above.

## Quick start

From a development checkout (the example files are not part of the published package):

```bash
candguard audit \
  --candidates examples/candidates.tsv \
  --truth examples/truth.tsv \
  --left-id source_id \
  --right-id candidate_id \
  --min-recall 0.60
```

Each input file must have a header. The candidate file contains the pairs passed to your matching model; the optional truth file contains known matching pairs. IDs are compared as trimmed strings. In cross-linkage mode, pair direction is preserved; in deduplication mode, pairs are treated as unordered and canonicalized.

The bundled data is intentionally tiny and synthetic. On the current development branch the command prints:

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

Advanced degree diagnostics (unique pairs per record)
-----------------------------------------------------
                                 Source            Target
Record coverage:                 n/a               n/a
Records without candidates:      n/a               n/a
Min / P50 / mean:                1 / 1 / 1.3333    1 / 1 / 1.3333
P95 / P99 / P99.9 / max:         2 / 2 / 2 / 2     2 / 2 / 2 / 2
Gini coefficient:                0.1667            0.1667
Top 1% candidate share:          50.0000%          50.0000%
Target/source mean degree ratio: 1.0000
Note: --left-records/--right-records not supplied; statistics cover only records represented in candidate output.

Quality gate: PASS
```

The published 0.1.0 package prints the same report without the "Advanced degree diagnostics" section.

## What CandGuard measures

Items marked *(unreleased)* exist only on the development branch.

- **Candidate volume:** candidate rows, unique pairs, duplicate rows, and the records represented on each side.
- **Per-source candidate distribution:** min, median, mean, P95, P99 and max.
- **Ground truth:** candidate recall (pair completeness) and missed true pairs, optionally written to a TSV.
- **Reduction ratio:** reported when dataset sizes are supplied.
- **Audit quality gates:** minimum recall, maximum unique pairs and maximum duplicate rows.
- **Optional Parquet input** *(unreleased)*: single `.parquet` candidate or truth files, alongside CSV/TSV. See [`docs/parquet-guide.md`](docs/parquet-guide.md).
- **Record coverage** *(unreleased, diagnostic)*: with `--left-records` / `--right-records`, the number of records on each side with no candidates and the fraction covered.
- **Candidate-degree diagnostics** *(unreleased, diagnostic)*: distributions of unique candidate pairs per record, with source and target separate in cross-linkage and all records together in dedupe. Each reports min, P50/P95/P99/P99.9, mean, max, the Gini coefficient and the top-1% candidate share, plus the target/source mean-degree ratio. See [`docs/advanced-metrics.md`](docs/advanced-metrics.md).
- **Regression comparison:** `candguard compare` between two audit reports, with recall-drop, pair-growth and duplicate-growth gates.
- **Candidate-set drift** *(unreleased)*: exact retained, removed and added unique pairs, Jaccard similarity and churn between two audits, with an optional minimum-Jaccard gate. See [`docs/candidate-set-drift.md`](docs/candidate-set-drift.md).
- **Safety checks** *(unreleased)*:
  - `--output` and sidecar outputs are rejected when they resolve to an input or another output file;
  - malformed ground-truth fields in compare reports are rejected instead of being silently accepted.

Candidate recall is a **ceiling on pair recall for downstream models**, not the final entity-resolution score. Interpret reduction ratio and candidate recall together: high recall alone can be achieved by generating nearly every possible pair, which may be too expensive.

### Technical contracts

- Python `>=3.10`.
- Zero default runtime dependencies. Parquet support is optional (`pyarrow>=25.0.1`) and loaded lazily.
- Processing is disk-backed through SQLite. Memory use is not constant: peak resident memory grows with dataset size (see [Limitations](#limitations-current-development-branch)).
- JSON reports use `schema_version` `"1.0"`. The development branch adds optional diagnostic fields to that schema; adding optional fields is not a breaking change, and consumers should ignore fields they do not recognize.
- [Deterministic exit codes](#exit-codes) for CI use.

## Auditing options

Measure recall against labeled pairs and save the pairs the blocker missed:

```bash
candguard audit \
  --candidates out/candidates.tsv \
  --truth data/known_matches.tsv \
  --left-id source_id --right-id target_id \
  --missed-pairs-out out/missed_pairs.tsv \
  --format json --output out/audit.json
```

Estimate how aggressively the candidate generator reduces the search space for two datasets. On the development branch, the same totals also enable record coverage:

```bash
candguard audit \
  --candidates out/candidates.tsv \
  --left-id source_id --right-id target_id \
  --left-records 100000 --right-records 80000
```

For within-dataset deduplication, use `--linkage-type dedupe --left-records N` instead. In dedupe mode, candidate and truth pairs are treated as unordered, self-pairs are disallowed, and candidate counts represent comparisons per represented record. Reduction ratio is reported only when record counts are supplied.

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

A `--min-recall` gate requires a truth file.

On the development branch, `.parquet` candidate and truth files are accepted wherever CSV/TSV files are:

```bash
candguard audit --candidates candidates.parquet --truth truth.parquet \
  --left-id source_id --right-id candidate_id
```

Identifier columns must be string or integer typed. Strings are trimmed like CSV/TSV values, integers become decimal text, and nulls or other types are rejected.

## Regression testing with `candguard compare`

Candidate generation changes, such as new blocking keys, indexing rules or nearest-neighbor thresholds, frequently trade recall against comparison volume. `candguard compare` checks a current audit against a baseline (for example from `main`) in CI, to catch silent recall regressions or comparison-volume explosions.

Save audit reports as JSON from both runs:

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

Example output:

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

- **Raw candidate rows vs. unique candidate pairs:** raw rows are the blocker's total output before deduplication; unique pairs count each pair once. Pair growth uses raw rows, to guard downstream pipelines against processing explosions.
- **Recall drop (`--max-recall-drop`):** the absolute decrease `max(0.0, baseline_recall - current_recall)`. A threshold of `0.02` allows up to two percentage points of drop (for example from 95% to 93%). If current recall improves, the drop is `0.0000` and passes.
- **Pair growth (`--max-pair-growth`):** fractional raw-row growth, `(current - baseline) / baseline`. A threshold of `0.20` permits up to 20% growth.
- **Duplicate growth (`--max-duplicate-growth`):** fractional duplicate-row growth, `(current - baseline) / baseline`.
- **Ground-truth comparability:** recall comparisons verify that both reports used exactly the same ground truth. They compare a SHA-256 fingerprint built from an unambiguous, length-prefixed encoding of the truth pairs. Reports record a `fingerprint_version` (currently `v2`). If the fingerprints differ, their versions are incompatible, or a fingerprint is missing, the recall comparison fails safely with exit code 2. Pair-growth and duplicate-growth comparisons do not require ground truth.
- **Report validation** *(unreleased)*: `compare` rejects malformed `ground_truth` sections with exit code 2. This covers recall outside `[0, 1]`, NaN, infinity, booleans, non-numeric recall, and empty or non-string fingerprint fields. Reports without ground truth, with absent or `null` recall, or without a `fingerprint_version` (older reports) remain valid.

### Candidate-set drift *(unreleased)*

Counts and recall can stay identical while a blocking change replaces most candidate pairs. To compare the actual candidate sets, export a sorted sidecar of unique pairs during each audit, then pass both sidecars to `compare`:

```bash
candguard audit --candidates base_candidates.tsv --left-id source_id --right-id candidate_id \
  --candidate-set-out artifacts/baseline.pairs.tsv --format json --output reports/baseline.json
candguard audit --candidates pr_candidates.tsv --left-id source_id --right-id candidate_id \
  --candidate-set-out artifacts/current.pairs.tsv --format json --output reports/current.json

candguard compare \
  --baseline reports/baseline.json --current reports/current.json \
  --baseline-candidate-set artifacts/baseline.pairs.tsv \
  --current-candidate-set artifacts/current.pairs.tsv \
  --min-candidate-jaccard 0.90
```

`compare` verifies each sidecar's header, sort order, pair count and SHA-256 fingerprint against its report. It then merges the two files in one streaming pass and reports these values, all computed on unique pairs:
- retained, removed and added pairs;
- Jaccard similarity and pair churn;
- the removed fraction of the baseline and the added fraction of the current set.

`--min-candidate-jaccard` is optional; without it, drift is diagnostic only. Cross-linkage pairs keep their direction, and dedupe pairs are undirected. See [`docs/candidate-set-drift.md`](docs/candidate-set-drift.md) for the sidecar format, definitions, a CI workflow, measured performance and limitations.

### Output safety *(unreleased)*

Before reading any input, the CLI rejects (exit code 2) an output path that resolves to another file the command uses. It compares exact paths, relative and absolute aliases, `..` paths and symlinks:
- `audit --output` cannot be the `--candidates`, `--truth`, `--missed-pairs-out` or `--candidate-set-out` file;
- `--candidate-set-out` cannot be an input or the missed-pairs file;
- `compare --output` cannot be either report or either candidate-set sidecar.

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

## Exit codes

Both `audit` and `compare` use the same exit codes:

- `0`: the run succeeded and every configured quality gate passed (or no gates were configured).
- `1`: one or more quality gates failed.
- `2`: invalid input or configuration, or an operational error. Examples: missing columns or files, malformed JSON, unsupported schema versions, incompatible linkage types, mismatched ground-truth datasets, unverifiable candidate-set artifacts, output-path collisions and I/O errors.

## Limitations (current development branch)

- **Inputs:** CSV/TSV files with headers, or single `.parquet` files with the optional extra. Partitioned Parquet datasets, remote storage (for example object stores or URLs) and other input formats are not supported. The published 0.1.0 package supports CSV/TSV only.
- **Scale and performance:** SQLite-backed processing has been validated up to 10,000,000 candidate pairs on one documented machine (Intel Core Ultra 5 125H, 15.5 GiB RAM, Windows 11, Python 3.12). There it took 34.9–49.7 s of elapsed time and about 419 MB of peak resident memory in isolated processes, with throughput of 200k–361k rows/s across tested scales. Larger workloads have not been validated, and these figures are measurements, not a performance guarantee. See [`docs/benchmark-report.md`](docs/benchmark-report.md).
- **Memory:** usage is disk-backed but not constant. Peak resident memory grew from about 45 MB at 100k rows to about 419 MB at 10M rows in those measurements; there is no O(1) memory guarantee.
- **Truth orientation:** for cross-dataset linkage, the truth file must list matching pairs in the same left/right orientation as the candidate file. Deduplication mode handles unordered pairs automatically.
- **Scope:** CandGuard audits output pairs; it does not generate blocking rules or infer missing ground truth.
- **Records without candidates** can be *counted* when `--left-records` / `--right-records` are supplied, but not *listed*. Listing their IDs requires a full record roster, which is not supported.
- **Identifiers** are compared as strings. Normalize identifier formatting consistently before running an audit.

## Development

```bash
pip install -e ".[dev]"
pytest

# include the optional Parquet tests (skipped when PyArrow is not installed)
pip install -e ".[dev,parquet]"
pytest
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for contribution guidelines, [`SECURITY.md`](SECURITY.md) for reporting vulnerabilities, and [`docs/production-release-checklist.md`](docs/production-release-checklist.md) for the release procedure.

## Why this tool exists

Existing record-linkage libraries and entity-resolution frameworks provide pieces of the workflow. CandGuard's proposed niche is to make *candidate-output auditing and CI thresholds* usable on candidate files from any framework, without requiring a team to adopt a particular matching stack.

See [`docs/market-validation.md`](docs/market-validation.md) for the initial competitor scan and the limits of the novelty claim.

## License

MIT

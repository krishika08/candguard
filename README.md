# CandGuard

**A quality gate for entity-resolution candidate generation.**

Candidate generation (also called *blocking*) decides which record pairs a matching model is allowed to inspect. If a true pair is filtered out here, a later model cannot recover it. But evaluating blocking often means writing one-off scripts, and the best trade-off depends on both candidate recall and the number of comparisons produced.

CandGuard is a small, local-first CLI that audits candidate-pair files and fails a command when quality thresholds are missed. It is framework-agnostic: use it with output from your own blocker, a competition pipeline, or an existing record-linkage package.

## Project status

CandGuard is experimental. The latest published release is **CandGuard 0.2.0**. This README also describes fixes in development. Run `candguard --version` to see which version you have installed.

| Version | What it includes |
|:--|:--|
| `0.2.0` | Everything in 0.1.0, plus optional Parquet input, record-coverage and candidate-degree diagnostics, candidate-set drift analysis with an optional Jaccard gate, output-collision protections, and stricter validation of compare reports and input files. |
| `0.1.0` | CSV/TSV candidate audits, candidate recall against ground truth, missed-pair export, reduction ratio, audit quality gates, and `candguard compare` with recall-drop, pair-growth and duplicate-growth gates. |

Features marked *(0.2.0)* below are not available in CandGuard 0.1.0. See [`CHANGELOG.md`](CHANGELOG.md) for the full list of changes.

## Installation

Requires Python 3.10 or newer. CandGuard has no default runtime dependencies.

```bash
pip install candguard
```

To read `.parquet` files, install the optional `parquet` extra (CandGuard 0.2.0 or later; 0.1.0 has no `parquet` extra):

```bash
pip install "candguard[parquet]"
```

The extra requires `pyarrow>=25.0.1`; on Python 3.10 this resolves to PyArrow 25.x. PyArrow is imported only when a `.parquet` file is read, so CSV/TSV audits never load it. If a `.parquet` file is given and PyArrow is missing, CandGuard exits with code 2. Its error message shows the install command for a release and for a source checkout, each labeled with the installation it applies to.

### From a source checkout

```bash
git clone https://github.com/krishika08/candguard.git
cd candguard
python -m venv .venv
# macOS/Linux:          source .venv/bin/activate
# Windows PowerShell:   .venv\Scripts\Activate.ps1
pip install -e .              # or, with Parquet support: pip install -e ".[parquet]"
```

Run these commands from the repository root. An editable install reflects the source on the branch you have checked out.

## Quick start

From a source checkout (the example files are not part of the published package):

```bash
candguard audit \
  --candidates examples/candidates.tsv \
  --truth examples/truth.tsv \
  --left-id source_id \
  --right-id candidate_id \
  --min-recall 0.60
```

Each input file must have a header. The candidate file contains the pairs passed to your matching model; the optional truth file contains known matching pairs. IDs are compared as trimmed strings. In cross-linkage mode, pair direction is preserved; in deduplication mode, pairs are treated as unordered and canonicalized.

The bundled data is intentionally tiny and synthetic. CandGuard 0.2.0 prints:

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

CandGuard 0.1.0 prints the same report without the "Advanced degree diagnostics" section.

## What CandGuard measures

Items marked *(0.2.0)* were added in CandGuard 0.2.0.

- **Candidate volume:** candidate rows, unique pairs, duplicate rows, and the records represented on each side.
- **Per-source candidate distribution:** min, median, mean, P95, P99 and max.
- **Ground truth:** candidate recall (pair completeness) and missed true pairs, optionally written to a TSV.
- **Reduction ratio:** reported when dataset sizes are supplied.
- **Audit quality gates:** minimum recall, maximum unique pairs and maximum duplicate rows.
- **Optional Parquet input** *(0.2.0)*: single `.parquet` candidate or truth files, alongside CSV/TSV. See [`docs/parquet-guide.md`](docs/parquet-guide.md).
- **Record coverage** *(0.2.0, diagnostic)*: with `--left-records` / `--right-records`, the number of records on each side with no candidates and the fraction covered.
- **Candidate-degree diagnostics** *(0.2.0, diagnostic)*: distributions of unique candidate pairs per record, with source and target separate in cross-linkage and all records together in dedupe. Each reports min, P50/P95/P99/P99.9, mean, max, the Gini coefficient and the top-1% candidate share, plus the target/source mean-degree ratio. See [`docs/advanced-metrics.md`](docs/advanced-metrics.md).
- **Regression comparison:** `candguard compare` between two audit reports, with recall-drop, pair-growth and duplicate-growth gates.
- **Candidate-set drift** *(0.2.0)*: exact retained, removed and added unique pairs, Jaccard similarity and churn between two audits, with an optional minimum-Jaccard gate. See [`docs/candidate-set-drift.md`](docs/candidate-set-drift.md).
- **Safety checks** *(0.2.0)*:
  - `--output`, `--missed-pairs-out` and `--candidate-set-out` are rejected when they resolve to an input or another output file;
  - malformed ground-truth fields in compare reports are rejected instead of being silently accepted;
  - unreadable input files and reports, and Parquet string identifiers that are not valid UTF-8, are reported as errors with exit code 2.

Candidate recall is a **ceiling on pair recall for downstream models**, not the final entity-resolution score. Interpret reduction ratio and candidate recall together: high recall alone can be achieved by generating nearly every possible pair, which may be too expensive.

### Technical contracts

- Python `>=3.10`.
- Zero default runtime dependencies. Parquet support is optional (`pyarrow>=25.0.1`) and loaded lazily.
- Processing is disk-backed through SQLite. Memory use is not constant: peak resident memory grows with dataset size (see [Limitations](#limitations)).
- JSON reports use `schema_version` `"1.0"`. CandGuard 0.2.0 adds optional diagnostic fields to that schema; adding optional fields is not a breaking change, and consumers should ignore fields they do not recognize.
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

Estimate how aggressively the candidate generator reduces the search space for two datasets. In CandGuard 0.2.0, the same totals also enable record coverage:

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

A `--min-recall` gate requires a truth file. The gate compares the recovered fraction of known pairs against the threshold before rounding recall for the report. Equality passes. With `--min-recall 1.0`, a single missing pair fails the gate even if displayed recall rounds to 100%.

With the `parquet` extra *(0.2.0)*, `.parquet` candidate and truth files are accepted wherever CSV/TSV files are:

```bash
candguard audit --candidates candidates.parquet --truth truth.parquet \
  --left-id source_id --right-id candidate_id
```

Identifier columns must be string or integer typed. Strings are trimmed like CSV/TSV values, integers become decimal text, and nulls, other types and string values that are not valid UTF-8 are rejected.

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
  - candidate recall drop 0.0230 (exact drop 23/1000) exceeds limit 0.01
  - candidate row growth 30.00% exceeds limit 20.00%
  - duplicate row growth 100.00% exceeds limit 10.00%
```

### Threshold and metric semantics

- **Raw candidate rows vs. unique candidate pairs:** raw rows are the blocker's total output before deduplication; unique pairs count each pair once. Pair growth uses raw rows, to guard downstream pipelines against processing explosions.
- **Recall drop (`--max-recall-drop`)** is the absolute decrease `max(0.0, baseline_recall - current_recall)`. A threshold of `0.02` allows up to two percentage points of drop (for example from 95% to 93%). Compare uses recovered and unique truth-pair counts when available and falls back to reported recall for legacy reports without usable counts. Recall gates use decimal thresholds and pass at equality. JSON recall and recall drop are still rounded to eight decimal places for display.
- **Pair growth (`--max-pair-growth`):** fractional raw-row growth, `(current - baseline) / baseline`. A threshold of `0.20` permits up to 20% growth.
- **Duplicate growth (`--max-duplicate-growth`):** fractional duplicate-row growth, `(current - baseline) / baseline`.
- **Ground-truth comparability** requires a recall value and a matching fingerprint in each report. Fingerprint versions must match. An omitted version defaults to `v1`. The current `v2` fingerprint uses SHA-256 over a length-prefixed encoding of the truth pairs. If recall is unavailable or the fingerprints are missing or incompatible, `--max-recall-drop` exits with code 2. Without that gate, compare still evaluates pair-growth and duplicate-growth gates. Text output explains why the drop is unavailable. JSON sets `metrics.recall_drop` to `null` and includes the explanation in `metrics.recall_drop_unavailable_reason`.
- **Report validation** requires `candidate_rows` to equal `unique_candidate_pairs + duplicate_candidate_rows`. Malformed candidate counts or `ground_truth` fields cause exit code 2. When compare uses truth counts for recall, it checks that they agree with the reported recall at its stored precision. Reports with absent ground truth, absent or `null` recall, or omitted fingerprint versions remain valid inputs.

### Candidate-set drift *(0.2.0)*

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

### Output safety *(0.2.0)*

Before reading any input, the CLI rejects (exit code 2) an output path that resolves to another file the command uses. It compares exact paths, relative and absolute aliases, `..` paths and symlinks:
- `audit --output` cannot be the `--candidates`, `--truth`, `--missed-pairs-out` or `--candidate-set-out` file;
- `--missed-pairs-out` cannot be the `--candidates` or `--truth` file;
- `--candidate-set-out` cannot be an input or the missed-pairs file;
- `compare --output` cannot be either report or either candidate-set sidecar.

Audit outputs are written in sequence. If a later output fails, an earlier output may already be present, even when the CLI returns exit code 2.

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
- `2`: invalid input or configuration, or an operational error. Examples: missing columns, missing or unreadable files, malformed JSON, unsupported schema versions, incompatible linkage types, mismatched ground-truth datasets, unverifiable candidate-set artifacts, output-path collisions and I/O errors.

## Limitations

- **Inputs:** CSV/TSV files with headers, or single `.parquet` files with the optional extra. Partitioned Parquet datasets, remote storage (for example object stores or URLs) and other input formats are not supported. CandGuard 0.1.0 supports CSV/TSV only.
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

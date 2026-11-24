# Changelog

All notable changes to CandGuard are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and CandGuard uses [Semantic Versioning](https://semver.org/).

## [Unreleased] — planned for v0.2.0

These changes are on the `main` branch and **have not been released**. The latest published package is 0.1.0 (`pip install candguard==0.1.0`), which does not include any of them. The package version on `main` is still `0.1.0` until the release version bump.

### Added

- **Optional Apache Parquet input** for single `.parquet` candidate and ground-truth files, via the optional `parquet` extra (`pyarrow>=25.0.1`).
  - PyArrow is imported only when a Parquet file is read, so the default install keeps zero runtime dependencies.
  - Identifier columns must be string or integer typed. Strings are trimmed like CSV/TSV values, integers become decimal text, and nulls are rejected.
  - Guide: [`docs/parquet-guide.md`](docs/parquet-guide.md).
- **Record coverage** against the existing `--left-records` / `--right-records` totals: the number and fraction of records with and without candidates.
- **Candidate-degree diagnostics** in a new `degree_analysis` report section:
  - distributions of unique candidate pairs per record (source and target separately in cross-linkage, all records in dedupe);
  - min, P50/P95/P99/P99.9, mean, max, Gini coefficient and top-1% candidate share;
  - the target/source mean-degree ratio.

  These metrics are diagnostic only and never affect the quality gate. Definitions: [`docs/advanced-metrics.md`](docs/advanced-metrics.md).
- **Candidate-set drift analysis:**
  - `audit --candidate-set-out` exports the unique normalized candidate pairs to a sorted, fingerprinted TSV sidecar (`candguard-pairs-tsv-v1`), written atomically;
  - `compare --baseline-candidate-set` / `--current-candidate-set` validates both sidecars and merges them in one streaming pass, reporting retained, removed and added pairs, Jaccard similarity, churn and directional fractions under `metrics.candidate_set_drift`.

  Details: [`docs/candidate-set-drift.md`](docs/candidate-set-drift.md).
- **Optional candidate-set Jaccard quality gate**, `compare --min-candidate-jaccard`.
- New report fields are additive and optional under `schema_version` `"1.0"`.

### Improved

- **Optional-dependency installation guidance:** when a `.parquet` input is given and PyArrow is missing, the error (exit code 2) now lists two commands, each labeled with the installation it applies to. `python -m pip install "candguard[parquet]"` is for a published release that includes Parquet support (0.1.0 does not). `python -m pip install -e ".[parquet]"` is for a source checkout and must be run from the repository root. Previously the error recommended only `pip install "candguard[parquet]"`, which does not work with any currently published release. The README and [`docs/parquet-guide.md`](docs/parquet-guide.md) match the new message.
- **Output-collision protection:** `audit --output` can no longer resolve to the candidates, truth, missed-pairs or candidate-set file, and `compare --output` can no longer resolve to either report or either candidate-set sidecar. Collisions exit with code 2 before any input is read.
- **Ground-truth validation in compare reports:**
  - `compare` rejects a `ground_truth` section that is not an object or `null`;
  - it rejects `candidate_recall` values that are booleans, non-numeric, NaN, infinite or outside `[0, 1]`, including integers too large to convert to a float;
  - it rejects fingerprint fields that are empty or not strings.

  Errors exit with code 2 and no traceback, where previously some malformed values could pass a recall gate or crash. Reports with absent or `null` recall, or without a `fingerprint_version`, remain valid.
- **Internal restructuring:** the package is organized into separate reader, storage, metrics, models, reporting, audit and compare modules, with no changes to the public API or CLI.

### Fixed

- `compare` now reports a clean "Could not parse … JSON report" error, with exit code 2, for a report containing an integer literal longer than Python's int-string conversion limit (Python 3.11+). Previously this was an unhandled traceback.

### CI and release engineering

- The test workflow now also runs an **optional Parquet test matrix**: Ubuntu Python 3.10 with PyArrow 25.x, plus Ubuntu 3.13, Windows 3.12 and macOS 3.12. It also checks that the default install does not pull in PyArrow, and smoke-tests candidate-set drift through the CLI.
- The **release workflow** (`.github/workflows/publish.yml`) is hardened. Publishing to PyPI through Trusted Publishing (OIDC) now requires all of the following:
  - a stable `vMAJOR.MINOR.PATCH` tag that matches the version in both `pyproject.toml` and `candguard.__version__`;
  - the full default test matrix (Ubuntu Python 3.10–3.13, Windows 3.12, macOS 3.12);
  - the optional Parquet test matrix;
  - CLI smoke tests covering audit, compare and candidate-set drift, including expected gate failures;
  - a verified build: `twine check`, then the wheel installed in a clean environment and its version, CLI and dependency-free install checked.

  `id-token: write` is granted only to the publish job.

## [0.1.0]

First published release on PyPI.

- CSV/TSV candidate-pair audits: candidate rows, unique pairs, duplicate rows, represented records and per-source candidate distribution.
- Candidate recall against ground truth, with optional export of missed pairs.
- Reduction ratio when dataset sizes are supplied, for cross-linkage and deduplication.
- Audit quality gates: `--min-recall`, `--max-pairs` and `--max-duplicate-rows`.
- `candguard compare` regression checks: `--max-recall-drop`, `--max-pair-growth` and `--max-duplicate-growth`, with ground-truth fingerprint (`v2`) comparability checks.
- Text and JSON reports (`schema_version` `"1.0"`), and CLI exit codes `0` (pass), `1` (quality gate failed) and `2` (invalid input or configuration).

[Unreleased]: https://github.com/krishika08/candguard/compare/v0.1.0...main
[0.1.0]: https://github.com/krishika08/candguard/tree/v0.1.0

# CandGuard v0.2.0 Architecture Specification

**Status:** Foundational Architecture Established (Milestone 1)<br>
**Target Release Line:** v0.2.0<br>
**Authors:** CandGuard Maintainers<br>
**Date:** November 2026<br>

---

## 1. Executive Summary & Mission

CandGuard is a local-first, framework-agnostic Python tool for auditing candidate generation (blocking) in entity-resolution and record-linkage pipelines. In entity resolution, candidate generation is the critical first stage: if a true matching pair is pruned during blocking, downstream scoring models cannot recover it. Conversely, if blocking generates too many candidate pairs, downstream comparison costs explode.

CandGuard serves as a **lightweight quality gate and regression test harness** that:
1. Computes candidate recall (pair completeness) against labeled ground truth.
2. Measures search-space reduction ratio and comparison volume.
3. Analyzes per-record candidate distribution percentiles and duplicate rows.
4. Detects recall regressions and candidate-count explosions via `candguard compare`.
5. Enforces automated threshold quality gates with deterministic exit codes (`0`, `1`, `2`) in CI pipelines.

The goal of the **v0.2.0 architectural evolution** is to transform the monolithic v0.1.0 implementation into a cleanly separated, extensible architecture that isolates input reading, storage execution, metrics computation, reporting, and public APIs while guaranteeing **100% backward compatibility**, **zero default runtime dependencies**, and **local-first data safety**.

---

## 2. Current Architecture (v0.1.0 Baseline)

In v0.1.0, CandGuard operated through three primary modules:
- `candguard.audit`: Contained delimited text parsing, CSV streaming, temporary SQLite database lifecycle management, index creation, SQL aggregation queries, nearest-rank percentile math, reduction ratio calculation, ground-truth join logic, missed-pairs TSV export, text rendering, and JSON formatting.
- `candguard.compare`: Contained JSON report loading, schema validation, ground-truth fingerprint comparability checks, metric delta calculations, threshold evaluation, and compare text rendering.
- `candguard.cli`: Provided `argparse` command-line definitions and mapped exceptions to CLI exit codes.

### Strengths of v0.1.0
- **Zero Runtime Dependencies:** Production package ran purely on Python 3.10+ Standard Library and built-in SQLite.
- **Disk-Backed Processing:** Ingests input files in streaming batches and delegates pair aggregation to an isolated, disk-backed SQLite database. Memory consumption is workload- and configuration-dependent; recorded benchmarks show peak memory increasing with dataset size (about 45 MB at 100k rows to about 419 MB at 10M rows; see `docs/benchmark-report.md`). The documented 10-million-pair boundary is an empirical validation limit on tested hardware, and no $O(1)$ constant-memory guarantee is made.
- **Deterministic CLI Exit Codes:** Predictable contract (`0` = pass, `1` = gate failure, `2` = invalid input).
- **Hardened Hashing:** Unambiguous length-prefixed ground-truth fingerprinting (v2) prevented false comparability claims.

### Limitations of v0.1.0
- **Tight Coupling:** Delimited text parsing was intertwined with SQLite table creation and batch insertion logic.
- **No Columnar Format Path:** Ingesting Parquet or Arrow files required refactoring the core audit loop.
- **Mixed Concerns:** Rendering logic lived inside core execution modules.
- **Absence of Domain Models:** Public APIs returned raw nested dictionaries without type-checked data contracts.
- **Root Namespace Inconvenience:** Public functions required deep imports (`from candguard.audit import audit_candidates`).

---

## 3. Target Architecture (v0.2.0 Foundation)

The v0.2.0 architecture decouples CandGuard into single-responsibility components with strict interface boundaries:

```
+-------------------------------------------------------------------------+
|                               CLI Layer                                 |
|                         (candguard.cli: main)                           |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|                            Public Python API                            |
|             (candguard: audit_candidates, compare_reports)              |
+-------------------+--------------------------------+--------------------+
                    |                                |
                    v                                v
+------------------------------------+   +--------------------------------+
|       Audit Coordination           |   |     Regression Comparison      |
|     (candguard.audit)              |   |     (candguard.compare)        |
+---------+--------------------+-----+   +---+----------------------------+
          |                    |             |
          v                    v             v
+-------------------+  +-------------------------+  +---------------------+
|   Input Readers   |  |     Storage Engine      |  |   Reporting Engine  |
| (CandidateReader, |  |  (StorageEngine,        |  | (render_text,       |
|  DelimitedReader, |  |   SQLiteStorageEngine)  |  |  render_compare,    |
|  get_reader)      |  +------------+------------+  |  report_json)       |
+-------------------+               |               +---------------------+
                                    v
                       +-------------------------+
                       |    Metrics Engine       |
                       | (reduction_ratio,       |
                       |  percentiles, growth,   |
                       |  fingerprint_v2)        |
                       +-------------------------+
                                    |
                                    v
                       +-------------------------+
                       |   Core Models & Types   |
                       | (models.py: AuditError, |
                       |  TypedDicts, validators)|
                       +-------------------------+
```

---

## 4. Module Responsibilities

| Module | Core Responsibility | Key Components |
|---|---|---|
| `candguard.models` | Domain models, TypedDict contracts, validation routines, base exceptions | `AuditError`, `AuditReport`, `CompareReport`, `InputsReport`, `validate_audit_arguments` |
| `candguard.readers` | Abstraction and streaming readers for input candidate and truth files | `CandidateReader` protocol, `DelimitedReader`, `resolve_delimiter`, `get_reader` factory |
| `candguard.storage` | Disk-backed state management, table indexing, set joins, percentile queries | `StorageEngine` protocol, `SQLiteStorageEngine` |
| `candguard.metrics` | Pure mathematical and cryptographic metric computations | `compute_possible_pairs`, `compute_reduction_ratio`, `compute_truth_fingerprint_v2`, `compute_relative_growth` |
| `candguard.reporting`| Transformation of metric models into user-facing text and JSON outputs | `render_text`, `render_compare_text`, `report_json` |
| `candguard.audit` | High-level audit workflow coordination and backward-compatible entry point | `audit_candidates()`, legacy aliases |
| `candguard.compare`| Report comparison, threshold validation, regression quality gate evaluation | `compare_reports()`, legacy aliases |
| `candguard.candidate_set`| Candidate-set sidecar format, atomic export, artifact validation, streaming sorted merge | `write_candidate_set_artifact()`, `CandidateSetArtifactReader`, `merge_sorted_pair_streams()` |
| `candguard.cli` | Argument parsing, help documentation, formatting dispatch, exit code mapping | `build_parser()`, `main()` |
| `candguard.__init__`| Top-level package namespace exporting public contracts | `__version__`, `audit_candidates`, `compare_reports`, `AuditError` |

---

## 5. Reader and Storage Boundaries

### 5.1 The `CandidateReader` Protocol
To decouple the storage engine from file formats, all inputs are accessed through a formal structural typing protocol:

```python
@runtime_checkable
class CandidateReader(Protocol):
    """Protocol for candidate-pair and ground-truth input readers."""

    @property
    def path(self) -> Path: ...

    @property
    def left_col(self) -> str: ...

    @property
    def right_col(self) -> str: ...

    def iter_pairs(self) -> Iterator[tuple[str, str]]: ...

    def iter_batches(self, batch_size: int = 50_000) -> Iterator[list[tuple[str, str]]]: ...
```

### 5.2 Batch Processing Contract
1. **Streaming Batch Ingestion:** Readers stream candidate files and yield fixed-size chunks (`batch_size=50_000` by default, validated to be a positive integer). The storage engine avoids loading whole candidate datasets into Python lists. Batching bounds the size of each Python-side insert batch, not total process memory. Memory consumption remains workload- and configuration-dependent and has been measured to increase with dataset size; no $O(1)$ constant-memory guarantee is made. The documented 10-million-pair boundary is an empirical validation limit on tested hardware, not a constant-memory guarantee.
2. **Whitespace and Identifier Normalization:** Identifiers are extracted, trimmed of leading/trailing whitespace, and validated as non-empty text strings before yielding.
3. **Delimiter Detection & Overrides:** `DelimitedReader` inspects file extension (`.tsv`, `.tab` vs `.csv`) or applies explicit delimiter overrides with full Unicode escape sequence decoding (`\t`, `|`, `;`).
4. **Parquet Reader:** The reader factory (`get_reader`) dispatches `.parquet` paths (case-insensitive) to `ParquetReader`, which implements `CandidateReader` using `pyarrow.parquet.ParquetFile.iter_batches()` with column projection. It needs no storage-engine changes. PyArrow is an optional extra (`candguard[parquet]`) imported lazily. If it is missing, `get_reader` raises an `AuditError` with install instructions. Delimiter overrides are rejected for Parquet input. See `docs/parquet-guide.md`.

### 5.3 Storage Engine Contract
The `SQLiteStorageEngine` manages local persistence:
- Creates isolated temporary SQLite databases in the OS temp directory (`candguard-*.sqlite3`).
- Applies high-performance embedded PRAGMAs:
  - `journal_mode = OFF` (minimizes disk write overhead)
  - `synchronous = OFF` (disables disk sync flushes during audit)
  - `temp_store = FILE` (preserves host memory ceiling)
  - `cache_size = -65536` (~64 MiB page cache)
  - `mmap_size = 268435456` (256 MiB memory-mapped I/O)
- Handles canonical pair ordering for within-dataset deduplication (`left, right = min(l, r), max(l, r)`) and rejects self-pairs (`left == right`).
- Guarantees complete resource cleanup via context management (`__enter__` / `__exit__`), ensuring temporary database files are unlinked on both success and error.

---

## 6. Proposed Metrics Extensions (v0.2.x Roadmap)

Subsequent v0.2.x releases will introduce advanced graph and distribution metrics without altering the JSON schema 1.0 contract:

### 6.1 Bipartite Degree Skew & Hub Detection

**Status: implemented** as the additive `degree_analysis` report section and the record-universe `coverage` fields. `SQLiteStorageEngine.compute_degree_analysis()` reuses the existing per-record degree table and, for cross-linkage, adds a `target_candidate_counts` table. Each distribution is summarized from an ordered SQL histogram of `(degree, record_count)` rows, so only one row per distinct degree value reaches Python. Definitions, null cases and measurements are in `docs/advanced-metrics.md`. The original proposal follows.

- **Left/Right Degree Asymmetry:** In cross-linkage, high candidate degree on the right (target) side can indicate uninformative generic records (e.g. placeholder addresses or missing entity names).
- **Hub Metric:** Calculate the Gini coefficient or 99th-percentile concentration of candidates across target records.

### 6.2 Connected Component & Cluster Analysis (Deduplication)
- In `dedupe` mode, candidate pairs implicitly form an undirected graph $G = (V, E)$.
- Evaluating connected component count, maximum component size, and density allows teams to detect "hairball" clusters before running costly entity clustering algorithms.

### 6.3 Isolated Record Enumeration

*Partially addressed:* records without candidates can be **counted** when `--left-records` / `--right-records` totals are supplied: the report gives `*_records_without_candidates` and the coverage ratio below as `*_record_coverage`. Their **IDs cannot be enumerated** without a complete roster of input record IDs. Roster ingestion and isolated-ID export remain future work.

- Coverage against supplied totals:
  $$\text{Coverage} = \frac{|\text{Source Records with } \ge 1 \text{ Candidate}|}{|\text{Total Source Records}|}$$
- Future `--source-roster` or `--left-roster` options would list the isolated IDs and validate the supplied totals.

---

## 7. Candidate-Set Drift Analysis

Candidate generation regressions are not limited to aggregate recall drops and pair volume growth. Pipeline modifications (e.g. changing BM25 tokens, altering embedding distance thresholds) can cause significant **churn** in the generated candidate set while keeping total counts identical.

*Implemented in v0.2.x.* See [`candidate-set-drift.md`](candidate-set-drift.md) for the user-facing contract.

### Metrics
1. **Jaccard Similarity of Candidate Sets:**
   $$J(C_{\text{base}}, C_{\text{curr}}) = \frac{|C_{\text{base}} \cap C_{\text{curr}}|}{|C_{\text{base}} \cup C_{\text{curr}}|}$$
   This is defined as 1 when both sets are empty. Pair churn is $(A + D)/U = 1 - J$.
2. **Directional Ratios:**
   - **Removed pairs** $D = |C_{\text{base}} \setminus C_{\text{curr}}|$; reported as `removed_fraction_of_baseline` $= D / |C_{\text{base}}|$.
   - **Added pairs** $A = |C_{\text{curr}} \setminus C_{\text{base}}|$; reported as `added_fraction_of_current` $= A / |C_{\text{curr}}|$.

### Workflow and Storage Design
1. **Sidecar export (audit).** `audit_candidates(candidate_set_path=...)` / `--candidate-set-out` runs after every other audit step has succeeded. It streams the deduplicated `candidate_pairs` table from the audit's SQLite database in `ORDER BY left_id COLLATE BINARY, right_id COLLATE BINARY` order (an index scan) into a `candguard-pairs-tsv-v1` file:
   - the export never re-reads the original candidate file;
   - the file is written to a temporary file in the destination directory and atomically renamed;
   - cross pairs keep their direction, and dedupe pairs are the existing canonical `(min, max)` edges.
2. **Report metadata.** The audit report records only `candidate_set = {format, unique_pairs, fingerprint, fingerprint_version, linkage_type}`. The sidecar path and the pairs themselves stay out of the JSON.
3. **Fingerprint.** `candidate-set:v1` is a domain-separated, length-prefixed SHA-256 over the sorted pairs, the same encoding as the ground-truth `v2` fingerprint but in a different domain. It serves as an integrity check that binds each sidecar to its report.
4. **Streaming merge (compare).** `compare_reports(baseline_candidate_set_path=..., current_candidate_set_path=...)` opens one `CandidateSetArtifactReader` per sidecar. Each reader:
   - validates the header, field count, non-empty IDs, strict ascending order (which also rejects duplicates) and dedupe canonical form, row by row;
   - stops as soon as a file holds more pairs than its report records;
   - verifies the pair count and fingerprint when the file ends.

   `merge_sorted_pair_streams()` holds at most one pair per side and drains both readers. Python's code-point string order matches SQLite `BINARY` order over UTF-8, so the producer and consumer agree on ordering. No temporary files are used, and no pair set is materialized.
5. **Gate.** The optional `min_candidate_jaccard` threshold, in [0, 1], fails the gate when the unrounded R/U is below it. Without the threshold, drift is diagnostic only. Without sidecar paths, compare output is byte-for-byte unchanged.

---

## 8. Blocking-Rule Attribution

In production record linkage, candidates are typically produced by a union of distinct blocking rules:
- **Rule 1:** Exact match on `(soundex(last_name), zip_code)`.
- **Rule 2:** Exact match on `email_domain` + fuzzy token match.
- **Rule 3:** Vector approximate-nearest-neighbor search.

### Planned Rule Attribution Support:
- Candidate inputs will support an optional rule attribution column (`--rule-id` / `--rule-col`).
- CandGuard will compute:
  1. **Rule Volume:** Raw and unique candidate pairs generated by each rule.
  2. **Unique Recall Contribution:** Known matching pairs that *only* Rule $k$ succeeded in recovering.
  3. **Rule Redundancy Matrix:** Pair overlap across rule pairs, allowing practitioners to safely prune redundant, expensive rules.

---

## 9. Reporting Architecture

The reporting layer cleanly separates metric calculation from presentation:
- `render_text()`: Formats human-readable CLI summaries with distinct semantics for cross-linkage vs. deduplication.
- `render_compare_text()`: Formats side-by-side delta summaries with percentage growth and threshold fail reasons, plus a candidate-set drift section when sidecar artifacts were compared.
- `report_json()`: Serializes deterministic, machine-readable JSON matching the published schema.

### Schema Versioning Policy:
- The current contract is `schema_version = "1.0"`.
- Backward-compatible additions of optional fields or sections may ship under `schema_version = "1.0"`, as the v0.2.x degree and coverage diagnostics do (Section 6). Consumers must ignore unknown fields and treat fields added after the initial 1.0 contract as optional. A `1.x` minor version is reserved for additive changes that alter how existing consumers should interpret a report; `candguard compare` accepts any `1.x`.
- Breaking schema modifications (e.g. renaming fields or modifying metric types) require a major version bump (`2.0`) and a multi-minor deprecation cycle.

---

## 10. Public Python API Design

The public API is accessible directly from the `candguard` root namespace:

```python
from candguard import audit_candidates, compare_reports, AuditError

# Run candidate-pair audit
report = audit_candidates(
    candidates_path="out/candidates.tsv",
    left_id="source_id",
    right_id="target_id",
    truth_path="data/truth.tsv",
    linkage_type="cross",
    min_recall=0.95,
)

# Run regression comparison
comparison = compare_reports(
    baseline_path="reports/main.json",
    current_path="reports/pr.json",
    max_recall_drop=0.01,
    max_pair_growth=0.10,
)
```

Both functions accept `str` or `pathlib.Path` objects and return standard dictionary objects that serialize directly to JSON.

---

## 11. Configuration Design

While CLI flags remain the primary interface in v0.1.0 and v0.2.0, future iterations will support declarative file-based configurations:
- `candguard.toml` or `pyproject.toml [tool.candguard]`.
- Configuration resolution hierarchy:
  1. Explicit CLI arguments (highest precedence).
  2. Environment variables (`CANDGUARD_*`).
  3. Configuration file (`candguard.toml` or `pyproject.toml`).
  4. Built-in system defaults (lowest precedence).

---

## 12. Dependency Policy

CandGuard enforces a **strict zero-runtime-dependency constraint**:
1. **Core Package:** Must depend solely on the Python Standard Library (3.10+) and standard SQLite3. No external packages may be listed in `dependencies = []`.
2. **Optional Features:** Advanced columnar readers (e.g. Parquet) must be packaged as optional extras:
   ```toml
   [project.optional-dependencies]
   parquet = ["pyarrow>=25.0.1"]
   dev = ["pytest>=7.0", "pytest-cov>=4.0"]
   ```
3. **No Heavy Frameworks:** CandGuard will not depend on pandas, polars, spark, or heavyweight ORMs for core functionality.

---

## 13. Compatibility Policy

1. **Python Versions:** Supported on Python 3.10, 3.11, 3.12, and 3.13 across Linux, macOS, and Windows.
2. **CLI Exit Codes:**
   - `0`: Quality gates passed.
   - `1`: Quality gate failed.
   - `2`: Invalid input, configuration, or operational error.
3. **Ground-Truth Fingerprints:**
   - Comparisons using `--max-recall-drop` verify comparability via SHA-256 v2 length-prefixed digests. Mismatched datasets or fingerprint versions reject execution with exit code 2.

---

## 14. Resource & Scale Boundaries

- **Disk-Backed SQLite Processing:** Candidate and ground-truth pairs are stored in a temporary SQLite database rather than in-memory Python structures.
- **Batch Ingestion:** Inputs are parsed and inserted in configurable batches (`batch_size=50_000` by default). Batch sizes must be positive integers (`batch_size > 0`).
- **Workload-Dependent Memory:** Resident memory consumption is workload- and configuration-dependent. Existing measurements in `docs/benchmark-report.md` show peak memory increasing with dataset size: roughly 45–47 MB at 100k rows, 163–170 MB at 1M rows, 325–327 MB at 5M rows, and 419–420 MB at 10M rows, across the `cross_no_truth`, `cross_with_truth`, and `dedupe_with_truth` workflows on the documented test machine.
- **No $O(1)$ Memory Guarantee:** CandGuard does not make an $O(1)$ constant-memory guarantee.
- **Empirical Scale Boundary:** The 10-million-pair benchmark represents an empirical validation boundary on documented hardware, not an architectural guarantee for arbitrary dataset sizes or hardware environments.

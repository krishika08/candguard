# CandGuard Phase 3: Performance, Scale, and Compatibility Benchmark Report

**Project:** CandGuard — Open-source CLI for auditing entity-resolution candidate generation  
**Scope:** Performance Reconciliation, 10M-Row Scale Validation, and Benchmark Integrity (Phases 3.1–3.3)  
**Date:** October 2026  
**Status:** Validated, zero runtime dependencies preserved, 57/57 tests passing.

---

## 1. Environment & Hardware Specifications

All benchmark measurements were conducted on a dedicated physical host with the following documented specifications (no virtualized nested hypervisors or cloud throttling):

| Component | Detailed Hardware / OS Specification |
|:---|:---|
| **Host Processor** | Intel(R) Core(TM) Ultra 5 125H (14 physical cores [4 Performance, 8 Efficient, 2 Low-Power], 18 logical threads; base 1.2 GHz, turbo up to 4.5 GHz) |
| **System Memory** | 15.52 GiB Physical LPDDR5 RAM (Available free RAM during test runs: 2.7–3.8 GiB) |
| **Storage Subsystem** | NVMe PCIe 4.0 Solid-State Drive (99.4 GiB free filesystem workspace) |
| **Operating System** | Windows 11 Pro 64-bit (Build `10.0.26200-SP0`) |
| **Python Runtime** | Python 3.12.10 (64-bit AMD64 architecture) |
| **Database Engine** | SQLite 3.49.1 (built into standard library `sqlite3`) |
| **External Dependencies** | **0** (strictly Python standard library + built-in SQLite) |

*Note: Benchmarks reflect this specific machine configuration. Performance and peak memory will vary on different operating systems, CPU architectures, and storage controllers.*

---

## 2. Benchmark Methodology

### 2.1 Synthetic Dataset Generation
To ensure reproducible measurements without checking multi-gigabyte fixtures into version control:
- A streaming generator is implemented in [benchmarks/generator.py](file:///f:/candguard/benchmarks/generator.py).
- TSV rows are generated and written to disk in streaming batches of 50,000 records with a fixed RNG seed (`42`), keeping generator RAM consumption under 2 MB.
- IDs follow predictable prefixes (`SRC_<index>` / `TGT_<index>` for cross-linkage; `ENT_<index>` for deduplication), with controlled duplicate-pair injection rates (default 5%) and known ground-truth targets (default 85% recall target, 5% of candidate volume).

### 2.2 Representative Workflows
1. **`cross_no_truth`**: Cross-dataset candidate audit without ground truth. Audits pair deduplication, record coverage, and candidate-per-source degree distributions.
2. **`cross_with_truth`**: Cross-dataset audit against labeled ground truth. Audits candidate recovery, missed matching pairs, candidate recall ratio, and unambiguous SHA-256 identity fingerprinting.
3. **`dedupe_with_truth`**: Deduplication audit. Enforces canonical pair ordering ($u < v$), audits degree distribution across the undirected candidate graph, audits unique record representation, and evaluates recall against labeled matches.
4. **`compare_regression`**: The `candguard compare` CLI command comparing baseline and candidate PR audit JSON reports across pair-growth and recall-drop thresholds.

### 2.3 Measurement Methodology
- **Wall-Clock Elapsed Time:** High-resolution timing measured via `time.perf_counter()`.
- **Peak Resident Memory:** Measured using the standard library (`ctypes` invoking the Windows Win32 `K32GetProcessMemoryInfo` API to query `PeakWorkingSetSize`, and POSIX `resource.getrusage` / `ru_maxrss`).
- **Disk & Database Cleanup:** Verified by counting matching temporary SQLite databases (`tempfile.gettempdir() + "/candguard-*.sqlite3*"`) before, during, and after every execution.

---

## 3. Investigation & Resolution of Benchmark Integrity Issues

### 3.1 Ground-Truth Counts Reconciliation (Issue A)
During earlier benchmarks of the 5M deduplication workload, the report cited `truth_pairs=250,000` but showed `truth_unique_pairs: 6,708`, `recovered_pairs: 6,708`, and `candidate_recall: 1.0`.
- **Root Cause:** In [benchmarks/generator.py](file:///f:/candguard/benchmarks/generator.py), the synthetic generator previously assigned pairs via `u = i % num_left` and `v = (i + 1) % num_left`. Because `num_left` was set to $\lfloor\sqrt{5,000,000}\times 3\rfloor = 6,708$, this modulo formula generated only 6,708 distinct pairs. When looping for 250,000 iterations, it wrote 250,000 lines to disk containing 6,708 unique pairs repeated ~37 times (243,292 duplicate truth rows). Furthermore, because `recovered_count = int(250,000 * 0.85) = 212,500 > 6,708`, all 6,708 unique pairs were placed into the candidate recovery list, artificially forcing candidate recall to 1.0 (100%).
- **Verification of Recall Arithmetic:** In [src/candguard/audit.py](file:///f:/candguard/src/candguard/audit.py), truth deduplication and recall calculation were verified to be strictly correct: `audit_candidates` accurately deduplicated the raw truth rows into 6,708 unique pairs, found 6,708 matches in the candidate set, and computed $6,708 / 6,708 = 1.0$.
- **Resolution:** `benchmarks/generator.py` was updated to use multi-stride ring indexing for dedupe mode (`stride = 1 + ((i // num_left) % max(1, (num_left - 1) // 2))`) and 2D grid mapping for cross mode (`v = (i // num_left) % num_right`). A regression test was added in [tests/test_audit.py](file:///f:/candguard/tests/test_audit.py). On the corrected generator:
  - Configured truth pairs: `250,000`
  - Actual truth input rows: `250,000`
  - Unique logical truth pairs: `250,000` (0 duplicate truth rows)
  - Recovered truth pairs: `219,289`
  - Missed truth pairs: `30,711`
  - Measured candidate recall: `0.877156` (87.72%, reflecting the 85% generator target plus random candidate collisions).

### 3.2 Peak-Memory Measurement Verification (Issue B)
Earlier benchmarks recorded monotonic per-run memory values (166 MB $\to$ 330.43 MB $\to$ 331.33 MB $\to$ 333.71 MB).
- **Root Cause:** In [benchmarks/system_info.py](file:///f:/candguard/benchmarks/system_info.py), `get_peak_memory_mb()` invokes Windows `GetProcessMemoryInfo(GetCurrentProcess())` to query `PeakWorkingSetSize`. This metric is the **cumulative lifetime high-water mark of the executing process** since start; it never decreases within a single process. When multiple benchmark iterations were executed in a single parent process, each subsequent run merely reported the cumulative maximum of all previous runs.
- **Resolution:** [benchmarks/reconcile_5m.py](file:///f:/candguard/benchmarks/reconcile_5m.py) was updated to spawn each individual benchmark run in an **isolated child process**. Each child process initializes fresh (~16 MB base working set) and reports only its own individual peak working set.
- **Observed Peak Working Sets (Independent Child Processes):**
  - Baseline (Phase 2): **162.38 MB median** (4 runs: 162.37 MB, 162.16 MB, 162.38 MB, 163.41 MB).
  - Optimized (Phase 3.2): **324.78 MB median** (4 runs: 324.39 MB, 325.74 MB, 324.65 MB, 324.91 MB).
  - *Explanation of Memory Difference:* The optimized implementation intentionally configures `PRAGMA mmap_size = 256MB` and SQLite page cache to allow zero-copy memory-mapped file access, which increases resident working set by ~162 MB while delivering a 28.7% reduction in elapsed execution time.
  - *Distinction:* Peak working set is an empirical per-process measurement on this Intel Core Ultra 5 system, not an established constant or bounded memory guarantee.

### 3.3 Conclusive Interleaved Repeat-Run Measurements (Strictly Comparable)
To eliminate execution order bias and temporary disk contention artifacts, an interleaved benchmark protocol was executed on the exact same generated dataset files:
- **Candidate File:** `candidates_5000000.tsv` (123.98 MB) — SHA-256: `6fbe7a51fef2e81e104ab0660ae219d5a24f0af774cd991b3ffb43c7e3e86472`
- **Truth File:** `truth_5000000.tsv` (6.20 MB) — SHA-256: `d26ea06088ebb0cfbd11901b7fd2d1dcc9eaafca3775ab2184f5d0903c724514`
- **Execution Order:** Alternating sequence `[Baseline 1, Optimized 1, Baseline 2, Optimized 2, Baseline 3, Optimized 3, Baseline 4, Optimized 4]` in independent child processes with a 1.0s settle pause.
- **Cache Limitation Note:** On Windows 11, OS filesystem page cache state and write-back buffers are managed by the kernel and cannot be purged by unprivileged user processes. The interleaved order ensures that neither implementation consistently benefits from warmer cache state or less write-buffer contention.

| Run Step | Implementation | Wall-Clock Time | Peak Resident Memory |
|:---:|:---|:---:|:---:|
| 1 | Baseline (Run 1) | 26.622s | 162.37 MB |
| 2 | Optimized (Run 1) | 19.035s | 324.39 MB |
| 3 | Baseline (Run 2) | 26.211s | 162.16 MB |
| 4 | Optimized (Run 2) | 19.089s | 325.74 MB |
| 5 | Baseline (Run 3) | 26.561s | 162.38 MB |
| 6 | Optimized (Run 3) | 18.738s | 324.65 MB |
| 7 | Baseline (Run 4) | 26.441s | 163.41 MB |
| 8 | Optimized (Run 4) | 18.643s | 324.91 MB |

#### Statistical Summary:
- **Baseline Implementation (4 child-process runs):**
  - Timings: `[26.622s, 26.211s, 26.561s, 26.441s]`
  - **Median: 26.501s** | Mean: 26.459s | StdDev: 0.182s | Median Peak RAM: 162.38 MB
- **Optimized Implementation (4 child-process runs):**
  - Timings: `[19.035s, 19.089s, 18.738s, 18.643s]`
  - **Median: 18.887s** | Mean: 18.876s | StdDev: 0.219s | Median Peak RAM: 324.78 MB
- **Speedup Ratio ($T_{\text{baseline}} / T_{\text{optimized}}$):** **1.40×**
- **Median Elapsed-Time Reduction ($(T_{\text{baseline}} - T_{\text{optimized}}) / T_{\text{baseline}}$):** **28.7%** (7.61 seconds faster per 5M-row run).
- **Logical Equivalence:** Both implementations produced 100% identical logical outputs (`candidate_rows=5,000,000`, `unique_candidate_pairs=4,290,355`, `duplicate_candidate_rows=709,645`, `records_represented=6,708`, `truth_rows=250,000`, `truth_unique_pairs=250,000`, `recovered_pairs=219,289`, `missed_pairs=30,711`, `candidate_recall=0.877156`, and identical degree distribution percentiles).

### 3.4 Correct Terminology: Speedup Ratio vs. Elapsed-Time Reduction (Issue C)
To prevent confusion in performance reporting, CandGuard strictly distinguishes between two mathematically different metrics:
- **Speedup Ratio ($S$):** Multiplicative factor defined as $S = \frac{T_{\text{baseline}}}{T_{\text{optimized}}}$.
  - On the current child-process interleaved benchmark: $S = \frac{26.501\,\text{s}}{18.887\,\text{s}} \approx \mathbf{1.40\times}$.
  - (On preliminary single-run measurements with $T_{\text{baseline}} = 57.426\,\text{s}$ and $T_{\text{optimized}} = 41.782\,\text{s}$, the speedup ratio was $S = \frac{57.426}{41.782} \approx \mathbf{1.37\times}$).
- **Elapsed-Time Reduction ($\Delta T_{\%}$):** Fractional time saved defined as $\Delta T_{\%} = \frac{T_{\text{baseline}} - T_{\text{optimized}}}{T_{\text{baseline}}} \times 100\%$.
  - On the current child-process interleaved benchmark: $\Delta T_{\%} = \frac{26.501 - 18.887}{26.501} \approx \mathbf{28.7\%}$ (saving 7.61 seconds per run).
  - (On preliminary single-run measurements with $T_{\text{baseline}} = 57.426\,\text{s}$ and $T_{\text{optimized}} = 41.782\,\text{s}$, the elapsed-time reduction was $\Delta T_{\%} = \frac{57.426 - 41.782}{57.426} \approx \mathbf{27.2\%}$).
- **Reporting Standard:** A speedup ratio of $1.40\times$ (or $1.37\times$) must **never** be conflated with or phrased as "+40% speedup" or "28.7% speedup". The speedup is a multiplicative factor ($1.40\times$), whereas the execution time savings is $28.7\%$. Neither `README.md` nor CandGuard reports conflate these terms.

---

## 4. Comprehensive Performance Measurements (Across Workloads)

The table below summarizes measured wall-clock time, throughput, and peak memory across all tested dataset sizes under comparable conditions:

| Dataset Size | Workflow | Baseline Wall Time | Optimized Wall Time | Throughput | Measured Peak Memory | Temp Storage Leaked |
|:---|:---|:---:|:---:|:---:|:---:|:---:|
| **100,000 rows** (~2.5 MB) | `cross_no_truth` | 0.656s | **0.387s** | 258,706 rows/s | 44.95 MB | 0 |
| | `cross_with_truth` | 0.390s | **0.277s** | 361,418 rows/s | 45.75 MB | 0 |
| | `dedupe_with_truth` | 0.441s | **0.320s** | 312,870 rows/s | 46.82 MB | 0 |
| **1,000,000 rows** (~24.8 MB) | `cross_no_truth` | 4.144s | **3.003s** | 332,989 rows/s | 163.42 MB | 0 |
| | `cross_with_truth` | 4.028s | **3.022s** | 330,945 rows/s | 167.97 MB | 0 |
| | `dedupe_with_truth` | 4.531s | **3.457s** | 289,271 rows/s | 170.10 MB | 0 |
| **5,000,000 rows** (~124 MB) | `cross_no_truth` | 24.192s | **17.439s** | 286,710 rows/s | 326.83 MB | 0 |
| | `cross_with_truth` | 23.671s | **21.101s** | 236,957 rows/s | 326.83 MB | 0 |
| | `dedupe_with_truth` | 26.501s (median) | **18.887s** (median) | 264,732 rows/s | 324.78 MB | 0 |
| **10,000,000 rows** (~248 MB) | `cross_no_truth` | *(untested)* | **49.733s** | 201,074 rows/s | 419.85 MB | 0 |
| | `cross_with_truth` | *(untested)* | **34.858s** | 286,878 rows/s | 419.25 MB | 0 |
| | `dedupe_with_truth` | *(untested)* | **37.730s** | 265,041 rows/s | 418.57 MB | 0 |

### Comparison Command (`candguard compare`) Performance
- **Wall-Clock Time:** **0.0004s** (< 1 millisecond).
- **Execution Profile:** In-memory JSON parsing and arithmetic comparison, independent of candidate file size. Suitable for instant pre-commit hooks and real-time CI gates.

---

## 5. Tested-Scale and Peak Memory Evaluation

### 5.1 Tested Scale Boundaries
- **Largest Successfully Validated Workload:** **10,000,000 candidate pairs** (candidate file: 247.96 MB; truth file: 12.40 MB).
- **Measured Elapsed Time on Documented Machine (Phase 3.3):** **34.9s to 49.7s** across cross and dedupe workflows in isolated processes.
- **Measured Peak Memory (Phase 3.3):** **418.57 MB to 419.85 MB** (~419 MB) in isolated child processes.
- **Comparison with Earlier Generator Results:**
  - *Legacy Generator (Preliminary):* 33.5s–40.0s elapsed, 422–428 MB peak memory (evaluated with the earlier wrapped generator and without strict child process separation).
  - *Corrected Generator & Isolated Child Processes (Phase 3.3):* 34.9s–49.7s elapsed, 418.6–419.9 MB peak memory. The 10M scale boundary remains fully validated, with peak working set bounded at ~419 MB.
- **Scale Limitation:** Workloads larger than 10,000,000 candidate pairs have **not** been benchmarked or validated. Extrapolating beyond 10M rows is not supported by empirical evidence.

### 5.2 Ten-Million-Row Workload File Hashes & Correctness Verification (Phase 3.3)
Every 10M dataset was generated with the corrected generator ([benchmarks/generator.py](file:///f:/candguard/benchmarks/generator.py)), validated for 100% uniqueness, and audited in an independent child process:

1. **`cross_no_truth` (10,000,000 rows, cross linkage, no truth):**
   - Candidate File: `candidates_10000000.tsv` (260,000,023 bytes / 247.96 MB)
   - SHA-256: `489cb0a91238eb98317f5bc1515ec55cc4adab37276fee5eb74b4d70aaebf4fc`
   - Audit Output: `candidate_rows=10,000,000`, `unique_candidate_pairs=9,015,345`, `duplicate_candidate_rows=984,655` (9.85%)
   - Performance: `49.733s` elapsed (201,074 rows/s), `419.85 MB` peak working set, 0 leaked databases.

2. **`cross_with_truth` (10,000,000 rows, cross linkage, with truth):**
   - Candidate File: `candidates_10000000.tsv` (260,000,023 bytes / 247.96 MB) — SHA-256: `a77d20bc80a29064bc5213f6aa7c3c4ed927d8a0261770bc5294b5db3658acba`
   - Truth File: `truth_10000000.tsv` (13,000,023 bytes / 12.40 MB) — SHA-256: `a85411a5162589c220c8bdd92d49acf36835a1c816f87eed4c99e0578ef646a6`
   - Input Verification: 500,000 truth rows, 500,000 unique pairs (0 duplicate rows).
   - Audit Output: `candidate_rows=10,000,000`, `unique_candidate_pairs=9,034,941`, `duplicate_candidate_rows=965,059`
   - Ground Truth: `truth_rows=500,000`, `truth_unique_pairs=500,000`, `recovered_pairs=432,134`, `missed_pairs=67,866`, `candidate_recall=0.864268` (86.43%, matching 425k injected target pairs + 7,134 natural random collisions).
   - Performance: `34.858s` elapsed (286,878 rows/s), `419.25 MB` peak working set, 0 leaked databases.

3. **`dedupe_with_truth` (10,000,000 rows, deduplication, with truth):**
   - Candidate File: `candidates_10000000.tsv` (260,000,023 bytes / 247.96 MB) — SHA-256: `485849df890fcb81421e9e5252e418b158c8175bf81ba297ce05c5a0a6feab06`
   - Truth File: `truth_10000000.tsv` (13,000,023 bytes / 12.40 MB) — SHA-256: `a516f6b9ca51a4ef61d6bfab0fe5031dc401b700f6d1a9c89cf3a3b0c540de2d`
   - Input Verification: 500,000 truth rows, 500,000 unique canonical pairs ($u < v$, 0 duplicate rows, 0 self-pairs).
   - Audit Output: `candidate_rows=10,000,000`, `unique_candidate_pairs=8,581,185`, `duplicate_candidate_rows=1,418,815`
   - Ground Truth: `truth_rows=500,000`, `truth_unique_pairs=500,000`, `recovered_pairs=438,927`, `missed_pairs=61,073`, `candidate_recall=0.877854` (87.79%, matching 425k injected target pairs + 13,927 natural random collisions).
   - Performance: `37.730s` elapsed (265,041 rows/s), `418.57 MB` peak working set, 0 leaked databases.

### 5.3 Memory Scaling: Measured Observation vs. Constant-Memory Guarantees
- The benchmark data confirms that peak resident memory **scales sub-linearly with dataset size**:
  - 100k rows: ~45 MB peak RAM
  - 1M rows: ~165 MB peak RAM
  - 5M rows: ~325 MB peak RAM
  - 10M rows: ~419 MB peak RAM
- **Explicit Distinction:** This measured peak memory behavior is an **empirical observation** on the documented Intel Ultra 5 system in isolated child processes. It is **not** an established constant-memory or $\mathcal{O}(1)$ mathematical guarantee. Memory increases due to SQLite internal cache pages, freelist allocation, B-tree depth, and operating system file mapping.

### 5.4 Throughput Distinction (Overall vs. 10M Rows)
- **Across All Workload Scales (100k, 1M, 5M, 10M rows):** Measured throughput ranges from **200,000 to 361,418 rows/second** on the tested host.
- **Specifically at the 10,000,000-Row Scale:** Measured throughput ranges from **201,074 to 286,878 rows/second** (`49.733s` for `cross_no_truth`, `34.858s` for `cross_with_truth`, and `37.730s` for `dedupe_with_truth`).
- **Performance Disclaimer:** Measured throughput is an empirical record of benchmark runs on the documented physical host; it is not a performance SLA or guarantee for arbitrary customer datasets or different hardware configurations.

### 5.5 Reproducible Configurations & Sequential Regeneration Lifecycle
To prevent disk exhaustion while ensuring strict reproducibility:
- **Reproducible Generator Configurations:** Every benchmark dataset is generated deterministically via [benchmarks/generator.py](file:///f:/candguard/benchmarks/generator.py) with fixed RNG seed (`42`), duplicate candidate injection rate (`0.05`), and recall target (`0.85`).
- **Sequential Regeneration:** Because the generator uses standardized filenames (`candidates_10000000.tsv` and `truth_10000000.tsv`), the scale harness ([benchmarks/scale_10m.py](file:///f:/candguard/benchmarks/scale_10m.py)) executes workflows sequentially. Each workflow generates its files in an isolated temporary subfolder (`wf_cross_no_truth`, `wf_cross_with_truth`, `wf_dedupe_with_truth`), records file hashes, verifies logical uniqueness, executes the audit in a dedicated child process, and immediately unlinks the 250 MB TSV files before proceeding to the next workflow. This ensures that workflows do not collide, cache state is controlled, and workspace disk footprint remains minimal (< 300 MB peak).

---

## 6. Ground-Truth Fingerprint Format & Compatibility

### 6.1 Unambiguous Serialization & Domain Separation
CandGuard computes a SHA-256 digest to verify ground-truth identity between audit reports. To prevent collision vectors when identifiers contain delimiters, quotes, newlines, or Unicode characters:
- **v1 Encoding (Legacy):** Serialized pairs as `f"{left}\t{right}\n"` with domain separation prefix `candguard:truth:v1:{linkage_type}\n`. While effective for simple alphanumeric IDs, an identifier containing a tab (e.g. `("a\tb", "c")` vs. `("a", "b\tc")`) could produce identical serialization.
- **v2 Encoding (Current):** Implements unambiguous length-prefixed serialization:
  $$\text{Prefix} = \text{"candguard:truth:v2:\{linkage\_type\}\textbackslash n"}$$
  $$\text{Row} = \text{len}(L)\text{:} L\text{\textbackslash t}\text{len}(R)\text{:} R\text{\textbackslash n}$$
  where lengths are byte-lengths in ASCII and strings are UTF-8 encoded.
- **Collision Characterization:** SHA-256 is treated as a cryptographically strong fingerprint over an unambiguous serialization. It is not described as mathematically collision-proof.

### 6.2 Explicit Versioning in Audit Reports
The audit JSON schema now explicitly records the serialization version:
```json
"ground_truth": {
  "rows": 50000,
  "unique_pairs": 48210,
  "recovered_pairs": 41200,
  "missed_pairs": 7010,
  "candidate_recall": 0.85459448,
  "fingerprint": "sha256:d5c2...",
  "fingerprint_version": "v2"
}
```

### 6.3 Regression Comparison Compatibility Semantics (`candguard compare`)
When comparing a baseline report against a current report:
1. **Pair-Count and Duplicate Growth Comparisons:**
   If `--max-recall-drop` is not specified, comparisons proceed successfully regardless of ground-truth presence or fingerprint version.
2. **Recall Regression Comparisons:**
   When `--max-recall-drop` is configured:
   - If either report lacks a fingerprint, comparison fails with exit code `2`.
   - If the baseline and current reports have different fingerprint versions (e.g. baseline is `v1` and current is `v2`), comparison fails with exit code `2`:
     `Incompatible ground-truth fingerprint versions: baseline uses 'v1', current uses 'v2'. Re-run audit with the same CandGuard version to compare recall.`
   - If both reports omit `fingerprint_version`, both are treated as `v1` for backward compatibility.
   - If versions match, comparison verifies that `baseline_fingerprint == current_fingerprint`.

---

## 7. Adversarial Identifier Test Suite

Unit tests in [tests/test_audit.py](file:///f:/candguard/tests/test_audit.py) validate edge-case robustness:
1. **Embedded Delimiters & Quotes:** Verified CSV/TSV inputs containing commas, tabs, escaped quotes (`""`), backslashes, newlines (`\n`), and Unicode emojis (`🔥`, `⚡`) parse accurately without line splitting or tuple corruption.
2. **Unambiguous Hash Separation:** Verified that distinct logical pairs with identical naive concatenation (e.g. `("a\tb", "c")` vs `("a", "b\tc")`, and `("a\nc", "d")` vs `("a", "c\nd")`) produce strictly distinct SHA-256 fingerprints.
3. **Missed Pairs Export:** Verified that missed ground-truth pairs with embedded newlines and quotes export correctly to TSV.

---

## 8. Summary of Engine Assessment

- **SQLite Suitability:** Built-in SQLite remains the appropriate choice for CandGuard’s single-node, dependency-free mission up to the validated 10M-row boundary.
- **When DuckDB or Multi-Threading Is Justified:** If candidate generation pipelines routinely require auditing candidate files exceeding **50,000,000+ rows**, or if direct column-store Parquet reading is required, adopting DuckDB or chunk-level multiprocessing would be justified based on measured benchmark evidence.

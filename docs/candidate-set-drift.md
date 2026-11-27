# Candidate-set drift analysis

`candguard compare` can compare the exact composition of two candidate sets, not just their sizes. It reports which unique candidate pairs were retained, removed and added between a baseline and a current run. It can also fail the quality gate when the sets are too different.

## Why count-only comparison is not enough

Without candidate sets, `compare` sees only candidate row growth, duplicate row growth and recall drop. A blocking change can replace most candidate pairs while keeping every one of those numbers the same:

| Baseline | Current |
|:--|:--|
| `(A1, B1)` | `(A1, B1)` |
| `(A1, B2)` | `(A1, B9)` |
| `(A2, B3)` | `(A2, B8)` |
| `(A3, B4)` | `(A3, B7)` |

Both sets contain four unique pairs, so pair growth is 0%. If the ground truth only contains `(A1, B1)`, recall is unchanged too. Yet only one pair was retained: Jaccard similarity is 1/7 ≈ 14.29%. Downstream matching now scores a mostly different set of comparisons.

Drift analysis detects this by comparing the two candidate sets pair by pair.

## Workflow

1. **Export a sidecar during each audit** with `--candidate-set-out`. The audit writes its unique, normalized candidate pairs to a sorted TSV file. It also records the file's format, pair count and fingerprint in the report's `candidate_set` section.
2. **Keep the sidecar next to its report.** The report does not store the sidecar's path, and the pairs are never embedded in the JSON.
3. **Pass both reports and both sidecars to `compare`.** `compare` validates each sidecar against the metadata in its report while it merges the two sorted files in one streaming pass. It then adds `metrics.candidate_set_drift` to the comparison report.

```bash
# Baseline (e.g. on main)
candguard audit \
  --candidates base_candidates.tsv --truth truth.tsv \
  --left-id source_id --right-id candidate_id \
  --candidate-set-out artifacts/baseline.pairs.tsv \
  --format json --output reports/baseline.json

# Current (e.g. on the PR branch)
candguard audit \
  --candidates pr_candidates.tsv --truth truth.tsv \
  --left-id source_id --right-id candidate_id \
  --candidate-set-out artifacts/current.pairs.tsv \
  --format json --output reports/current.json

candguard compare \
  --baseline reports/baseline.json \
  --current reports/current.json \
  --baseline-candidate-set artifacts/baseline.pairs.tsv \
  --current-candidate-set artifacts/current.pairs.tsv \
  --max-recall-drop 0.01 \
  --max-pair-growth 0.20 \
  --min-candidate-jaccard 0.90
```

The Python API takes the same options as keyword arguments:

```python
from candguard import audit_candidates, compare_reports

report = audit_candidates(
    candidates_path="pr_candidates.tsv",
    left_id="source_id",
    right_id="candidate_id",
    candidate_set_path="artifacts/current.pairs.tsv",
)

comparison = compare_reports(
    baseline_path="reports/baseline.json",
    current_path="reports/current.json",
    baseline_candidate_set_path="artifacts/baseline.pairs.tsv",
    current_candidate_set_path="artifacts/current.pairs.tsv",
    min_candidate_jaccard=0.90,
)
```

No sidecar is written unless one is requested. Without sidecar paths, `compare` behaves exactly as before: its output is unchanged and it opens no extra files.

## Sidecar format: `candguard-pairs-tsv-v1`

- **Encoding:** UTF-8 without a byte-order mark, written by Python's `csv.writer`.
- **Delimiter and quoting:** tab-delimited, with `"` as the quote character. A field is quoted when it contains a tab, a quote or `\n`; embedded quotes are doubled. Rows end with `\n`. If either identifier in a pair contains `\r`, both fields of that row are quoted. Python 3.10 and 3.11's minimal quoting would leave a bare `\r` unquoted, so this rule keeps the bytes identical on every supported Python version.
- **Header:** exactly `left_id<TAB>right_id`. It does not repeat the input file's column names.
- **Rows:** each data row is exactly one unique candidate pair. Duplicate raw candidate rows are collapsed before export.
- **Order:** rows are in strict ascending `(left_id, right_id)` order. The export runs `ORDER BY left_id COLLATE BINARY, right_id COLLATE BINARY` over the audit's deduplicated SQLite table. For UTF-8 text, SQLite `BINARY` order is the same as Unicode code-point order, which is the order Python uses to compare `str` values. `compare` checks this order using Python string comparison.

Identifiers are exported exactly as the audit normalized them: trimmed text, with integer Parquet identifiers converted to decimal text. The export reads the already-ingested SQLite database, never the original candidate file, so CSV, TSV and Parquet inputs holding the same logical pairs produce byte-identical sidecars. Identifiers containing tabs, quotes, commas, newlines or any Unicode characters round-trip exactly.

### Pair semantics

- **Cross-linkage (`--linkage-type cross`):** pairs are directional. `(A, B)` and `(B, A)` are different pairs and are exported as separate rows. A pair whose direction changes between runs therefore counts as one removal plus one addition.
- **Deduplication (`--linkage-type dedupe`):** pairs are undirected. Each pair is stored once in canonical form with `left_id < right_id`. Input rows `(B, A)` and `(A, B)` are the same edge, and self-pairs are still rejected during the audit.

### Output publication

CandGuard streams the sidecar to a temporary file in its destination directory. It verifies the pair count against `inputs.unique_candidate_pairs`, flushes and syncs the file, and closes it before publication. The audit coordinates its requested sidecars and any CLI report file. It stages them all before replacing the first destination and publishes the report last. An audit that completes still publishes the sidecar when a quality gate fails (exit code 1).

During rollback after a replacement failure, CandGuard restores previous outputs from backups and removes newly published outputs. With a rollback failure, the error names the destinations that need attention and the paths of any recovery backups. See the [output safety notes](../README.md#output-safety) for the concurrent-writer assumption and stdout and crash limitations.

The output path is rejected (exit code 2) when it resolves to any of these:
- the candidate input file;
- the ground-truth input file;
- the `--missed-pairs-out` file;
- the CLI `--output` report file;
- an existing directory.

## Report metadata

When `--candidate-set-out` is used, the audit report gains a top-level `candidate_set` section:

```json
"candidate_set": {
  "format": "candguard-pairs-tsv-v1",
  "unique_pairs": 100000,
  "fingerprint": "sha256:…",
  "fingerprint_version": "v1",
  "linkage_type": "cross"
}
```

- `unique_pairs` always equals `inputs.unique_candidate_pairs`.
- The sidecar's absolute path is deliberately not stored, so reports stay portable. `compare` receives the artifact paths explicitly.
- Reports written without `--candidate-set-out`, and reports from earlier CandGuard versions, have no `candidate_set` section. They remain valid for every existing comparison.

## Fingerprint and integrity validation

The candidate-set fingerprint is a SHA-256 digest with domain separation:

```text
candguard:candidate-set:v1:<linkage_type>\n
<len(left)>:<left>\t<len(right)>:<right>\n     (one line per pair, in canonical sorted order)
```

- `<len>` is the decimal UTF-8 byte length of the identifier. Because of this length prefix, identifiers that contain tabs, newlines, colons or digits cannot shift the pair boundaries.
- The linkage type is part of the domain, so a cross set and a dedupe set holding the same pairs have different fingerprints.
- The prefix is different from the ground-truth fingerprint's (`candguard:truth:v2:…`), so a candidate-set digest can never equal a ground-truth digest. `compute_truth_fingerprint_v2()` and its behavior are unchanged.

`compare` refuses to calculate drift from an artifact it cannot verify. Each check below produces an error that names the artifact (baseline or current) and the reason, with exit code 2.

**Report metadata:**
- both reports contain `candidate_set` metadata (otherwise the error says to re-run both audits with `--candidate-set-out`);
- the format is `candguard-pairs-tsv-v1` and the fingerprint version is `v1`;
- `unique_pairs` is a non-negative integer equal to `inputs.unique_candidate_pairs`;
- the fingerprint is well formed;
- the metadata's linkage type matches the report's linkage type;
- the two reports have the same linkage type (the existing check).

**The artifact file:**
- the path exists and is a regular file;
- it decodes as UTF-8;
- it is well-formed TSV under strict CSV parsing;
- the header is exactly `left_id`, `right_id`;
- each data row has exactly two fields, and neither identifier is empty;
- rows are in strictly ascending order, so out-of-order and duplicate rows are both rejected;
- in dedupe artifacts every pair is canonical (`left_id < right_id`), so reversed pairs and self-pairs are rejected;
- the artifact has no more pairs than its report records (checked while reading, so an extended file fails early);
- at the end of the file, the pair count equals `unique_pairs` (catches truncation) and the recomputed fingerprint equals the report's (catches modified values, swapped artifacts, and artifacts that belong to another audit).

Errors are raised as soon as they are found; they are never swallowed while the other artifact is being read. Both files are always read to the end, even when one set runs out first, so both fingerprints are always verified. Both files are closed whether the comparison succeeds or fails.

The fingerprint covers the logical pairs, not the file's bytes. If a tool converts the line endings between rows to `\r\n`, for example Git with `autocrlf`, the pairs are unchanged and the artifact still verifies. If a conversion changes a newline *inside* a quoted identifier, the identifier changes and the artifact is rejected.

The fingerprint is an integrity check against accidental corruption and mix-ups. It is not a security signature: someone who can edit both a report and its sidecar can make them agree.

## Streaming comparison

The two sidecars are compared with an exact sorted merge. Each artifact reader yields one validated pair at a time, and the merge holds at most the current pair from each side:

- **equal pairs:** retained;
- **baseline pair sorts first:** removed (in baseline only);
- **current pair sorts first:** added (in current only).

When one side runs out, the rest of the other side is drained. The implementation uses no Python `set`, no `list(reader)`, no `readlines()`, no pandas or NumPy, and no in-memory pair dictionary. Validation, hashing and merging happen in the same single pass over each file. The comparison needs no temporary files.

## Metric definitions

Let **B** be the baseline candidate set and **C** the current set, both sets of unique normalized pairs, and:

- **R** = retained pairs = |B ∩ C|
- **A** = added pairs = |C − B|
- **D** = removed pairs = |B − C|
- **U** = union pairs = |B ∪ C| = R + A + D

`|B| = R + D` and `|C| = R + A`; both are checked against the report metadata.

| Field | Definition | Empty-set behavior |
|:--|:--|:--|
| `baseline_unique_pairs` | \|B\| | 0 |
| `current_unique_pairs` | \|C\| | 0 |
| `retained_pairs` | R | 0 |
| `added_pairs` | A | 0 |
| `removed_pairs` | D | 0 |
| `union_pairs` | U | 0 |
| `jaccard_similarity` | R / U | `1.0` when both sets are empty |
| `pair_churn` | (A + D) / U, which equals 1 − Jaccard for a non-empty union | `0.0` when both sets are empty |
| `removed_fraction_of_baseline` | D / \|B\| | `null` when the baseline set is empty |
| `added_fraction_of_current` | A / \|C\| | `null` when the current set is empty |

- Fractions are rounded to 8 decimal places, like CandGuard's other ratios. Jaccard and churn are each rounded on their own, so their rounded values can differ from summing to exactly 1 by at most 1e-8.
- All drift metrics use **unique pairs only**. Raw row counts and duplicate rows never affect them; those stay covered by the existing growth metrics.

When the sidecars are supplied, the comparison report gains these fields:
- `metrics.candidate_set_drift`;
- `thresholds.min_candidate_jaccard`, which is `null` when no minimum is set;
- `candidate_set_path` and `candidate_set_fingerprint` under both `baseline` and `current`.

Without sidecars none of these keys appear, so no misleading zero values are reported. All existing metrics and sections are unchanged, and `schema_version` stays `"1.0"` under the additive-field policy.

```json
"candidate_set_drift": {
  "baseline_unique_pairs": 1000,
  "current_unique_pairs": 1050,
  "retained_pairs": 900,
  "added_pairs": 150,
  "removed_pairs": 100,
  "union_pairs": 1150,
  "jaccard_similarity": 0.7826087,
  "pair_churn": 0.2173913,
  "removed_fraction_of_baseline": 0.1,
  "added_fraction_of_current": 0.14285714
}
```

The text report adds a section before the quality gate:

```text
Candidate-set drift (unique pairs)
----------------------------------
Baseline unique pairs:          1,000
Current unique pairs:           1,050
Retained pairs:                 900
Removed from baseline:          100 (10.00%)
Added in current:               150 (14.29%)
Union of candidate pairs:       1,150
Jaccard similarity:             78.2609%
Pair churn:                     21.7391%
Minimum Jaccard similarity:     90.0000%

Quality gate: FAIL
  - candidate-set Jaccard similarity 0.78260870 (78.2609%; 900 retained of 1,150 union pairs) is below required minimum 0.90000000 (90.0000%)
```

The labels are aligned to the same 32-character column as the rest of the compare report.

## Minimum Jaccard gate

`--min-candidate-jaccard X` (Python: `min_candidate_jaccard=X`) fails the quality gate when the measured Jaccard similarity is **below** `X`.

- **Boundary:** a similarity exactly equal to the threshold passes. The check uses the unrounded ratio R / U, computed as one correctly rounded floating-point division. For example, 9 retained pairs out of a union of 10 pass `--min-candidate-jaccard 0.9`.
- **Valid values:** `X` must be a finite number in [0, 1]. Booleans, NaN, infinities, negative values and values above 1 are rejected with exit code 2.
- **Requirement:** `X` requires both candidate-set paths, and the two paths must be supplied together.
- **Without the threshold,** drift metrics are diagnostic only and never affect the quality gate.

Exit codes are unchanged:
- `0`: all gates passed;
- `1`: a gate failed, including the Jaccard gate;
- `2`: invalid input or configuration, including any invalid, missing or unverifiable artifact.

## CI artifact workflow

Store each sidecar next to its report as a build artifact. For example, in GitHub Actions:

```yaml
# Job on main: produce the baseline
- run: |
    candguard audit --candidates out/candidates.tsv --left-id source_id --right-id candidate_id \
      --candidate-set-out baseline/pairs.tsv --format json --output baseline/report.json
- uses: actions/upload-artifact@v4   # use the action's current major version
  with:
    name: candguard-baseline
    path: baseline/

# Job on the PR: produce the current run and compare
- uses: actions/download-artifact@v4   # e.g. with run-id/github-token to fetch main's latest baseline
  with:
    name: candguard-baseline
    path: baseline/
- run: |
    candguard audit --candidates out/candidates.tsv --left-id source_id --right-id candidate_id \
      --candidate-set-out current/pairs.tsv --format json --output current/report.json
    candguard compare \
      --baseline baseline/report.json --current current/report.json \
      --baseline-candidate-set baseline/pairs.tsv --current-candidate-set current/pairs.tsv \
      --max-pair-growth 0.20 --min-candidate-jaccard 0.90
```

Each report and its sidecar must come from the same audit. A report from one run combined with a sidecar from another fails fingerprint verification instead of producing misleading drift.

## Performance and memory

Measured with `python -m benchmarks.candidate_drift --pairs 100000,1000000 --repeats 3`. The benchmark uses synthetic cross-linkage data with controlled overlap and 5% duplicate rows, written in scrambled order. Each audit and each comparison runs in an isolated child process.

Setup: Windows 11, 18 logical CPUs, 15.5 GiB RAM, Python 3.12.10, SQLite 3.49.1, PyArrow not used. Medians of 3 runs.

**Export** (baseline audit with `--candidate-set-out`):

| Unique pairs | Candidate rows | Export time, s (min–max) | Full audit, s (median; incl. export) | Sidecar MiB | Peak RSS MB before → after export | SQLite DB MiB at export | Peak temp disk MiB | Leaked temp files |
|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 100,000 | 105,000 | 0.148 (0.145–0.153) | 0.596 | 1.72 | 44.97 → 44.97 | 8.81 | 10.53 | 0 |
| 1,000,000 | 1,050,000 | 1.425 (1.319–1.455) | 12.92 (one run: 21.44) | 17.17 | 203.5 → 203.5 | 88.25 | 105.42 | 0 |

The export did not raise the audit's peak RSS; that peak is reached earlier, during ingestion. Peak temporary disk is the SQLite database plus the full temporary sidecar, which briefly coexist before the rename.

**Comparison** (child process running `compare_reports()` with both sidecars):

| Unique pairs (baseline) | Scenario | Current pairs | Retained | Added | Removed | Jaccard | Compare time, s (min–max) | Peak RSS MB before → after compare | Temp files | Matches expected counts |
|--:|:--|--:|--:|--:|--:|--:|--:|--:|--:|:--|
| 100,000 | identical | 100,000 | 100,000 | 0 | 0 | 1.0 | 0.210 (0.208–0.222) | 20.33 → 20.49 | 0 | 3/3 |
| 100,000 | partial | 105,000 | 90,000 | 15,000 | 10,000 | 0.7826087 | 0.213 (0.211–0.214) | 20.31 → 20.41 | 0 | 3/3 |
| 100,000 | low-overlap | 100,000 | 10,000 | 90,000 | 90,000 | 0.05263158 | 0.208 (0.206–0.229) | 20.30 → 20.39 | 0 | 3/3 |
| 100,000 | equal-high-churn | 100,000 | 50,000 | 50,000 | 50,000 | 0.33333333 | 0.200 (0.198–0.210) | 20.36 → 20.50 | 0 | 3/3 |
| 1,000,000 | identical | 1,000,000 | 1,000,000 | 0 | 0 | 1.0 | 2.002 (2.000–2.049) | 20.36 → 20.59 | 0 | 3/3 |
| 1,000,000 | partial | 1,050,000 | 900,000 | 150,000 | 100,000 | 0.7826087 | 2.159 (2.088–2.179) | 20.25 → 20.48 | 0 | 3/3 |
| 1,000,000 | low-overlap | 1,000,000 | 100,000 | 900,000 | 900,000 | 0.05263158 | 2.209 (2.119–2.213) | 20.31 → 20.54 | 0 | 3/3 |
| 1,000,000 | equal-high-churn | 1,000,000 | 500,000 | 500,000 | 500,000 | 0.33333333 | 2.116 (2.112–2.192) | 20.30 → 20.47 | 0 | 3/3 |

"Matches expected counts" compares retained, added and removed with values derived arithmetically from the generator's id ranges. The benchmark also checks a 2,000-pair run of every scenario against Python `set` operations on the generated rows; all four matched.

These are single-machine measurements, not a performance guarantee. Compare time grows linearly with the total number of pairs in both files, because every row is parsed, validated and hashed in Python. **No constant-memory claim is made:**
- process peak memory still depends on interpreter overhead and identifier lengths;
- the merge's own working set holds only the current pair from each side and does not grow with the number of pairs (see the test `test_comparison_memory_does_not_grow_with_set_size`).

**Disk:**
- a sidecar is plain uncompressed text: the UTF-8 identifiers plus two bytes per pair (tab and newline), plus quotes around any identifier that needs them. For the benchmark's ids, that is 1.72 MiB per 100,000 pairs;
- during export, the temporary sidecar in the destination directory exists alongside the audit's temporary SQLite database;
- the comparison itself creates no temporary files.

## Known limitations

- **Both audits must opt in.** Drift needs sidecars and `candidate_set` metadata from both runs. Reports from earlier versions, or audits run without `--candidate-set-out`, can only use the existing count-based comparison.
- **Paths are supplied explicitly.** `compare` never infers sidecar locations from report paths.
- **Identifiers are compared as normalized strings.** Values such as `001` and `1`, or Unicode strings that look the same but are encoded differently (for example `é` versus `e` plus a combining accent), are different identifiers. Normalize identifiers consistently before auditing.
- **Each pair counts equally.** Drift says how much the candidate set changed, not whether the removed pairs were true matches. Use recall drop and `--missed-pairs-out` for that. There is no per-record or per-blocking-rule drift breakdown.
- **Sidecars grow with the candidate set** and must be stored and moved by your CI system. They are not compressed.
- **Long identifiers:** when reading, identifiers longer than Python's default `csv` field-size limit (131,072 characters) are rejected as malformed.
- **NUL characters:** identifiers containing NUL characters can be exported and compared on Python 3.11+. On Python 3.10 the export refuses them with an explicit error, because that version's `csv` reader cannot read them back.
- **Publication visibility:** readers can see partially updated output sets while the audit replaces files. Each replacement is atomic where supported. A crash or failed rollback can leave a partial result. With stdout, sidecars are already published when the CLI writes the report.

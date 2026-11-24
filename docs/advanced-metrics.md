# Advanced Degree and Coverage Metrics

CandGuard reports two groups of diagnostics alongside its existing metrics:

1. **Record-universe coverage.** How many source/target records (or records, in dedupe mode) appear in at least one candidate pair, measured against the record totals you supply.
2. **Degree analysis.** The distribution of candidate pairs per record on each side, and how concentrated those pairs are.

These are **diagnostics only**. They never affect the quality gate or the exit code, and they add no new CLI flags. The record totals come from the existing `--left-records` / `--right-records` options (`left_records` / `right_records` in the Python API).

All metrics are computed inside the same disk-backed SQLite database as the rest of the audit, from **unique** candidate pairs. Duplicate candidate rows never add degree.

## Definitions

### Terms

- **Degree** of a record: the number of unique candidate pairs it appears in.
  - Cross-linkage: a source record's degree counts distinct targets paired with it; a target record's degree counts distinct sources.
  - Deduplication: pairs are undirected edges. A record's degree is the number of unique pairs it belongs to, counted at both endpoints. `(a, b)` and `(b, a)` are one edge, and self-pairs are rejected during ingestion.
- **Represented record:** a record that appears in at least one candidate pair, so its degree is ≥ 1.
- **Record universe:** all records on a side, as given by the supplied total. Records in the universe that are not represented have degree 0.
- **N (statistical population):** the record total when it is supplied; otherwise the number of represented records.
- **S (total degree):** the number of unique pairs on each side in cross-linkage, and `2 × unique pairs` in dedupe.

### Record coverage (`coverage` section)

These fields are added to the existing `coverage` object. The existing `*_represented` fields are unchanged.

| Mode | Field | Definition |
|:--|:--|:--|
| cross | `left_records_total`, `right_records_total` | The supplied `--left-records` / `--right-records` |
| cross | `left_records_without_candidates` | `left_records_total − left_records_represented` |
| cross | `right_records_without_candidates` | `right_records_total − right_records_represented` |
| cross | `left_record_coverage` | `left_records_represented / left_records_total` |
| cross | `right_record_coverage` | `right_records_represented / right_records_total` |
| dedupe | `records_total` | The supplied `--left-records` |
| dedupe | `records_without_candidates` | `records_total − records_represented` |
| dedupe | `record_coverage` | `records_represented / records_total` |

- The fields are always present. They are `null` when the matching total was not supplied, because CandGuard never invents a total.
- A coverage ratio is `null` when the supplied total is `0`.
- Ratios lie in [0, 1] and are rounded to 8 decimal places.
- A supplied total smaller than the represented count is still rejected with exit code 2 and the existing `Supplied --left-records=… conflicts with candidate data` message.

> **Counts, not IDs.** These fields count how many records have no candidates. They cannot tell you *which* records those are, because a total count carries no IDs. Listing isolated records requires a full record roster, which is future work.

### Degree distribution (`degree_analysis` section)

Each distribution object has these fields:

| Field | Definition |
|:--|:--|
| `statistics_basis` | `"record_universe"` when a total was supplied (zero-degree records are included); `"represented_records"` otherwise |
| `records_in_universe` | The supplied total, or `null` |
| `records_with_candidates` | Represented records (degree ≥ 1) |
| `records_without_candidates` | `records_in_universe − records_with_candidates`, or `null` |
| `total_degree` | S |
| `min`, `max` | Smallest and largest degree in the population, including zeros when a total is supplied |
| `p50`, `p95`, `p99`, `p99_9` | Nearest-rank percentiles (below) |
| `mean` | `S / N` |
| `gini_coefficient` | Gini coefficient of the degrees (below) |
| `top_1_percent_candidate_share` | Share of S held by the highest-degree 1% of records (below) |

**Nearest-rank percentiles.** Sort the N degrees in ascending order, zero-degree records included. Percentile *p* is the value at 1-based rank `max(1, ceil(p × N))`. The rank is computed with exact integer arithmetic (P99.9 uses `ceil(999 × N / 1000)`), so floating-point error can't move it by one. For example, with N = 1000 and degrees 1…1000: P50 = 500, P95 = 950, P99 = 990, P99.9 = 999.

**Mean.** In cross-linkage, `left mean = unique pairs / left N` and `right mean = unique pairs / right N`. In dedupe, `mean = 2 × unique pairs / N`.

**Gini coefficient.** With degrees sorted ascending (`x₁ ≤ … ≤ x_N`):

```
G = (2 · Σᵢ i·xᵢ − (N + 1) · S) / (N · S)
```

This is the standard population Gini, which equals the mean absolute difference between all pairs of records divided by twice the mean. Properties:

- It is 0 when every record has the same degree.
- Its maximum is `(N − 1) / N`, reached when one record holds every pair. For example, 99 zero-degree records plus one record of degree 100 gives G = 0.99.
- It is computed in O(number of distinct degree values) from an ordered histogram, using exact integer arithmetic for `Σ i·xᵢ`. There are no pairwise differences.
- Including zero-degree records raises G, because they represent missing candidates.

**Top 1% candidate share.** Let `k = ceil(N / 100)`. The share is `(sum of the k largest degrees) / S`. Records are ordered by degree descending, then ID ascending, but when the cut-off splits a tie the value doesn't depend on which tied record is chosen. For N ≤ 100, k = 1, so the share is `max / S`.

**`right_to_left_mean_degree_ratio`** (cross only) is `right mean / left mean`, computed from unrounded means. It is `null` when either population is empty or the left mean is 0 (no pairs).

How to read it: every pair adds one degree to each side, so whenever pairs exist the ratio simplifies to `left N / right N`. With totals supplied, that is the ratio of the dataset sizes. Without totals, it is the ratio of represented records. It tells you how much more often, on average, a target record is paired than a source record. It does **not** describe skew within a side; use the Gini coefficient, P99/P99.9 and the top-1% share for that.

### When values are `null`

| Situation | Effect |
|:--|:--|
| No total supplied | Coverage fields, `records_in_universe` and `records_without_candidates` are `null`; statistics use only represented records |
| Supplied total is 0 | Coverage ratio is `null`; when N is also 0, every statistic is `null` |
| N = 0 (no pairs and no total) | `min`, every percentile, `mean`, `max`, `gini_coefficient` and `top_1_percent_candidate_share` are `null` |
| S = 0 but N > 0 (a total was supplied and there are no pairs) | min, percentiles and max are 0; `mean` is 0.0; `gini_coefficient` and `top_1_percent_candidate_share` are `null` (0/0) |
| Left mean is 0, or a side is empty | `right_to_left_mean_degree_ratio` is `null` |

### Interpreting skew

CandGuard deliberately doesn't label any record a "hub" or "outlier", and it has no built-in thresholds. Use these signals together:

- **A high Gini coefficient** means candidates are concentrated on a minority of records. It is often caused by generic values, such as placeholder names or blank-ish addresses, or by over-broad blocking keys. When totals are supplied, many zero-degree records also raise it.
- **A large gap between P99 or P99.9 and `max`** means a few records sit far above the bulk of the distribution.
- **A high top-1% share** gives the fraction of all candidate pairs that the busiest 1% of records account for, which is a direct measure of downstream scoring cost.

Compare these values across pipeline versions in your own reviews. `candguard compare` doesn't gate on them.

## Example

Candidates (TSV) contain 5 unique pairs and 3 duplicate rows: `L1–R1`, `L1–R2`, `L1–R3`, `L2–R1` and `L3–R1`, with `L1–R1` repeated twice and `" L2 "–R1`, which trims to `L2–R1`. The command supplies 6 source records and 4 target records:

```bash
candguard audit --candidates c.tsv --left-id source_id --right-id candidate_id \
  --left-records 6 --right-records 4 --format json
```

```json
"coverage": {
  "left_record_coverage": 0.5,
  "left_records_represented": 3,
  "left_records_total": 6,
  "left_records_without_candidates": 3,
  "right_record_coverage": 0.75,
  "right_records_represented": 3,
  "right_records_total": 4,
  "right_records_without_candidates": 1
},
"degree_analysis": {
  "linkage_type": "cross",
  "left": {
    "statistics_basis": "record_universe", "records_in_universe": 6, "records_with_candidates": 3,
    "records_without_candidates": 3, "total_degree": 5,
    "min": 0, "p50": 0, "p95": 3, "p99": 3, "p99_9": 3, "mean": 0.83333333, "max": 3,
    "gini_coefficient": 0.63333333, "top_1_percent_candidate_share": 0.6
  },
  "right": {
    "statistics_basis": "record_universe", "records_in_universe": 4, "records_with_candidates": 3,
    "records_without_candidates": 1, "total_degree": 5,
    "min": 0, "p50": 1, "p95": 3, "p99": 3, "p99_9": 3, "mean": 1.25, "max": 3,
    "gini_coefficient": 0.45, "top_1_percent_candidate_share": 0.6
  },
  "right_to_left_mean_degree_ratio": 1.5
}
```

(The JSON is reformatted here for readability; real output is sorted and indented.) Hand check for the source side: the degrees are [0, 0, 0, 1, 1, 3], so `Σ i·xᵢ = 4 + 5 + 18 = 27` and `G = (54 − 7·5) / (6·5) = 19/30`.

The text report adds this block before the quality gate. Earlier sections are unchanged:

```
Advanced degree diagnostics (unique pairs per record)
-----------------------------------------------------
                                 Source            Target
Record coverage:                 50.0000%          75.0000%
Records without candidates:      3                 1
Min / P50 / mean:                0 / 0 / 0.8333    0 / 1 / 1.2500
P95 / P99 / P99.9 / max:         3 / 3 / 3 / 3     3 / 3 / 3 / 3
Gini coefficient:                0.6333            0.4500
Top 1% candidate share:          60.0000%          60.0000%
Target/source mean degree ratio: 1.5000
```

When totals are not supplied, the coverage rows show `n/a` and a note says the statistics cover only records represented in candidate output. In dedupe mode, a single column is shown, along with a line such as `Record coverage: 71.4286% (5 of 7 records; 2 without candidates)`.

## Report compatibility

- `schema_version` stays `"1.0"`. The new `degree_analysis` object and the new `coverage` keys are additive. No existing field was renamed, removed or recalculated. `candidate_distribution_per_left_record` keeps its existing definition: represented source records only, with the mean rounded to 4 places.
- Reports from earlier versions lack the new fields. **Consumers should ignore unknown fields and treat these as optional.** `candguard compare` accepts old and new reports in either order.
- When no totals are supplied, `degree_analysis.left` (cross) uses the same represented population as `candidate_distribution_per_left_record`, so their min, P50, P95, P99 and max agree.

## Performance and memory

- **What runs in SQLite.** The source-side (and dedupe) degree table is the one the existing audit already builds. Cross-linkage adds one target-side table, `target_candidate_counts`, with an index; it lives in the same temporary database and is removed with it.
- **What reaches Python.** Each distribution is summarized from one ordered SQL histogram, `SELECT candidate_count, COUNT(*) … GROUP BY candidate_count`, streamed from the cursor. Python receives one row per *distinct degree value*, never per record. That is at most about √(2·S) rows. No record IDs or per-record degrees are fetched, and nothing is collected into a list.
- **Zero-degree records** are handled arithmetically from the supplied total, never materialized.
- **No quadratic steps.** Gini uses the rank-weighted closed form, and top-1% is computed by subtraction in the same pass. The number of SQL statements is constant regardless of input size (this is tested).

Measured with `python -m benchmarks.degree_metrics --pairs 100000,1000000 --repeats 3`. Setup: Windows 11, Intel Family 6 Model 170, 18 logical CPUs, 15.5 GiB RAM, Python 3.12.10, SQLite 3.49.1. Synthetic cross-linkage data with heavy-tailed degrees on both sides and 5% duplicate rows. Each run executed in an isolated child process, and figures are medians of 3 runs.

| Unique pairs | Candidate rows | Degree analysis, s (min–max) | Ingest + existing metrics, s | Peak RSS MB before → after analysis | SQLite DB MiB before → after analysis | Leaked temp files | Matches independent reference |
|--:|--:|--:|--:|--:|--:|--:|:--|
| 100,000 | 104,930 | 0.038 (0.037–0.040) | 0.285 | 45.26 → 45.26 | 5.94 → 6.56 | 0 | yes (3/3 runs) |
| 1,000,000 | 1,049,616 | 0.444 (0.421–0.671) | 3.091 | 171.22 → 173.34 | 59.23 → 65.62 | 0 | yes (3/3 runs) |

The reference statistics were derived from the generator's parameters, with Gini computed from the Lorenz-curve area: an independent method that never touches SQLite. At 1M pairs the analysis added 2.1–3.4 MB of peak resident memory per run and about 6.4 MiB of SQLite storage, for the target-side table and index.

These figures come from one machine and one synthetic workload. They are not a guarantee, and memory still grows with dataset size during ingestion, as described in [benchmark-report.md](benchmark-report.md). This feature makes **no constant-memory claim**.

## Limitations

- Coverage counts cannot identify isolated record IDs. Listing them needs a roster of record IDs, which is future work.
- Supplied totals are trusted as long as they are not smaller than the represented counts. CandGuard cannot detect a total that is too large.
- In cross-linkage, the two totals must be supplied together, as before. In dedupe, only `--left-records` applies.
- There are no thresholds or gates for skew metrics, and no connected-component or cluster analysis.

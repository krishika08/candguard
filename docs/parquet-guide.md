# Parquet Input Guide

CandGuard can read candidate-pair and ground-truth files in Apache Parquet format in addition to CSV/TSV. Parquet support is **optional**: it requires [PyArrow](https://arrow.apache.org/docs/python/), which is not installed by default. CandGuard's default installation still has no runtime dependencies.

## Installing

```bash
pip install "candguard[parquet]"
# from a source checkout:
pip install -e ".[parquet]"
```

The `parquet` extra declares `pyarrow>=25.0.1` with no upper bound. pip chooses a PyArrow release whose `Requires-Python` metadata matches your interpreter:

| Python | Version resolved (checked on 2026-11-24 against PyPI) |
|:--|:--|
| 3.10 | `pyarrow 25.0.1` (PyArrow 26.x requires Python ≥ 3.11) |
| 3.11, 3.12, 3.13 | `pyarrow 26.0.1` |

CSV/TSV users do not need PyArrow. Importing CandGuard never imports PyArrow; it is loaded only when a `.parquet` input is actually opened.

If a `.parquet` input is given and PyArrow is missing, the audit stops with exit code `2`:

```
candguard: error: Reading Parquet input requires the optional PyArrow dependency, which is not installed or could not be imported (No module named 'pyarrow'). Install it with: pip install "candguard[parquet]"
```

## Usage

CandGuard picks the reader from the file extension. `.parquet` (in any letter case, e.g. `.PARQUET`) uses the Parquet reader. Every other extension keeps its existing behavior: `.tsv`/`.tab` are tab-delimited and everything else is comma-delimited. The command-line flags are unchanged.

Parquet candidates:

```bash
candguard audit \
  --candidates candidates.parquet \
  --left-id source_id --right-id candidate_id
```

Parquet ground truth (formats can be mixed freely):

```bash
candguard audit \
  --candidates candidates.tsv \
  --truth truth.parquet \
  --left-id source_id --right-id candidate_id \
  --min-recall 0.95
```

Python API:

```python
from candguard import audit_candidates

report = audit_candidates(
    candidates_path="candidates.parquet",
    truth_path="truth.parquet",
    left_id="source_id",
    right_id="candidate_id",
)
```

`--truth-left-id`/`--truth-right-id`, `--linkage-type`, the quality gates, `--missed-pairs-out`, and `candguard compare` all behave the same regardless of input format. The JSON report schema stays at `1.0`; only the `inputs.candidates` / `inputs.truth` path strings show which file was used.

## Identifier types and normalization

Identifier columns must have one of these Arrow types:

| Arrow type of the column | Converted to text by |
|:--|:--|
| `string`, `large_string`, `string_view`, dictionary-encoded strings | `str.strip()`, the same trimming CSV/TSV identifiers get |
| Signed or unsigned integers of any width (`int8`…`int64`, `uint8`…`uint64`), dictionary-encoded integers | base-10 text: `42` → `"42"`, `-7` → `"-7"` |

Rules:

- **Nulls are rejected.** Example: `Null identifier in column 'source_id' at pairs.parquet row 3; ...`
- **Empty or whitespace-only strings are rejected**, as in CSV/TSV. Example: `Empty identifier in column 'candidate_id' at pairs.parquet row 2; ...`
- **Every other type is rejected rather than silently converted to text.** This covers floats, booleans, binary, decimals, dates and timestamps, lists, structs, and the null type. Example: `Unsupported Parquet type 'double' for identifier column 'source_id' in pairs.parquet. Identifier columns must be string or integer typed; ...` To use one of these, cast the column to string when you write the file, so you choose its exact text form yourself.
- No Unicode normalization is applied, in any format. `"é"` and `"é"` stay distinct identifiers.
- Row numbers in Parquet error messages count data rows from 1. CSV/TSV error messages give the file line number, with the header on line 1.

### CSV/TSV versus Parquet

| | CSV/TSV | Parquet |
|:--|:--|:--|
| Dependency | none | `pyarrow` (optional extra) |
| Identifier values | always text | string or integer columns |
| Trimming | `str.strip()` | `str.strip()` for strings; integers have no whitespace |
| Leading zeros | kept (`"007"` stays `"007"`) | kept in string columns; an **integer** column holds `7`, which becomes `"7"` |
| Missing/empty IDs | rejected | null and empty/whitespace-only values rejected |
| Columns read | the whole row is parsed | only the two identifier columns are read from the file |
| Extra columns | ignored | ignored, whatever their type |
| Delimiter flags | `--delimiter`, `--truth-delimiter` | **rejected** with exit code 2, because a delimiter has no meaning for Parquet |
| Duplicate column names | rejected | rejected |

An integer ID in a Parquet file matches the same ID written as decimal text in a CSV/TSV file. This is tested: `7` in Parquet equals `"7"` in CSV. It does not match a zero-padded text form such as `"007"`. If your IDs have significant leading zeros, store them as strings.

## Batch processing and memory

The reader opens the file with `pyarrow.parquet.ParquetFile` and streams it with `ParquetFile.iter_batches()`. It reads only the two identifier columns, in batches of 65,536 rows, and keeps the file's row order. It never calls `read_table()` on the whole file. Pairs are passed to SQLite in batches of exactly `batch_size` pairs (default 50,000; only the last batch may be smaller), whatever the file's row-group layout. `batch_size` must be a positive integer, and `0`, negative values, `True`/`False`, and non-integers are rejected.

**This is not a constant-memory guarantee.** Peak memory depends on:

- **Row-group size and compression of the file.** PyArrow decodes data per column chunk, so a file written as a single huge row group needs more memory to read than the same data split into many row groups.
- **PyArrow itself.** Importing `pyarrow.parquet` raised peak resident memory by about 14.5 MB in a fresh Python 3.12 process on the benchmark machine below (3 runs: 14.43–14.47 MB). Arrow read buffers add more on top of that.
- **Dataset size.** SQLite page cache and memory-mapped I/O grow with the workload, as for CSV/TSV (see [benchmark-report.md](benchmark-report.md)).

Aggregation still happens in the same disk-backed SQLite database used for CSV/TSV, so temporary disk use is the same for every format.

## Compression

Any codec your PyArrow build supports works, because decompression is handled entirely by PyArrow. The tests cover `none`, `snappy`, `gzip`, `brotli`, `zstd`, and `lz4`, and skip any codec that is unavailable in the installed build.

If a file was written with page checksums (for example `pyarrow.parquet.write_table(..., write_page_checksum=True)`), CandGuard verifies them and rejects corrupted data pages. Most writers do not store checksums by default. Without them, a corrupted data page whose footer is still valid may not be detected.

## Common errors

Every error below gives exit code `2`.

| Message (abridged) | Cause / fix |
|:--|:--|
| `Reading Parquet input requires the optional PyArrow dependency ...` | Run `pip install "candguard[parquet]"`. |
| `A delimiter override (',') was supplied for Parquet input ...` | Remove `--delimiter`/`--truth-delimiter` for Parquet inputs. |
| `Missing column(s) in X: col. Available columns: ...` | Check `--left-id`/`--right-id` (or `--truth-left-id`/`--truth-right-id`). |
| `Left and right identifier columns cannot be identical ...` | Use two different columns. |
| `Duplicate column 'col' in X schema ...` | Rename the duplicated column. |
| `Unsupported Parquet type 'T' for identifier column ...` | Cast the column to string or integer. |
| `Null identifier in column 'col' at X row N ...` | Fill or remove null IDs. |
| `Empty identifier in column 'col' at X row N ...` | Fill or remove empty IDs. |
| `Could not read Parquet file X: ...` | The file is not Parquet, is truncated or corrupted, or cannot be read. The PyArrow error is included. |
| `Input file does not exist: X` | Check the path. |
| `Input path is not a file: X. Partitioned Parquet dataset directories are not supported ...` | Pass a single Parquet file, not a dataset directory. |

## Performance measurements

These are measured results from [`benchmarks/parquet_vs_delimited.py`](../benchmarks/parquet_vs_delimited.py). They describe one machine and one synthetic workload, so treat them as an example rather than a performance guarantee.

**Setup**
- Windows 11 (10.0.26200), Intel64 Family 6 Model 170, 18 logical CPUs, 15.5 GiB RAM (about 1.8 GiB free at start). Python 3.12.10, SQLite 3.49.1, PyArrow 26.0.1.
- 1,000,000 candidate rows plus 50,000 truth rows from the existing deterministic generator (`seed=42`, 5% duplicate rate). The same logical data was written as TSV, CSV, and Parquet (snappy compression, 100,000-row row groups, string columns).
- Each audit ran in its own child process. Results are medians of 3 runs per format, with the format order rotated between runs. Peak RSS is Windows `PeakWorkingSetSize`. Peak temp is the largest total size of the child's private temporary directory, sampled every 5 ms.
- Command: `python -m benchmarks.parquet_vs_delimited --rows 1000000 --repeats 3`

| Linkage | Format | Input MiB (cand + truth) | Median audit time, s (min–max) | Rows/s (median) | Peak RSS MB (median / max) | Peak temp MiB | Leaked temp files |
|:--|:--|--:|--:|--:|--:|--:|--:|
| cross | TSV | 24.80 + 1.24 | 3.903 (3.826–3.981) | 256,225 | 181.46 / 181.50 | 61.46 | 0 |
| cross | CSV | 24.80 + 1.24 | 3.735 (3.685–3.950) | 267,756 | 181.17 / 181.38 | 61.46 | 0 |
| cross | Parquet | 3.05 + 0.04 | 2.983 (2.947–3.163) | 335,254 | 202.41 / 205.45 | 61.46 | 0 |
| dedupe | TSV | 24.80 + 1.24 | 4.295 (4.228–4.418) | 232,830 | 179.54 / 179.74 | 59.12 | 0 |
| dedupe | CSV | 24.80 + 1.24 | 4.248 (4.245–4.797) | 235,431 | 179.29 / 180.20 | 59.12 | 0 |
| dedupe | Parquet | 3.10 + 0.11 | 3.464 (3.424–3.679) | 288,684 | 202.58 / 203.07 | 59.12 | 0 |

**Logical equivalence:** all 9 runs for each linkage type produced the same logical report (the JSON report with file paths removed), with SHA-256 digests `509cb802889433c9…` for cross and `40c0df9604c4f325…` for dedupe. Cross: 902,947 unique pairs, 97,053 duplicate rows, recall 0.86468. Dedupe: 857,493 unique pairs, 142,507 duplicate rows, recall 0.87704.

**What the numbers show:**
- On this machine and workload, Parquet input cut median audit time by about 24% (cross) and 19% (dedupe) compared with TSV.
- Parquet input files were about 8× smaller than TSV.
- Parquet runs used about 21–23 MB more peak resident memory. Of that, about 14.5 MB is the measured cost of importing PyArrow. The rest is presumably Arrow read and decode buffers, but that part was not measured separately.
- SQLite temporary storage was the same for every format, because aggregation is format-independent.

Results on other hardware, data shapes, row-group sizes, or codecs may differ. Workloads larger than 1M rows have not been benchmarked with Parquet input.

## Known limitations

- Only single `.parquet` files are supported. Partitioned dataset directories, `.parq`/`.pq` extensions, and remote filesystems (S3, GCS, HTTP) are not.
- The format comes from the file extension; file contents are not sniffed.
- Identifier columns must be top-level fields. Nested fields cannot be selected.
- Corrupted data pages are detected only when the file contains page checksums.
- The PyArrow version is not checked at runtime. An older PyArrow installed outside the extra may work but is not tested.

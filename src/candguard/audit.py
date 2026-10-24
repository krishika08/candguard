"""Streaming candidate-pair audits backed by a disk-based SQLite work database.

CandGuard deliberately keeps the first release dependency-free. Input files are
streamed into SQLite rather than read into pandas lists, so pair data lives on
disk. DuckDB/Parquet support is a possible later performance enhancement.
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterator

from . import __version__


class AuditError(ValueError):
    """Raised when an input file or audit configuration is invalid."""


def _delimiter(path: Path, override: str | None) -> str:
    if override is not None:
        try:
            delim = codecs.decode(override, "unicode_escape")
        except (ValueError, UnicodeDecodeError) as exc:
            raise AuditError(f"Invalid delimiter escape sequence {override!r}: {exc}") from exc
        if len(delim) != 1:
            raise AuditError(f"Delimiter must be a single character, got {override!r}")
        return delim
    if path.suffix.lower() in {".tsv", ".tab"}:
        return "\t"
    return ","


def _rows(path: Path, left_col: str, right_col: str, delimiter: str | None) -> Iterator[tuple[str, str]]:
    if not path.exists():
        raise AuditError(f"Input file does not exist: {path}")
    if not path.is_file():
        raise AuditError(f"Input path is not a file: {path}")
    sep = _delimiter(path, delimiter)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, delimiter=sep)
            try:
                header = next(reader)
            except StopIteration:
                raise AuditError(f"Input file is empty or has no header: {path}")

            if not header:
                raise AuditError(f"Input file is empty or has no header: {path}")

            valid_fieldnames = [name for name in header if name is not None]
            missing = [name for name in (left_col, right_col) if name not in valid_fieldnames]
            if missing:
                raise AuditError(
                    f"Missing column(s) in {path}: {', '.join(missing)}. "
                    f"Available columns: {', '.join(valid_fieldnames)}"
                )

            if left_col == right_col:
                raise AuditError(
                    f"Left and right identifier columns cannot be identical ('{left_col}') in {path}."
                )
            if valid_fieldnames.count(left_col) > 1:
                raise AuditError(
                    f"Duplicate column '{left_col}' in {path} header makes identifier selection ambiguous."
                )
            if valid_fieldnames.count(right_col) > 1:
                raise AuditError(
                    f"Duplicate column '{right_col}' in {path} header makes identifier selection ambiguous."
                )

            left_idx = header.index(left_col)
            right_idx = header.index(right_col)
            expected_len = len(header)

            for line_no, row in enumerate(reader, start=2):
                if not row or all(not field.strip() for field in row):
                    raise AuditError(
                        f"Blank line encountered at {path}:{line_no}; remove empty lines from delimited input."
                    )
                if len(row) > expected_len:
                    raise AuditError(f"Unexpected extra field at {path}:{line_no}; check the delimiter.")
                if len(row) <= left_idx or len(row) <= right_idx:
                    raise AuditError(
                        f"Truncated row with missing column(s) at {path}:{line_no}; "
                        f"expected at least {max(left_idx, right_idx) + 1} fields, got {len(row)}."
                    )
                left = row[left_idx]
                right = row[right_idx]
                if not left.strip() or not right.strip():
                    raise AuditError(
                        f"Missing/empty pair identifier at {path}:{line_no}; "
                        f"columns '{left_col}' and '{right_col}' must both be populated."
                    )
                yield left.strip(), right.strip()
    except UnicodeDecodeError as exc:
        raise AuditError(f"Could not read {path} as UTF-8 text: {exc}") from exc
    except csv.Error as exc:
        raise AuditError(f"Could not parse delimited file {path}: {exc}") from exc


def _load_pairs(
    conn: sqlite3.Connection,
    path: Path,
    table: str,
    left_col: str,
    right_col: str,
    delimiter: str | None,
    linkage_type: str = "cross",
    batch_size: int = 50_000,
) -> int:
    conn.execute(f"CREATE TABLE {table} (left_id TEXT NOT NULL, right_id TEXT NOT NULL)")
    insert_sql = f"INSERT INTO {table} (left_id, right_id) VALUES (?, ?)"
    batch: list[tuple[str, str]] = []
    count = 0
    for left, right in _rows(path, left_col, right_col, delimiter):
        if linkage_type == "dedupe":
            if left == right:
                raise AuditError(
                    f"Self-pair ({left!r}, {right!r}) found in {path}; "
                    f"deduplication pairs must connect distinct records."
                )
            if left > right:
                left, right = right, left
        batch.append((left, right))
        count += 1
        if len(batch) >= batch_size:
            conn.executemany(insert_sql, batch)
            batch.clear()
    if batch:
        conn.executemany(insert_sql, batch)
    return count


def _percentile(conn: sqlite3.Connection, total_sources: int, percentile: float) -> int:
    """Return a nearest-rank percentile of per-left candidate counts."""
    if total_sources <= 0:
        return 0
    rank = max(0, math.ceil(percentile * total_sources) - 1)
    row = conn.execute(
        "SELECT candidate_count FROM source_candidate_counts "
        "ORDER BY candidate_count, left_id LIMIT 1 OFFSET ?",
        (rank,),
    ).fetchone()
    return int(row[0]) if row else 0


def audit_candidates(
    *,
    candidates_path: str | Path,
    left_id: str,
    right_id: str,
    truth_path: str | Path | None = None,
    truth_left_id: str | None = None,
    truth_right_id: str | None = None,
    delimiter: str | None = None,
    truth_delimiter: str | None = None,
    linkage_type: str = "cross",
    left_records: int | None = None,
    right_records: int | None = None,
    min_recall: float | None = None,
    max_pairs: int | None = None,
    max_duplicate_rows: int | None = None,
    missed_pairs_path: str | Path | None = None,
) -> dict[str, Any]:
    """Audit a candidate-pair file and optionally compare it with ground truth.

    Files must be CSV or TSV with header rows. IDs are compared as trimmed text.
    In cross mode, candidate pairs are directional: (left, right) is distinct from (right, left).
    In dedupe mode, pairs are logically unordered and stored canonically (min, max).
    """
    candidates_file = Path(candidates_path)
    truth_file = Path(truth_path) if truth_path is not None else None
    if linkage_type not in {"cross", "dedupe"}:
        raise AuditError("linkage_type must be 'cross' or 'dedupe'.")
    if left_records is not None and left_records < 0:
        raise AuditError("left_records cannot be negative.")
    if right_records is not None and right_records < 0:
        raise AuditError("right_records cannot be negative.")
    if linkage_type == "cross" and ((left_records is None) != (right_records is None)):
        raise AuditError("For cross-dataset reduction ratio, provide both --left-records and --right-records.")
    if linkage_type == "dedupe" and right_records is not None:
        raise AuditError("For dedupe mode, provide only --left-records.")
    if min_recall is not None and not 0 <= min_recall <= 1:
        raise AuditError("min_recall must be between 0 and 1.")
    if min_recall is not None and truth_file is None:
        raise AuditError("--min-recall requires --truth.")
    if missed_pairs_path is not None and truth_file is None:
        raise AuditError("--missed-pairs-out requires --truth.")
    if max_pairs is not None and max_pairs < 0:
        raise AuditError("max_pairs cannot be negative.")
    if max_duplicate_rows is not None and max_duplicate_rows < 0:
        raise AuditError("max_duplicate_rows cannot be negative.")

    tmp = tempfile.NamedTemporaryFile(prefix="candguard-", suffix=".sqlite3", delete=False)
    db_path = tmp.name
    tmp.close()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = FILE")
        conn.execute("PRAGMA cache_size = -65536")  # roughly 64 MiB cache
        conn.execute("PRAGMA mmap_size = 268435456")  # 256 MiB memory-mapped I/O

        candidate_rows = _load_pairs(
            conn, candidates_file, "candidate_raw", left_id, right_id, delimiter, linkage_type=linkage_type
        )
        conn.commit()
        conn.execute(
            "CREATE TABLE candidate_pairs AS "
            "SELECT left_id, right_id FROM candidate_raw GROUP BY left_id, right_id"
        )
        conn.execute("DROP TABLE candidate_raw")
        conn.commit()
        conn.execute("CREATE UNIQUE INDEX idx_candidates_pair ON candidate_pairs(left_id, right_id)")
        unique_candidate_pairs = int(conn.execute("SELECT COUNT(*) FROM candidate_pairs").fetchone()[0])
        duplicate_rows = candidate_rows - unique_candidate_pairs

        if linkage_type == "dedupe":
            conn.execute(
                "CREATE TABLE source_candidate_counts AS "
                "SELECT record_id AS left_id, COUNT(*) AS candidate_count FROM ("
                "SELECT left_id AS record_id FROM candidate_pairs "
                "UNION ALL "
                "SELECT right_id AS record_id FROM candidate_pairs"
                ") GROUP BY record_id"
            )
            conn.execute("CREATE INDEX idx_source_counts ON source_candidate_counts(candidate_count, left_id)")
            count_row = conn.execute(
                "SELECT COUNT(*), COALESCE(MIN(candidate_count), 0), "
                "COALESCE(AVG(candidate_count), 0), COALESCE(MAX(candidate_count), 0) "
                "FROM source_candidate_counts"
            ).fetchone()
            unique_records = int(count_row[0])
            unique_left = unique_records
            unique_right = unique_records
            coverage: dict[str, int] = {
                "records_represented": unique_records,
                "left_records_represented": unique_records,
                "right_records_represented": unique_records,
            }
        else:
            unique_left = int(conn.execute("SELECT COUNT(DISTINCT left_id) FROM candidate_pairs").fetchone()[0])
            unique_right = int(conn.execute("SELECT COUNT(DISTINCT right_id) FROM candidate_pairs").fetchone()[0])
            coverage = {
                "left_records_represented": unique_left,
                "right_records_represented": unique_right,
            }

            conn.execute(
                "CREATE TABLE source_candidate_counts AS "
                "SELECT left_id, COUNT(*) AS candidate_count FROM candidate_pairs GROUP BY left_id"
            )
            conn.execute("CREATE INDEX idx_source_counts ON source_candidate_counts(candidate_count, left_id)")
            count_row = conn.execute(
                "SELECT COUNT(*), COALESCE(MIN(candidate_count), 0), "
                "COALESCE(AVG(candidate_count), 0), COALESCE(MAX(candidate_count), 0) "
                "FROM source_candidate_counts"
            ).fetchone()
        sources_with_candidates = int(count_row[0])
        distribution = {
            "sources_with_candidates": sources_with_candidates,
            "min": int(count_row[1]),
            "mean": round(float(count_row[2]), 4),
            "p50": _percentile(conn, sources_with_candidates, 0.50),
            "p95": _percentile(conn, sources_with_candidates, 0.95),
            "p99": _percentile(conn, sources_with_candidates, 0.99),
            "max": int(count_row[3]),
        }

        efficiency: dict[str, Any] = {"linkage_type": linkage_type, "possible_pairs": None, "reduction_ratio": None}
        possible_pairs: int | None = None
        if linkage_type == "cross" and left_records is not None and right_records is not None:
            if unique_left > left_records:
                raise AuditError(
                    f"Supplied --left-records={left_records} conflicts with candidate data: "
                    f"candidate file contains {unique_left} distinct source ID(s)."
                )
            if unique_right > right_records:
                raise AuditError(
                    f"Supplied --right-records={right_records} conflicts with candidate data: "
                    f"candidate file contains {unique_right} distinct target ID(s)."
                )
            possible_pairs = left_records * right_records
            if unique_candidate_pairs > possible_pairs:
                raise AuditError(
                    f"Supplied dataset sizes ({left_records} x {right_records} = {possible_pairs} possible pairs) "
                    f"conflict with candidate data: candidate file contains {unique_candidate_pairs} unique pair(s)."
                )
        elif linkage_type == "dedupe" and left_records is not None:
            if unique_records > left_records:
                raise AuditError(
                    f"Supplied --left-records={left_records} conflicts with candidate data: "
                    f"candidate file contains {unique_records} distinct record(s)."
                )
            possible_pairs = left_records * (left_records - 1) // 2
            if unique_candidate_pairs > possible_pairs:
                raise AuditError(
                    f"Supplied dataset size ({left_records} records -> {possible_pairs} possible pairs) "
                    f"conflicts with candidate data: candidate file contains {unique_candidate_pairs} unique pair(s)."
                )

        if possible_pairs is not None:
            efficiency["possible_pairs"] = possible_pairs
            efficiency["reduction_ratio"] = round(1 - unique_candidate_pairs / possible_pairs, 8) if possible_pairs else None

        ground_truth: dict[str, Any] | None = None
        if truth_file is not None:
            truth_left = truth_left_id or left_id
            truth_right = truth_right_id or right_id
            truth_rows = _load_pairs(
                conn, truth_file, "truth_raw", truth_left, truth_right, truth_delimiter, linkage_type=linkage_type
            )
            conn.commit()
            conn.execute(
                "CREATE TABLE truth_pairs AS SELECT left_id, right_id FROM truth_raw GROUP BY left_id, right_id"
            )
            conn.execute("DROP TABLE truth_raw")
            conn.commit()
            conn.execute("CREATE UNIQUE INDEX idx_truth_pair ON truth_pairs(left_id, right_id)")
            truth_unique = int(conn.execute("SELECT COUNT(*) FROM truth_pairs").fetchone()[0])
            if truth_unique == 0 and min_recall is not None:
                raise AuditError(
                    f"Ground-truth file contains no pairs ({truth_file}); cannot evaluate --min-recall."
                )

            recovered = int(conn.execute(
                "SELECT COUNT(*) FROM truth_pairs t JOIN candidate_pairs c "
                "ON c.left_id = t.left_id AND c.right_id = t.right_id"
            ).fetchone()[0])
            missed = truth_unique - recovered
            recall = recovered / truth_unique if truth_unique else None

            hasher = hashlib.sha256()
            hasher.update(f"candguard:truth:v2:{linkage_type}\n".encode("utf-8"))
            for left, right in conn.execute(
                "SELECT left_id, right_id FROM truth_pairs ORDER BY left_id, right_id"
            ):
                l_bytes = left.encode("utf-8")
                r_bytes = right.encode("utf-8")
                hasher.update(
                    f"{len(l_bytes)}:".encode("ascii")
                    + l_bytes
                    + b"\t"
                    + f"{len(r_bytes)}:".encode("ascii")
                    + r_bytes
                    + b"\n"
                )
            truth_fingerprint = f"sha256:{hasher.hexdigest()}"

            ground_truth = {
                "rows": truth_rows,
                "unique_pairs": truth_unique,
                "recovered_pairs": recovered,
                "missed_pairs": missed,
                "candidate_recall": round(recall, 8) if recall is not None else None,
                "fingerprint": truth_fingerprint,
                "fingerprint_version": "v2",
            }
            if missed_pairs_path is not None:
                try:
                    out_path = Path(missed_pairs_path)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with out_path.open("w", encoding="utf-8", newline="") as out_stream:
                        writer = csv.writer(out_stream, delimiter="\t", lineterminator="\n")
                        writer.writerow([truth_left, truth_right])
                        cursor = conn.execute(
                            "SELECT t.left_id, t.right_id FROM truth_pairs t "
                            "LEFT JOIN candidate_pairs c ON c.left_id=t.left_id AND c.right_id=t.right_id "
                            "WHERE c.left_id IS NULL ORDER BY t.left_id, t.right_id"
                        )
                        writer.writerows(cursor)
                    ground_truth["missed_pairs_file"] = str(out_path)
                except OSError as exc:
                    raise AuditError(f"Could not write missed pairs to {missed_pairs_path}: {exc}") from exc

        failures: list[str] = []
        if min_recall is not None:
            measured = ground_truth["candidate_recall"] if ground_truth else None
            if measured is None or measured < min_recall:
                failures.append(f"candidate recall {measured} is below required {min_recall:.4f}")
        if max_pairs is not None and unique_candidate_pairs > max_pairs:
            failures.append(f"unique candidate pairs {unique_candidate_pairs} exceed limit {max_pairs}")
        if max_duplicate_rows is not None and duplicate_rows > max_duplicate_rows:
            failures.append(f"duplicate candidate rows {duplicate_rows} exceed limit {max_duplicate_rows}")

        return {
            "tool": "candguard",
            "version": __version__,
            "schema_version": "1.0",
            "inputs": {
                "candidates": str(candidates_file),
                "truth": str(truth_file) if truth_file else None,
                "candidate_rows": candidate_rows,
                "unique_candidate_pairs": unique_candidate_pairs,
                "duplicate_candidate_rows": duplicate_rows,
            },
            "coverage": coverage,
            "candidate_distribution_per_left_record": distribution,
            "efficiency": efficiency,
            "ground_truth": ground_truth,
            "quality_gate": {"passed": not failures, "failures": failures},
        }
    except sqlite3.Error as exc:
        raise AuditError(f"SQLite failed while auditing the input files: {exc}") from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        try:
            if os.path.exists(db_path):
                os.unlink(db_path)
        except OSError:
            pass


def render_text(report: dict[str, Any]) -> str:
    inputs = report["inputs"]
    coverage = report["coverage"]
    dist = report["candidate_distribution_per_left_record"]
    eff = report["efficiency"]
    truth = report["ground_truth"]
    gate = report["quality_gate"]
    is_dedupe = eff.get("linkage_type") == "dedupe"

    lines = [
        "CandGuard — Candidate Generation Audit",
        "=" * 42,
        f"Candidate rows:                 {inputs['candidate_rows']:,}",
        f"Unique candidate pairs:         {inputs['unique_candidate_pairs']:,}",
        f"Duplicate candidate rows:       {inputs['duplicate_candidate_rows']:,}",
    ]
    if is_dedupe:
        records_rep = coverage.get("records_represented", coverage["left_records_represented"])
        lines.append(f"Records represented:            {records_rep:,}")
    else:
        lines.extend([
            f"Source records represented:     {coverage['left_records_represented']:,}",
            f"Target records represented:     {coverage['right_records_represented']:,}",
        ])
    lines.extend([
        "",
        "Candidates per represented record" if is_dedupe else "Candidates per represented source record",
        "---------------------------------" if is_dedupe else "-----------------------------------------",
        f"Min / median / mean:             {dist['min']} / {dist['p50']} / {dist['mean']}",
        f"P95 / P99 / max:                 {dist['p95']} / {dist['p99']} / {dist['max']}",
    ])
    if eff["possible_pairs"] is not None:
        lines += [
            "",
            "Search-space efficiency",
            "-----------------------",
            f"Possible pairs:                  {eff['possible_pairs']:,}",
            f"Reduction ratio:                 {eff['reduction_ratio']:.4%}" if eff["reduction_ratio"] is not None else "Reduction ratio:                 n/a",
        ]
    if truth is not None:
        recall_value = truth["candidate_recall"]
        recall_text = f"{recall_value:.4%}" if recall_value is not None else "n/a (no ground-truth pairs)"
        lines += [
            "",
            "Ground-truth audit",
            "------------------",
            f"Unique known matching pairs:     {truth['unique_pairs']:,}",
            f"Recovered by candidate set:      {truth['recovered_pairs']:,}",
            f"Missing before model scoring:    {truth['missed_pairs']:,}",
            f"Candidate recall:                {recall_text}",
        ]
        if truth.get("missed_pairs_file"):
            lines.append(f"Missing pairs written to:        {truth['missed_pairs_file']}")
    lines += ["", f"Quality gate: {'PASS' if gate['passed'] else 'FAIL'}"]
    lines.extend(f"  - {failure}" for failure in gate["failures"])
    return "\n".join(lines)


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)

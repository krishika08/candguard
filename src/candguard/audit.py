"""Candidate-pair audits backed by disk-based SQLite storage.

Separates input reading, metric calculation, storage, and reporting
while preserving full backward compatibility with CandGuard v0.1.0 contracts.
"""
from __future__ import annotations

import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from ._output import _OutputPublication, _regular_destination
from .candidate_set import ensure_distinct_output, same_path
from .metrics import compute_possible_pairs, compute_record_coverage, compute_reduction_ratio
from .models import AuditError, argument_path, validate_audit_arguments
from .readers import CandidateReader, DelimitedReader, get_reader, resolve_delimiter
from .reporting import render_text, report_json
from .storage import SQLiteStorageEngine, compute_nearest_rank_percentile

__all__ = [
    "AuditError",
    "audit_candidates",
    "render_text",
    "report_json",
]


# Backward-compatibility aliases for private functions that tests might reference
def _delimiter(path: Path, override: str | None) -> str:
    return resolve_delimiter(path, override)


def _rows(path: Path, left_col: str, right_col: str, delimiter: str | None) -> Iterator[tuple[str, str]]:
    reader = DelimitedReader(path, left_col, right_col, delimiter)
    return reader.iter_pairs()


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
    reader = DelimitedReader(path, left_col, right_col, delimiter)
    conn.execute(f"CREATE TABLE {table} (left_id TEXT NOT NULL, right_id TEXT NOT NULL)")
    insert_sql = f"INSERT INTO {table} (left_id, right_id) VALUES (?, ?)"
    count = 0
    is_dedupe = linkage_type == "dedupe"

    for batch in reader.iter_batches(batch_size=batch_size):
        processed: list[tuple[str, str]] = []
        for left, right in batch:
            if is_dedupe:
                if left == right:
                    raise AuditError(
                        f"Self-pair ({left!r}, {right!r}) found in {path}; "
                        "deduplication pairs must connect distinct records."
                    )
                if left > right:
                    left, right = right, left
            processed.append((left, right))
        count += len(processed)
        conn.executemany(insert_sql, processed)
    return count


def _percentile(conn: sqlite3.Connection, total_sources: int, percentile: float) -> int:
    return compute_nearest_rank_percentile(conn, total_sources, percentile)


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
    candidate_set_path: str | Path | None = None,
) -> dict[str, Any]:
    """Audit a candidate-pair file and optionally compare it with ground truth.

    Files must be CSV or TSV with header rows, or Parquet files (``.parquet``; requires the
    optional ``candguard[parquet]`` extra). IDs are compared as trimmed text.
    In cross mode, candidate pairs are directional: (left, right) is distinct from (right, left).
    In dedupe mode, pairs are logically unordered and stored canonically (min, max).

    Requested sidecars are staged before publication. If a replacement fails, the audit
    attempts to restore any sidecar it already replaced. A completed audit publishes
    its sidecars even when the quality gate fails.
    """
    with _OutputPublication() as publication:
        report = _audit_candidates(
            candidates_path=candidates_path, left_id=left_id, right_id=right_id,
            truth_path=truth_path, truth_left_id=truth_left_id, truth_right_id=truth_right_id,
            delimiter=delimiter, truth_delimiter=truth_delimiter, linkage_type=linkage_type,
            left_records=left_records, right_records=right_records, min_recall=min_recall,
            max_pairs=max_pairs, max_duplicate_rows=max_duplicate_rows,
            missed_pairs_path=missed_pairs_path, candidate_set_path=candidate_set_path,
            publication=publication,
        )
        publication.publish()
    return report


def _audit_candidates(
    *,
    publication: _OutputPublication,
    report_path: Path | None = None,
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
    candidate_set_path: str | Path | None = None,
) -> dict[str, Any]:
    validate_audit_arguments(
        linkage_type=linkage_type,
        left_records=left_records,
        right_records=right_records,
        min_recall=min_recall,
        max_pairs=max_pairs,
        max_duplicate_rows=max_duplicate_rows,
        has_truth=truth_path is not None,
        has_missed_pairs_path=missed_pairs_path is not None,
    )
    if not isinstance(left_id, str) or not isinstance(right_id, str):
        raise AuditError("left_id and right_id must be strings.")
    for name, value in (
        ("truth_left_id", truth_left_id), ("truth_right_id", truth_right_id),
        ("delimiter", delimiter), ("truth_delimiter", truth_delimiter),
    ):
        if value is not None and not isinstance(value, str):
            raise AuditError(f"{name} must be a string.")

    candidates_file = argument_path("candidates_path", candidates_path)
    truth_file = argument_path("truth_path", truth_path) if truth_path is not None else None
    missed_pairs_file = argument_path("missed_pairs_path", missed_pairs_path) if missed_pairs_path is not None else None
    candidate_set_file = argument_path("candidate_set_path", candidate_set_path) if candidate_set_path is not None else None

    _preflight_outputs(candidates_file, truth_file, missed_pairs_file, candidate_set_file, report_path)

    # Build readers before ingesting anything so configuration errors (for example a delimiter
    # override on Parquet input, or a missing optional dependency) are reported up front.
    cand_reader = get_reader(candidates_file, left_id, right_id, delimiter=delimiter)
    truth_left = truth_left_id or left_id
    truth_right = truth_right_id or right_id
    truth_reader: CandidateReader | None = None
    if truth_file is not None:
        truth_reader = get_reader(truth_file, truth_left, truth_right, delimiter=truth_delimiter)

    with SQLiteStorageEngine() as engine:
        candidate_rows, unique_candidate_pairs = engine.ingest_candidates(
            cand_reader, linkage_type=linkage_type
        )
        duplicate_rows = candidate_rows - unique_candidate_pairs

        coverage, distribution = engine.compute_coverage_and_distribution(linkage_type)

        efficiency: dict[str, Any] = {
            "linkage_type": linkage_type,
            "possible_pairs": None,
            "reduction_ratio": None,
        }
        possible_pairs: int | None = None

        if linkage_type == "cross" and left_records is not None and right_records is not None:
            unique_left = coverage["left_records_represented"]
            unique_right = coverage["right_records_represented"]
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
            unique_records = coverage.get("records_represented", coverage["left_records_represented"])
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
            efficiency["reduction_ratio"] = compute_reduction_ratio(
                unique_pairs=unique_candidate_pairs,
                possible_pairs=possible_pairs,
            )

        # Additive diagnostics (schema 1.0 extension). Totals were validated above, so these
        # never see represented counts larger than the supplied record totals.
        coverage.update(compute_record_coverage(linkage_type, coverage, left_records, right_records))
        degree_analysis = engine.compute_degree_analysis(
            linkage_type,
            left_records=left_records,
            right_records=right_records if linkage_type == "cross" else None,
        )

        failures: list[str] = []
        ground_truth: dict[str, Any] | None = None
        if truth_file is not None and truth_reader is not None:
            truth_rows, truth_unique = engine.ingest_truth(truth_reader, linkage_type=linkage_type)

            if truth_unique == 0 and min_recall is not None:
                raise AuditError(
                    f"Ground-truth file contains no pairs ({truth_file}); cannot evaluate --min-recall."
                )

            recovered, missed, recall, truth_fingerprint = engine.evaluate_ground_truth(
                truth_unique=truth_unique,
                linkage_type=linkage_type,
            )

            ground_truth = {
                "rows": truth_rows,
                "unique_pairs": truth_unique,
                "recovered_pairs": recovered,
                "missed_pairs": missed,
                "candidate_recall": recall,
                "fingerprint": truth_fingerprint,
                "fingerprint_version": "v2",
            }

            # Interpret a float threshold by its decimal spelling, as with a CLI threshold.
            if min_recall is not None and Fraction(recovered, truth_unique) < Fraction(str(float(min_recall))):
                failures.append(f"candidate recall ({recovered}/{truth_unique}) is below required {min_recall}")

            if missed_pairs_file is not None:
                with publication.stage(missed_pairs_file, "missed pairs", newline="") as stream:
                    engine.export_missed_pairs(stream, truth_left, truth_right)
                ground_truth["missed_pairs_file"] = str(missed_pairs_file)

        if max_pairs is not None and unique_candidate_pairs > max_pairs:
            failures.append(f"unique candidate pairs {unique_candidate_pairs} exceed limit {max_pairs}")
        if max_duplicate_rows is not None and duplicate_rows > max_duplicate_rows:
            failures.append(f"duplicate candidate rows {duplicate_rows} exceed limit {max_duplicate_rows}")

        report: dict[str, Any] = {
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
            "degree_analysis": degree_analysis,
            "efficiency": efficiency,
            "ground_truth": ground_truth,
            "quality_gate": {"passed": not failures, "failures": failures},
        }

        if candidate_set_file is not None:
            report["candidate_set"] = engine.export_candidate_set(
                candidate_set_file,
                linkage_type=linkage_type,
                expected_pairs=unique_candidate_pairs,
                _publication=publication,
            )
        return report


def _preflight_outputs(
    candidates: Path, truth: Path | None, missed: Path | None, candidate_set: Path | None, report: Path | None
) -> None:
    """Reject colliding or unsupported destinations before building any input reader."""
    if report is not None:
        if candidate_set is not None and same_path(candidate_set, report):
            raise AuditError(
                f"--candidate-set-out ({candidate_set}) and --output ({report}) must be different files."
            )
        for flag, other in (
            ("--candidates", candidates), ("--truth", truth), ("--missed-pairs-out", missed)
        ):
            if other is not None and same_path(report, other):
                raise AuditError(
                    f"--output ({report}) resolves to the same file as {flag} ({other}); "
                    f"--output must point to a different file so {flag} is not overwritten."
                )
        _regular_destination(report, "output")

    inputs = {"candidate input file": candidates, "ground-truth input file": truth}
    if missed is not None:
        ensure_distinct_output(missed, inputs, description="Missed-pairs output path", flag="--missed-pairs-out")
    if candidate_set is not None:
        ensure_distinct_output(candidate_set, {**inputs, "missed-pairs output file": missed})

    paths = [path for path in (missed, candidate_set, report) if path is not None]
    for path in paths:
        for other in paths:
            if path != other and any(same_path(path, parent) for parent in other.parents):
                raise AuditError(f"Audit output {path} cannot be a parent directory of another output ({other}).")

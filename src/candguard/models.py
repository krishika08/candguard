"""Core data models, types, and validation for CandGuard."""
from __future__ import annotations

import math
from typing import Any, Literal, TypedDict


class AuditError(ValueError):
    """Raised when an input file, configuration, or report is invalid."""


LinkageType = Literal["cross", "dedupe"]
ReportFormat = Literal["text", "json"]


class InputsReport(TypedDict):
    candidates: str
    truth: str | None
    candidate_rows: int
    unique_candidate_pairs: int
    duplicate_candidate_rows: int


class CoverageReport(TypedDict, total=False):
    left_records_represented: int
    right_records_represented: int
    records_represented: int


class DistributionReport(TypedDict):
    sources_with_candidates: int
    min: int
    mean: float
    p50: int
    p95: int
    p99: int
    max: int


class EfficiencyReport(TypedDict):
    linkage_type: str
    possible_pairs: int | None
    reduction_ratio: float | None


class GroundTruthReport(TypedDict, total=False):
    rows: int
    unique_pairs: int
    recovered_pairs: int
    missed_pairs: int
    candidate_recall: float | None
    fingerprint: str
    fingerprint_version: str
    missed_pairs_file: str


class QualityGateReport(TypedDict):
    passed: bool
    failures: list[str]


class AuditReport(TypedDict):
    tool: str
    version: str
    schema_version: str
    inputs: InputsReport
    coverage: CoverageReport
    candidate_distribution_per_left_record: DistributionReport
    efficiency: EfficiencyReport
    ground_truth: GroundTruthReport | None
    quality_gate: QualityGateReport


def validate_audit_arguments(
    *,
    linkage_type: str,
    left_records: int | None,
    right_records: int | None,
    min_recall: float | None,
    max_pairs: int | None,
    max_duplicate_rows: int | None,
    has_truth: bool,
    has_missed_pairs_path: bool,
) -> None:
    """Validate argument values for candidate auditing."""
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
    if min_recall is not None and not has_truth:
        raise AuditError("--min-recall requires --truth.")
    if has_missed_pairs_path and not has_truth:
        raise AuditError("--missed-pairs-out requires --truth.")
    if max_pairs is not None and max_pairs < 0:
        raise AuditError("max_pairs cannot be negative.")
    if max_duplicate_rows is not None and max_duplicate_rows < 0:
        raise AuditError("max_duplicate_rows cannot be negative.")


def validate_compare_threshold(name: str, threshold: float | None) -> None:
    """Validate comparison threshold argument."""
    if threshold is not None:
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or math.isnan(threshold)
            or math.isinf(threshold)
            or threshold < 0
        ):
            raise AuditError(f"{name} must be a finite non-negative number, got {threshold!r}.")

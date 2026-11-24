"""Core data models, types, and validation for CandGuard."""
from __future__ import annotations

import math
from typing import Any, Literal, TypedDict, Union


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
    # Record-universe coverage (schema 1.0 additive extension). Present in every new report;
    # None when the corresponding --left-records/--right-records total was not supplied.
    # Cross-linkage fields:
    left_records_total: int | None
    right_records_total: int | None
    left_records_without_candidates: int | None
    right_records_without_candidates: int | None
    left_record_coverage: float | None
    right_record_coverage: float | None
    # Deduplication fields:
    records_total: int | None
    records_without_candidates: int | None
    record_coverage: float | None


StatisticsBasis = Literal["record_universe", "represented_records"]


class DegreeDistributionReport(TypedDict):
    """Degree distribution over unique candidate pairs (schema 1.0 additive extension).

    All statistics are None when the statistical population is empty; ``gini_coefficient``
    and ``top_1_percent_candidate_share`` are None when the total degree is zero.
    """

    statistics_basis: StatisticsBasis
    records_in_universe: int | None
    records_with_candidates: int
    records_without_candidates: int | None
    total_degree: int
    min: int | None
    p50: int | None
    p95: int | None
    p99: int | None
    p99_9: int | None
    mean: float | None
    max: int | None
    gini_coefficient: float | None
    top_1_percent_candidate_share: float | None


class CrossDegreeAnalysisReport(TypedDict):
    linkage_type: Literal["cross"]
    left: DegreeDistributionReport
    right: DegreeDistributionReport
    right_to_left_mean_degree_ratio: float | None


class DedupeDegreeAnalysisReport(TypedDict):
    linkage_type: Literal["dedupe"]
    records: DegreeDistributionReport


DegreeAnalysisReport = Union[CrossDegreeAnalysisReport, DedupeDegreeAnalysisReport]


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


class CandidateSetReport(TypedDict):
    """Metadata for an exported candidate-set sidecar (schema 1.0 additive extension).

    Present only when the audit was run with ``--candidate-set-out``. The artifact path is
    deliberately not recorded; ``compare`` receives artifact paths explicitly.
    """

    format: Literal["candguard-pairs-tsv-v1"]
    unique_pairs: int
    fingerprint: str
    fingerprint_version: Literal["v1"]
    linkage_type: LinkageType


class CandidateSetDriftReport(TypedDict):
    """Exact unique-pair set comparison added to ``compare`` metrics when artifacts are supplied."""

    baseline_unique_pairs: int
    current_unique_pairs: int
    retained_pairs: int
    added_pairs: int
    removed_pairs: int
    union_pairs: int
    jaccard_similarity: float
    pair_churn: float
    removed_fraction_of_baseline: float | None
    added_fraction_of_current: float | None


class _AuditReportOptional(TypedDict, total=False):
    # Schema 1.0 additive extension; present only when a candidate-set sidecar was exported.
    candidate_set: CandidateSetReport


class AuditReport(_AuditReportOptional):
    tool: str
    version: str
    schema_version: str
    inputs: InputsReport
    coverage: CoverageReport
    candidate_distribution_per_left_record: DistributionReport
    # Schema 1.0 additive extension; absent from reports produced by earlier versions.
    degree_analysis: DegreeAnalysisReport
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


def validate_unit_interval_threshold(name: str, threshold: float | None) -> None:
    """Validate an optional threshold that must be a finite number in [0, 1]."""
    if threshold is not None:
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or math.isnan(threshold)
            or math.isinf(threshold)
            or not 0 <= threshold <= 1
        ):
            raise AuditError(f"{name} must be a finite number between 0 and 1, got {threshold!r}.")

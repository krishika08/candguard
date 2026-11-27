"""Regression comparison of CandGuard candidate audit reports."""
from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import __version__
from .candidate_set import compare_candidate_set_artifacts, validate_candidate_set_metadata
from .metrics import METRIC_PRECISION, compute_candidate_set_drift, compute_relative_growth
from .models import (
    AuditError,
    argument_path,
    is_unit_interval_number,
    validate_compare_threshold,
    validate_unit_interval_threshold,
)
from .reporting import render_compare_text

__all__ = [
    "AuditError",
    "compare_reports",
    "render_compare_text",
]


def _load_report(path_str: str | Path, label: str) -> tuple[Path, dict[str, Any]]:
    path = argument_path(f"{label.lower()}_path", path_str)
    if not path.exists():
        raise AuditError(f"{label} file does not exist: {path}")
    if not path.is_file():
        raise AuditError(f"{label} path is not a file: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AuditError(f"Could not read {label} file as UTF-8 text ({path}): {exc}") from exc
    except OSError as exc:
        # The file exists but cannot be opened or read (for example permission denied).
        raise AuditError(f"Could not read {label} file ({path}): {exc}") from exc

    try:
        data = json.loads(content)
    except ValueError as exc:
        # JSONDecodeError is a ValueError; Python 3.11+ also raises a plain ValueError for an
        # integer literal longer than the interpreter's int-string conversion limit.
        raise AuditError(f"Could not parse {label} JSON report ({path}): {exc}") from exc

    if not isinstance(data, dict):
        raise AuditError(f"{label} report ({path}) root must be a JSON object.")

    schema_version = data.get("schema_version")
    if not schema_version or not str(schema_version).startswith("1."):
        raise AuditError(
            f"Unsupported schema version in {label} report ({path}): "
            f"expected '1.x', got {schema_version!r}."
        )

    inputs = data.get("inputs")
    if not isinstance(inputs, dict):
        raise AuditError(f"Missing or invalid 'inputs' section in {label} report ({path}).")

    for field in ("candidate_rows", "unique_candidate_pairs", "duplicate_candidate_rows"):
        val = inputs.get(field)
        if val is None or isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise AuditError(
                f"Missing or invalid 'inputs.{field}' in {label} report ({path}): "
                f"expected non-negative integer, got {val!r}."
            )

    rows = inputs["candidate_rows"]
    unique = inputs["unique_candidate_pairs"]
    duplicates = inputs["duplicate_candidate_rows"]
    if rows != unique + duplicates:  # Non-negative duplicates also imply unique <= rows.
        raise AuditError(
            f"Inconsistent candidate counts in {label} report ({path}): "
            "candidate_rows must equal unique_candidate_pairs plus duplicate_candidate_rows "
            f"(got {rows}, {unique}, and {duplicates}, respectively)."
        )

    efficiency = data.get("efficiency")
    if not isinstance(efficiency, dict) or efficiency.get("linkage_type") not in ("cross", "dedupe"):
        raise AuditError(
            f"Missing or invalid 'efficiency.linkage_type' in {label} report ({path}): "
            "expected 'cross' or 'dedupe'."
        )

    _validate_ground_truth(data, label, path)
    return path, data


def _validate_ground_truth(data: dict[str, Any], label: str, path: Path) -> None:
    """Validate the optional ``ground_truth`` section so malformed values cannot skew recall gates.

    - ``ground_truth`` is absent, null, or an object.
    - ``candidate_recall`` is absent, null, or a finite number in [0, 1] (booleans rejected).
    - ``fingerprint`` is absent, null (treated as missing), or a non-empty string.
    - ``fingerprint_version`` is absent (legacy reports default to 'v1') or a non-empty string.
    """
    truth = data.get("ground_truth")
    if truth is None:
        return
    if not isinstance(truth, dict):
        raise AuditError(
            f"Invalid 'ground_truth' in {label} report ({path}): expected an object or null, "
            f"got {type(truth).__name__} {truth!r}."
        )

    recall = truth.get("candidate_recall")
    if recall is not None and not is_unit_interval_number(recall):
        raise AuditError(
            f"Invalid 'ground_truth.candidate_recall' in {label} report ({path}): "
            f"expected a finite number between 0 and 1 or null, got {recall!r}."
        )

    _validate_ground_truth_counts(truth, label, path, data["inputs"]["unique_candidate_pairs"])

    fingerprint = truth.get("fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not fingerprint):
        raise AuditError(
            f"Invalid 'ground_truth.fingerprint' in {label} report ({path}): "
            f"expected a non-empty string, got {fingerprint!r}."
        )

    if "fingerprint_version" in truth:
        version = truth["fingerprint_version"]
        if not isinstance(version, str) or not version:
            raise AuditError(
                f"Invalid 'ground_truth.fingerprint_version' in {label} report ({path}): "
                f"expected a non-empty string (omit it for legacy reports), got {version!r}."
            )


def _validate_ground_truth_counts(
    truth: dict[str, Any], label: str, path: Path, unique_candidate_pairs: int,
) -> None:
    counts: dict[str, int] = {}
    for field in ("rows", "unique_pairs", "recovered_pairs", "missed_pairs"):
        if field not in truth:
            continue
        count = truth[field]
        if type(count) is not int or count < 0:
            raise AuditError(
                f"Invalid 'ground_truth.{field}' in {label} report ({path}): "
                f"expected non-negative integer, got {count!r}."
            )
        counts[field] = count

    if not counts:
        return

    unique = counts.get("unique_pairs")
    recovered = counts.get("recovered_pairs")
    context = f"'ground_truth' in {label} report ({path})"
    if unique is None or recovered is None:
        raise AuditError(
            f"Incomplete {context}: found {', '.join(counts)}. Both unique_pairs and "
            "recovered_pairs are required when any ground-truth count is present."
        )
    if recovered > unique:
        raise AuditError(f"Inconsistent {context}: recovered_pairs cannot exceed unique_pairs.")
    if recovered > unique_candidate_pairs:
        raise AuditError(
            f"Inconsistent {context}: recovered_pairs ({recovered}) cannot exceed "
            f"inputs.unique_candidate_pairs ({unique_candidate_pairs})."
        )
    if "missed_pairs" in counts and recovered + counts["missed_pairs"] != unique:
        raise AuditError(f"Inconsistent {context}: recovered_pairs plus missed_pairs must equal unique_pairs.")
    if "rows" in counts and counts["rows"] < unique:
        raise AuditError(f"Inconsistent {context}: rows cannot be fewer than unique_pairs.")

    recall = truth.get("candidate_recall")
    if recall is None:
        return
    if unique == 0:
        raise AuditError(f"Inconsistent {context}: candidate_recall must be null or omitted when unique_pairs is zero.")

    # Match audit's float-then-round representation. Older consumers may store the unrounded float.
    measured = float(_recall_from_report(truth))
    if recall not in (measured, round(measured, METRIC_PRECISION)):
        raise AuditError(
            f"ground_truth.candidate_recall in {label} report ({path}) conflicts with recovered_pairs and unique_pairs."
        )


def _recall_unavailable_reason(base: dict[str, Any], current: dict[str, Any]) -> str | None:
    for label, truth in (("baseline", base), ("current", current)):
        if truth.get("candidate_recall") is None:
            return f"{label} report has no candidate recall."
    for label, truth in (("baseline", base), ("current", current)):
        if not truth.get("fingerprint"):
            return f"{label} report lacks ground-truth fingerprint for comparability verification."

    base_version = base.get("fingerprint_version", "v1")
    current_version = current.get("fingerprint_version", "v1")
    if base_version != current_version:
        return (
            f"Incompatible ground-truth fingerprint versions: baseline uses '{base_version}', "
            f"current uses '{current_version}'. Re-run audit with the same CandGuard version to compare recall."
        )
    if base["fingerprint"] != current["fingerprint"]:
        return (
            f"Incompatible ground truth: baseline fingerprint ({base['fingerprint']}) does not match "
            f"current fingerprint ({current['fingerprint']}). Recall comparisons require identical ground truth."
        )
    return None


def _recall_from_report(truth: dict[str, Any]) -> Fraction:
    """Return the exact recall from validated counts or a legacy report's stored decimal."""
    if "unique_pairs" in truth:
        return Fraction(truth["recovered_pairs"], truth["unique_pairs"])
    return Fraction(str(truth["candidate_recall"]))


def compare_reports(
    *,
    baseline_path: str | Path,
    current_path: str | Path,
    max_recall_drop: float | None = None,
    max_pair_growth: float | None = None,
    max_duplicate_growth: float | None = None,
    baseline_candidate_set_path: str | Path | None = None,
    current_candidate_set_path: str | Path | None = None,
    min_candidate_jaccard: float | None = None,
) -> dict[str, Any]:
    """Compare two CandGuard audit reports and evaluate regression quality gates.

    When both ``baseline_candidate_set_path`` and ``current_candidate_set_path`` are given, the
    two candidate-set sidecar artifacts are validated against the ``candidate_set`` metadata of
    their reports and merged in one streaming pass to add ``metrics.candidate_set_drift``.
    ``min_candidate_jaccard`` optionally fails the gate when Jaccard similarity is below it.
    Without the artifact paths the comparison output is unchanged.
    """
    for name, threshold in (
        ("max_recall_drop", max_recall_drop),
        ("max_pair_growth", max_pair_growth),
        ("max_duplicate_growth", max_duplicate_growth),
    ):
        validate_compare_threshold(name, threshold)
    validate_unit_interval_threshold("min_candidate_jaccard", min_candidate_jaccard)

    if (baseline_candidate_set_path is None) != (current_candidate_set_path is None):
        missing = "current" if current_candidate_set_path is None else "baseline"
        raise AuditError(
            "Candidate-set drift requires both --baseline-candidate-set and --current-candidate-set; "
            f"the {missing} candidate-set artifact was not supplied."
        )
    compare_candidate_sets = baseline_candidate_set_path is not None
    if min_candidate_jaccard is not None and not compare_candidate_sets:
        raise AuditError(
            "--min-candidate-jaccard requires both --baseline-candidate-set and --current-candidate-set."
        )

    baseline_file, baseline = _load_report(baseline_path, "Baseline")
    current_file, current = _load_report(current_path, "Current")

    base_linkage = baseline["efficiency"]["linkage_type"]
    curr_linkage = current["efficiency"]["linkage_type"]
    if base_linkage != curr_linkage:
        raise AuditError(
            f"Incompatible linkage types: baseline is '{base_linkage}', "
            f"current is '{curr_linkage}'."
        )

    base_truth = baseline.get("ground_truth") or {}
    curr_truth = current.get("ground_truth") or {}
    recall_unavailable = _recall_unavailable_reason(base_truth, curr_truth)
    if recall_unavailable is not None and max_recall_drop is not None:
        raise AuditError(recall_unavailable)

    recall_drop: Fraction | None = None
    if recall_unavailable is None:
        base_recall = _recall_from_report(base_truth)
        current_recall = _recall_from_report(curr_truth)
        recall_drop = max(Fraction(0), base_recall - current_recall)

    base_rows = baseline["inputs"]["candidate_rows"]
    curr_rows = current["inputs"]["candidate_rows"]
    pair_growth = compute_relative_growth(base_rows, curr_rows)

    base_dupes = baseline["inputs"]["duplicate_candidate_rows"]
    curr_dupes = current["inputs"]["duplicate_candidate_rows"]
    duplicate_growth = compute_relative_growth(base_dupes, curr_dupes)

    # Threshold gate evaluation
    failures: list[str] = []
    if max_recall_drop is not None and recall_drop is not None:
        limit = Fraction(max_recall_drop) if isinstance(max_recall_drop, int) else Fraction(str(max_recall_drop))
        if recall_drop > limit:
            displayed_drop = float(recall_drop)
            failures.append(
                f"candidate recall drop {displayed_drop:.4f} (exact drop {recall_drop}) "
                f"exceeds limit {max_recall_drop}"
            )

    for label, base, current_count, threshold in (
        ("candidate", base_rows, curr_rows, max_pair_growth),
        ("duplicate", base_dupes, curr_dupes, max_duplicate_growth),
    ):
        if threshold is None:
            continue
        if base == 0:
            if current_count:
                rows = "candidate rows" if label == "candidate" else "duplicate candidate rows"
                failures.append(f"{rows} grew from 0 to {current_count} (unbounded growth exceeds any finite limit)")
            continue
        growth = Fraction(current_count - base, base)
        limit = Fraction(threshold) if isinstance(threshold, int) else Fraction(str(threshold))
        if growth > limit:
            measured = f"{float(growth):.2%}"
            allowed = f"{threshold:.2%}"
            if measured == allowed:
                failures.append(f"{label} row growth ({current_count - base}/{base}) exceeds limit {threshold}")
            else:
                failures.append(f"{label} row growth {measured} exceeds limit {allowed}")

    report: dict[str, Any] = {
        "tool": "candguard",
        "version": __version__,
        "command": "compare",
        "schema_version": "1.0",
        "baseline": {
            "path": str(baseline_file),
            "candguard_version": baseline.get("version"),
            "linkage_type": base_linkage,
            "candidate_rows": base_rows,
            "unique_candidate_pairs": baseline["inputs"]["unique_candidate_pairs"],
            "duplicate_candidate_rows": base_dupes,
            "candidate_recall": base_truth.get("candidate_recall") if base_truth else None,
            "ground_truth_fingerprint": base_truth.get("fingerprint") if base_truth else None,
            "ground_truth_fingerprint_version": base_truth.get("fingerprint_version", "v1") if base_truth else None,
        },
        "current": {
            "path": str(current_file),
            "candguard_version": current.get("version"),
            "linkage_type": curr_linkage,
            "candidate_rows": curr_rows,
            "unique_candidate_pairs": current["inputs"]["unique_candidate_pairs"],
            "duplicate_candidate_rows": curr_dupes,
            "candidate_recall": curr_truth.get("candidate_recall") if curr_truth else None,
            "ground_truth_fingerprint": curr_truth.get("fingerprint") if curr_truth else None,
            "ground_truth_fingerprint_version": curr_truth.get("fingerprint_version", "v1") if curr_truth else None,
        },
        "metrics": {
            "recall_drop": round(float(recall_drop), 8) if recall_drop is not None else None,
            "candidate_pair_growth": None if math.isinf(pair_growth) else round(pair_growth, 8),
            "candidate_pair_growth_unbounded": math.isinf(pair_growth),
            "duplicate_row_growth": None if math.isinf(duplicate_growth) else round(duplicate_growth, 8),
            "duplicate_row_growth_unbounded": math.isinf(duplicate_growth),
        },
        "thresholds": {
            "max_recall_drop": max_recall_drop,
            "max_pair_growth": max_pair_growth,
            "max_duplicate_growth": max_duplicate_growth,
        },
        "quality_gate": {
            "passed": not failures,
            "failures": failures,
        },
    }

    if recall_unavailable is not None:
        report["metrics"]["recall_drop_unavailable_reason"] = recall_unavailable

    if baseline_candidate_set_path is not None and current_candidate_set_path is not None:
        drift = _candidate_set_drift(
            baseline=baseline,
            current=current,
            baseline_file=baseline_file,
            current_file=current_file,
            baseline_artifact=argument_path("baseline_candidate_set_path", baseline_candidate_set_path),
            current_artifact=argument_path("current_candidate_set_path", current_candidate_set_path),
            report=report,
        )
        report["metrics"]["candidate_set_drift"] = drift
        report["thresholds"]["min_candidate_jaccard"] = min_candidate_jaccard
        if min_candidate_jaccard is not None:
            union = drift["union_pairs"]
            # Gate on the unrounded ratio (correctly rounded division); equality passes.
            measured = drift["retained_pairs"] / union if union else 1.0
            if measured < min_candidate_jaccard:
                failures.append(
                    f"candidate-set Jaccard similarity {measured:.8f} ({measured:.4%}; "
                    f"{drift['retained_pairs']:,} retained of {union:,} union pairs) "
                    f"is below required minimum {min_candidate_jaccard:.8f} ({min_candidate_jaccard:.4%})"
                )
                report["quality_gate"]["passed"] = False

    return report


def _candidate_set_drift(
    *,
    baseline: dict[str, Any],
    current: dict[str, Any],
    baseline_file: Path,
    current_file: Path,
    baseline_artifact: Path,
    current_artifact: Path,
    report: dict[str, Any],
) -> dict[str, Any]:
    """Validate both artifacts against their reports and compute exact candidate-set drift."""
    base_meta = validate_candidate_set_metadata(baseline, "Baseline", baseline_file)
    curr_meta = validate_candidate_set_metadata(current, "Current", current_file)

    retained, added, removed = compare_candidate_set_artifacts(
        baseline_artifact, current_artifact, base_meta, curr_meta
    )
    drift = compute_candidate_set_drift(retained=retained, added=added, removed=removed)
    if (
        drift["baseline_unique_pairs"] != base_meta["unique_pairs"]
        or drift["current_unique_pairs"] != curr_meta["unique_pairs"]
    ):  # pragma: no cover - guaranteed by the artifact readers' count verification
        raise AuditError("Candidate-set drift counts are inconsistent with the artifact metadata.")

    for section, artifact, meta in (
        ("baseline", baseline_artifact, base_meta),
        ("current", current_artifact, curr_meta),
    ):
        report[section]["candidate_set_path"] = str(artifact)
        report[section]["candidate_set_fingerprint"] = meta["fingerprint"]
    return drift

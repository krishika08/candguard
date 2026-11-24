"""Regression comparison of CandGuard candidate audit reports."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from . import __version__
from .candidate_set import compare_candidate_set_artifacts, validate_candidate_set_metadata
from .metrics import compute_candidate_set_drift, compute_relative_growth
from .models import AuditError, validate_compare_threshold, validate_unit_interval_threshold
from .reporting import render_compare_text

__all__ = [
    "AuditError",
    "compare_reports",
    "render_compare_text",
]


def _load_report(path_str: str | Path, label: str) -> tuple[Path, dict[str, Any]]:
    path = Path(path_str)
    if not path.exists():
        raise AuditError(f"{label} file does not exist: {path}")
    if not path.is_file():
        raise AuditError(f"{label} path is not a file: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AuditError(f"Could not read {label} file as UTF-8 text ({path}): {exc}") from exc

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
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

    efficiency = data.get("efficiency")
    if not isinstance(efficiency, dict) or efficiency.get("linkage_type") not in {"cross", "dedupe"}:
        raise AuditError(
            f"Missing or invalid 'efficiency.linkage_type' in {label} report ({path}): "
            "expected 'cross' or 'dedupe'."
        )

    return path, data


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

    base_truth = baseline.get("ground_truth")
    curr_truth = current.get("ground_truth")

    if max_recall_drop is not None:
        if base_truth is None or base_truth.get("candidate_recall") is None:
            raise AuditError("Cannot evaluate --max-recall-drop: baseline report has no candidate recall.")
        if curr_truth is None or curr_truth.get("candidate_recall") is None:
            raise AuditError("Cannot evaluate --max-recall-drop: current report has no candidate recall.")

        base_fp = base_truth.get("fingerprint")
        curr_fp = curr_truth.get("fingerprint")
        if not base_fp:
            raise AuditError(
                "Cannot evaluate --max-recall-drop: baseline report lacks ground-truth fingerprint "
                "for comparability verification."
            )
        if not curr_fp:
            raise AuditError(
                "Cannot evaluate --max-recall-drop: current report lacks ground-truth fingerprint "
                "for comparability verification."
            )
        base_fp_ver = base_truth.get("fingerprint_version", "v1")
        curr_fp_ver = curr_truth.get("fingerprint_version", "v1")
        if base_fp_ver != curr_fp_ver:
            raise AuditError(
                f"Incompatible ground-truth fingerprint versions: baseline uses '{base_fp_ver}', "
                f"current uses '{curr_fp_ver}'. Re-run audit with the same CandGuard version to compare recall."
            )
        if base_fp != curr_fp:
            raise AuditError(
                f"Incompatible ground truth: baseline fingerprint ({base_fp}) does not match "
                f"current fingerprint ({curr_fp}). Recall comparisons require identical ground truth."
            )

    # Metric computations
    recall_drop: float | None = None
    if (
        base_truth is not None
        and base_truth.get("candidate_recall") is not None
        and curr_truth is not None
        and curr_truth.get("candidate_recall") is not None
    ):
        base_rec = float(base_truth["candidate_recall"])
        curr_rec = float(curr_truth["candidate_recall"])
        recall_drop = max(0.0, base_rec - curr_rec)

    base_rows = baseline["inputs"]["candidate_rows"]
    curr_rows = current["inputs"]["candidate_rows"]
    pair_growth = compute_relative_growth(base_rows, curr_rows)

    base_dupes = baseline["inputs"]["duplicate_candidate_rows"]
    curr_dupes = current["inputs"]["duplicate_candidate_rows"]
    duplicate_growth = compute_relative_growth(base_dupes, curr_dupes)

    # Threshold gate evaluation
    failures: list[str] = []
    if max_recall_drop is not None:
        if recall_drop is None:
            failures.append("candidate recall is missing but --max-recall-drop was configured")
        elif recall_drop - max_recall_drop > 1e-9:
            failures.append(
                f"candidate recall drop {recall_drop:.4f} ({recall_drop:.2%}) "
                f"exceeds limit {max_recall_drop:.4f} ({max_recall_drop:.2%})"
            )

    if max_pair_growth is not None:
        if math.isinf(pair_growth):
            failures.append(
                f"candidate rows grew from 0 to {curr_rows} "
                f"(unbounded growth exceeds limit {max_pair_growth:.2%})"
            )
        elif pair_growth - max_pair_growth > 1e-9:
            failures.append(
                f"candidate row growth {pair_growth:.2%} exceeds limit {max_pair_growth:.2%}"
            )

    if max_duplicate_growth is not None:
        if math.isinf(duplicate_growth):
            failures.append(
                f"duplicate candidate rows grew from 0 to {curr_dupes} "
                f"(unbounded growth exceeds limit {max_duplicate_growth:.2%})"
            )
        elif duplicate_growth - max_duplicate_growth > 1e-9:
            failures.append(
                f"duplicate row growth {duplicate_growth:.2%} exceeds limit {max_duplicate_growth:.2%}"
            )

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
            "recall_drop": round(recall_drop, 8) if recall_drop is not None else None,
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

    if baseline_candidate_set_path is not None and current_candidate_set_path is not None:
        drift = _candidate_set_drift(
            baseline=baseline,
            current=current,
            baseline_file=baseline_file,
            current_file=current_file,
            baseline_artifact=Path(baseline_candidate_set_path),
            current_artifact=Path(current_candidate_set_path),
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

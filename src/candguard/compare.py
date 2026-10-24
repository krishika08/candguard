"""Regression comparison of CandGuard candidate audit reports."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from . import __version__
from .audit import AuditError


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
) -> dict[str, Any]:
    """Compare two CandGuard audit reports and evaluate regression quality gates."""
    for name, threshold in (
        ("max_recall_drop", max_recall_drop),
        ("max_pair_growth", max_pair_growth),
        ("max_duplicate_growth", max_duplicate_growth),
    ):
        if threshold is not None:
            if (
                not isinstance(threshold, (int, float))
                or isinstance(threshold, bool)
                or math.isnan(threshold)
                or math.isinf(threshold)
                or threshold < 0
            ):
                raise AuditError(f"{name} must be a finite non-negative number, got {threshold!r}.")

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
    if base_rows == 0:
        pair_growth = 0.0 if curr_rows == 0 else float("inf")
    else:
        pair_growth = (curr_rows - base_rows) / base_rows

    base_dupes = baseline["inputs"]["duplicate_candidate_rows"]
    curr_dupes = current["inputs"]["duplicate_candidate_rows"]
    if base_dupes == 0:
        duplicate_growth = 0.0 if curr_dupes == 0 else float("inf")
    else:
        duplicate_growth = (curr_dupes - base_dupes) / base_dupes

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

    return {
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


def render_compare_text(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    current = report["current"]
    metrics = report["metrics"]
    gate = report["quality_gate"]

    base_rows = baseline["candidate_rows"]
    curr_rows = current["candidate_rows"]
    p_unbounded = metrics["candidate_pair_growth_unbounded"]
    p_growth = metrics["candidate_pair_growth"]
    if p_unbounded:
        row_growth_str = " (unbounded growth)"
    elif p_growth is not None:
        row_growth_str = f" ({'+' if p_growth >= 0 else ''}{p_growth:.2%})"
    else:
        row_growth_str = ""

    base_uniq = baseline["unique_candidate_pairs"]
    curr_uniq = current["unique_candidate_pairs"]
    if base_uniq == 0:
        uniq_growth_str = " (0.00%)" if curr_uniq == 0 else " (unbounded growth)"
    else:
        u_growth = (curr_uniq - base_uniq) / base_uniq
        uniq_growth_str = f" ({'+' if u_growth >= 0 else ''}{u_growth:.2%})"

    base_dupes = baseline["duplicate_candidate_rows"]
    curr_dupes = current["duplicate_candidate_rows"]
    d_unbounded = metrics["duplicate_row_growth_unbounded"]
    d_growth = metrics["duplicate_row_growth"]
    if d_unbounded:
        dupe_growth_str = " (unbounded growth)"
    elif d_growth is not None:
        dupe_growth_str = f" ({'+' if d_growth >= 0 else ''}{d_growth:.2%})"
    else:
        dupe_growth_str = ""

    base_rec = baseline["candidate_recall"]
    curr_rec = current["candidate_recall"]
    if base_rec is not None and curr_rec is not None:
        r_drop = metrics["recall_drop"]
        recall_str = f"{base_rec:.4%} -> {curr_rec:.4%} (drop: {r_drop:.4f})"
    elif base_rec is not None or curr_rec is not None:
        recall_str = f"{base_rec} -> {curr_rec}"
    else:
        recall_str = "n/a"

    lines = [
        "CandGuard — Regression Comparison Audit",
        "=" * 42,
        f"Baseline report:                {baseline['path']}",
        f"Current report:                 {current['path']}",
        f"Linkage type:                   {baseline['linkage_type']}",
        "",
        f"Candidate rows:                 {base_rows:,} -> {curr_rows:,}{row_growth_str}",
        f"Unique candidate pairs:         {base_uniq:,} -> {curr_uniq:,}{uniq_growth_str}",
        f"Duplicate candidate rows:       {base_dupes:,} -> {curr_dupes:,}{dupe_growth_str}",
        f"Candidate recall:               {recall_str}",
        "",
        f"Quality gate: {'PASS' if gate['passed'] else 'FAIL'}",
    ]
    lines.extend(f"  - {failure}" for failure in gate["failures"])
    return "\n".join(lines)

"""Rendering functions for CandGuard audit and regression comparison reports."""
from __future__ import annotations

import json
from typing import Any


def render_text(report: dict[str, Any]) -> str:
    """Render a human-readable text report from an audit report dictionary."""
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
    degree = report.get("degree_analysis")
    if degree is not None:
        lines += _render_degree_analysis(degree, coverage)
    lines += ["", f"Quality gate: {'PASS' if gate['passed'] else 'FAIL'}"]
    lines.extend(f"  - {failure}" for failure in gate["failures"])
    return "\n".join(lines)


def _fmt(value: Any, kind: str = "int") -> str:
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value:.4%}"
    if kind == "float":
        return f"{value:.4f}"
    return f"{value:,}"


def _row(label: str, *values: str) -> str:
    if len(values) == 1:
        return f"{label:<33}{values[0]}"
    return f"{label:<33}" + "".join(f"{v:<18}" for v in values).rstrip()


def _distribution_rows(*dists: dict[str, Any]) -> list[str]:
    def col(key: str, kind: str = "int") -> list[str]:
        return [_fmt(d[key], kind) for d in dists]

    def joined(*keys: str) -> list[str]:
        return [" / ".join(_fmt(d[k], "float" if k == "mean" else "int") for k in keys) for d in dists]

    return [
        _row("Min / P50 / mean:", *joined("min", "p50", "mean")),
        _row("P95 / P99 / P99.9 / max:", *joined("p95", "p99", "p99_9", "max")),
        _row("Gini coefficient:", *col("gini_coefficient", "float")),
        _row("Top 1% candidate share:", *col("top_1_percent_candidate_share", "pct")),
    ]


def _render_degree_analysis(degree: dict[str, Any], coverage: dict[str, Any]) -> list[str]:
    """Render the advanced coverage and degree diagnostics block."""
    if degree.get("linkage_type") == "dedupe":
        dist = degree["records"]
        title = "Advanced degree diagnostics (unique pairs per record, both endpoints)"
        lines = ["", title, "-" * len(title)]
        if dist["records_in_universe"] is not None:
            lines.append(_row(
                "Record coverage:",
                f"{_fmt(coverage.get('record_coverage'), 'pct')} "
                f"({dist['records_with_candidates']:,} of {dist['records_in_universe']:,} records; "
                f"{dist['records_without_candidates']:,} without candidates)",
            ))
        else:
            lines.append(_row("Record coverage:", "n/a (record total not supplied)"))
        lines += _distribution_rows(dist)
        if dist["statistics_basis"] == "represented_records":
            lines.append(
                "Note: --left-records not supplied; statistics cover only records represented in candidate output."
            )
        return lines

    left, right = degree["left"], degree["right"]
    title = "Advanced degree diagnostics (unique pairs per record)"
    lines = [
        "",
        title,
        "-" * len(title),
        _row("", "Source", "Target"),
        _row("Record coverage:", _fmt(coverage.get("left_record_coverage"), "pct"), _fmt(coverage.get("right_record_coverage"), "pct")),
        _row(
            "Records without candidates:",
            _fmt(left["records_without_candidates"]),
            _fmt(right["records_without_candidates"]),
        ),
    ]
    lines += _distribution_rows(left, right)
    lines.append(_row("Target/source mean degree ratio:", _fmt(degree["right_to_left_mean_degree_ratio"], "float")))
    if left["statistics_basis"] == "represented_records" or right["statistics_basis"] == "represented_records":
        lines.append(
            "Note: --left-records/--right-records not supplied; statistics cover only records "
            "represented in candidate output."
        )
    return lines


def render_compare_text(report: dict[str, Any]) -> str:
    """Render a human-readable text report from a comparison report dictionary."""
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
    r_drop = metrics["recall_drop"]
    if base_rec is not None and curr_rec is not None:
        recall_str = f"{base_rec:.4%} -> {curr_rec:.4%}"
    elif base_rec is not None or curr_rec is not None:
        recall_str = f"{base_rec} -> {curr_rec}"
    else:
        recall_str = "n/a"
    if r_drop is not None:
        recall_str += f" (drop: {r_drop:.4f})"
    elif reason := metrics.get("recall_drop_unavailable_reason"):
        recall_str += f" (recall drop unavailable: {reason})"

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
    ]
    drift = metrics.get("candidate_set_drift")
    if drift is not None:
        lines.extend(_render_candidate_set_drift(drift, report.get("thresholds", {})))
        lines.append("")
    lines.append(f"Quality gate: {'PASS' if gate['passed'] else 'FAIL'}")
    lines.extend(f"  - {failure}" for failure in gate["failures"])
    return "\n".join(lines)


def _render_candidate_set_drift(drift: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    """Render the candidate-set drift section of a comparison report."""
    def share(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2%}"

    title = "Candidate-set drift (unique pairs)"
    lines = [
        title,
        "-" * len(title),
        f"Baseline unique pairs:          {drift['baseline_unique_pairs']:,}",
        f"Current unique pairs:           {drift['current_unique_pairs']:,}",
        f"Retained pairs:                 {drift['retained_pairs']:,}",
        f"Removed from baseline:          {drift['removed_pairs']:,} ({share(drift['removed_fraction_of_baseline'])})",
        f"Added in current:               {drift['added_pairs']:,} ({share(drift['added_fraction_of_current'])})",
        f"Union of candidate pairs:       {drift['union_pairs']:,}",
        f"Jaccard similarity:             {drift['jaccard_similarity']:.4%}",
        f"Pair churn:                     {drift['pair_churn']:.4%}",
    ]
    minimum = thresholds.get("min_candidate_jaccard")
    if minimum is not None:
        lines.append(f"Minimum Jaccard similarity:     {minimum:.4%}")
    return lines


def report_json(report: dict[str, Any]) -> str:
    """Format report dictionary as indented JSON."""
    return json.dumps(report, indent=2, sort_keys=True)

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
    lines += ["", f"Quality gate: {'PASS' if gate['passed'] else 'FAIL'}"]
    lines.extend(f"  - {failure}" for failure in gate["failures"])
    return "\n".join(lines)


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


def report_json(report: dict[str, Any]) -> str:
    """Format report dictionary as indented JSON."""
    return json.dumps(report, indent=2, sort_keys=True)

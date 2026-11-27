"""Tests for candguard compare regression audit functionality."""
import json
import math
from pathlib import Path
from typing import Any

import pytest

from candguard.audit import AuditError, audit_candidates, report_json
from candguard.cli import main
from candguard.compare import compare_reports, render_compare_text


def _valid_report(
    *,
    linkage_type: str = "cross",
    candidate_rows: int = 100,
    unique_candidate_pairs: int | None = None,
    duplicate_candidate_rows: int = 10,
    candidate_recall: float | None = 0.85,
    fingerprint: str | None = "sha256:abc123def456",
    fingerprint_version: str | None = "v2",
    schema_version: str = "1.0",
) -> dict[str, Any]:
    gt: dict[str, Any] | None = None
    if candidate_recall is not None or fingerprint is not None:
        gt = {
            "candidate_recall": candidate_recall,
            "fingerprint": fingerprint,
            "fingerprint_version": fingerprint_version,
        }
    return {
        "tool": "candguard",
        "version": "0.1.0",
        "schema_version": schema_version,
        "inputs": {
            "candidates": "candidates.tsv",
            "truth": "truth.tsv" if gt else None,
            "candidate_rows": candidate_rows,
            "unique_candidate_pairs": (
                candidate_rows - duplicate_candidate_rows if unique_candidate_pairs is None else unique_candidate_pairs
            ),
            "duplicate_candidate_rows": duplicate_candidate_rows,
        },
        "coverage": {
            "left_records_represented": 40,
            "right_records_represented": 40,
        },
        "candidate_distribution_per_left_record": {
            "min": 1,
            "p50": 2,
            "mean": 2.25,
            "p95": 4,
            "p99": 5,
            "max": 5,
            "sources_with_candidates": 40,
        },
        "efficiency": {
            "linkage_type": linkage_type,
            "possible_pairs": 1000,
            "reduction_ratio": 0.90,
        },
        "ground_truth": gt,
        "quality_gate": {"passed": True, "failures": []},
    }


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


@pytest.mark.parametrize("parameter", ["max_recall_drop", "max_pair_growth", "max_duplicate_growth", "min_candidate_jaccard"])
@pytest.mark.parametrize("value", [
    True, False, -1, "0.2", float("nan"), float("inf"), -float("inf"), [],
    pytest.param(-(10**400), id="huge-negative-integer"),
])
def test_compare_rejects_invalid_numeric_arguments(parameter, value):
    with pytest.raises(AuditError, match=parameter):
        compare_reports(baseline_path="unused.json", current_path="unused.json", **{parameter: value})


def test_compare_large_integer_thresholds(tmp_path):
    base, current = _pair_files(tmp_path, _valid_report(), _valid_report(candidate_rows=200))
    huge = 10**400
    options = dict(baseline_path=base, current_path=current, max_recall_drop=huge,
                   max_pair_growth=huge, max_duplicate_growth=huge)
    assert compare_reports(**options)["quality_gate"]["passed"]
    with pytest.raises(AuditError, match="min_candidate_jaccard"):
        compare_reports(**options, min_candidate_jaccard=huge)
    _write_json(base, _valid_report(candidate_rows=0, duplicate_candidate_rows=0))
    result = compare_reports(**options)
    assert not result["quality_gate"]["passed"]
    assert len(result["quality_gate"]["failures"]) == 2


@pytest.mark.parametrize("parameter", [
    "baseline_path", "current_path", "baseline_candidate_set_path", "current_candidate_set_path",
])
def test_compare_rejects_invalid_path_types(tmp_path, parameter):
    base, current = _pair_files(tmp_path, _valid_report(), _valid_report())
    options = dict(baseline_path=base, current_path=current, baseline_candidate_set_path=base,
                   current_candidate_set_path=current)
    options[parameter] = []
    with pytest.raises(AuditError, match=parameter):
        compare_reports(**options)


@pytest.mark.parametrize("label", ["Baseline", "Current"])
@pytest.mark.parametrize(("bad_counts", "fields"), [
    ({"candidate_rows": 10, "unique_candidate_pairs": 12, "duplicate_candidate_rows": 2},
     ["candidate_rows", "unique_candidate_pairs", "duplicate_candidate_rows"]),
    ({"duplicate_candidate_rows": 2}, ["candidate_rows", "unique_candidate_pairs", "duplicate_candidate_rows"]),
    ({"candidate_rows": True}, ["candidate_rows"]),
    ({"unique_candidate_pairs": 90.0}, ["unique_candidate_pairs"]),
    ({"duplicate_candidate_rows": "10"}, ["duplicate_candidate_rows"]),
])
def test_compare_rejects_invalid_report_counts(tmp_path, capsys, label, bad_counts, fields):
    base, current = _valid_report(), _valid_report()
    (base if label == "Baseline" else current)["inputs"].update(bad_counts)
    b_f, c_f = _pair_files(tmp_path, base, current)
    with pytest.raises(AuditError, match=label) as exc:
        compare_reports(baseline_path=b_f, current_path=c_f)
    assert all(field in str(exc.value) for field in fields)
    assert main(["compare", "--baseline", str(b_f), "--current", str(c_f)]) == 2
    assert label in capsys.readouterr().err


@pytest.mark.parametrize("rows", [0, 100])
def test_compare_consistent_legacy_counts(tmp_path, rows):
    report = {
        "schema_version": "1.0",
        "inputs": {"candidate_rows": rows, "unique_candidate_pairs": rows, "duplicate_candidate_rows": 0},
        "efficiency": {"linkage_type": "cross"},
    }
    b_f, c_f = _pair_files(tmp_path, report, report)
    assert compare_reports(baseline_path=b_f, current_path=c_f)["quality_gate"]["passed"]


@pytest.mark.parametrize(("known", "missed", "threshold", "passed"), [
    (10**18, 1, 0.0, False),
    (10**10, 1, 1e-10, True),
    (10**10, 1, 9e-11, False),
    (100, 2, 0.02, True),
])
def test_compare_recall_gate_uses_counts(tmp_path, known, missed, threshold, passed):
    base = _valid_report(candidate_rows=known + 10, candidate_recall=1.0)
    current = _valid_report(candidate_rows=known + 10, candidate_recall=round((known - missed) / known, 8))
    base["ground_truth"].update(unique_pairs=known, recovered_pairs=known)
    current["ground_truth"].update(unique_pairs=known, recovered_pairs=known - missed)
    b_f, c_f = _pair_files(tmp_path, base, current)
    result = compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=threshold)
    assert result["metrics"]["recall_drop"] == round(missed / known, 8)
    assert result["quality_gate"]["passed"] is passed


def test_compare_legacy_recall_has_no_absolute_gate_tolerance(tmp_path):
    b_f, c_f = _pair_files(tmp_path, _valid_report(candidate_recall=0.8), _valid_report(candidate_recall=0.78))
    result = compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.0199999999)
    assert not result["quality_gate"]["passed"]


def test_compare_rejects_truth_counts_that_contradict_reported_recall(tmp_path):
    base = _valid_report(candidate_recall=0.9)
    current = _valid_report(candidate_recall=0.8)
    current["ground_truth"].update(unique_pairs=100, recovered_pairs=90)
    b_f, c_f = _pair_files(tmp_path, base, current)
    with pytest.raises(AuditError, match=r"candidate_recall.*Current report.*recovered_pairs.*unique_pairs"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.0)


@pytest.mark.parametrize(("side", "changed", "reason"), [
    (1, {"fingerprint": "different"}, "Incompatible ground truth"),
    (1, {"fingerprint_version": "v1"}, "Incompatible ground-truth fingerprint versions"),
    (0, {"fingerprint": None}, "baseline report lacks ground-truth fingerprint"),
    (1, {"fingerprint": None}, "current report lacks ground-truth fingerprint"),
    (0, {"candidate_recall": None}, "baseline report has no candidate recall"),
    (1, {"candidate_recall": None}, "current report has no candidate recall"),
])
def test_compare_unavailable_recall_still_runs_volume_gates(tmp_path, side, changed, reason):
    reports = (_valid_report(), _valid_report(candidate_rows=200, duplicate_candidate_rows=30, candidate_recall=0.7))
    reports[side]["ground_truth"].update(changed)
    b_f, c_f = _pair_files(tmp_path, *reports)
    options = dict(baseline_path=b_f, current_path=c_f, max_pair_growth=0.2, max_duplicate_growth=0.2)
    result = compare_reports(**options)
    assert result["metrics"]["recall_drop"] is None
    assert reason in result["metrics"]["recall_drop_unavailable_reason"]
    assert reason in render_compare_text(result)
    assert len(result["quality_gate"]["failures"]) == 2
    with pytest.raises(AuditError, match=reason):
        compare_reports(**options, max_recall_drop=0.2)


def test_compare_no_regression(tmp_path: Path) -> None:
    rep = _valid_report()
    base_file = _write_json(tmp_path / "base.json", rep)
    curr_file = _write_json(tmp_path / "curr.json", rep)

    res = compare_reports(
        baseline_path=base_file,
        current_path=curr_file,
        max_recall_drop=0.01,
        max_pair_growth=0.05,
        max_duplicate_growth=0.05,
    )
    assert res["quality_gate"]["passed"] is True
    assert len(res["quality_gate"]["failures"]) == 0
    assert res["metrics"]["recall_drop"] == 0.0
    assert res["metrics"]["candidate_pair_growth"] == 0.0
    assert res["metrics"]["duplicate_row_growth"] == 0.0


def test_compare_recall_drop_pass_fail_and_exact_threshold(tmp_path: Path) -> None:
    # Baseline recall 0.80, Current recall 0.78 -> drop is exactly 0.02
    base = _valid_report(candidate_recall=0.80)
    curr = _valid_report(candidate_recall=0.78)
    base_f = _write_json(tmp_path / "base.json", base)
    curr_f = _write_json(tmp_path / "curr.json", curr)

    # 1. Permitted drop: 0.025 >= 0.02 -> passes
    res_pass = compare_reports(baseline_path=base_f, current_path=curr_f, max_recall_drop=0.025)
    assert res_pass["quality_gate"]["passed"] is True
    assert res_pass["metrics"]["recall_drop"] == pytest.approx(0.02)

    # 2. Exact threshold: 0.020 == 0.02 -> passes
    res_exact = compare_reports(baseline_path=base_f, current_path=curr_f, max_recall_drop=0.02)
    assert res_exact["quality_gate"]["passed"] is True

    # 3. Exceeded drop: 0.015 < 0.02 -> fails
    res_fail = compare_reports(baseline_path=base_f, current_path=curr_f, max_recall_drop=0.015)
    assert res_fail["quality_gate"]["passed"] is False
    assert any("candidate recall drop" in f for f in res_fail["quality_gate"]["failures"])


def test_compare_higher_recall_in_current_report(tmp_path: Path) -> None:
    # Baseline 0.75, Current 0.85 -> recall increased!
    base = _valid_report(candidate_recall=0.75)
    curr = _valid_report(candidate_recall=0.85)
    base_f = _write_json(tmp_path / "base.json", base)
    curr_f = _write_json(tmp_path / "curr.json", curr)

    res = compare_reports(baseline_path=base_f, current_path=curr_f, max_recall_drop=0.01)
    assert res["metrics"]["recall_drop"] == 0.0
    assert res["quality_gate"]["passed"] is True


def test_compare_pair_growth_pass_fail_and_exact(tmp_path: Path) -> None:
    # Baseline 100 rows, Current 120 rows -> growth is exactly 20%
    base = _valid_report(candidate_rows=100)
    curr = _valid_report(candidate_rows=120)
    base_f = _write_json(tmp_path / "base.json", base)
    curr_f = _write_json(tmp_path / "curr.json", curr)

    # Exact 20% growth passes at 0.20
    res_exact = compare_reports(baseline_path=base_f, current_path=curr_f, max_pair_growth=0.20)
    assert res_exact["quality_gate"]["passed"] is True

    # 20% growth fails at 0.19
    res_fail = compare_reports(baseline_path=base_f, current_path=curr_f, max_pair_growth=0.19)
    assert res_fail["quality_gate"]["passed"] is False
    assert any("candidate row growth" in f for f in res_fail["quality_gate"]["failures"])


def test_compare_duplicate_growth_pass_fail(tmp_path: Path) -> None:
    # Baseline 50 dupes, Current 75 dupes -> growth is 50%
    base = _valid_report(duplicate_candidate_rows=50)
    curr = _valid_report(duplicate_candidate_rows=75)
    base_f = _write_json(tmp_path / "base.json", base)
    curr_f = _write_json(tmp_path / "curr.json", curr)

    res_pass = compare_reports(baseline_path=base_f, current_path=curr_f, max_duplicate_growth=0.55)
    assert res_pass["quality_gate"]["passed"] is True

    res_fail = compare_reports(baseline_path=base_f, current_path=curr_f, max_duplicate_growth=0.45)
    assert res_fail["quality_gate"]["passed"] is False
    assert any("duplicate row growth" in f for f in res_fail["quality_gate"]["failures"])


def test_compare_baseline_counts_zero(tmp_path: Path) -> None:
    # 1. 0 to 0 -> growth is 0.0 and passes
    base_zero = _valid_report(candidate_rows=0, duplicate_candidate_rows=0)
    curr_zero = _valid_report(candidate_rows=0, duplicate_candidate_rows=0)
    b_f = _write_json(tmp_path / "base_zero.json", base_zero)
    c_f = _write_json(tmp_path / "curr_zero.json", curr_zero)

    res_zero = compare_reports(
        baseline_path=b_f,
        current_path=c_f,
        max_pair_growth=0.10,
        max_duplicate_growth=0.10,
    )
    assert res_zero["metrics"]["candidate_pair_growth"] == 0.0
    assert res_zero["metrics"]["duplicate_row_growth"] == 0.0
    assert res_zero["quality_gate"]["passed"] is True

    # 2. 0 to positive -> unbounded growth, fails enabled gate
    curr_pos = _valid_report(candidate_rows=10, duplicate_candidate_rows=5)
    c_pos_f = _write_json(tmp_path / "curr_pos.json", curr_pos)

    res_pos = compare_reports(
        baseline_path=b_f,
        current_path=c_pos_f,
        max_pair_growth=0.50,
        max_duplicate_growth=0.50,
    )
    assert res_pos["metrics"]["candidate_pair_growth_unbounded"] is True
    assert res_pos["metrics"]["duplicate_row_growth_unbounded"] is True
    assert res_pos["quality_gate"]["passed"] is False
    assert len(res_pos["quality_gate"]["failures"]) == 2
    assert any("candidate rows grew from 0 to 10" in f for f in res_pos["quality_gate"]["failures"])
    assert any("duplicate candidate rows grew from 0 to 5" in f for f in res_pos["quality_gate"]["failures"])


def test_compare_no_thresholds_supplied(tmp_path: Path) -> None:
    base = _valid_report(candidate_rows=100, candidate_recall=0.90)
    curr = _valid_report(candidate_rows=200, candidate_recall=0.70)
    b_f = _write_json(tmp_path / "b.json", base)
    c_f = _write_json(tmp_path / "c.json", curr)

    # When no thresholds are supplied, quality gate passes and metrics are reported
    res = compare_reports(baseline_path=b_f, current_path=c_f)
    assert res["quality_gate"]["passed"] is True
    assert len(res["quality_gate"]["failures"]) == 0
    assert res["metrics"]["candidate_pair_growth"] == 1.0
    assert res["metrics"]["recall_drop"] == pytest.approx(0.20)


def test_compare_incompatible_linkage_types(tmp_path: Path) -> None:
    base = _valid_report(linkage_type="cross")
    curr = _valid_report(linkage_type="dedupe")
    b_f = _write_json(tmp_path / "b.json", base)
    c_f = _write_json(tmp_path / "c.json", curr)

    with pytest.raises(AuditError, match="Incompatible linkage types: baseline is 'cross', current is 'dedupe'"):
        compare_reports(baseline_path=b_f, current_path=c_f)


def test_compare_ground_truth_fingerprint_mismatch(tmp_path: Path) -> None:
    base = _valid_report(fingerprint="sha256:1111111111")
    curr = _valid_report(fingerprint="sha256:2222222222")
    b_f = _write_json(tmp_path / "b.json", base)
    c_f = _write_json(tmp_path / "c.json", curr)

    # With recall comparison -> rejected due to comparability requirement
    with pytest.raises(AuditError, match="Incompatible ground truth: baseline fingerprint"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)

    # Without recall comparison (e.g. only pair growth) -> permitted
    res = compare_reports(baseline_path=b_f, current_path=c_f, max_pair_growth=0.50)
    assert res["quality_gate"]["passed"] is True


def test_compare_fingerprint_version_mismatch_blocks_recall_drop(tmp_path: Path) -> None:
    base = _valid_report(fingerprint="sha256:fixed", fingerprint_version="v1")
    curr = _valid_report(fingerprint="sha256:fixed", fingerprint_version="v2")
    b_f = _write_json(tmp_path / "b.json", base)
    c_f = _write_json(tmp_path / "c.json", curr)

    with pytest.raises(AuditError, match="Incompatible ground-truth fingerprint versions: baseline uses 'v1', current uses 'v2'"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)

    # Pair growth only should succeed without error
    res = compare_reports(baseline_path=b_f, current_path=c_f, max_pair_growth=0.20)
    assert res["quality_gate"]["passed"] is True


def test_compare_legacy_report_without_fingerprint_version_defaults_to_v1(tmp_path: Path) -> None:
    base = _valid_report(fingerprint="sha256:same", fingerprint_version=None)
    del base["ground_truth"]["fingerprint_version"]
    curr = _valid_report(fingerprint="sha256:same", fingerprint_version="v2")
    b_f = _write_json(tmp_path / "b.json", base)
    c_f = _write_json(tmp_path / "c.json", curr)

    with pytest.raises(AuditError, match="Incompatible ground-truth fingerprint versions: baseline uses 'v1', current uses 'v2'"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)

    # Two legacy reports without fingerprint_version can compare if fingerprints match
    curr_legacy = _valid_report(fingerprint="sha256:same", fingerprint_version=None)
    del curr_legacy["ground_truth"]["fingerprint_version"]
    c_legacy_f = _write_json(tmp_path / "c_leg.json", curr_legacy)

    res = compare_reports(baseline_path=b_f, current_path=c_legacy_f, max_recall_drop=0.05)
    assert res["quality_gate"]["passed"] is True


def test_compare_missing_fingerprint_when_recall_required(tmp_path: Path) -> None:
    base = _valid_report(fingerprint=None)
    curr = _valid_report(fingerprint="sha256:2222222222")
    b_f = _write_json(tmp_path / "b.json", base)
    c_f = _write_json(tmp_path / "c.json", curr)

    with pytest.raises(AuditError, match="baseline report lacks ground-truth fingerprint"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)


def test_compare_null_recall_when_recall_required(tmp_path: Path) -> None:
    base = _valid_report(candidate_recall=None)
    curr = _valid_report(candidate_recall=0.80)
    b_f = _write_json(tmp_path / "b.json", base)
    c_f = _write_json(tmp_path / "c.json", curr)

    with pytest.raises(AuditError, match="baseline report has no candidate recall"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)


def test_compare_invalid_thresholds(tmp_path: Path) -> None:
    b_f = _write_json(tmp_path / "b.json", _valid_report())
    c_f = _write_json(tmp_path / "c.json", _valid_report())

    # Negative threshold
    with pytest.raises(AuditError, match="max_recall_drop must be a finite non-negative number"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=-0.01)

    # NaN threshold
    with pytest.raises(AuditError, match="max_pair_growth must be a finite non-negative number"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_pair_growth=float("nan"))

    # Infinity threshold
    with pytest.raises(AuditError, match="max_duplicate_growth must be a finite non-negative number"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_duplicate_growth=float("inf"))


def test_compare_malformed_json_and_unsupported_schema(tmp_path: Path) -> None:
    good_f = _write_json(tmp_path / "good.json", _valid_report())

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{invalid json", encoding="utf-8")
    with pytest.raises(AuditError, match="Could not parse Baseline JSON report"):
        compare_reports(baseline_path=bad_json, current_path=good_f)

    # Root not a dict
    array_json = tmp_path / "array.json"
    array_json.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(AuditError, match="root must be a JSON object"):
        compare_reports(baseline_path=array_json, current_path=good_f)

    # Unsupported schema version
    bad_schema = tmp_path / "bad_schema.json"
    bad_schema_data = _valid_report(schema_version="2.0")
    _write_json(bad_schema, bad_schema_data)
    with pytest.raises(AuditError, match="Unsupported schema version .* expected '1.x', got '2.0'"):
        compare_reports(baseline_path=bad_schema, current_path=good_f)


def test_compare_missing_required_metrics(tmp_path: Path) -> None:
    good_f = _write_json(tmp_path / "good.json", _valid_report())

    # Missing inputs.candidate_rows
    missing_data = _valid_report()
    del missing_data["inputs"]["candidate_rows"]
    missing_f = _write_json(tmp_path / "missing.json", missing_data)
    with pytest.raises(AuditError, match="Missing or invalid 'inputs.candidate_rows'"):
        compare_reports(baseline_path=missing_f, current_path=good_f)

    # Negative inputs.duplicate_candidate_rows
    negative_data = _valid_report()
    negative_data["inputs"]["duplicate_candidate_rows"] = -1
    negative_f = _write_json(tmp_path / "negative.json", negative_data)
    with pytest.raises(AuditError, match="Missing or invalid 'inputs.duplicate_candidate_rows'"):
        compare_reports(baseline_path=negative_f, current_path=good_f)


def test_compare_render_text(tmp_path: Path) -> None:
    base = _valid_report(candidate_rows=1000, candidate_recall=0.95)
    curr = _valid_report(candidate_rows=1200, candidate_recall=0.92)
    b_f = _write_json(tmp_path / "b.json", base)
    c_f = _write_json(tmp_path / "c.json", curr)

    res = compare_reports(
        baseline_path=b_f,
        current_path=c_f,
        max_recall_drop=0.02,
        max_pair_growth=0.10,
    )
    rendered = render_compare_text(res)
    assert "Regression Comparison Audit" in rendered
    assert "Candidate rows:                 1,000 -> 1,200 (+20.00%)" in rendered
    assert "Candidate recall:               95.0000% -> 92.0000% (drop: 0.0300)" in rendered
    assert "Quality gate: FAIL" in rendered
    assert "candidate recall drop" in rendered
    assert "candidate row growth" in rendered


def test_compare_deterministic_results(tmp_path: Path) -> None:
    base = _valid_report(candidate_rows=100, duplicate_candidate_rows=10, candidate_recall=0.90)
    curr = _valid_report(candidate_rows=110, duplicate_candidate_rows=12, candidate_recall=0.88)
    b_f = _write_json(tmp_path / "b.json", base)
    c_f = _write_json(tmp_path / "c.json", curr)

    res1 = compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05, max_pair_growth=0.20)
    res2 = compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05, max_pair_growth=0.20)
    assert res1 == res2
    assert render_compare_text(res1) == render_compare_text(res2)


def test_compare_end_to_end_audits(tmp_path: Path) -> None:
    # 1. Generate baseline candidate output and audit report
    base_cand = tmp_path / "base_candidates.tsv"
    base_cand.write_text("s\tc\nA1\tB1\nA1\tB2\nA2\tB3\n", encoding="utf-8")
    truth = tmp_path / "truth.tsv"
    truth.write_text("s\tc\nA1\tB1\nA2\tB3\nA3\tB4\n", encoding="utf-8")

    base_audit = audit_candidates(
        candidates_path=base_cand,
        truth_path=truth,
        left_id="s",
        right_id="c",
    )
    base_report_path = tmp_path / "base_report.json"
    base_report_path.write_text(report_json(base_audit), encoding="utf-8")

    # 2. Generate current candidate output with recall regression and volume growth
    # Misses A2-B3, adds many false candidate rows
    curr_cand = tmp_path / "curr_candidates.tsv"
    curr_cand.write_text(
        "s\tc\nA1\tB1\nA1\tB9\nA2\tB9\nA3\tB9\nA4\tB9\nA5\tB9\n",
        encoding="utf-8",
    )
    curr_audit = audit_candidates(
        candidates_path=curr_cand,
        truth_path=truth,
        left_id="s",
        right_id="c",
    )
    curr_report_path = tmp_path / "curr_report.json"
    curr_report_path.write_text(report_json(curr_audit), encoding="utf-8")

    # Baseline recall: 2/3 (66.67%), rows: 3
    # Current recall: 1/3 (33.33%), rows: 6 (+100% growth)
    res = compare_reports(
        baseline_path=base_report_path,
        current_path=curr_report_path,
        max_recall_drop=0.10,
        max_pair_growth=0.50,
    )
    assert res["quality_gate"]["passed"] is False
    assert len(res["quality_gate"]["failures"]) == 2
    assert res["metrics"]["recall_drop"] == pytest.approx(1 / 3)
    assert res["metrics"]["candidate_pair_growth"] == pytest.approx(1.0)


# --- Malformed ground-truth validation ------------------------------------------------------

def _pair_files(tmp_path: Path, base: dict[str, Any], curr: dict[str, Any]) -> tuple[Path, Path]:
    return _write_json(tmp_path / "base.json", base), _write_json(tmp_path / "curr.json", curr)


def _with_truth(report: dict[str, Any], **fields: Any) -> dict[str, Any]:
    report["ground_truth"].update(fields)
    return report


@pytest.mark.parametrize("side", ["Baseline", "Current"])
@pytest.mark.parametrize("gate", [None, 0.0], ids=["diagnostic", "gated"])
@pytest.mark.parametrize(
    ("counts", "recall", "fields"),
    [
        pytest.param({"rows": -1, "unique_pairs": 10, "recovered_pairs": 10}, 1.0, ("rows",), id="negative-rows"),
        pytest.param({"unique_pairs": -1, "recovered_pairs": 0}, 1.0, ("unique_pairs",), id="negative-unique"),
        pytest.param({"unique_pairs": 10, "recovered_pairs": -1, "missed_pairs": 11}, 1.0,
                     ("recovered_pairs",), id="fallback-bypass"),
        pytest.param({"unique_pairs": 10, "recovered_pairs": 10, "missed_pairs": -1}, 1.0,
                     ("missed_pairs",), id="negative-missed"),
        pytest.param({"rows": True, "unique_pairs": 1, "recovered_pairs": 1}, 1.0, ("rows",), id="bool-rows"),
        pytest.param({"unique_pairs": 1, "recovered_pairs": True}, 1.0, ("recovered_pairs",), id="bool-recovered"),
        pytest.param({"unique_pairs": 10.0, "recovered_pairs": 10}, 1.0, ("unique_pairs",), id="float-unique"),
        pytest.param({"unique_pairs": 10, "recovered_pairs": 10, "missed_pairs": "0"}, 1.0,
                     ("missed_pairs",), id="string-missed"),
        pytest.param({"unique_pairs": 10, "recovered_pairs": None}, 1.0, ("recovered_pairs",), id="null-recovered"),
        pytest.param({"unique_pairs": 10, "recovered_pairs": 11}, 1.0,
                     ("recovered_pairs", "unique_pairs"), id="too-many-recovered"),
        pytest.param({"unique_pairs": 10, "recovered_pairs": 10, "missed_pairs": 1}, 1.0,
                     ("recovered_pairs", "missed_pairs", "unique_pairs"), id="wrong-missed"),
        pytest.param({"rows": 9, "unique_pairs": 10, "recovered_pairs": 10}, 1.0,
                     ("rows", "unique_pairs"), id="too-few-rows"),
        pytest.param({"unique_pairs": 10}, 1.0, ("unique_pairs", "recovered_pairs"), id="unique-only"),
        pytest.param({"recovered_pairs": 10}, 1.0, ("unique_pairs", "recovered_pairs"), id="recovered-only"),
        pytest.param({"rows": 10}, 1.0, ("rows", "unique_pairs", "recovered_pairs"), id="rows-only"),
        pytest.param({"missed_pairs": 0}, 1.0, ("missed_pairs", "unique_pairs", "recovered_pairs"), id="missed-only"),
        pytest.param({"unique_pairs": 0, "recovered_pairs": 1}, None,
                     ("recovered_pairs", "unique_pairs"), id="empty-recovered"),
        pytest.param({"unique_pairs": 0, "recovered_pairs": 0, "missed_pairs": 1}, None,
                     ("missed_pairs", "unique_pairs"), id="empty-missed"),
        pytest.param({"unique_pairs": 0, "recovered_pairs": 0}, 0.0,
                     ("candidate_recall", "unique_pairs"), id="empty-numeric-recall"),
        pytest.param({"unique_pairs": 10, "recovered_pairs": 9}, 1.0,
                     ("candidate_recall", "recovered_pairs", "unique_pairs"), id="wrong-recall"),
    ],
)
def test_ground_truth_count_validation_rejects_invalid_reports(tmp_path, capsys, side, gate, counts, recall, fields):
    bad = _with_truth(_valid_report(), candidate_recall=recall, **counts)
    good = _valid_report(candidate_recall=1.0)
    base, current = _pair_files(tmp_path, *((bad, good) if side == "Baseline" else (good, bad)))

    # A stored 1.0 must never turn invalid count metadata into a passing recall gate.
    with pytest.raises(AuditError, match=f"{side} report") as exc:
        compare_reports(baseline_path=base, current_path=current, max_recall_drop=gate)
    assert all(field in str(exc.value) for field in fields)

    args = ["compare", "--baseline", str(base), "--current", str(current)]
    if gate is not None:
        args.extend(["--max-recall-drop", str(gate)])
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("candguard: error:")
    assert f"{side} report" in captured.err
    assert all(field in captured.err for field in fields)
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("counts", "reported"),
    [
        pytest.param({}, 0.66666667, id="legacy-without-counts"),
        pytest.param({"unique_pairs": 3, "recovered_pairs": 2}, 0.66666667, id="exact-counts-only"),
        pytest.param({"rows": 4, "unique_pairs": 3, "recovered_pairs": 2, "missed_pairs": 1},
                     0.66666667, id="complete-with-duplicate-truth-row"),
        pytest.param({"rows": 4, "unique_pairs": 3, "recovered_pairs": 2, "missed_pairs": 1},
                     2 / 3, id="consistent-unrounded-recall"),
        pytest.param({"rows": 10**400, "unique_pairs": 10**400, "recovered_pairs": 10**400 - 1, "missed_pairs": 1},
                     1.0, id="large-integer-counts"),
    ],
)
def test_ground_truth_count_validation_accepts_compatible_reports(tmp_path, counts, reported):
    report = _with_truth(_valid_report(), candidate_recall=reported, **counts)
    base, current = _pair_files(tmp_path, report, report)
    result = compare_reports(baseline_path=base, current_path=current, max_recall_drop=0.0)
    assert result["quality_gate"]["passed"] is True
    assert result["metrics"]["recall_drop"] == 0.0


@pytest.mark.parametrize("unique", [0, 10], ids=["empty", "nonempty"])
@pytest.mark.parametrize("include_null_recall", [True, False], ids=["null", "omitted"])
def test_ground_truth_count_validation_accepts_unavailable_recall(tmp_path, unique, include_null_recall):
    report = _with_truth(_valid_report(), rows=unique, unique_pairs=unique, recovered_pairs=0, missed_pairs=unique)
    if include_null_recall:
        report["ground_truth"]["candidate_recall"] = None
    else:
        del report["ground_truth"]["candidate_recall"]
    base, current = _pair_files(tmp_path, report, report)
    result = compare_reports(baseline_path=base, current_path=current, max_pair_growth=0.0)
    assert result["quality_gate"]["passed"] is True
    assert result["metrics"]["recall_drop"] is None
    with pytest.raises(AuditError, match="baseline report has no candidate recall"):
        compare_reports(baseline_path=base, current_path=current, max_recall_drop=0.0)


def test_out_of_range_current_recall_no_longer_passes_recall_gate(tmp_path: Path) -> None:
    # Before the fix, recall 1.2 produced recall_drop = max(0, 0.9 - 1.2) = 0 and PASSED the gate.
    b_f, c_f = _pair_files(tmp_path, _valid_report(candidate_recall=0.9), _valid_report(candidate_recall=1.2))
    with pytest.raises(AuditError, match=r"Invalid 'ground_truth.candidate_recall' in Current report .* got 1\.2"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.01)


@pytest.mark.parametrize("bad_truth", ["sha256:abc", [0.9], 0.9, 5, True, False])
@pytest.mark.parametrize("side", ["Baseline", "Current"])
@pytest.mark.parametrize("gate", [None, 0.05])
def test_ground_truth_must_be_object_or_null(tmp_path: Path, bad_truth: Any, side: str, gate: float | None) -> None:
    bad = _valid_report()
    bad["ground_truth"] = bad_truth
    good = _valid_report()
    b_f, c_f = _pair_files(tmp_path, bad if side == "Baseline" else good, good if side == "Baseline" else bad)
    with pytest.raises(AuditError, match=rf"Invalid 'ground_truth' in {side} report .* expected an object or null"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=gate)


@pytest.mark.parametrize(
    "bad_recall",
    [-0.1, 1.01, 1.2, -1, 2, float("nan"), float("inf"), float("-inf"), "0.9", "", True, False, [0.9], {"v": 0.9}],
)
@pytest.mark.parametrize("side", ["Baseline", "Current"])
@pytest.mark.parametrize("gate", [None, 0.05])
def test_invalid_candidate_recall_rejected(tmp_path: Path, bad_recall: Any, side: str, gate: float | None) -> None:
    bad = _valid_report(candidate_recall=0.85)
    bad["ground_truth"]["candidate_recall"] = bad_recall
    good = _valid_report(candidate_recall=0.85)
    b_f, c_f = _pair_files(tmp_path, bad if side == "Baseline" else good, good if side == "Baseline" else bad)
    with pytest.raises(AuditError, match=rf"Invalid 'ground_truth.candidate_recall' in {side} report .* between 0 and 1"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=gate)


HUGE_INT = 10**400  # a valid JSON number that math.isfinite() cannot convert to float


@pytest.mark.parametrize("huge", [HUGE_INT, -HUGE_INT], ids=["10**400", "-10**400"])
@pytest.mark.parametrize("side", ["Baseline", "Current"])
@pytest.mark.parametrize("gate", [None, 0.05])
def test_huge_integer_recall_rejected_without_overflow(tmp_path: Path, huge: int, side: str, gate: float | None) -> None:
    bad = _with_truth(_valid_report(), candidate_recall=huge)
    good = _valid_report()
    b_f, c_f = _pair_files(tmp_path, bad if side == "Baseline" else good, good if side == "Baseline" else bad)
    # JSON round trip: the report on disk yields the same arbitrary-precision int via json.loads().
    loaded = json.loads((b_f if side == "Baseline" else c_f).read_text(encoding="utf-8"))
    assert type(loaded["ground_truth"]["candidate_recall"]) is int
    assert loaded["ground_truth"]["candidate_recall"] == huge
    with pytest.raises(AuditError, match=rf"Invalid 'ground_truth.candidate_recall' in {side} report .* between 0 and 1"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=gate)


def test_huge_integer_recall_in_both_reports_rejected(tmp_path: Path) -> None:
    b_f, c_f = _pair_files(
        tmp_path, _with_truth(_valid_report(), candidate_recall=HUGE_INT),
        _with_truth(_valid_report(), candidate_recall=HUGE_INT),
    )
    with pytest.raises(AuditError, match=r"Invalid 'ground_truth.candidate_recall' in Baseline report"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)


def test_integer_literal_beyond_int_conversion_limit_is_clean_error(tmp_path: Path) -> None:
    # 5,001 digits: Python 3.11+ json.loads() raises a plain ValueError (int-string conversion
    # limit); Python 3.10 parses it and recall validation rejects it. Both must be AuditError.
    text = json.dumps(_valid_report(candidate_recall=0.85)).replace(
        '"candidate_recall": 0.85', '"candidate_recall": 1' + "0" * 5000
    )
    assert '"candidate_recall": 1000' in text
    c_f = tmp_path / "curr.json"
    c_f.write_text(text, encoding="utf-8")
    b_f = _write_json(tmp_path / "base.json", _valid_report())
    with pytest.raises(AuditError, match=r"Could not parse Current JSON report|Invalid 'ground_truth.candidate_recall' in Current report"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)


@pytest.mark.parametrize("edge", [0, 1, 0.0, 1.0])
def test_boundary_recall_values_are_valid(tmp_path: Path, edge: float) -> None:
    b_f, c_f = _pair_files(
        tmp_path, _valid_report(candidate_recall=0.85), _with_truth(_valid_report(), candidate_recall=edge)
    )
    res = compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=1.0)
    assert res["quality_gate"]["passed"] is True
    assert res["current"]["candidate_recall"] == edge
    assert res["metrics"]["recall_drop"] == pytest.approx(max(0.0, 0.85 - edge))


@pytest.mark.parametrize("missing", ["absent", "null"])
def test_legacy_reports_without_recall_still_compare(tmp_path: Path, missing: str) -> None:
    legacy = _valid_report()
    if missing == "absent":
        del legacy["ground_truth"]["candidate_recall"]
    else:
        legacy["ground_truth"]["candidate_recall"] = None
    b_f, c_f = _pair_files(tmp_path, legacy, _valid_report())
    res = compare_reports(baseline_path=b_f, current_path=c_f, max_pair_growth=0.5)
    assert res["quality_gate"]["passed"] is True
    assert res["metrics"]["recall_drop"] is None
    assert res["baseline"]["candidate_recall"] is None
    # Requesting a recall gate keeps the existing, explicit error.
    with pytest.raises(AuditError, match="baseline report has no candidate recall"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)


def test_reports_without_ground_truth_still_compare_without_recall_gate(tmp_path: Path) -> None:
    no_truth = _valid_report(candidate_recall=None, fingerprint=None)
    assert no_truth["ground_truth"] is None
    b_f, c_f = _pair_files(tmp_path, no_truth, _valid_report(candidate_recall=None, fingerprint=None))
    res = compare_reports(baseline_path=b_f, current_path=c_f, max_pair_growth=0.1)
    assert res["quality_gate"]["passed"] is True
    assert res["baseline"]["ground_truth_fingerprint"] is None


def test_legacy_report_without_fingerprint_version_still_valid(tmp_path: Path) -> None:
    legacy = _valid_report(fingerprint="sha256:same", fingerprint_version="v1")
    del legacy["ground_truth"]["fingerprint_version"]
    other = _valid_report(fingerprint="sha256:same", fingerprint_version="v1")
    b_f, c_f = _pair_files(tmp_path, legacy, other)
    res = compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)
    assert res["quality_gate"]["passed"] is True
    assert res["baseline"]["ground_truth_fingerprint_version"] == "v1"


def test_null_fingerprint_is_still_treated_as_missing(tmp_path: Path) -> None:
    b_f, c_f = _pair_files(tmp_path, _valid_report(fingerprint=None), _valid_report(fingerprint="sha256:2"))
    assert compare_reports(baseline_path=b_f, current_path=c_f)["quality_gate"]["passed"] is True
    with pytest.raises(AuditError, match="baseline report lacks ground-truth fingerprint"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)


@pytest.mark.parametrize("bad_fp", ["", 123, 1.5, True, ["sha256:x"], {"sha256": "x"}])
@pytest.mark.parametrize("gate", [None, 0.05])
def test_invalid_fingerprint_type_rejected(tmp_path: Path, bad_fp: Any, gate: float | None) -> None:
    bad = _with_truth(_valid_report(), fingerprint=bad_fp)
    b_f, c_f = _pair_files(tmp_path, _valid_report(), bad)
    with pytest.raises(AuditError, match=r"Invalid 'ground_truth.fingerprint' in Current report .* non-empty string"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=gate)


@pytest.mark.parametrize("bad_version", [None, "", 2, 2.0, True, ["v2"]])
@pytest.mark.parametrize("gate", [None, 0.05])
def test_invalid_fingerprint_version_type_rejected(tmp_path: Path, bad_version: Any, gate: float | None) -> None:
    bad = _with_truth(_valid_report(), fingerprint_version=bad_version)
    b_f, c_f = _pair_files(tmp_path, bad, _valid_report())
    with pytest.raises(AuditError, match=r"Invalid 'ground_truth.fingerprint_version' in Baseline report .* non-empty string"):
        compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=gate)


def test_valid_recall_gate_and_exact_threshold_unchanged(tmp_path: Path) -> None:
    b_f, c_f = _pair_files(tmp_path, _valid_report(candidate_recall=0.9), _valid_report(candidate_recall=0.85))
    exact = compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.05)
    assert exact["quality_gate"]["passed"] is True
    assert exact["metrics"]["recall_drop"] == pytest.approx(0.05)
    below = compare_reports(baseline_path=b_f, current_path=c_f, max_recall_drop=0.0499)
    assert below["quality_gate"]["passed"] is False
    assert below["quality_gate"]["failures"][0].startswith("candidate recall drop 0.0500")


MALFORMED_TRUTH_CASES = {
    "truth-string": lambda r: r.update(ground_truth="oops"),
    "truth-list": lambda r: r.update(ground_truth=[1, 2]),
    "recall-high": lambda r: r["ground_truth"].update(candidate_recall=1.2),
    "recall-nan": lambda r: r["ground_truth"].update(candidate_recall=float("nan")),
    "recall-string": lambda r: r["ground_truth"].update(candidate_recall="0.9"),
    "recall-bool": lambda r: r["ground_truth"].update(candidate_recall=True),
    "fingerprint-int": lambda r: r["ground_truth"].update(fingerprint=7),
    "version-null": lambda r: r["ground_truth"].update(fingerprint_version=None),
    "recall-huge-int": lambda r: r["ground_truth"].update(candidate_recall=HUGE_INT),
}


@pytest.mark.parametrize("case", list(MALFORMED_TRUTH_CASES))
@pytest.mark.parametrize("gate", [[], ["--max-recall-drop", "0.05"]])
def test_cli_malformed_report_exits_2_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str, gate: list[str]
) -> None:
    from candguard.cli import main

    bad = _valid_report()
    MALFORMED_TRUTH_CASES[case](bad)
    b_f, c_f = _pair_files(tmp_path, _valid_report(), bad)
    code = main(["compare", "--baseline", str(b_f), "--current", str(c_f), *gate])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("candguard: error: Invalid 'ground_truth")
    assert "Traceback" not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    ("bad_recall", "expected"),
    [(float("inf"), "got inf"), (HUGE_INT, "got 1000000000"), (-HUGE_INT, "got -1000000000")],
    ids=["infinity", "huge-int", "huge-negative-int"],
)
def test_cli_subprocess_malformed_report_has_no_traceback(tmp_path: Path, bad_recall: Any, expected: str) -> None:
    import os
    import subprocess
    import sys

    bad = _with_truth(_valid_report(), candidate_recall=bad_recall)
    b_f, c_f = _pair_files(tmp_path, _valid_report(), bad)
    src = str(Path(__file__).resolve().parent.parent / "src")
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.run(
        [sys.executable, "-m", "candguard", "compare", "--baseline", str(b_f), "--current", str(c_f),
         "--max-recall-drop", "0.01"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "OverflowError" not in proc.stderr
    assert "Invalid 'ground_truth.candidate_recall' in Current report" in proc.stderr
    assert expected in proc.stderr

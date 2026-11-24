"""Tests for advanced record-coverage and degree-distribution diagnostics.

Expected values are hand-calculated in comments, and the summary implementation is also checked
against an independent brute-force reference (full sorted list, O(N^2) pairwise Gini).
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from candguard import AuditError, audit_candidates, compare_reports
from candguard.cli import main
from candguard.metrics import (
    compute_mean_degree_ratio,
    compute_record_coverage,
    nearest_rank,
    summarize_degree_histogram,
)
from candguard.reporting import render_text
from candguard.storage import SQLiteStorageEngine

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - exercised in the dependency-free CI job
    pa = None
    pq = None

needs_pyarrow = pytest.mark.skipif(pa is None, reason="optional dependency pyarrow is not installed")

DIST_KEYS = {
    "statistics_basis", "records_in_universe", "records_with_candidates", "records_without_candidates",
    "total_degree", "min", "p50", "p95", "p99", "p99_9", "mean", "max", "gini_coefficient",
    "top_1_percent_candidate_share",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def summarize(degrees: list[int], universe: int | None = None) -> dict[str, Any]:
    """Summarize positive degrees (zeros are implied by ``universe``)."""
    assert all(d > 0 for d in degrees)
    return summarize_degree_histogram(
        iter(sorted(Counter(degrees).items())),
        represented_records=len(degrees),
        total_degree=sum(degrees),
        universe_records=universe,
    )


def reference_summary(values: list[int]) -> dict[str, Any]:
    """Independent brute-force reference over the full list of degrees, including zeros."""
    xs = sorted(values)
    n, s = len(xs), sum(xs)
    ref: dict[str, Any] = {
        "min": xs[0],
        "max": xs[-1],
        "mean": round(s / n, 8),
        "p50": xs[max(1, math.ceil(0.5 * n)) - 1],
        "p95": xs[max(1, math.ceil(0.95 * n)) - 1],
        "p99": xs[max(1, math.ceil(0.99 * n)) - 1],
        "p99_9": xs[max(1, math.ceil(0.999 * n)) - 1],
        "gini_coefficient": None,
        "top_1_percent_candidate_share": None,
    }
    if s:
        mean_abs_diff = sum(abs(a - b) for a in xs for b in xs) / (n * n)
        ref["gini_coefficient"] = round(mean_abs_diff / (2 * s / n), 8)
        k = math.ceil(n / 100)
        ref["top_1_percent_candidate_share"] = round(sum(sorted(xs, reverse=True)[:k]) / s, 8)
    return ref


def write_tsv(path: Path, rows: list[tuple[str, str]], header: tuple[str, str] = ("source_id", "candidate_id")) -> Path:
    path.write_text("\n".join("\t".join(r) for r in [header, *rows]) + "\n", encoding="utf-8")
    return path


def audit(path: Path, linkage_type: str = "cross", **kwargs: Any) -> dict[str, Any]:
    return audit_candidates(
        candidates_path=path, left_id="source_id", right_id="candidate_id", linkage_type=linkage_type, **kwargs
    )


@pytest.fixture
def sqlite_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "sqlite_tmp"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


# Cross fixture: unique pairs L1-R1, L1-R2, L1-R3, L2-R1, L3-R1; three duplicate rows
# (two exact repeats of L1-R1 and " L2 "-R1, which trims to L2-R1).
CROSS_ROWS = [
    ("L1", "R1"), ("L1", "R2"), ("L1", "R3"), ("L2", "R1"), ("L3", "R1"),
    ("L1", "R1"), ("L1", "R1"), (" L2 ", "R1"),
]
# Dedupe fixture: unique undirected pairs {a,b}, {a,c}, {d,e}; "b a" and repeated "a b" are duplicates.
DEDUPE_ROWS = [("a", "b"), ("b", "a"), ("a", "c"), ("d", "e"), ("a", "b")]


# ---------------------------------------------------------------------------
# Mathematical definitions (pure functions)
# ---------------------------------------------------------------------------


def test_nearest_rank_uses_exact_arithmetic() -> None:
    assert nearest_rank(50, 100, 1) == 1
    assert nearest_rank(50, 100, 5) == 3
    assert nearest_rank(95, 100, 20) == 19
    assert nearest_rank(999, 1000, 1000) == 999
    assert nearest_rank(999, 1000, 1001) == 1000
    # Float math gives ceil(0.07 * 100) == 8; the exact rank is 7.
    assert math.ceil(0.07 * 100) == 8
    assert nearest_rank(7, 100, 100) == 7
    with pytest.raises(ValueError):
        nearest_rank(50, 100, 0)


def test_percentiles_on_sequential_universe() -> None:
    s = summarize(list(range(1, 1001)), universe=1000)
    assert (s["p50"], s["p95"], s["p99"], s["p99_9"]) == (500, 950, 990, 999)
    assert (s["min"], s["max"]) == (1, 1000)
    s7 = summarize(list(range(1, 8)))
    # N=7: ranks ceil(3.5)=4, ceil(6.65)=7, ceil(6.93)=7, ceil(6.993)=7
    assert (s7["p50"], s7["p95"], s7["p99"], s7["p99_9"]) == (4, 7, 7, 7)


def test_uniform_distribution() -> None:
    s = summarize([3, 3, 3, 3])
    assert s["gini_coefficient"] == 0.0
    assert s["top_1_percent_candidate_share"] == 0.25  # k = ceil(4/100) = 1 -> 3 / 12
    assert (s["min"], s["p50"], s["p99_9"], s["max"], s["mean"]) == (3, 3, 3, 3, 3.0)


def test_highly_skewed_distribution() -> None:
    # 99 zero-degree records and one record with degree 100.
    s = summarize([100], universe=100)
    # sum(i * x_i) = 100 * 100; G = (2*10000 - 101*100) / (100*100) = 0.99 = (N-1)/N, the maximum
    assert s["gini_coefficient"] == 0.99
    assert s["top_1_percent_candidate_share"] == 1.0
    assert (s["min"], s["p50"], s["p95"], s["p99"], s["p99_9"], s["max"]) == (0, 0, 0, 0, 100, 100)
    assert s["mean"] == 1.0


def test_all_zero_distribution() -> None:
    s = summarize([], universe=5)
    assert (s["min"], s["p50"], s["p95"], s["p99"], s["p99_9"], s["max"]) == (0, 0, 0, 0, 0, 0)
    assert s["mean"] == 0.0
    assert s["gini_coefficient"] is None  # 0 / 0: undefined, reported as null
    assert s["top_1_percent_candidate_share"] is None
    assert (s["records_in_universe"], s["records_with_candidates"], s["records_without_candidates"]) == (5, 0, 5)


def test_single_edge_distribution() -> None:
    s = summarize([1], universe=1)
    assert s["gini_coefficient"] == 0.0
    assert s["top_1_percent_candidate_share"] == 1.0
    assert (s["min"], s["max"], s["mean"]) == (1, 1, 1.0)


def test_zero_degree_records_change_statistics() -> None:
    # Degrees [0, 0, 1, 1, 2]: sum(i*x) = 3 + 4 + 10 = 17; G = (34 - 6*4) / (5*4) = 0.5
    s = summarize([1, 1, 2], universe=5)
    assert s["gini_coefficient"] == 0.5
    assert s["top_1_percent_candidate_share"] == 0.5
    assert (s["min"], s["p50"], s["mean"]) == (0, 1, 0.8)
    represented_only = summarize([1, 1, 2])
    # Degrees [1, 1, 2]: sum(i*x) = 1 + 2 + 6 = 9; G = (18 - 4*4) / (3*4) = 1/6
    assert represented_only["gini_coefficient"] == round(1 / 6, 8)
    assert represented_only["statistics_basis"] == "represented_records"
    assert represented_only["records_in_universe"] is None
    assert represented_only["records_without_candidates"] is None
    assert (represented_only["min"], represented_only["mean"]) == (1, round(4 / 3, 8))


def test_tied_degrees() -> None:
    # Degrees [1, 2, 2, 2]: sum(i*x) = 1 + 4 + 6 + 8 = 19; G = (38 - 5*7) / (4*7) = 3/28
    s = summarize([1, 2, 2, 2])
    assert s["gini_coefficient"] == round(3 / 28, 8)
    assert s["top_1_percent_candidate_share"] == round(2 / 7, 8)  # one of the tied 2s, whichever is chosen


@pytest.mark.parametrize(
    "population,expected_k",
    [(1, 1), (99, 1), (100, 1), (101, 2), (150, 2), (200, 2), (201, 3)],
)
def test_top_one_percent_rounds_up(population: int, expected_k: int) -> None:
    # population - 1 records of degree 1 plus one record of degree 1000.
    degrees = [1] * (population - 1) + [1000]
    s = summarize(degrees)
    total = sum(degrees)
    top = sorted(degrees, reverse=True)[:expected_k]
    assert s["top_1_percent_candidate_share"] == round(sum(top) / total, 8)


def test_empty_population() -> None:
    s = summarize([])
    assert set(s) == DIST_KEYS
    assert s["records_with_candidates"] == 0
    assert all(s[k] is None for k in ("min", "p50", "p95", "p99", "p99_9", "mean", "max", "gini_coefficient", "top_1_percent_candidate_share"))
    s0 = summarize([], universe=0)
    assert s0["records_in_universe"] == 0 and s0["records_without_candidates"] == 0 and s0["mean"] is None


def test_summary_matches_bruteforce_reference() -> None:
    rng = random.Random(20261124)
    checked = 0
    for _ in range(400):
        degrees = [max(1, int(rng.paretovariate(rng.choice([0.6, 1.1, 2.5])))) for _ in range(rng.randint(0, 70))]
        zeros = rng.choice([0, rng.randint(1, 50)])
        if not degrees and not zeros:
            continue
        got = summarize(degrees, universe=len(degrees) + zeros)
        ref = reference_summary(degrees + [0] * zeros)
        for key, value in ref.items():
            if isinstance(value, float):
                assert got[key] == pytest.approx(value, abs=1e-8), key
            else:
                assert got[key] == value, key
        assert 0.0 <= (got["gini_coefficient"] or 0.0) <= 1.0
        checked += 1
    assert checked > 350


def test_histogram_validation_errors() -> None:
    with pytest.raises(AuditError, match="ascending"):
        summarize_degree_histogram(iter([(2, 1), (1, 1)]), represented_records=2, total_degree=3, universe_records=None)
    with pytest.raises(AuditError, match="consistency"):
        summarize_degree_histogram(iter([(1, 2)]), represented_records=3, total_degree=2, universe_records=None)
    with pytest.raises(AuditError, match="smaller than"):
        summarize_degree_histogram(iter([(1, 2)]), represented_records=2, total_degree=2, universe_records=1)


def test_mean_degree_ratio() -> None:
    equal = compute_mean_degree_ratio(summarize([1, 1]), summarize([1, 1]))
    assert equal == 1.0
    # left: 5 edges over 6 records; right: 5 edges over 4 records -> (5/4)/(5/6) = 1.5
    assert compute_mean_degree_ratio(summarize([3, 1, 1], universe=6), summarize([3, 1, 1], universe=4)) == 1.5
    assert compute_mean_degree_ratio(summarize([], universe=3), summarize([], universe=3)) is None  # zero mean
    assert compute_mean_degree_ratio(summarize([], universe=0), summarize([], universe=0)) is None  # empty
    assert compute_mean_degree_ratio(summarize([]), summarize([])) is None


def test_record_coverage_fields() -> None:
    cross = {"left_records_represented": 3, "right_records_represented": 2}
    assert compute_record_coverage("cross", cross, 6, 2) == {
        "left_records_total": 6, "right_records_total": 2,
        "left_records_without_candidates": 3, "right_records_without_candidates": 0,
        "left_record_coverage": 0.5, "right_record_coverage": 1.0,
    }
    assert set(compute_record_coverage("cross", cross, None, None).values()) == {None}
    zero = compute_record_coverage("cross", {"left_records_represented": 0, "right_records_represented": 0}, 0, 0)
    assert zero["left_record_coverage"] is None and zero["left_records_without_candidates"] == 0
    third = compute_record_coverage("cross", {"left_records_represented": 1, "right_records_represented": 1}, 3, 3)
    assert third["left_record_coverage"] == 0.33333333  # 8 decimal places
    dedupe = {"records_represented": 5, "left_records_represented": 5, "right_records_represented": 5}
    assert compute_record_coverage("dedupe", dedupe, 7, None) == {
        "records_total": 7, "records_without_candidates": 2, "record_coverage": round(5 / 7, 8),
    }
    assert set(compute_record_coverage("dedupe", dedupe, None, None).values()) == {None}
    with pytest.raises(AuditError, match="smaller than"):
        compute_record_coverage("cross", cross, 2, 2)


# ---------------------------------------------------------------------------
# Record coverage through audit_candidates
# ---------------------------------------------------------------------------


def test_cross_coverage_with_totals(tmp_path: Path) -> None:
    report = audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS), left_records=6, right_records=4)
    cov = report["coverage"]
    assert cov["left_records_represented"] == 3 and cov["right_records_represented"] == 3
    assert (cov["left_records_total"], cov["left_records_without_candidates"], cov["left_record_coverage"]) == (6, 3, 0.5)
    assert (cov["right_records_total"], cov["right_records_without_candidates"], cov["right_record_coverage"]) == (4, 1, 0.75)


@pytest.mark.parametrize(
    "left_total,right_total,expected",
    [
        (3, 3, (1.0, 1.0)),  # full coverage on both sides
        (6, 3, (0.5, 1.0)),  # partial source coverage
        (3, 6, (1.0, 0.5)),  # partial target coverage
    ],
)
def test_cross_partial_and_full_coverage(tmp_path: Path, left_total: int, right_total: int, expected: tuple[float, float]) -> None:
    report = audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS), left_records=left_total, right_records=right_total)
    cov = report["coverage"]
    assert (cov["left_record_coverage"], cov["right_record_coverage"]) == expected
    deg = report["degree_analysis"]
    assert deg["left"]["records_without_candidates"] == left_total - 3
    assert deg["right"]["records_without_candidates"] == right_total - 3
    if left_total == 3:
        assert deg["left"]["min"] >= 1


def test_cross_zero_records_and_zero_pairs(tmp_path: Path) -> None:
    report = audit(write_tsv(tmp_path / "empty.tsv", []), left_records=0, right_records=0)
    cov = report["coverage"]
    assert (cov["left_records_total"], cov["left_records_without_candidates"], cov["left_record_coverage"]) == (0, 0, None)
    assert (cov["right_records_total"], cov["right_records_without_candidates"], cov["right_record_coverage"]) == (0, 0, None)
    deg = report["degree_analysis"]
    for side in ("left", "right"):
        assert deg[side]["records_in_universe"] == 0
        assert all(deg[side][k] is None for k in ("min", "p50", "p99_9", "max", "mean", "gini_coefficient", "top_1_percent_candidate_share"))
    assert deg["right_to_left_mean_degree_ratio"] is None
    assert report["efficiency"]["reduction_ratio"] is None


def test_cross_zero_pairs_with_positive_totals(tmp_path: Path) -> None:
    report = audit(write_tsv(tmp_path / "empty.tsv", []), left_records=4, right_records=2)
    assert report["coverage"]["left_record_coverage"] == 0.0
    left = report["degree_analysis"]["left"]
    assert (left["min"], left["max"], left["mean"], left["gini_coefficient"]) == (0, 0, 0.0, None)
    assert report["degree_analysis"]["right_to_left_mean_degree_ratio"] is None


def test_cross_missing_totals(tmp_path: Path) -> None:
    report = audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS))
    cov = report["coverage"]
    for key in ("left_records_total", "right_records_total", "left_records_without_candidates",
                "right_records_without_candidates", "left_record_coverage", "right_record_coverage"):
        assert key in cov and cov[key] is None
    for side in ("left", "right"):
        dist = report["degree_analysis"][side]
        assert dist["statistics_basis"] == "represented_records"
        assert dist["records_in_universe"] is None and dist["records_without_candidates"] is None
        assert dist["records_with_candidates"] == 3


@pytest.mark.parametrize("left_total,right_total,which", [(2, 10, "left"), (10, 2, "right")])
def test_cross_totals_smaller_than_represented_keep_existing_error(
    tmp_path: Path, left_total: int, right_total: int, which: str
) -> None:
    with pytest.raises(AuditError, match=rf"Supplied --{which}-records=2 conflicts with candidate data"):
        audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS), left_records=left_total, right_records=right_total)


def test_dedupe_coverage(tmp_path: Path) -> None:
    path = write_tsv(tmp_path / "d.tsv", DEDUPE_ROWS)
    full = audit(path, "dedupe", left_records=5)
    assert (full["coverage"]["records_total"], full["coverage"]["records_without_candidates"], full["coverage"]["record_coverage"]) == (5, 0, 1.0)
    partial = audit(path, "dedupe", left_records=7)
    assert (partial["coverage"]["records_without_candidates"], partial["coverage"]["record_coverage"]) == (2, round(5 / 7, 8))
    missing = audit(path, "dedupe")
    assert {k: missing["coverage"][k] for k in ("records_total", "records_without_candidates", "record_coverage")} == {
        "records_total": None, "records_without_candidates": None, "record_coverage": None,
    }
    assert "left_records_total" not in missing["coverage"]
    with pytest.raises(AuditError, match="Supplied --left-records=4 conflicts with candidate data"):
        audit(path, "dedupe", left_records=4)


def test_dedupe_zero_record_dataset(tmp_path: Path) -> None:
    report = audit(write_tsv(tmp_path / "empty.tsv", []), "dedupe", left_records=0)
    assert (report["coverage"]["records_total"], report["coverage"]["record_coverage"]) == (0, None)
    dist = report["degree_analysis"]["records"]
    assert dist["records_in_universe"] == 0 and dist["mean"] is None and dist["gini_coefficient"] is None


# ---------------------------------------------------------------------------
# Degree distributions through audit_candidates
# ---------------------------------------------------------------------------


def test_cross_degree_distributions_with_totals(tmp_path: Path) -> None:
    report = audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS), left_records=6, right_records=4)
    deg = report["degree_analysis"]
    assert deg["linkage_type"] == "cross"
    assert set(deg) == {"linkage_type", "left", "right", "right_to_left_mean_degree_ratio"}
    left, right = deg["left"], deg["right"]
    assert set(left) == DIST_KEYS and set(right) == DIST_KEYS
    # Left degrees with zeros: [0, 0, 0, 1, 1, 3]; duplicates of L1-R1 and L2-R1 add nothing.
    assert left == {
        "statistics_basis": "record_universe", "records_in_universe": 6, "records_with_candidates": 3,
        "records_without_candidates": 3, "total_degree": 5,
        "min": 0, "p50": 0, "p95": 3, "p99": 3, "p99_9": 3, "mean": round(5 / 6, 8), "max": 3,
        # sum(i*x) = 4 + 5 + 18 = 27; G = (54 - 7*5) / (6*5) = 19/30
        "gini_coefficient": round(19 / 30, 8),
        "top_1_percent_candidate_share": 0.6,  # k = 1 -> 3 / 5
    }
    # Right degrees with zeros: [0, 1, 1, 3] (R1 has L1, L2, L3).
    assert right == {
        "statistics_basis": "record_universe", "records_in_universe": 4, "records_with_candidates": 3,
        "records_without_candidates": 1, "total_degree": 5,
        "min": 0, "p50": 1, "p95": 3, "p99": 3, "p99_9": 3, "mean": 1.25, "max": 3,
        # sum(i*x) = 2 + 3 + 12 = 17; G = (34 - 5*5) / (4*5) = 9/20
        "gini_coefficient": 0.45,
        "top_1_percent_candidate_share": 0.6,
    }
    assert deg["right_to_left_mean_degree_ratio"] == 1.5  # 1.25 / (5/6)


def test_cross_left_and_right_distributions_differ(tmp_path: Path) -> None:
    # One source fans out to 4 targets; each target has a single source.
    rows = [("S", f"T{i}") for i in range(4)]
    deg = audit(write_tsv(tmp_path / "c.tsv", rows))["degree_analysis"]
    assert (deg["left"]["max"], deg["left"]["records_with_candidates"], deg["left"]["mean"]) == (4, 1, 4.0)
    assert (deg["right"]["max"], deg["right"]["records_with_candidates"], deg["right"]["mean"]) == (1, 4, 1.0)
    assert deg["right_to_left_mean_degree_ratio"] == 0.25


def test_duplicate_rows_do_not_increase_degree(tmp_path: Path) -> None:
    unique = audit(write_tsv(tmp_path / "u.tsv", [("L1", "R1"), ("L1", "R2")]), left_records=2, right_records=2)
    dupes = audit(write_tsv(tmp_path / "d.tsv", [("L1", "R1")] * 5 + [("L1", "R2")] * 3), left_records=2, right_records=2)
    assert dupes["inputs"]["duplicate_candidate_rows"] == 6
    assert dupes["degree_analysis"] == unique["degree_analysis"]


def test_represented_basis_matches_legacy_distribution(tmp_path: Path) -> None:
    rng = random.Random(5)
    rows = [(f"L{rng.randint(0, 40)}", f"R{rng.randint(0, 60)}") for _ in range(400)]
    report = audit(write_tsv(tmp_path / "c.tsv", rows))
    legacy = report["candidate_distribution_per_left_record"]
    left = report["degree_analysis"]["left"]
    for key in ("min", "p50", "p95", "p99", "max"):
        assert left[key] == legacy[key], key
    assert left["records_with_candidates"] == legacy["sources_with_candidates"]
    assert round(left["mean"], 4) == legacy["mean"]


def test_cross_degrees_match_bruteforce_reference(tmp_path: Path) -> None:
    rng = random.Random(11)
    rows = [(f"L{int(rng.paretovariate(1.1))}", f"R{rng.randint(0, 80)}") for _ in range(600)]
    unique = set(rows)
    left_deg = Counter(left for left, _ in unique)
    right_deg = Counter(right for _, right in unique)
    left_total, right_total = len(left_deg) + 17, len(right_deg) + 9
    deg = audit(write_tsv(tmp_path / "c.tsv", rows), left_records=left_total, right_records=right_total)["degree_analysis"]
    for side, counts, total in (("left", left_deg, left_total), ("right", right_deg, right_total)):
        ref = reference_summary(list(counts.values()) + [0] * (total - len(counts)))
        for key, value in ref.items():
            assert deg[side][key] == pytest.approx(value, abs=1e-8), (side, key)


def test_dedupe_degree_distribution(tmp_path: Path) -> None:
    report = audit(write_tsv(tmp_path / "d.tsv", DEDUPE_ROWS), "dedupe", left_records=7)
    deg = report["degree_analysis"]
    assert set(deg) == {"linkage_type", "records"}
    # Degrees counting both endpoints: a=2 (b, c), b=1, c=1, d=1, e=1, plus 2 zero-degree records.
    # [0, 0, 1, 1, 1, 1, 2]: sum(i*x) = 3+4+5+6+14 = 32; G = (64 - 8*6) / (7*6) = 8/21
    assert deg["records"] == {
        "statistics_basis": "record_universe", "records_in_universe": 7, "records_with_candidates": 5,
        "records_without_candidates": 2, "total_degree": 6,
        "min": 0, "p50": 1, "p95": 2, "p99": 2, "p99_9": 2, "mean": round(6 / 7, 8), "max": 2,
        "gini_coefficient": round(8 / 21, 8),
        "top_1_percent_candidate_share": round(2 / 6, 8),
    }


def test_dedupe_counts_both_endpoints_and_ignores_reversed_duplicates(tmp_path: Path) -> None:
    star = audit(write_tsv(tmp_path / "s.tsv", [("hub", f"n{i}") for i in range(5)]), "dedupe")
    dist = star["degree_analysis"]["records"]
    assert (dist["records_with_candidates"], dist["total_degree"], dist["max"], dist["min"]) == (6, 10, 5, 1)
    reversed_dupes = audit(
        write_tsv(tmp_path / "r.tsv", [("hub", f"n{i}") for i in range(5)] + [(f"n{i}", "hub") for i in range(5)]),
        "dedupe",
    )
    assert reversed_dupes["inputs"]["duplicate_candidate_rows"] == 5
    assert reversed_dupes["degree_analysis"] == star["degree_analysis"]


def test_dedupe_degrees_match_bruteforce_reference(tmp_path: Path) -> None:
    rng = random.Random(23)
    rows = []
    for _ in range(500):
        a, b = rng.randint(0, 120), int(rng.paretovariate(0.9))
        if a != b:
            rows.append((f"E{a}", f"E{b}"))
    edges = {tuple(sorted(r)) for r in rows}
    degrees = Counter(x for edge in edges for x in edge)
    total = len(degrees) + 33
    dist = audit(write_tsv(tmp_path / "d.tsv", rows), "dedupe", left_records=total)["degree_analysis"]["records"]
    assert dist["total_degree"] == 2 * len(edges)
    ref = reference_summary(list(degrees.values()) + [0] * (total - len(degrees)))
    for key, value in ref.items():
        assert dist[key] == pytest.approx(value, abs=1e-8), key


# ---------------------------------------------------------------------------
# Backward compatibility (golden values produced by main @ 87d8cf0)
# ---------------------------------------------------------------------------

LEGACY_GOLDEN: dict[str, dict[str, Any]] = {
    "cross_totals": json.loads(
        '{"candidate_distribution_per_left_record": {"max": 3, "mean": 1.6667, "min": 1, "p50": 1, "p95": 3, "p99": 3, "sources_with_candidates": 3}, "coverage": {"left_records_represented": 3, "right_records_represented": 3}, "efficiency": {"linkage_type": "cross", "possible_pairs": 24, "reduction_ratio": 0.79166667}, "ground_truth": {"candidate_recall": 0.5, "fingerprint": "sha256:ba924f1f6576b10a86fc7058f5527de37d2fb996ce46b10afc5b8fd670e319e4", "fingerprint_version": "v2", "missed_pairs": 1, "recovered_pairs": 1, "rows": 2, "unique_pairs": 2}, "inputs": {"candidate_rows": 8, "candidates": null, "duplicate_candidate_rows": 3, "truth": null, "unique_candidate_pairs": 5}, "quality_gate": {"failures": [], "passed": true}, "schema_version": "1.0", "tool": "candguard"}'
    ),
    "cross_no_totals": json.loads(
        '{"candidate_distribution_per_left_record": {"max": 3, "mean": 1.6667, "min": 1, "p50": 1, "p95": 3, "p99": 3, "sources_with_candidates": 3}, "coverage": {"left_records_represented": 3, "right_records_represented": 3}, "efficiency": {"linkage_type": "cross", "possible_pairs": null, "reduction_ratio": null}, "ground_truth": null, "inputs": {"candidate_rows": 8, "candidates": null, "duplicate_candidate_rows": 3, "truth": null, "unique_candidate_pairs": 5}, "quality_gate": {"failures": [], "passed": true}, "schema_version": "1.0", "tool": "candguard"}'
    ),
    "dedupe_totals": json.loads(
        '{"candidate_distribution_per_left_record": {"max": 2, "mean": 1.2, "min": 1, "p50": 1, "p95": 2, "p99": 2, "sources_with_candidates": 5}, "coverage": {"left_records_represented": 5, "records_represented": 5, "right_records_represented": 5}, "efficiency": {"linkage_type": "dedupe", "possible_pairs": 21, "reduction_ratio": 0.85714286}, "ground_truth": {"candidate_recall": 0.5, "fingerprint": "sha256:22f37ecdc11049997dddc35ac80687922ba1e96824afb7f1ab5556825504720b", "fingerprint_version": "v2", "missed_pairs": 1, "recovered_pairs": 1, "rows": 2, "unique_pairs": 2}, "inputs": {"candidate_rows": 5, "candidates": null, "duplicate_candidate_rows": 2, "truth": null, "unique_candidate_pairs": 3}, "quality_gate": {"failures": [], "passed": true}, "schema_version": "1.0", "tool": "candguard"}'
    ),
}

LEGACY_TEXT_PREFIX = {
    "cross_totals": [
        "Candidate rows:                 8",
        "Unique candidate pairs:         5",
        "Duplicate candidate rows:       3",
        "Source records represented:     3",
        "Target records represented:     3",
        "",
        "Candidates per represented source record",
        "-----------------------------------------",
        "Min / median / mean:             1 / 1 / 1.6667",
        "P95 / P99 / max:                 3 / 3 / 3",
        "",
        "Search-space efficiency",
        "-----------------------",
        "Possible pairs:                  24",
        "Reduction ratio:                 79.1667%",
        "",
        "Ground-truth audit",
        "------------------",
        "Unique known matching pairs:     2",
        "Recovered by candidate set:      1",
        "Missing before model scoring:    1",
        "Candidate recall:                50.0000%",
    ],
    "dedupe_totals": [
        "Candidate rows:                 5",
        "Unique candidate pairs:         3",
        "Duplicate candidate rows:       2",
        "Records represented:            5",
        "",
        "Candidates per represented record",
        "---------------------------------",
        "Min / median / mean:             1 / 1 / 1.2",
        "P95 / P99 / max:                 2 / 2 / 2",
        "",
        "Search-space efficiency",
        "-----------------------",
        "Possible pairs:                  21",
        "Reduction ratio:                 85.7143%",
        "",
        "Ground-truth audit",
        "------------------",
        "Unique known matching pairs:     2",
        "Recovered by candidate set:      1",
        "Missing before model scoring:    1",
        "Candidate recall:                50.0000%",
    ],
}


def _golden_inputs(tmp_path: Path) -> dict[str, dict[str, Any]]:
    cross = write_tsv(tmp_path / "cross.tsv", CROSS_ROWS)
    cross_truth = write_tsv(tmp_path / "cross_truth.tsv", [("L1", "R1"), ("L3", "R9")])
    dedupe = write_tsv(tmp_path / "dedupe.tsv", DEDUPE_ROWS, header=("a", "b"))
    dedupe_truth = write_tsv(tmp_path / "dedupe_truth.tsv", [("c", "a"), ("f", "g")], header=("a", "b"))
    return {
        "cross_totals": dict(candidates_path=cross, truth_path=cross_truth, left_id="source_id", right_id="candidate_id", left_records=6, right_records=4),
        "cross_no_totals": dict(candidates_path=cross, left_id="source_id", right_id="candidate_id"),
        "dedupe_totals": dict(candidates_path=dedupe, truth_path=dedupe_truth, left_id="a", right_id="b", linkage_type="dedupe", left_records=7),
    }


@pytest.mark.parametrize("case", sorted(LEGACY_GOLDEN))
def test_existing_report_fields_unchanged(tmp_path: Path, case: str) -> None:
    report = audit_candidates(**_golden_inputs(tmp_path)[case])
    golden = LEGACY_GOLDEN[case]
    assert report["schema_version"] == "1.0"
    assert set(golden) | {"version", "degree_analysis"} == set(report)
    for section, expected in golden.items():
        if section == "inputs":
            for key, value in expected.items():
                if key not in ("candidates", "truth"):
                    assert report["inputs"][key] == value, key
        elif isinstance(expected, dict):
            for key, value in expected.items():
                assert report[section][key] == value, (section, key)
        else:
            assert report[section] == expected, section
    # Only additive keys appear in pre-existing sections.
    assert set(report["candidate_distribution_per_left_record"]) == set(golden["candidate_distribution_per_left_record"])
    assert set(report["efficiency"]) == set(golden["efficiency"])


@pytest.mark.parametrize("case", sorted(LEGACY_TEXT_PREFIX))
def test_existing_text_sections_unchanged(tmp_path: Path, case: str) -> None:
    lines = render_text(audit_candidates(**_golden_inputs(tmp_path)[case])).splitlines()
    expected = LEGACY_TEXT_PREFIX[case]
    assert lines[2 : 2 + len(expected)] == expected
    advanced = lines[2 + len(expected) :]
    assert advanced[0] == ""
    assert advanced[1].startswith("Advanced degree diagnostics")
    assert advanced[-1] == "Quality gate: PASS"


def test_text_output_cross_sections(tmp_path: Path) -> None:
    text = render_text(audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS), left_records=6, right_records=4))
    assert "Source            Target" in text
    assert "Record coverage:                 50.0000%          75.0000%" in text
    assert "Records without candidates:      3                 1" in text
    assert "Min / P50 / mean:                0 / 0 / 0.8333    0 / 1 / 1.2500" in text
    assert "P95 / P99 / P99.9 / max:         3 / 3 / 3 / 3     3 / 3 / 3 / 3" in text
    assert "Gini coefficient:                0.6333            0.4500" in text
    assert "Top 1% candidate share:          60.0000%          60.0000%" in text
    assert "Target/source mean degree ratio: 1.5000" in text
    assert "not supplied" not in text


def test_text_output_notes_missing_totals(tmp_path: Path) -> None:
    cross = render_text(audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS)))
    assert "Record coverage:                 n/a               n/a" in cross
    assert "statistics cover only records represented in candidate output" in cross
    dedupe = render_text(audit(write_tsv(tmp_path / "d.tsv", DEDUPE_ROWS), "dedupe"))
    assert "Record coverage:                 n/a (record total not supplied)" in dedupe
    assert "statistics cover only records represented in candidate output" in dedupe


def test_text_output_dedupe(tmp_path: Path) -> None:
    text = render_text(audit(write_tsv(tmp_path / "d.tsv", DEDUPE_ROWS), "dedupe", left_records=7))
    assert "Advanced degree diagnostics (unique pairs per record, both endpoints)" in text
    assert "Record coverage:                 71.4286% (5 of 7 records; 2 without candidates)" in text
    assert "Gini coefficient:                0.3810" in text
    assert "Top 1% candidate share:          33.3333%" in text


def test_render_text_tolerates_reports_without_degree_analysis(tmp_path: Path) -> None:
    report = audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS))
    del report["degree_analysis"]
    assert "Advanced degree diagnostics" not in render_text(report)


def test_compare_accepts_old_and_new_reports(tmp_path: Path) -> None:
    new = audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS), left_records=6, right_records=4)
    old = json.loads(json.dumps(new))
    del old["degree_analysis"]
    for key in list(old["coverage"]):
        if key not in ("left_records_represented", "right_records_represented"):
            del old["coverage"][key]
    (tmp_path / "old.json").write_text(json.dumps(old), encoding="utf-8")
    (tmp_path / "new.json").write_text(json.dumps(new), encoding="utf-8")
    for base, curr in (("old", "new"), ("new", "old"), ("new", "new")):
        result = compare_reports(baseline_path=tmp_path / f"{base}.json", current_path=tmp_path / f"{curr}.json", max_pair_growth=0.0)
        assert result["quality_gate"]["passed"] is True


def test_cli_exit_codes_with_totals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = str(write_tsv(tmp_path / "c.tsv", CROSS_ROWS))
    base = ["audit", "--candidates", path, "--left-id", "source_id", "--right-id", "candidate_id"]
    assert main(base + ["--left-records", "6", "--right-records", "4"]) == 0
    assert main(base + ["--left-records", "6", "--right-records", "4", "--max-duplicate-rows", "0"]) == 1
    assert main(base + ["--left-records", "2", "--right-records", "4"]) == 2
    assert main(base + ["--left-records", "6"]) == 2
    out = tmp_path / "r.json"
    assert main(base + ["--left-records", "6", "--right-records", "4", "--format", "json", "--output", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0"
    assert data["degree_analysis"]["left"]["gini_coefficient"] == round(19 / 30, 8)
    capsys.readouterr()


def test_new_metrics_are_not_quality_gates(tmp_path: Path) -> None:
    # Maximal concentration: one source holds every pair among many zero-degree sources.
    rows = [("hub", f"T{i}") for i in range(50)]
    report = audit(write_tsv(tmp_path / "c.tsv", rows), left_records=1000, right_records=50)
    assert report["degree_analysis"]["left"]["gini_coefficient"] == 0.999
    assert report["quality_gate"] == {"passed": True, "failures": []}


# ---------------------------------------------------------------------------
# Format parity (optional Parquet)
# ---------------------------------------------------------------------------


@needs_pyarrow
@pytest.mark.parametrize("linkage_type,rows,totals", [
    ("cross", CROSS_ROWS, {"left_records": 6, "right_records": 4}),
    ("dedupe", DEDUPE_ROWS, {"left_records": 7}),
])
def test_degree_analysis_parity_across_formats(tmp_path: Path, linkage_type: str, rows: list[tuple[str, str]], totals: dict[str, int]) -> None:
    tsv = write_tsv(tmp_path / "c.tsv", rows)
    csv_path = tmp_path / "c.csv"
    csv_path.write_text("\n".join(",".join(r) for r in [("source_id", "candidate_id"), *rows]) + "\n", encoding="utf-8")
    parquet = tmp_path / "c.parquet"
    pq.write_table(pa.table({"source_id": [r[0] for r in rows], "candidate_id": [r[1] for r in rows]}), parquet)
    reports = [audit(p, linkage_type, **totals) for p in (tsv, csv_path, parquet)]
    for report in reports[1:]:
        assert report["degree_analysis"] == reports[0]["degree_analysis"]
        assert report["coverage"] == reports[0]["coverage"]


# ---------------------------------------------------------------------------
# Resource behavior
# ---------------------------------------------------------------------------


def test_temp_files_removed_after_success(tmp_path: Path, sqlite_tmp: Path) -> None:
    audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS), left_records=6, right_records=4)
    audit(write_tsv(tmp_path / "d.tsv", DEDUPE_ROWS), "dedupe", left_records=7)
    assert list(sqlite_tmp.iterdir()) == []


def test_temp_files_removed_after_input_failure(tmp_path: Path, sqlite_tmp: Path) -> None:
    bad = tmp_path / "bad.tsv"
    bad.write_text("source_id\tcandidate_id\nL1\tR1\nL2\n", encoding="utf-8")
    with pytest.raises(AuditError):
        audit(bad, left_records=6, right_records=4)
    assert list(sqlite_tmp.iterdir()) == []


def test_temp_files_removed_after_failure_during_degree_analysis(
    tmp_path: Path, sqlite_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AuditError("simulated failure inside degree analysis")

    monkeypatch.setattr("candguard.storage.summarize_degree_histogram", fail)
    with pytest.raises(AuditError, match="simulated failure"):
        audit(write_tsv(tmp_path / "c.tsv", CROSS_ROWS), left_records=6, right_records=4)
    assert list(sqlite_tmp.iterdir()) == []


def _engine_with_pairs(pairs: list[tuple[str, str]], linkage_type: str) -> SQLiteStorageEngine:
    class _Reader:
        path = Path("synthetic")
        left_col = "l"
        right_col = "r"

        def iter_batches(self, batch_size: int = 50_000) -> Any:
            for start in range(0, len(pairs), batch_size):
                yield pairs[start : start + batch_size]

    engine = SQLiteStorageEngine()
    engine.ingest_candidates(_Reader(), linkage_type=linkage_type)  # type: ignore[arg-type]
    engine.compute_coverage_and_distribution(linkage_type)
    return engine


@pytest.mark.parametrize("linkage_type", ["cross", "dedupe"])
def test_degrees_are_streamed_as_histogram_not_materialized(monkeypatch: pytest.MonkeyPatch, linkage_type: str) -> None:
    # 3,000 records with degrees in {1, 2, 3}: the histogram has at most 3 rows per side.
    pairs = [(f"L{i:05d}", f"R{i:05d}-{j}") for i in range(3000) for j in range(1 + i % 3)]
    seen: list[tuple[str, int]] = []
    from candguard import storage

    original = storage.summarize_degree_histogram

    def spy(histogram: Any, **kwargs: Any) -> Any:
        assert isinstance(histogram, sqlite3.Cursor), "histogram must be streamed from SQLite, not a Python list"
        rows = 0

        def counting() -> Any:
            nonlocal rows
            for row in histogram:
                rows += 1
                yield row

        result = original(counting(), **kwargs)
        seen.append((kwargs["represented_records"], rows))
        return result

    monkeypatch.setattr(storage, "summarize_degree_histogram", spy)
    with _engine_with_pairs(pairs, linkage_type) as engine:
        engine.compute_degree_analysis(linkage_type, left_records=None, right_records=None)
    assert seen
    for represented, histogram_rows in seen:
        assert represented >= 3000
        assert histogram_rows <= 6  # distinct degree values, never one row per record


@pytest.mark.parametrize("linkage_type", ["cross", "dedupe"])
def test_sql_statement_count_is_independent_of_size(linkage_type: str) -> None:
    def statements_for(n: int) -> int:
        rng = random.Random(n)
        pairs = list({(f"E{rng.randint(0, n)}", f"E{rng.randint(n + 1, 3 * n)}") for _ in range(n * 4)})
        with _engine_with_pairs(pairs, linkage_type) as engine:
            executed: list[str] = []
            engine.connection.set_trace_callback(executed.append)
            engine.compute_degree_analysis(linkage_type, left_records=None, right_records=None)
            engine.connection.set_trace_callback(None)
            return len(executed)

    assert statements_for(200) == statements_for(5000)


def test_larger_synthetic_dataset_matches_reference(tmp_path: Path) -> None:
    # ~120k unique pairs with a heavy-tailed target side; verifies correctness at scale.
    rng = random.Random(99)
    rows = []
    for i in range(30_000):
        for j in range(1 + int(rng.paretovariate(1.3)) % 7):
            rows.append((f"L{i}", f"R{(j * j * 7919 + i % (j + 1)) % 5000}"))
    unique = set(rows)
    left_deg = Counter(left for left, _ in unique)
    right_deg = Counter(right for _, right in unique)
    path = write_tsv(tmp_path / "big.tsv", rows)
    deg = audit(path, left_records=len(left_deg) + 1000, right_records=6000)["degree_analysis"]
    for side, counts, total in (("left", left_deg, len(left_deg) + 1000), ("right", right_deg, 6000)):
        xs = sorted(list(counts.values()) + [0] * (total - len(counts)))
        n, s = len(xs), sum(xs)
        # Gini via the Lorenz-curve area (independent of the rank-weighted formula used in code).
        cumulative, area = 0, 0.0
        for x in xs:
            area += (2 * cumulative + x) / (2 * s)
            cumulative += x
        gini = 1 - 2 * area / n
        assert deg[side]["gini_coefficient"] == pytest.approx(gini, abs=1e-8), side
        assert deg[side]["p99_9"] == xs[math.ceil(0.999 * n) - 1]
        assert deg[side]["top_1_percent_candidate_share"] == pytest.approx(sum(xs[-math.ceil(n / 100):]) / s, abs=1e-8)
        assert deg[side]["mean"] == round(s / n, 8)

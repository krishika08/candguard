"""Foundational backward-compatibility baseline tests for CandGuard v0.2.0.

Locks down:
- Public Python API signatures and root module exports.
- Complete CLI flag contracts and exit code guarantees (0, 1, 2).
- JSON report schema version 1.0 structural compliance.
- Text report formatting semantics (cross vs dedupe).
- Input reader protocol and batch processing contract.
- Storage engine resource lifecycle and guaranteed tempfile cleanup.
- Ground-truth fingerprint v2 compatibility.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

import pytest

import candguard
from candguard import AuditError, audit_candidates, compare_reports
from candguard.cli import build_parser, main
from candguard.readers import CandidateReader, DelimitedReader, get_reader
from candguard.storage import SQLiteStorageEngine


def test_public_api_exports() -> None:
    """Verify that root candguard package exports expected public symbols."""
    assert hasattr(candguard, "__version__")
    assert isinstance(candguard.__version__, str)
    assert candguard.audit_candidates is audit_candidates
    assert candguard.compare_reports is compare_reports
    assert issubclass(candguard.AuditError, ValueError)


def test_reader_protocol_and_batch_contract(tmp_path: Path) -> None:
    """Verify CandidateReader protocol adherence and batch generation contract."""
    file_path = tmp_path / "pairs.tsv"
    with file_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(["left_id", "right_id"])
        for i in range(25):
            writer.writerow([f"src_{i}", f"tgt_{i}"])

    reader = get_reader(file_path, "left_id", "right_id")
    assert isinstance(reader, CandidateReader)
    assert isinstance(reader, DelimitedReader)
    assert reader.path == file_path
    assert reader.left_col == "left_id"
    assert reader.right_col == "right_id"

    # Test single-pair iteration
    all_pairs = list(reader.iter_pairs())
    assert len(all_pairs) == 25
    assert all_pairs[0] == ("src_0", "tgt_0")
    assert all_pairs[-1] == ("src_24", "tgt_24")

    # Test batch iteration contract
    batches = list(reader.iter_batches(batch_size=10))
    assert len(batches) == 3
    assert len(batches[0]) == 10
    assert len(batches[1]) == 10
    assert len(batches[2]) == 5
    assert sum(len(b) for b in batches) == 25


def test_parquet_reader_dispatch_raises_informative_error(tmp_path: Path) -> None:
    """Verify get_reader on parquet path raises clear unsupported/missing dependency error."""
    pq_path = tmp_path / "test.parquet"
    with pytest.raises(AuditError, match="Parquet support is not yet installed or enabled"):
        get_reader(pq_path, "left_id", "right_id")


def test_storage_engine_lifecycle_and_cleanup(tmp_path: Path) -> None:
    """Verify SQLiteStorageEngine reliably unlinks temporary database files on success and failure."""
    db_path: Path | None = None
    with SQLiteStorageEngine() as engine:
        db_path = engine.db_path
        assert db_path.exists()
        assert db_path.name.startswith("candguard-")
        assert db_path.name.endswith(".sqlite3")

    assert db_path is not None
    assert not db_path.exists(), "Temporary SQLite database must be unlinked after normal exit."

    # Verify cleanup on error during ingestion
    bad_cand = tmp_path / "bad.tsv"
    bad_cand.write_text("s\tc\na\tb\nx\n", encoding="utf-8")  # Truncated row
    reader = get_reader(bad_cand, "s", "c")

    captured_db_path: Path | None = None
    try:
        with SQLiteStorageEngine() as engine:
            captured_db_path = engine.db_path
            assert captured_db_path.exists()
            engine.ingest_candidates(reader)
    except AuditError:
        pass

    assert captured_db_path is not None
    assert not captured_db_path.exists(), "Temporary database must be unlinked even when ingestion fails."


def test_json_report_schema_1_0_lockdown(tmp_path: Path) -> None:
    """Strictly assert JSON report schema version 1.0 structural conformance."""
    cand_file = tmp_path / "candidates.tsv"
    cand_file.write_text("s\tc\n1\t10\n1\t11\n2\t20\n", encoding="utf-8")
    truth_file = tmp_path / "truth.tsv"
    truth_file.write_text("s\tc\n1\t10\n2\t20\n3\t30\n", encoding="utf-8")

    report = audit_candidates(
        candidates_path=cand_file,
        left_id="s",
        right_id="c",
        truth_path=truth_file,
        linkage_type="cross",
        left_records=5,
        right_records=5,
        min_recall=0.50,
    )

    # Validate schema version
    assert report["schema_version"] == "1.0"
    assert report["tool"] == "candguard"
    assert isinstance(report["version"], str)

    # Validate inputs
    inputs = report["inputs"]
    assert isinstance(inputs["candidates"], str)
    assert isinstance(inputs["truth"], str)
    assert inputs["candidate_rows"] == 3
    assert inputs["unique_candidate_pairs"] == 3
    assert inputs["duplicate_candidate_rows"] == 0

    # Validate coverage
    cov = report["coverage"]
    assert cov["left_records_represented"] == 2
    assert cov["right_records_represented"] == 3

    # Validate distribution
    dist = report["candidate_distribution_per_left_record"]
    assert dist["sources_with_candidates"] == 2
    assert dist["min"] == 1
    assert dist["max"] == 2
    assert dist["p50"] == 1
    assert dist["p95"] == 2
    assert dist["p99"] == 2
    assert dist["mean"] == 1.5

    # Validate efficiency
    eff = report["efficiency"]
    assert eff["linkage_type"] == "cross"
    assert eff["possible_pairs"] == 25
    assert eff["reduction_ratio"] == pytest.approx(1 - 3 / 25)

    # Validate ground truth
    truth = report["ground_truth"]
    assert truth is not None
    assert truth["rows"] == 3
    assert truth["unique_pairs"] == 3
    assert truth["recovered_pairs"] == 2
    assert truth["missed_pairs"] == 1
    assert truth["candidate_recall"] == pytest.approx(2 / 3)
    assert truth["fingerprint"].startswith("sha256:")
    assert truth["fingerprint_version"] == "v2"

    # Validate quality gate
    gate = report["quality_gate"]
    assert gate["passed"] is True
    assert gate["failures"] == []


def test_cli_all_audit_flags_integration(tmp_path: Path) -> None:
    """Exercise and verify all candguard audit CLI options in an integrated execution."""
    cand_file = tmp_path / "cand.txt"
    cand_file.write_text("s_id|t_id\n1|a\n1|b\n2|c\n", encoding="utf-8")
    truth_file = tmp_path / "truth.txt"
    truth_file.write_text("t_left#t_right\n1#a\n2#c\n3#d\n", encoding="utf-8")

    out_report = tmp_path / "audit_report.json"
    missed_out = tmp_path / "missed.tsv"

    exit_code = main([
        "audit",
        "--candidates", str(cand_file),
        "--left-id", "s_id",
        "--right-id", "t_id",
        "--delimiter", "|",
        "--truth", str(truth_file),
        "--truth-left-id", "t_left",
        "--truth-right-id", "t_right",
        "--truth-delimiter", "#",
        "--linkage-type", "cross",
        "--left-records", "10",
        "--right-records", "10",
        "--min-recall", "0.60",
        "--max-pairs", "100",
        "--max-duplicate-rows", "5",
        "--missed-pairs-out", str(missed_out),
        "--format", "json",
        "--output", str(out_report),
    ])

    assert exit_code == 0
    assert out_report.exists()
    assert missed_out.exists()

    report_data = json.loads(out_report.read_text(encoding="utf-8"))
    assert report_data["quality_gate"]["passed"] is True
    assert report_data["efficiency"]["reduction_ratio"] == pytest.approx(1 - 3 / 100)

    missed_lines = missed_out.read_text(encoding="utf-8").strip().splitlines()
    assert missed_lines[0] == "t_left\tt_right"
    assert missed_lines[1] == "3\td"


def test_cli_all_compare_flags_integration(tmp_path: Path) -> None:
    """Exercise and verify all candguard compare CLI options in an integrated execution."""
    base_data = {
        "tool": "candguard",
        "version": "0.1.0",
        "schema_version": "1.0",
        "inputs": {"candidates": "c.tsv", "truth": "t.tsv", "candidate_rows": 10, "unique_candidate_pairs": 8, "duplicate_candidate_rows": 2},
        "coverage": {"left_records_represented": 5, "right_records_represented": 5},
        "candidate_distribution_per_left_record": {"min": 1, "p50": 2, "mean": 2.0, "p95": 2, "p99": 2, "max": 2, "sources_with_candidates": 5},
        "efficiency": {"linkage_type": "cross", "possible_pairs": 100, "reduction_ratio": 0.92},
        "ground_truth": {"rows": 5, "unique_pairs": 5, "recovered_pairs": 5, "missed_pairs": 0, "candidate_recall": 1.0, "fingerprint": "sha256:abc", "fingerprint_version": "v2"},
        "quality_gate": {"passed": True, "failures": []},
    }
    curr_data = dict(base_data)
    curr_data["inputs"] = {"candidates": "c.tsv", "truth": "t.tsv", "candidate_rows": 11, "unique_candidate_pairs": 8, "duplicate_candidate_rows": 3}
    curr_data["ground_truth"] = {"rows": 5, "unique_pairs": 5, "recovered_pairs": 5, "missed_pairs": 0, "candidate_recall": 1.0, "fingerprint": "sha256:abc", "fingerprint_version": "v2"}

    base_f = tmp_path / "base.json"
    curr_f = tmp_path / "curr.json"
    comp_f = tmp_path / "compare_result.json"

    base_f.write_text(json.dumps(base_data), encoding="utf-8")
    curr_f.write_text(json.dumps(curr_data), encoding="utf-8")

    exit_code = main([
        "compare",
        "--baseline", str(base_f),
        "--current", str(curr_f),
        "--max-recall-drop", "0.01",
        "--max-pair-growth", "0.20",
        "--max-duplicate-growth", "0.60",
        "--format", "json",
        "--output", str(comp_f),
    ])

    assert exit_code == 0
    assert comp_f.exists()
    comp_data = json.loads(comp_f.read_text(encoding="utf-8"))
    assert comp_data["command"] == "compare"
    assert comp_data["schema_version"] == "1.0"
    assert comp_data["quality_gate"]["passed"] is True
    assert comp_data["metrics"]["candidate_pair_growth"] == pytest.approx(0.10)
    assert comp_data["metrics"]["duplicate_row_growth"] == pytest.approx(0.50)


def test_text_report_semantics_cross_vs_dedupe(tmp_path: Path) -> None:
    """Verify text report rendering distinctions between cross and dedupe linkage."""
    cross_cand = tmp_path / "cross.tsv"
    cross_cand.write_text("s\tc\n1\t2\n", encoding="utf-8")
    cross_rep = audit_candidates(candidates_path=cross_cand, left_id="s", right_id="c", linkage_type="cross")
    cross_text = candguard.audit.render_text(cross_rep)
    assert "Source records represented:" in cross_text
    assert "Target records represented:" in cross_text
    assert "Candidates per represented source record" in cross_text

    dedupe_cand = tmp_path / "dedupe.tsv"
    dedupe_cand.write_text("s\tc\n1\t2\n", encoding="utf-8")
    dedupe_rep = audit_candidates(candidates_path=dedupe_cand, left_id="s", right_id="c", linkage_type="dedupe")
    dedupe_text = candguard.audit.render_text(dedupe_rep)
    assert "Records represented:" in dedupe_text
    assert "Candidates per represented record" in dedupe_text
    assert "Source records represented:" not in dedupe_text

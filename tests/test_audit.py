import csv
import glob
import json
import os
import tempfile
from pathlib import Path

import pytest

from candguard.audit import AuditError, audit_candidates, render_text, report_json
from candguard.cli import main
from candguard.storage import SQLiteStorageEngine


def write_tsv(path: Path, header: list[str], rows: list[tuple[str, ...]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


@pytest.mark.parametrize("parameter", ["left_records", "right_records", "max_pairs", "max_duplicate_rows", "min_recall"])
@pytest.mark.parametrize("value", [True, False, -1, "2", float("nan"), float("inf"), -float("inf"), []])
def test_audit_rejects_invalid_numeric_arguments(parameter, value):
    options = {"left_records": 2, "right_records": 2, parameter: value}
    with pytest.raises(AuditError, match=parameter):
        audit_candidates(candidates_path="unused.tsv", truth_path="unused.tsv", left_id="l", right_id="r", **options)


@pytest.mark.parametrize("parameter", ["left_records", "right_records", "max_pairs", "max_duplicate_rows"])
def test_audit_rejects_fractional_counts(parameter):
    options = {"left_records": 2, "right_records": 2, parameter: 2.5}
    with pytest.raises(AuditError, match=parameter):
        audit_candidates(candidates_path="unused.tsv", left_id="l", right_id="r", **options)


@pytest.mark.parametrize("parameter", [
    "candidates_path", "truth_path", "missed_pairs_path", "candidate_set_path",
    "left_id", "right_id", "truth_left_id", "truth_right_id", "delimiter", "truth_delimiter",
])
def test_audit_rejects_invalid_path_and_text_argument_types(tmp_path, parameter):
    candidates = tmp_path / "candidates.tsv"
    write_tsv(candidates, ["l", "r"], [("a", "b")])
    options = dict(candidates_path=candidates, truth_path=candidates, left_id="l", right_id="r")
    options[parameter] = []
    with pytest.raises(AuditError, match=parameter):
        audit_candidates(**options)


def test_audit_large_integer_arguments(tmp_path):
    candidates = tmp_path / "candidates.tsv"
    write_tsv(candidates, ["l", "r"], [("a", "b")])
    huge = 10**400
    options = dict(candidates_path=candidates, left_id="l", right_id="r")
    report = audit_candidates(**options, left_records=huge, right_records=huge,
                              max_pairs=huge, max_duplicate_rows=huge)
    assert report["efficiency"]["possible_pairs"] == huge**2
    assert report["quality_gate"]["passed"]
    with pytest.raises(AuditError, match="min_recall"):
        audit_candidates(**options, truth_path=candidates, min_recall=huge)
    with pytest.raises(AuditError, match="record totals"):
        audit_candidates(**options, left_records=huge, right_records=1)


@pytest.mark.parametrize(("known", "recovered", "threshold", "expected_exit", "displayed"), [
    (10**18, 10**18 - 1, "1.0", 1, 1.0),
    (10**18, 10**18, "1.0", 0, 1.0),
    (100, 95, "0.95", 0, 0.95),
    (3, 2, "0.66666667", 1, 0.66666667),
    (10**10, 9_499_999_999, "0.95", 1, 0.95),
])
def test_audit_recall_gate_uses_counts(tmp_path, monkeypatch, capsys, known, recovered, threshold, expected_exit, displayed):
    candidates = tmp_path / "candidates.tsv"
    write_tsv(candidates, ["l", "r"], [("a", "b")])
    monkeypatch.setattr(SQLiteStorageEngine, "ingest_truth", lambda *args, **kwargs: (known, known))
    monkeypatch.setattr(
        SQLiteStorageEngine, "evaluate_ground_truth",
        lambda *args, **kwargs: (recovered, known - recovered, round(recovered / known, 8), "sha256:test"),
    )
    exit_code = main([
        "audit", "--candidates", str(candidates), "--truth", str(candidates),
        "--left-id", "l", "--right-id", "r", "--min-recall", threshold, "--format", "json",
    ])
    report = json.loads(capsys.readouterr().out)
    assert report["ground_truth"]["candidate_recall"] == displayed
    assert exit_code == expected_exit


def test_audit_recall_duplicates_distribution_and_reduction(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.tsv"
    truth = tmp_path / "truth.tsv"
    write_tsv(candidates, ["src", "dst"], [
        ("a", "x"), ("a", "x"), ("a", "y"), ("b", "z"), ("c", "z"),
    ])
    write_tsv(truth, ["left", "right"], [("a", "x"), ("b", "z"), ("b", "q")])

    report = audit_candidates(
        candidates_path=candidates,
        left_id="src",
        right_id="dst",
        truth_path=truth,
        truth_left_id="left",
        truth_right_id="right",
        linkage_type="cross",
        left_records=3,
        right_records=4,
        min_recall=0.6,
    )

    assert report["schema_version"] == "1.0"
    assert report["inputs"]["candidate_rows"] == 5
    assert report["inputs"]["unique_candidate_pairs"] == 4
    assert report["inputs"]["duplicate_candidate_rows"] == 1
    assert report["coverage"]["left_records_represented"] == 3
    assert report["coverage"]["right_records_represented"] == 3
    assert report["ground_truth"]["unique_pairs"] == 3
    assert report["ground_truth"]["recovered_pairs"] == 2
    assert report["ground_truth"]["missed_pairs"] == 1
    assert report["ground_truth"]["candidate_recall"] == pytest.approx(2 / 3)
    assert report["ground_truth"]["fingerprint"].startswith("sha256:")
    assert report["ground_truth"]["fingerprint_version"] == "v2"
    assert report["efficiency"]["possible_pairs"] == 12
    assert report["efficiency"]["reduction_ratio"] == pytest.approx(1 - 4 / 12)
    assert report["quality_gate"]["passed"] is True
    assert "Candidate Generation Audit" in render_text(report)


def test_quality_gate_fails_on_low_recall_and_pair_budget(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.csv"
    truth = tmp_path / "truth.csv"
    candidates.write_text("a,b\n1,2\n", encoding="utf-8")
    truth.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")

    report = audit_candidates(
        candidates_path=candidates,
        left_id="a",
        right_id="b",
        truth_path=truth,
        min_recall=0.9,
        max_pairs=0,
    )
    assert report["quality_gate"]["passed"] is False
    assert len(report["quality_gate"]["failures"]) == 2


def test_missed_pairs_are_written_as_tsv(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.csv"
    truth = tmp_path / "truth.csv"
    missed = tmp_path / "missed.tsv"
    candidates.write_text("l,r\na,x\n", encoding="utf-8")
    truth.write_text("l,r\na,x\nb,y\n", encoding="utf-8")

    report = audit_candidates(
        candidates_path=candidates,
        left_id="l",
        right_id="r",
        truth_path=truth,
        missed_pairs_path=missed,
    )
    assert report["ground_truth"]["missed_pairs"] == 1
    assert missed.read_text(encoding="utf-8").splitlines() == ["l\tr", "b\ty"]


def test_missing_column_is_a_friendly_error(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.csv"
    candidates.write_text("left,right\na,b\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Missing column"):
        audit_candidates(candidates_path=candidates, left_id="source", right_id="target")


def test_dedupe_reduction_ratio(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.csv"
    candidates.write_text("l,r\n1,2\n1,3\n2,3\n", encoding="utf-8")
    report = audit_candidates(
        candidates_path=candidates,
        left_id="l",
        right_id="r",
        linkage_type="dedupe",
        left_records=4,
    )
    assert report["efficiency"]["possible_pairs"] == 6
    assert report["efficiency"]["reduction_ratio"] == pytest.approx(0.5)


def test_dedupe_unordered_pair_semantics(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.csv"
    truth = tmp_path / "truth.csv"
    # (a, b) and (b, a) are the same unordered pair in dedupe mode
    candidates.write_text("l,r\na,b\nb,a\nb,c\n", encoding="utf-8")
    # Ground truth has (b, a), which should be recovered by candidate (a, b)
    truth.write_text("l,r\nb,a\n", encoding="utf-8")

    report = audit_candidates(
        candidates_path=candidates,
        left_id="l",
        right_id="r",
        truth_path=truth,
        linkage_type="dedupe",
        left_records=3,
        min_recall=1.0,
    )

    assert report["inputs"]["candidate_rows"] == 3
    assert report["inputs"]["unique_candidate_pairs"] == 2
    assert report["inputs"]["duplicate_candidate_rows"] == 1
    assert report["coverage"]["records_represented"] == 3
    assert report["ground_truth"]["unique_pairs"] == 1
    assert report["ground_truth"]["recovered_pairs"] == 1
    assert report["ground_truth"]["candidate_recall"] == 1.0
    assert report["quality_gate"]["passed"] is True

    # Check candidate counts per represented record:
    # a has neighbor b (1)
    # b has neighbors a, c (2)
    # c has neighbor b (1)
    dist = report["candidate_distribution_per_left_record"]
    assert dist["sources_with_candidates"] == 3
    assert dist["min"] == 1
    assert dist["max"] == 2
    assert dist["mean"] == pytest.approx(4 / 3, abs=1e-4)


def test_dedupe_rejects_self_pairs(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.csv"
    candidates.write_text("l,r\na,a\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Self-pair .* deduplication pairs must connect distinct records"):
        audit_candidates(candidates_path=candidates, left_id="l", right_id="r", linkage_type="dedupe")


def test_cross_mode_preserves_pair_direction(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.csv"
    truth = tmp_path / "truth.csv"
    # Directional: (a, b) is distinct from (b, a)
    candidates.write_text("l,r\na,b\nb,a\n", encoding="utf-8")
    truth.write_text("l,r\nb,a\n", encoding="utf-8")

    report = audit_candidates(
        candidates_path=candidates,
        left_id="l",
        right_id="r",
        truth_path=truth,
        linkage_type="cross",
    )
    assert report["inputs"]["candidate_rows"] == 2
    assert report["inputs"]["unique_candidate_pairs"] == 2
    assert report["inputs"]["duplicate_candidate_rows"] == 0
    assert report["ground_truth"]["recovered_pairs"] == 1


def test_delimiter_override_and_tsv_escaping(tmp_path: Path) -> None:
    # Test literal \t escape sequence passed as raw string r"\t"
    candidates = tmp_path / "candidates.dat"
    candidates.write_text("s\tc\n1\t2\n", encoding="utf-8")

    report = audit_candidates(
        candidates_path=candidates,
        left_id="s",
        right_id="c",
        delimiter=r"\t",
    )
    assert report["inputs"]["unique_candidate_pairs"] == 1

    # Test custom delimiter ';'
    candidates_semi = tmp_path / "candidates.txt"
    candidates_semi.write_text("s;c\n1;2\n", encoding="utf-8")
    report_semi = audit_candidates(
        candidates_path=candidates_semi,
        left_id="s",
        right_id="c",
        delimiter=";",
    )
    assert report_semi["inputs"]["unique_candidate_pairs"] == 1

    # Test invalid multi-character delimiter
    with pytest.raises(AuditError, match="Delimiter must be a single character"):
        audit_candidates(
            candidates_path=candidates,
            left_id="s",
            right_id="c",
            delimiter="comma",
        )


def test_quoted_values_and_embedded_delimiters(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.csv"
    # ID contains comma inside quotes: "A,1",B
    candidates.write_text('l,r\n"A,1",B\n"A,1",B\n', encoding="utf-8")
    report = audit_candidates(candidates_path=candidates, left_id="l", right_id="r")
    assert report["inputs"]["candidate_rows"] == 2
    assert report["inputs"]["unique_candidate_pairs"] == 1
    assert report["coverage"]["left_records_represented"] == 1


def test_whitespace_trimmed_and_blank_id_error(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.csv"
    # Whitespace around IDs is trimmed
    candidates.write_text("l,r\n  A1  ,  B1  \n", encoding="utf-8")
    report = audit_candidates(candidates_path=candidates, left_id="l", right_id="r")
    assert report["inputs"]["unique_candidate_pairs"] == 1

    # Pure whitespace ID raises AuditError with line number
    bad_candidates = tmp_path / "bad.csv"
    bad_candidates.write_text("l,r\n   ,B1\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Missing/empty pair identifier at .*bad.csv:2"):
        audit_candidates(candidates_path=bad_candidates, left_id="l", right_id="r")


def test_dataset_size_conflicts_raise_audit_error(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.csv"
    candidates.write_text("l,r\n1,10\n2,20\n3,30\n", encoding="utf-8")

    # Conflict: candidate file has 3 distinct left IDs, but left_records=2
    with pytest.raises(AuditError, match="Supplied --left-records=2 conflicts with candidate data"):
        audit_candidates(
            candidates_path=candidates,
            left_id="l",
            right_id="r",
            linkage_type="cross",
            left_records=2,
            right_records=5,
        )

    # Conflict: candidate file has 3 distinct right IDs, but right_records=2
    with pytest.raises(AuditError, match="Supplied --right-records=2 conflicts with candidate data"):
        audit_candidates(
            candidates_path=candidates,
            left_id="l",
            right_id="r",
            linkage_type="cross",
            left_records=5,
            right_records=2,
        )

    # Conflict in dedupe mode: candidate file has 6 records, but left_records=4
    with pytest.raises(AuditError, match="Supplied --left-records=4 conflicts with candidate data"):
        audit_candidates(
            candidates_path=candidates,
            left_id="l",
            right_id="r",
            linkage_type="dedupe",
            left_records=4,
        )

    # Conflict: possible pairs < unique candidate pairs
    cand_small = tmp_path / "cand_small.csv"
    cand_small.write_text("l,r\n1,10\n1,20\n", encoding="utf-8")
    with pytest.raises(AuditError, match=r"conflicts? with candidate data"):
        audit_candidates(
            candidates_path=cand_small,
            left_id="l",
            right_id="r",
            linkage_type="cross",
            left_records=1,
            right_records=1,
        )


def test_empty_candidates_file_with_header(tmp_path: Path) -> None:
    candidates = tmp_path / "empty_candidates.csv"
    candidates.write_text("l,r\n", encoding="utf-8")
    truth = tmp_path / "truth.csv"
    truth.write_text("l,r\n1,2\n", encoding="utf-8")

    report = audit_candidates(
        candidates_path=candidates,
        left_id="l",
        right_id="r",
        truth_path=truth,
        min_recall=0.5,
    )
    assert report["inputs"]["candidate_rows"] == 0
    assert report["inputs"]["unique_candidate_pairs"] == 0
    assert report["ground_truth"]["recovered_pairs"] == 0
    assert report["ground_truth"]["candidate_recall"] == 0.0
    assert report["quality_gate"]["passed"] is False


def test_empty_file_without_header_raises_error(tmp_path: Path) -> None:
    empty_file = tmp_path / "zero_bytes.csv"
    empty_file.write_text("", encoding="utf-8")
    with pytest.raises(AuditError, match="empty or has no header"):
        audit_candidates(candidates_path=empty_file, left_id="l", right_id="r")


def test_empty_truth_file_handling(tmp_path: Path) -> None:
    candidates = tmp_path / "cand.csv"
    candidates.write_text("l,r\n1,2\n", encoding="utf-8")
    truth_empty = tmp_path / "empty_truth.csv"
    truth_empty.write_text("l,r\n", encoding="utf-8")

    # With min_recall, empty truth raises AuditError
    with pytest.raises(AuditError, match="Ground-truth file contains no pairs .* cannot evaluate --min-recall"):
        audit_candidates(
            candidates_path=candidates,
            left_id="l",
            right_id="r",
            truth_path=truth_empty,
            min_recall=0.8,
        )

    # Without min_recall, empty truth audits cleanly
    report = audit_candidates(
        candidates_path=candidates,
        left_id="l",
        right_id="r",
        truth_path=truth_empty,
    )
    assert report["ground_truth"]["unique_pairs"] == 0
    assert report["ground_truth"]["candidate_recall"] is None
    assert "n/a (no ground-truth pairs)" in render_text(report)


def test_missed_pairs_requires_truth(tmp_path: Path) -> None:
    candidates = tmp_path / "cand.csv"
    candidates.write_text("l,r\n1,2\n", encoding="utf-8")
    with pytest.raises(AuditError, match="--missed-pairs-out requires --truth"):
        audit_candidates(
            candidates_path=candidates,
            left_id="l",
            right_id="r",
            missed_pairs_path=tmp_path / "missed.tsv",
        )


def test_sqlite_temp_file_cleaned_up(tmp_path: Path) -> None:
    temp_dir = tempfile.gettempdir()
    candidates = tmp_path / "candidates.csv"
    candidates.write_text("l,r\n1,2\n", encoding="utf-8")

    initial_dbs = set(glob.glob(os.path.join(temp_dir, "candguard-*.sqlite3")))

    # Successful run
    audit_candidates(candidates_path=candidates, left_id="l", right_id="r")
    after_success = set(glob.glob(os.path.join(temp_dir, "candguard-*.sqlite3")))
    assert after_success == initial_dbs

    # Run with error during processing
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("l,r\n1,\n", encoding="utf-8")
    with pytest.raises(AuditError):
        audit_candidates(candidates_path=bad_csv, left_id="l", right_id="r")

    after_error = set(glob.glob(os.path.join(temp_dir, "candguard-*.sqlite3")))
    assert after_error == initial_dbs


def test_extra_and_missing_fields_in_rows(tmp_path: Path) -> None:
    # Row with extra field
    extra_field_csv = tmp_path / "extra.csv"
    extra_field_csv.write_text("l,r\n1,2,extra\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Unexpected extra field at .*extra.csv:2"):
        audit_candidates(candidates_path=extra_field_csv, left_id="l", right_id="r")

    # Row missing value for column
    missing_field_csv = tmp_path / "missing.csv"
    missing_field_csv.write_text("l,r\n1\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Truncated row with missing column.*missing.csv:2"):
        audit_candidates(candidates_path=missing_field_csv, left_id="l", right_id="r")


def test_non_utf8_encoding_error(tmp_path: Path) -> None:
    bad_bytes = tmp_path / "bad_bytes.csv"
    bad_bytes.write_bytes(b"l,r\n\xff\xfe,1\n")
    with pytest.raises(AuditError, match="Could not read .* as UTF-8 text"):
        audit_candidates(candidates_path=bad_bytes, left_id="l", right_id="r")


def test_threshold_exact_boundaries(tmp_path: Path) -> None:
    candidates = tmp_path / "cand.csv"
    truth = tmp_path / "truth.csv"
    candidates.write_text("l,r\n1,2\n1,2\n", encoding="utf-8")
    truth.write_text("l,r\n1,2\n", encoding="utf-8")

    # Boundary: unique_candidate_pairs == max_pairs (1 == 1) should pass
    # duplicate_rows == max_duplicate_rows (1 == 1) should pass
    # recall == min_recall (1.0 == 1.0) should pass
    report = audit_candidates(
        candidates_path=candidates,
        left_id="l",
        right_id="r",
        truth_path=truth,
        min_recall=1.0,
        max_pairs=1,
        max_duplicate_rows=1,
    )
    assert report["quality_gate"]["passed"] is True


def test_render_text_dedupe_vs_cross(tmp_path: Path) -> None:
    candidates = tmp_path / "cand.csv"
    candidates.write_text("l,r\n1,2\n", encoding="utf-8")

    report_cross = audit_candidates(candidates_path=candidates, left_id="l", right_id="r", linkage_type="cross")
    text_cross = render_text(report_cross)
    assert "Source records represented:" in text_cross
    assert "Target records represented:" in text_cross

    report_dedupe = audit_candidates(candidates_path=candidates, left_id="l", right_id="r", linkage_type="dedupe")
    text_dedupe = render_text(report_dedupe)
    assert "Records represented:" in text_dedupe
    assert "Candidates per represented record" in text_dedupe


def test_dedupe_truth_rejects_self_pairs(tmp_path: Path) -> None:
    candidates = tmp_path / "cand.csv"
    truth = tmp_path / "truth.csv"
    candidates.write_text("l,r\n1,2\n", encoding="utf-8")
    truth.write_text("l,r\n1,1\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Self-pair .* deduplication pairs must connect distinct records"):
        audit_candidates(
            candidates_path=candidates,
            left_id="l",
            right_id="r",
            truth_path=truth,
            linkage_type="dedupe",
        )


def test_adversarial_identifiers_with_embedded_delimiters_and_quotes(tmp_path: Path) -> None:
    candidates = tmp_path / "adversarial_candidates.csv"
    truth = tmp_path / "adversarial_truth.csv"

    # Write CSV with quoted values containing commas, tabs, quotes, newlines, backslashes, emojis
    rows = [
        ["left_id", "right_id"],
        ['"id,with,commas"', '"target,with,commas"'],
        ['"id\twith\ttabs"', '"target\twith\ttabs"'],
        ['"id""with""quotes"', '"target""with""quotes"'],
        ['"id\nwith\nnewlines"', '"target\nwith\nnewlines"'],
        ['"id\\with\\backslashes"', '"target\\with\\backslashes"'],
        ['"id_🔥_emoji"', '"target_⚡_emoji"'],
    ]
    csv_content = "\n".join(f"{r[0]},{r[1]}" for r in rows) + "\n"
    candidates.write_text(csv_content, encoding="utf-8")
    truth.write_text(csv_content, encoding="utf-8")

    report = audit_candidates(
        candidates_path=candidates,
        left_id="left_id",
        right_id="right_id",
        truth_path=truth,
        min_recall=1.0,
    )
    assert report["inputs"]["unique_candidate_pairs"] == 6
    assert report["ground_truth"]["recovered_pairs"] == 6
    assert report["ground_truth"]["candidate_recall"] == 1.0
    assert report["quality_gate"]["passed"] is True


def test_fingerprint_no_collision_on_adversarial_pairs(tmp_path: Path) -> None:
    # Pair A: left has tab, right is simple: ("a\tb", "c")
    # Pair B: left is simple, right has tab: ("a", "b\tc")
    # A naive serialization f"{l}\t{r}\n" produces "a\tb\tc\n" for both!
    cand = tmp_path / "cand.csv"
    cand.write_text("l,r\nx,y\n", encoding="utf-8")

    truth_a = tmp_path / "truth_a.csv"
    truth_a.write_text('l,r\n"a\tb",c\n', encoding="utf-8")

    truth_b = tmp_path / "truth_b.csv"
    truth_b.write_text('l,r\na,"b\tc"\n', encoding="utf-8")

    rep_a = audit_candidates(candidates_path=cand, left_id="l", right_id="r", truth_path=truth_a)
    rep_b = audit_candidates(candidates_path=cand, left_id="l", right_id="r", truth_path=truth_b)

    fp_a = rep_a["ground_truth"]["fingerprint"]
    fp_b = rep_b["ground_truth"]["fingerprint"]

    assert fp_a != fp_b, f"Fingerprints collided for distinct pairs! {fp_a} == {fp_b}"


def test_missed_pairs_export_adversarial_identifiers(tmp_path: Path) -> None:
    cand = tmp_path / "cand.csv"
    cand.write_text("l,r\nclean_1,clean_2\n", encoding="utf-8")

    truth = tmp_path / "truth.csv"
    truth.write_text('l,r\nclean_1,clean_2\n"id\t1","target,2"\n"id\nline2","target""3"\n', encoding="utf-8")

    missed_out = tmp_path / "missed.tsv"
    report = audit_candidates(
        candidates_path=cand,
        left_id="l",
        right_id="r",
        truth_path=truth,
        missed_pairs_path=missed_out,
    )
    assert report["ground_truth"]["missed_pairs"] == 2
    assert missed_out.exists()

    # Verify missed pairs file is parseable and contains the 2 missed pairs
    with missed_out.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader)
        assert header == ["l", "r"]
        rows = list(reader)
        assert len(rows) == 2


def test_fingerprint_unambiguous_serialization_with_newlines_and_quotes(tmp_path: Path) -> None:
    # Test pairs with newlines that would serialize identically if newlines were naive record separators:
    # Pair A: ("a\nc", "d")
    # Pair B: ("a", "c\nd")
    cand = tmp_path / "cand.csv"
    cand.write_text("l,r\nx,y\n", encoding="utf-8")

    truth_a = tmp_path / "truth_a.csv"
    truth_a.write_text('l,r\n"a\nc",d\n', encoding="utf-8")

    truth_b = tmp_path / "truth_b.csv"
    truth_b.write_text('l,r\na,"c\nd"\n', encoding="utf-8")

    rep_a = audit_candidates(candidates_path=cand, left_id="l", right_id="r", truth_path=truth_a)
    rep_b = audit_candidates(candidates_path=cand, left_id="l", right_id="r", truth_path=truth_b)

    assert rep_a["ground_truth"]["fingerprint_version"] == "v2"
    assert rep_b["ground_truth"]["fingerprint_version"] == "v2"
    assert rep_a["ground_truth"]["fingerprint"] != rep_b["ground_truth"]["fingerprint"]


def test_generator_produces_unique_ground_truth_pairs_and_target_recall(tmp_path: Path) -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from benchmarks.generator import generate_dataset

    # 1. Dedupe mode
    c_dedupe, t_dedupe = generate_dataset(
        tmp_path / "dedupe",
        num_rows=20_000,
        truth_pairs=1000,
        recall_target=0.85,
        linkage_type="dedupe",
        seed=42,
    )
    rep_dedupe = audit_candidates(
        candidates_path=c_dedupe,
        truth_path=t_dedupe,
        left_id="source_id",
        right_id="candidate_id",
        linkage_type="dedupe",
    )
    assert rep_dedupe["ground_truth"]["rows"] == 1000
    assert rep_dedupe["ground_truth"]["unique_pairs"] == 1000
    assert rep_dedupe["ground_truth"]["candidate_recall"] == pytest.approx(0.85, abs=0.03)

    # 2. Cross mode
    c_cross, t_cross = generate_dataset(
        tmp_path / "cross",
        num_rows=20_000,
        truth_pairs=1000,
        recall_target=0.85,
        linkage_type="cross",
        seed=42,
    )
    rep_cross = audit_candidates(
        candidates_path=c_cross,
        truth_path=t_cross,
        left_id="source_id",
        right_id="candidate_id",
        linkage_type="cross",
    )
    assert rep_cross["ground_truth"]["rows"] == 1000
    assert rep_cross["ground_truth"]["unique_pairs"] == 1000
    assert rep_cross["ground_truth"]["candidate_recall"] == pytest.approx(0.85, abs=0.03)


def test_blank_lines_raise_explicit_audit_error(tmp_path: Path) -> None:
    # Trailing blank line
    trailing_blank = tmp_path / "trailing_blank.csv"
    trailing_blank.write_text("l,r\n1,2\n\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Blank line encountered at .*trailing_blank.csv:3"):
        audit_candidates(candidates_path=trailing_blank, left_id="l", right_id="r")

    # Intermediate blank line
    mid_blank = tmp_path / "mid_blank.csv"
    mid_blank.write_text("l,r\n1,2\n   \n3,4\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Blank line encountered at .*mid_blank.csv:3"):
        audit_candidates(candidates_path=mid_blank, left_id="l", right_id="r")


def test_duplicate_and_identical_header_columns_raise_error(tmp_path: Path) -> None:
    # Duplicate left column name makes identifier selection ambiguous
    dup_left = tmp_path / "dup_left.csv"
    dup_left.write_text("l,r,l\n1,2,3\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Duplicate column 'l' in .*dup_left.csv header makes identifier selection ambiguous"):
        audit_candidates(candidates_path=dup_left, left_id="l", right_id="r")

    # Duplicate right column name
    dup_right = tmp_path / "dup_right.csv"
    dup_right.write_text("l,r,r\n1,2,3\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Duplicate column 'r' in .*dup_right.csv header makes identifier selection ambiguous"):
        audit_candidates(candidates_path=dup_right, left_id="l", right_id="r")

    # Identical left and right column names
    same_col = tmp_path / "same_col.csv"
    same_col.write_text("id,other\n1,2\n", encoding="utf-8")
    with pytest.raises(AuditError, match="Left and right identifier columns cannot be identical"):
        audit_candidates(candidates_path=same_col, left_id="id", right_id="id")

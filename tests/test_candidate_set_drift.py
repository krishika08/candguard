"""Tests for candidate-set sidecar export, artifact validation, and exact drift comparison."""
from __future__ import annotations

import csv
import gc
import hashlib
import inspect
import io
import json
import math
import os
import random
import sys
import tracemalloc
from pathlib import Path
from typing import Any, Iterable, Iterator

import pytest

from candguard import __version__ as candguard_version
from candguard import candidate_set as cs
from candguard.audit import AuditError, audit_candidates
from candguard.candidate_set import (
    CANDIDATE_SET_FORMAT,
    CandidateSetArtifactReader,
    merge_sorted_pair_streams,
    write_candidate_set_artifact,
)
from candguard.cli import main
from candguard.compare import compare_reports, render_compare_text
from candguard.metrics import (
    CandidateSetFingerprintV1,
    compute_candidate_set_drift,
    compute_candidate_set_fingerprint_v1,
    compute_truth_fingerprint_v2,
)
from candguard.storage import SQLiteStorageEngine

Pair = tuple[str, str]


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------

def _write_delimited(path: Path, rows: Iterable[Iterable[str]], header: tuple[str, str] = ("l", "r"), delimiter: str = "\t") -> Path:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter=delimiter, lineterminator="\n")
        # Python < 3.12 does not quote a bare "\r" under QUOTE_MINIMAL; quote such rows fully.
        quote_all = csv.writer(stream, delimiter=delimiter, lineterminator="\n", quoting=csv.QUOTE_ALL)
        writer.writerow(header)
        for row in rows:
            (quote_all if any("\r" in field for field in row) else writer).writerow(row)
    return path


def _audit(
    tmp_path: Path,
    name: str,
    rows: Iterable[Pair],
    *,
    linkage_type: str = "cross",
    export: bool = True,
    **kwargs: Any,
) -> tuple[dict[str, Any], Path, Path]:
    """Audit ``rows`` and write the JSON report; return (report, report_path, artifact_path)."""
    candidates = _write_delimited(tmp_path / f"{name}.tsv", rows)
    artifact = tmp_path / "artifacts" / f"{name}.pairs.tsv"
    report = audit_candidates(
        candidates_path=candidates,
        left_id="l",
        right_id="r",
        linkage_type=linkage_type,
        candidate_set_path=artifact if export else None,
        **kwargs,
    )
    report_path = tmp_path / f"{name}.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return report, report_path, artifact


def _minimal_report(metadata: dict[str, Any] | None, *, linkage_type: str, unique_pairs: int) -> dict[str, Any]:
    report: dict[str, Any] = {
        "tool": "candguard",
        "version": "0.1.0",
        "schema_version": "1.0",
        "inputs": {
            "candidates": "c.tsv",
            "truth": None,
            "candidate_rows": unique_pairs,
            "unique_candidate_pairs": unique_pairs,
            "duplicate_candidate_rows": 0,
        },
        "efficiency": {"linkage_type": linkage_type, "possible_pairs": None, "reduction_ratio": None},
        "ground_truth": None,
        "quality_gate": {"passed": True, "failures": []},
    }
    if metadata is not None:
        report["candidate_set"] = metadata
    return report


def _artifact(tmp_path: Path, name: str, pairs: Iterable[Pair], linkage_type: str = "cross") -> tuple[Path, Path, dict[str, Any]]:
    """Write a canonical artifact plus a matching minimal report; return (artifact, report, metadata)."""
    ordered = sorted(set(pairs))
    artifact = tmp_path / f"{name}.pairs.tsv"
    metadata = write_candidate_set_artifact(ordered, artifact, linkage_type=linkage_type, expected_pairs=len(ordered))
    report_path = tmp_path / f"{name}.json"
    report_path.write_text(
        json.dumps(_minimal_report(metadata, linkage_type=linkage_type, unique_pairs=len(ordered))), encoding="utf-8"
    )
    return artifact, report_path, metadata


def _drift(tmp_path: Path, base: Iterable[Pair], curr: Iterable[Pair], linkage_type: str = "cross", **kwargs: Any) -> dict[str, Any]:
    b_art, b_rep, _ = _artifact(tmp_path, "base", base, linkage_type)
    c_art, c_rep, _ = _artifact(tmp_path, "curr", curr, linkage_type)
    return compare_reports(
        baseline_path=b_rep,
        current_path=c_rep,
        baseline_candidate_set_path=b_art,
        current_candidate_set_path=c_art,
        **kwargs,
    )


def _compare_files(base_art: Path, base_rep: Path, curr_art: Path, curr_rep: Path, **kwargs: Any) -> dict[str, Any]:
    return compare_reports(
        baseline_path=base_rep,
        current_path=curr_rep,
        baseline_candidate_set_path=base_art,
        current_candidate_set_path=curr_art,
        **kwargs,
    )


def _read_artifact_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.reader(stream, delimiter="\t", strict=True))


def _reference_counts(base: Iterable[Pair], curr: Iterable[Pair]) -> tuple[int, int, int]:
    b, c = set(base), set(curr)
    return len(b & c), len(c - b), len(b - c)


def _seq_pairs(start: int, stop: int) -> Iterator[Pair]:
    """Pair ``j`` -> ("L<j % 997>", "R<j>"): injective, interleaved in sorted order."""
    for j in range(start, stop):
        yield (f"L{j % 997:04d}", f"R{j:09d}")


def _write_sorted_artifact(path: Path, pairs: Iterable[Pair], linkage_type: str = "cross") -> dict[str, Any]:
    """Write an artifact for already-unique pairs (sorting them) without holding a Python set."""
    ordered = sorted(pairs)
    return write_candidate_set_artifact(ordered, path, linkage_type=linkage_type, expected_pairs=len(ordered))


def _rewrite(path: Path, transform) -> None:  # type: ignore[no-untyped-def]
    text = path.read_text(encoding="utf-8")
    path.write_bytes(transform(text).encode("utf-8"))


CROSS_ROWS: list[Pair] = [("A2", "B3"), ("A1", "B2"), ("A1", "B1"), ("B1", "A1"), ("A1", "B1"), ("A3", "B4")]


# --------------------------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------------------------

def test_fingerprint_matches_documented_encoding() -> None:
    pairs = [("A1", "B1"), ("é", "日本")]
    expected = hashlib.sha256()
    expected.update(b"candguard:candidate-set:v1:cross\n")
    expected.update(b"2:A1\t2:B1\n")
    expected.update(b"2:\xc3\xa9\t6:\xe6\x97\xa5\xe6\x9c\xac\n")
    assert compute_candidate_set_fingerprint_v1(pairs, "cross") == f"sha256:{expected.hexdigest()}"
    empty = hashlib.sha256(b"candguard:candidate-set:v1:dedupe\n").hexdigest()
    assert compute_candidate_set_fingerprint_v1([], "dedupe") == f"sha256:{empty}"


def test_fingerprint_domain_separation_and_unambiguous_encoding() -> None:
    pairs = [("a", "b"), ("c", "d")]
    cross = compute_candidate_set_fingerprint_v1(pairs, "cross")
    assert cross != compute_candidate_set_fingerprint_v1(pairs, "dedupe")
    assert cross != compute_truth_fingerprint_v2(pairs, "cross")
    assert cross != compute_candidate_set_fingerprint_v1(list(reversed(pairs)), "cross")
    # Delimiter characters inside identifiers cannot shift pair boundaries.
    assert compute_candidate_set_fingerprint_v1([("a\tb", "c")], "cross") != compute_candidate_set_fingerprint_v1([("a", "b\tc")], "cross")
    assert compute_candidate_set_fingerprint_v1([("a\nb", "c")], "cross") != compute_candidate_set_fingerprint_v1([("a", "b"), ("", "c")], "cross")
    assert compute_candidate_set_fingerprint_v1([("1:a", "b")], "cross") != compute_candidate_set_fingerprint_v1([("1", "a:b")], "cross")


def test_fingerprint_hasher_counts_and_rejects_bad_linkage() -> None:
    hasher = CandidateSetFingerprintV1("cross")
    for pair in [("a", "b"), ("a", "c")]:
        hasher.update(*pair)
    assert hasher.count == 2
    assert hasher.hexdigest() == compute_candidate_set_fingerprint_v1([("a", "b"), ("a", "c")], "cross")
    with pytest.raises(AuditError, match="linkage_type"):
        CandidateSetFingerprintV1("linked")


def test_truth_fingerprint_v2_is_unchanged() -> None:
    # Golden value from main @ 7d77822 (truth pairs of the compare golden fixture below).
    pairs = [("A1", "B1"), ("A2", "B3"), ("A3", "B7")]
    assert compute_truth_fingerprint_v2(pairs, "cross") == (
        "sha256:2e3c100e15eac48452552ee384f1b10eb8bf8e7bf23264c489042ad4ae3505c2"
    )


# --------------------------------------------------------------------------------------------
# Artifact export
# --------------------------------------------------------------------------------------------

def test_cross_export_is_sorted_unique_and_directional(tmp_path: Path) -> None:
    report, _, artifact = _audit(tmp_path, "c", CROSS_ROWS)
    assert _read_artifact_rows(artifact) == [
        ["left_id", "right_id"], ["A1", "B1"], ["A1", "B2"], ["A2", "B3"], ["A3", "B4"], ["B1", "A1"],
    ]
    assert artifact.read_bytes().startswith(b"left_id\tright_id\n")
    assert b"\r" not in artifact.read_bytes()
    expected_pairs = [("A1", "B1"), ("A1", "B2"), ("A2", "B3"), ("A3", "B4"), ("B1", "A1")]
    assert CANDIDATE_SET_FORMAT == "candguard-pairs-tsv-v1"
    assert report["candidate_set"] == {
        "format": CANDIDATE_SET_FORMAT,
        "unique_pairs": 5,
        "fingerprint": compute_candidate_set_fingerprint_v1(expected_pairs, "cross"),
        "fingerprint_version": "v1",
        "linkage_type": "cross",
    }
    assert report["candidate_set"]["unique_pairs"] == report["inputs"]["unique_candidate_pairs"]


def test_dedupe_export_uses_canonical_undirected_pairs(tmp_path: Path) -> None:
    rows = [("B", "A"), ("A", "B"), ("C", "A"), ("A", "C"), ("D", "B")]
    report, _, artifact = _audit(tmp_path, "d", rows, linkage_type="dedupe")
    assert _read_artifact_rows(artifact) == [["left_id", "right_id"], ["A", "B"], ["A", "C"], ["B", "D"]]
    assert report["candidate_set"]["linkage_type"] == "dedupe"
    assert report["candidate_set"]["unique_pairs"] == report["inputs"]["unique_candidate_pairs"] == 3
    assert report["candidate_set"]["fingerprint"] == compute_candidate_set_fingerprint_v1(
        [("A", "B"), ("A", "C"), ("B", "D")], "dedupe"
    )


def test_dedupe_self_pair_still_rejected_and_no_artifact(tmp_path: Path) -> None:
    with pytest.raises(AuditError, match="Self-pair"):
        _audit(tmp_path, "d", [("A", "B"), ("C", "C")], linkage_type="dedupe")
    assert not (tmp_path / "artifacts" / "d.pairs.tsv").exists()


SPECIAL_IDS = ["tab\tinside", 'quote"inside', "comma,inside", "new\nline", "carriage\rreturn", "crlf\r\nmid",
               "ünïcødé", "日本語", "emoji😀", "é", "é", "A", "a", "10", "9"]


def test_export_round_trips_special_identifiers(tmp_path: Path) -> None:
    rows = [(left, right) for left in SPECIAL_IDS[:8] for right in SPECIAL_IDS[7:]]
    report, report_path, artifact = _audit(tmp_path, "s", rows)
    expected = sorted(set(rows))
    with CandidateSetArtifactReader(artifact, report["candidate_set"], label="Baseline") as reader:
        assert list(reader) == expected
        assert reader.verified
    assert report["candidate_set"]["fingerprint"] == compute_candidate_set_fingerprint_v1(expected, "cross")
    drift = _compare_files(artifact, report_path, artifact, report_path)["metrics"]["candidate_set_drift"]
    assert drift["jaccard_similarity"] == 1.0 and drift["retained_pairs"] == len(expected)


def test_export_bytes_are_identical_on_every_python_version(tmp_path: Path) -> None:
    pairs = sorted([("a\rb", "x"), ("c\nd", "y"), ('e"f', "z"), ("g\th", "w"), ("plain", "v"), ("p", "q\r\nr")])
    out = tmp_path / "p.tsv"
    write_candidate_set_artifact(pairs, out, linkage_type="cross", expected_pairs=len(pairs))
    assert out.read_bytes() == (
        b'left_id\tright_id\n'
        b'"a\rb"\t"x"\n'          # any "\r" -> both fields quoted (Python < 3.12 would not quote it)
        b'"c\nd"\ty\n'
        b'"e""f"\tz\n'
        b'"g\th"\tw\n'
        b'"p"\t"q\r\nr"\n'
        b'plain\tv\n'
    )
    meta = {"linkage_type": "cross", "unique_pairs": len(pairs),
            "fingerprint": compute_candidate_set_fingerprint_v1(pairs, "cross")}
    with CandidateSetArtifactReader(out, meta, label="Baseline") as reader:
        assert list(reader) == pairs
        assert reader.verified


def test_export_empty_candidate_set(tmp_path: Path) -> None:
    report, report_path, artifact = _audit(tmp_path, "e", [])
    assert artifact.read_bytes() == b"left_id\tright_id\n"
    assert report["candidate_set"]["unique_pairs"] == 0
    assert report["candidate_set"]["fingerprint"] == compute_candidate_set_fingerprint_v1([], "cross")
    drift = _compare_files(artifact, report_path, artifact, report_path)["metrics"]["candidate_set_drift"]
    assert drift["jaccard_similarity"] == 1.0 and drift["pair_churn"] == 0.0


def test_report_does_not_store_artifact_path_or_pairs(tmp_path: Path) -> None:
    report, report_path, artifact = _audit(tmp_path, "c", CROSS_ROWS)
    text = report_path.read_text(encoding="utf-8")
    assert "pairs.tsv" not in text and "artifacts" not in text
    assert set(report["candidate_set"]) == {"format", "unique_pairs", "fingerprint", "fingerprint_version", "linkage_type"}
    assert "B4" not in json.dumps(report["candidate_set"])


def test_export_does_not_change_other_report_fields(tmp_path: Path) -> None:
    truth = _write_delimited(tmp_path / "t.tsv", [("A1", "B1"), ("A9", "B9")])
    with_export, _, _ = _audit(tmp_path, "x", CROSS_ROWS, truth_path=truth, left_records=5, right_records=6)
    without, _, _ = _audit(tmp_path, "x", CROSS_ROWS, truth_path=truth, left_records=5, right_records=6, export=False)
    candidate_set = with_export.pop("candidate_set")
    assert with_export == without
    assert "candidate_set" not in without
    assert candidate_set["unique_pairs"] == without["inputs"]["unique_candidate_pairs"]


def test_no_sidecar_generated_by_default(tmp_path: Path) -> None:
    candidates = _write_delimited(tmp_path / "c.tsv", CROSS_ROWS)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    report = audit_candidates(candidates_path=candidates, left_id="l", right_id="r")
    assert "candidate_set" not in report
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_export_atomically_replaces_existing_file(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "c.pairs.tsv"
    artifact.parent.mkdir()
    artifact.write_text("stale content\n", encoding="utf-8")
    report, _, artifact = _audit(tmp_path, "c", CROSS_ROWS)
    assert _read_artifact_rows(artifact)[0] == ["left_id", "right_id"]
    assert sorted(p.name for p in artifact.parent.iterdir()) == ["c.pairs.tsv"]


def test_export_creates_parent_directories(tmp_path: Path) -> None:
    candidates = _write_delimited(tmp_path / "c.tsv", CROSS_ROWS)
    out = tmp_path / "deep" / "nested" / "dir" / "pairs.tsv"
    report = audit_candidates(candidates_path=candidates, left_id="l", right_id="r", candidate_set_path=out)
    assert out.is_file() and report["candidate_set"]["unique_pairs"] == 5


def _leftover_sqlite_files() -> set[str]:
    import tempfile

    return {p.name for p in Path(tempfile.gettempdir()).glob("candguard-*.sqlite3")}


def test_failed_audit_leaves_no_artifact_and_keeps_existing_file(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "c.pairs.tsv"
    artifact.parent.mkdir()
    artifact.write_text("previous artifact\n", encoding="utf-8")
    bad_truth = _write_delimited(tmp_path / "t.tsv", [("A1", "B1")], header=("x", "y"))
    before = _leftover_sqlite_files()
    with pytest.raises(AuditError):
        _audit(tmp_path, "c", CROSS_ROWS, truth_path=bad_truth)
    assert artifact.read_text(encoding="utf-8") == "previous artifact\n"
    assert sorted(p.name for p in artifact.parent.iterdir()) == ["c.pairs.tsv"]
    assert _leftover_sqlite_files() <= before


def test_write_failure_cleans_temp_file_and_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "artifacts" / "c.pairs.tsv"
    artifact.parent.mkdir()
    artifact.write_text("previous artifact\n", encoding="utf-8")

    def failing_fsync(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cs.os, "fsync", failing_fsync)
    before = _leftover_sqlite_files()
    with pytest.raises(AuditError, match="Could not write candidate-set artifact"):
        _audit(tmp_path, "c", CROSS_ROWS)
    assert artifact.read_text(encoding="utf-8") == "previous artifact\n"
    assert sorted(p.name for p in artifact.parent.iterdir()) == ["c.pairs.tsv"]
    assert _leftover_sqlite_files() <= before


def test_writer_rejects_unsorted_or_miscounted_input_without_partial_file(tmp_path: Path) -> None:
    out = tmp_path / "p.tsv"
    with pytest.raises(AuditError, match="strict ascending order"):
        write_candidate_set_artifact([("b", "x"), ("a", "x")], out, linkage_type="cross", expected_pairs=2)
    with pytest.raises(AuditError, match="strict ascending order"):
        write_candidate_set_artifact([("a", "x"), ("a", "x")], out, linkage_type="cross", expected_pairs=2)
    with pytest.raises(AuditError, match="not canonical"):
        write_candidate_set_artifact([("b", "a")], out, linkage_type="dedupe", expected_pairs=1)
    with pytest.raises(AuditError, match="empty identifier"):
        write_candidate_set_artifact([("", "a")], out, linkage_type="cross", expected_pairs=1)
    with pytest.raises(AuditError, match="audit counted 3"):
        write_candidate_set_artifact([("a", "b")], out, linkage_type="cross", expected_pairs=3)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("target", ["candidates", "truth", "missed", "candidates_alias"])
def test_output_cannot_overwrite_inputs_or_other_outputs(tmp_path: Path, target: str) -> None:
    candidates = _write_delimited(tmp_path / "c.tsv", CROSS_ROWS)
    truth = _write_delimited(tmp_path / "t.tsv", [("A1", "B1"), ("A9", "B9")])
    missed = tmp_path / "missed.tsv"
    out = {
        "candidates": candidates,
        "truth": truth,
        "missed": missed,
        "candidates_alias": tmp_path / "sub" / ".." / "c.tsv",
    }[target]
    original = {p: p.read_bytes() for p in (candidates, truth)}
    with pytest.raises(AuditError, match="resolves to the"):
        audit_candidates(
            candidates_path=candidates, left_id="l", right_id="r", truth_path=truth,
            missed_pairs_path=missed, candidate_set_path=out,
        )
    assert {p: p.read_bytes() for p in (candidates, truth)} == original
    assert not missed.exists()


def test_unwritable_output_location_is_an_audit_error(tmp_path: Path) -> None:
    candidates = _write_delimited(tmp_path / "c.tsv", CROSS_ROWS)
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(AuditError, match="Could not create candidate-set artifact"):
        audit_candidates(candidates_path=candidates, left_id="l", right_id="r", candidate_set_path=blocker / "pairs.tsv")
    assert blocker.read_text(encoding="utf-8") == "x"


def test_output_directory_rejected(tmp_path: Path) -> None:
    candidates = _write_delimited(tmp_path / "c.tsv", CROSS_ROWS)
    (tmp_path / "out").mkdir()
    with pytest.raises(AuditError, match="not a regular file"):
        audit_candidates(candidates_path=candidates, left_id="l", right_id="r", candidate_set_path=tmp_path / "out")


class _ListReader:
    def __init__(self, rows: list[Pair]) -> None:
        self.rows = rows
        self.path = Path("memory.tsv")

    def iter_batches(self, batch_size: int = 50_000) -> Iterator[list[Pair]]:
        yield list(self.rows)


def test_nul_identifiers_round_trip_or_are_rejected_explicitly(tmp_path: Path) -> None:
    out = tmp_path / "nul.pairs.tsv"
    with SQLiteStorageEngine() as engine:
        _, unique = engine.ingest_candidates(_ListReader([("a\x00b", "c"), ("a", "c")]))
        if sys.version_info >= (3, 11):
            meta = engine.export_candidate_set(out, linkage_type="cross", expected_pairs=unique)
        else:
            with pytest.raises(AuditError, match="NUL"):
                engine.export_candidate_set(out, linkage_type="cross", expected_pairs=unique)
            assert not out.exists()
            return
    with CandidateSetArtifactReader(out, meta, label="Baseline") as reader:
        assert list(reader) == [("a", "c"), ("a\x00b", "c")]


# --------------------------------------------------------------------------------------------
# Drift metric definitions
# --------------------------------------------------------------------------------------------

def test_drift_metric_function_hand_values() -> None:
    assert compute_candidate_set_drift(retained=900, added=150, removed=100) == {
        "baseline_unique_pairs": 1000,
        "current_unique_pairs": 1050,
        "retained_pairs": 900,
        "added_pairs": 150,
        "removed_pairs": 100,
        "union_pairs": 1150,
        "jaccard_similarity": 0.7826087,
        "pair_churn": 0.2173913,
        "removed_fraction_of_baseline": 0.1,
        "added_fraction_of_current": 0.14285714,
    }
    both_empty = compute_candidate_set_drift(retained=0, added=0, removed=0)
    assert both_empty["jaccard_similarity"] == 1.0 and both_empty["pair_churn"] == 0.0
    assert both_empty["removed_fraction_of_baseline"] is None and both_empty["added_fraction_of_current"] is None
    for bad in (-1, True, 1.5, None):
        with pytest.raises(AuditError):
            compute_candidate_set_drift(retained=bad, added=0, removed=0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("base", "curr", "expected"),
    [
        pytest.param([("a", "1"), ("b", "2")], [("a", "1"), ("b", "2")], (2, 0, 0, 1.0, 0.0, 0.0, 0.0), id="identical"),
        pytest.param([("a", "1"), ("b", "2")], [("c", "3"), ("d", "4")], (0, 2, 2, 0.0, 1.0, 1.0, 1.0), id="disjoint"),
        pytest.param([("a", "1"), ("a", "2"), ("b", "1")], [("a", "1"), ("b", "1"), ("c", "9"), ("d", "9")],
                     (2, 2, 1, 0.4, 0.6, 1 / 3, 0.5), id="partial"),
        pytest.param([("A1", "B1"), ("A1", "B2"), ("A2", "B3"), ("A3", "B4")],
                     [("A1", "B1"), ("A1", "B9"), ("A2", "B8"), ("A3", "B7")],
                     (1, 3, 3, 1 / 7, 6 / 7, 0.75, 0.75), id="same-count-different-composition"),
        pytest.param([("a", "1")], [("a", "1"), ("a", "2"), ("z", "0")], (1, 2, 0, 1 / 3, 2 / 3, 0.0, 2 / 3), id="only-added"),
        pytest.param([("a", "1"), ("a", "2"), ("z", "0")], [("a", "2")], (1, 0, 2, 1 / 3, 2 / 3, 2 / 3, 0.0), id="only-removed"),
        pytest.param([], [("a", "1"), ("b", "2")], (0, 2, 0, 0.0, 1.0, None, 1.0), id="baseline-empty"),
        pytest.param([("a", "1"), ("b", "2")], [], (0, 0, 2, 0.0, 1.0, 1.0, None), id="current-empty"),
        pytest.param([], [], (0, 0, 0, 1.0, 0.0, None, None), id="both-empty"),
        pytest.param([("A", "B")], [("B", "A")], (0, 1, 1, 0.0, 1.0, 1.0, 1.0), id="cross-direction-change"),
    ],
)
def test_drift_cases(tmp_path: Path, base: list[Pair], curr: list[Pair], expected: tuple[Any, ...]) -> None:
    retained, added, removed, jaccard, churn, removed_frac, added_frac = expected
    drift = _drift(tmp_path, base, curr)["metrics"]["candidate_set_drift"]
    assert (drift["retained_pairs"], drift["added_pairs"], drift["removed_pairs"]) == (retained, added, removed)
    assert drift["baseline_unique_pairs"] == len(base) == retained + removed
    assert drift["current_unique_pairs"] == len(curr) == retained + added
    assert drift["union_pairs"] == retained + added + removed
    assert drift["jaccard_similarity"] == pytest.approx(jaccard, abs=1e-8)
    assert drift["pair_churn"] == pytest.approx(churn, abs=1e-8)
    for key, value in (("removed_fraction_of_baseline", removed_frac), ("added_fraction_of_current", added_frac)):
        if value is None:
            assert drift[key] is None
        else:
            assert drift[key] == pytest.approx(value, abs=1e-8)
    if drift["union_pairs"]:
        assert drift["pair_churn"] == pytest.approx(1 - drift["jaccard_similarity"], abs=1e-8)


def test_dedupe_reversed_pairs_are_the_same_edge(tmp_path: Path) -> None:
    _, b_rep, b_art = _audit(tmp_path, "b", [("A", "B"), ("C", "D")], linkage_type="dedupe")
    _, c_rep, c_art = _audit(tmp_path, "c", [("B", "A"), ("D", "C"), ("C", "D")], linkage_type="dedupe")
    drift = _compare_files(b_art, b_rep, c_art, c_rep)["metrics"]["candidate_set_drift"]
    assert (drift["retained_pairs"], drift["added_pairs"], drift["removed_pairs"]) == (2, 0, 0)
    assert drift["jaccard_similarity"] == 1.0


def test_cross_direction_change_via_audit_is_removal_plus_addition(tmp_path: Path) -> None:
    _, b_rep, b_art = _audit(tmp_path, "b", [("A", "B"), ("C", "D")])
    _, c_rep, c_art = _audit(tmp_path, "c", [("B", "A"), ("C", "D")])
    drift = _compare_files(b_art, b_rep, c_art, c_rep)["metrics"]["candidate_set_drift"]
    assert (drift["retained_pairs"], drift["added_pairs"], drift["removed_pairs"]) == (1, 1, 1)


def test_duplicate_raw_rows_and_whitespace_do_not_affect_drift(tmp_path: Path) -> None:
    _, b_rep, b_art = _audit(tmp_path, "b", [("A", "B"), ("C", "D")])
    _, c_rep, c_art = _audit(tmp_path, "c", [("A", "B"), (" A ", "B"), ("A", "B"), ("C", " D"), ("C", "D")])
    report = _compare_files(b_art, b_rep, c_art, c_rep)
    assert report["current"]["candidate_rows"] == 5
    drift = report["metrics"]["candidate_set_drift"]
    assert (drift["retained_pairs"], drift["added_pairs"], drift["removed_pairs"]) == (2, 0, 0)


def _random_id(rng: random.Random) -> str:
    alphabet = ["a", "b", "B", "z", "1", "é", "日", "😀", "\t", '"', ",", "\n", " "]
    core = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 3)))
    return f"x{core}y"  # never empty, never trimmed


def test_merge_matches_set_reference_on_random_sets(tmp_path: Path) -> None:
    rng = random.Random(20260611)
    for trial in range(150):
        linkage = rng.choice(["cross", "dedupe"])
        universe = [(_random_id(rng), _random_id(rng)) for _ in range(rng.randint(0, 40))]
        if linkage == "dedupe":
            universe = [(min(p), max(p)) for p in universe if p[0] != p[1]]
        base = {p for p in universe if rng.random() < 0.6}
        curr = {p for p in universe if rng.random() < 0.6}
        assert merge_sorted_pair_streams(iter(sorted(base)), iter(sorted(curr))) == _reference_counts(base, curr)
        if trial % 5 == 0:
            work = tmp_path / str(trial)
            work.mkdir()
            drift = _drift(work, base, curr, linkage)["metrics"]["candidate_set_drift"]
            r, a, d = _reference_counts(base, curr)
            assert (drift["retained_pairs"], drift["added_pairs"], drift["removed_pairs"]) == (r, a, d)
            union = len(base | curr)
            assert drift["jaccard_similarity"] == (round(r / union, 8) if union else 1.0)


@pytest.mark.parametrize("linkage", ["cross", "dedupe"])
def test_end_to_end_audits_match_set_reference(tmp_path: Path, linkage: str) -> None:
    rng = random.Random(7 if linkage == "cross" else 8)
    ids = [f"id{i}" for i in range(25)] + ["ü", "日本", "a b"]
    for trial in range(6):
        def rows() -> list[Pair]:
            out = []
            for _ in range(rng.randint(0, 60)):
                left, right = rng.choice(ids), rng.choice(ids)
                if linkage == "dedupe" and left == right:
                    continue
                out.append((left, right))
            return out

        base_rows, curr_rows = rows(), rows()
        work = tmp_path / str(trial)
        work.mkdir()
        _, b_rep, b_art = _audit(work, "b", base_rows, linkage_type=linkage)
        _, c_rep, c_art = _audit(work, "c", curr_rows, linkage_type=linkage)
        drift = _compare_files(b_art, b_rep, c_art, c_rep)["metrics"]["candidate_set_drift"]

        def norm(rows_: list[Pair]) -> set[Pair]:
            return {(min(p), max(p)) if linkage == "dedupe" else p for p in rows_}

        assert (drift["retained_pairs"], drift["added_pairs"], drift["removed_pairs"]) == _reference_counts(
            norm(base_rows), norm(curr_rows)
        )


def test_csv_tsv_and_parquet_inputs_produce_identical_artifacts(tmp_path: Path) -> None:
    rows = [("A1", "B,1"), ("A2", 'B"2'), ("A1", "B,1"), ("日本", "x")]
    tsv = _write_delimited(tmp_path / "c.tsv", rows)
    csv_file = _write_delimited(tmp_path / "c.csv", rows, delimiter=",")
    outputs = {}
    for name, path in (("tsv", tsv), ("csv", csv_file)):
        out = tmp_path / f"{name}.pairs.tsv"
        report = audit_candidates(candidates_path=path, left_id="l", right_id="r", candidate_set_path=out)
        outputs[name] = (out.read_bytes(), report["candidate_set"])
    assert outputs["tsv"] == outputs["csv"]

    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    parquet = tmp_path / "c.parquet"
    pq.write_table(pa.table({"l": [r[0] for r in rows], "r": [r[1] for r in rows]}), parquet)
    out = tmp_path / "parquet.pairs.tsv"
    report = audit_candidates(candidates_path=parquet, left_id="l", right_id="r", candidate_set_path=out)
    assert (out.read_bytes(), report["candidate_set"]) == outputs["tsv"]


# --------------------------------------------------------------------------------------------
# Artifact integrity
# --------------------------------------------------------------------------------------------

@pytest.fixture()
def valid_pair(tmp_path: Path) -> dict[str, Path]:
    b_art, b_rep, _ = _artifact(tmp_path, "base", [("a", "1"), ("a", "2"), ("b", "1"), ("c", "3")])
    c_art, c_rep, _ = _artifact(tmp_path, "curr", [("a", "1"), ("b", "1"), ("d", "4")])
    return {"b_art": b_art, "b_rep": b_rep, "c_art": c_art, "c_rep": c_rep}


def _run(files: dict[str, Path], **kwargs: Any) -> dict[str, Any]:
    return _compare_files(files["b_art"], files["b_rep"], files["c_art"], files["c_rep"], **kwargs)


ARTIFACT_CORRUPTIONS = {
    "wrong-header": (lambda t: t.replace("left_id\tright_id", "source_id\tcandidate_id"), "invalid header"),
    "comma-header": (lambda t: t.replace("left_id\tright_id", "left_id,right_id"), "invalid header"),
    "bom-header": (lambda t: "﻿" + t, "byte-order mark"),
    "three-columns": (lambda t: t.replace("a\t2\n", "a\t2\textra\n"), "expected 2 tab-separated fields, found 3"),
    "one-column": (lambda t: t.replace("a\t2\n", "a2\n"), "expected 2 tab-separated fields, found 1"),
    "blank-line": (lambda t: t.replace("a\t2\n", "a\t2\n\n"), "found 0"),
    "empty-identifier": (lambda t: t.replace("a\t2\n", "a\t\n"), "empty identifier"),
    "duplicate-row": (lambda t: t.replace("a\t2\n", "a\t2\na\t2\n"), "duplicate pair"),
    "unsorted-rows": (lambda t: t.replace("a\t1\na\t2\n", "a\t2\na\t1\n"), "not in ascending"),
    "modified-value": (lambda t: t.replace("b\t1\n", "b\t0\n"), "fingerprint"),
    "truncated": (lambda t: t[: t.rindex("c\t3\n")], "truncated"),
    "extra-row": (lambda t: t + "z\t9\n", "more pairs than the 4"),
    "header-only": (lambda t: "left_id\tright_id\n", "contains 0 pair"),
    "empty-file": (lambda t: "", "is empty"),
    "bad-quoting": (lambda t: t.replace("a\t2\n", '"a"x\t2\n'), "malformed"),
    "unterminated-quote": (lambda t: t.replace("c\t3\n", '"c\t3\n'), "malformed|unexpected end"),
}


@pytest.mark.parametrize("case", list(ARTIFACT_CORRUPTIONS))
def test_corrupted_baseline_artifact_is_rejected(valid_pair: dict[str, Path], case: str) -> None:
    transform, message = ARTIFACT_CORRUPTIONS[case]
    _rewrite(valid_pair["b_art"], transform)
    with pytest.raises(AuditError, match=message) as excinfo:
        _run(valid_pair)
    assert "Baseline candidate-set artifact" in str(excinfo.value)


def test_corrupted_current_artifact_is_identified(valid_pair: dict[str, Path]) -> None:
    _rewrite(valid_pair["c_art"], lambda t: t.replace("d\t4\n", "d\t5\n"))
    with pytest.raises(AuditError, match=r"Current candidate-set artifact .* fingerprint"):
        _run(valid_pair)


def test_invalid_utf8_artifact_is_rejected(valid_pair: dict[str, Path]) -> None:
    data = valid_pair["b_art"].read_bytes()
    valid_pair["b_art"].write_bytes(data.replace(b"b\t1\n", b"b\xff\xfe\t1\n"))
    with pytest.raises(AuditError, match="not valid UTF-8"):
        _run(valid_pair)


def test_non_canonical_and_self_pairs_rejected_for_dedupe(tmp_path: Path) -> None:
    b_art, b_rep, _ = _artifact(tmp_path, "base", [("a", "b"), ("a", "c")], "dedupe")
    c_art, c_rep, _ = _artifact(tmp_path, "curr", [("a", "b")], "dedupe")
    _rewrite(b_art, lambda t: t.replace("a\tc\n", "c\ta\n"))
    with pytest.raises(AuditError, match="non-canonical pair"):
        _compare_files(b_art, b_rep, c_art, c_rep)
    _rewrite(b_art, lambda t: t.replace("c\ta\n", "c\tc\n"))
    with pytest.raises(AuditError, match="self-pair"):
        _compare_files(b_art, b_rep, c_art, c_rep)


def test_cross_artifact_keeps_reversed_pairs(tmp_path: Path) -> None:
    drift = _drift(tmp_path, [("b", "a"), ("a", "b")], [("b", "a")])["metrics"]["candidate_set_drift"]
    assert (drift["retained_pairs"], drift["removed_pairs"]) == (1, 1)


def test_missing_and_directory_artifacts(valid_pair: dict[str, Path], tmp_path: Path) -> None:
    files = dict(valid_pair)
    files["b_art"] = tmp_path / "nope.tsv"
    with pytest.raises(AuditError, match="Baseline candidate-set artifact does not exist"):
        _run(files)
    files["b_art"] = tmp_path
    with pytest.raises(AuditError, match="Baseline candidate-set artifact is not a regular file"):
        _run(files)
    files = dict(valid_pair)
    files["c_art"] = tmp_path / "missing" / "curr.tsv"
    with pytest.raises(AuditError, match="Current candidate-set artifact does not exist"):
        _run(files)


def test_swapped_artifacts_fail_verification(valid_pair: dict[str, Path]) -> None:
    files = dict(valid_pair, b_art=valid_pair["c_art"], c_art=valid_pair["b_art"])
    # The current reader overruns its recorded 3 pairs before the baseline reaches its end.
    with pytest.raises(AuditError, match=r"Current candidate-set artifact .* more pairs than the 3"):
        _run(files)
    same = dict(valid_pair, c_art=valid_pair["b_art"])  # baseline artifact reused for the current report
    with pytest.raises(AuditError, match=r"Current candidate-set artifact .* more pairs than the 3"):
        _run(same)


def test_crlf_line_endings_do_not_change_logical_pairs(valid_pair: dict[str, Path]) -> None:
    _rewrite(valid_pair["b_art"], lambda t: t.replace("\n", "\r\n"))
    drift = _run(valid_pair)["metrics"]["candidate_set_drift"]
    assert drift["baseline_unique_pairs"] == 4


def _edit_report(path: Path, edit) -> None:  # type: ignore[no-untyped-def]
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(json.dumps(data), encoding="utf-8")


METADATA_PROBLEMS = {
    "count-mismatch": (lambda d: d["candidate_set"].update(unique_pairs=3), "does not match inputs.unique_candidate_pairs"),
    "count-and-inputs-mismatch": (
        lambda d: (d["candidate_set"].update(unique_pairs=5), d["inputs"].update(unique_candidate_pairs=5)),
        "more pairs than the 5|contains 4 pair",
    ),
    "wrong-fingerprint": (lambda d: d["candidate_set"].update(fingerprint="sha256:" + "0" * 64), "fingerprint"),
    "malformed-fingerprint": (lambda d: d["candidate_set"].update(fingerprint="md5:abc"), "64 hex digits"),
    "fingerprint-version": (lambda d: d["candidate_set"].update(fingerprint_version="v9"), "Unsupported candidate-set fingerprint version"),
    "format": (lambda d: d["candidate_set"].update(format="candguard-pairs-parquet-v1"), "Unsupported candidate-set artifact format"),
    "linkage-mismatch": (lambda d: d["candidate_set"].update(linkage_type="dedupe"), "does not match the report linkage type"),
    "bool-count": (lambda d: d["candidate_set"].update(unique_pairs=True), "non-negative integer"),
    "negative-count": (lambda d: d["candidate_set"].update(unique_pairs=-1), "non-negative integer"),
    "not-object": (lambda d: d.update(candidate_set=[1, 2]), "expected an object"),
    "missing": (lambda d: d.pop("candidate_set"), "--candidate-set-out"),
}


@pytest.mark.parametrize("case", list(METADATA_PROBLEMS))
def test_invalid_candidate_set_metadata(valid_pair: dict[str, Path], case: str) -> None:
    edit, message = METADATA_PROBLEMS[case]
    _edit_report(valid_pair["b_rep"], edit)
    with pytest.raises(AuditError, match=message):
        _run(valid_pair)


def test_report_linkage_types_must_match(tmp_path: Path) -> None:
    b_art, b_rep, _ = _artifact(tmp_path, "base", [("a", "b")], "cross")
    c_art, c_rep, _ = _artifact(tmp_path, "curr", [("a", "b")], "dedupe")
    with pytest.raises(AuditError, match="Incompatible linkage types"):
        _compare_files(b_art, b_rep, c_art, c_rep)


def test_old_report_without_metadata_explains_rerun(tmp_path: Path) -> None:
    _, old_rep, _ = _audit(tmp_path, "old", CROSS_ROWS, export=False)
    _, new_rep, new_art = _audit(tmp_path, "new", CROSS_ROWS)
    with pytest.raises(AuditError, match=r"Baseline report .* no 'candidate_set' metadata.*--candidate-set-out"):
        _compare_files(new_art, old_rep, new_art, new_rep)
    # Old reports remain valid for the existing comparison.
    assert compare_reports(baseline_path=old_rep, current_path=new_rep)["quality_gate"]["passed"]


# --------------------------------------------------------------------------------------------
# Streaming behavior and resources
# --------------------------------------------------------------------------------------------

class _SpyFile(io.TextIOBase):
    """Wraps a real text file; forbids bulk reads and records line iteration and closing."""

    instances: list["_SpyFile"] = []

    def __init__(self, real: Any) -> None:
        self._real = real
        self.lines_read = 0
        _SpyFile.instances.append(self)

    def __iter__(self) -> "_SpyFile":
        return self

    def __next__(self) -> str:
        line = next(self._real)
        self.lines_read += 1
        return line

    def read(self, *args: Any) -> str:  # pragma: no cover - must never be called
        raise AssertionError("artifact read() must not be used")

    def readlines(self, *args: Any) -> list[str]:  # pragma: no cover - must never be called
        raise AssertionError("artifact readlines() must not be used")

    def close(self) -> None:
        self._real.close()
        super().close()


@pytest.fixture()
def spy_open(monkeypatch: pytest.MonkeyPatch) -> type[_SpyFile]:
    _SpyFile.instances = []

    def opener(*args: Any, **kwargs: Any) -> _SpyFile:
        return _SpyFile(open(*args, **kwargs))

    monkeypatch.setattr(cs, "open", opener, raising=False)
    return _SpyFile


def test_comparison_streams_line_by_line_and_closes_files(valid_pair: dict[str, Path], spy_open: type[_SpyFile]) -> None:
    _run(valid_pair)
    assert len(spy_open.instances) == 2
    assert [s.lines_read for s in spy_open.instances] == [5, 4]  # header + every pair, nothing skipped
    assert all(s.closed for s in spy_open.instances)


def test_merge_receives_iterators_not_materialized_lists(valid_pair: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real_merge = cs.merge_sorted_pair_streams

    def spy(baseline: Any, current: Any) -> tuple[int, int, int]:
        seen.extend([baseline, current])
        return real_merge(baseline, current)

    monkeypatch.setattr(cs, "merge_sorted_pair_streams", spy)
    _run(valid_pair)
    assert all(isinstance(arg, CandidateSetArtifactReader) for arg in seen)
    assert not any(isinstance(arg, (list, tuple, set, dict)) for arg in seen)
    assert all(arg.verified for arg in seen)


def test_comparison_does_not_use_python_sets(valid_pair: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "set", lambda *a: (_ for _ in ()).throw(AssertionError("set() used")), raising=False)
    monkeypatch.setattr(cs, "list", lambda *a: (_ for _ in ()).throw(AssertionError("list() used")), raising=False)
    assert _run(valid_pair)["metrics"]["candidate_set_drift"]["retained_pairs"] == 2


def test_early_read_error_is_raised_without_reading_everything(tmp_path: Path, spy_open: type[_SpyFile]) -> None:
    b_art, b_rep, _ = _artifact(tmp_path, "base", _seq_pairs(0, 5000))
    c_art, c_rep, _ = _artifact(tmp_path, "curr", _seq_pairs(0, 5000))
    lines = b_art.read_text(encoding="utf-8").split("\n")
    lines[2], lines[3] = lines[3], lines[2]
    b_art.write_bytes("\n".join(lines).encode("utf-8"))
    with pytest.raises(AuditError, match="line 4: pairs are not in ascending"):
        _compare_files(b_art, b_rep, c_art, c_rep)
    assert max(s.lines_read for s in spy_open.instances) < 10
    assert all(s.closed for s in spy_open.instances)
    os.remove(b_art)  # closed on failure, so removable even on Windows
    os.remove(c_art)


def test_both_artifacts_fully_verified_when_one_side_ends_early(tmp_path: Path) -> None:
    # ("A", "1") sorts before every ("L...", ...) pair, so the baseline is exhausted first.
    b_art, b_rep, _ = _artifact(tmp_path, "base", [("A", "1")])
    c_art, c_rep, _ = _artifact(tmp_path, "curr", list(_seq_pairs(0, 300)))
    _rewrite(c_art, lambda t: t[: t.rindex("L")] + "L0300\tR000000299\n")
    with pytest.raises(AuditError, match="Current candidate-set artifact .* fingerprint"):
        _compare_files(b_art, b_rep, c_art, c_rep)
    b_art2, b_rep2, _ = _artifact(tmp_path, "base2", list(_seq_pairs(0, 300)))
    c_art2, c_rep2, _ = _artifact(tmp_path, "curr2", [("A", "1")])
    _rewrite(b_art2, lambda t: t + "zzz\t1\n")
    with pytest.raises(AuditError, match="Baseline candidate-set artifact .* more pairs"):
        _compare_files(b_art2, b_rep2, c_art2, c_rep2)


def test_large_synthetic_comparison_is_exact(tmp_path: Path) -> None:
    n, shift = 100_000, 12_345
    b_meta = _write_sorted_artifact(tmp_path / "b.tsv", _seq_pairs(0, n))
    c_meta = _write_sorted_artifact(tmp_path / "c.tsv", _seq_pairs(shift, n + shift + 500))
    for name, meta in (("b", b_meta), ("c", c_meta)):
        (tmp_path / f"{name}.json").write_text(
            json.dumps(_minimal_report(meta, linkage_type="cross", unique_pairs=meta["unique_pairs"])), encoding="utf-8"
        )
    drift = _compare_files(tmp_path / "b.tsv", tmp_path / "b.json", tmp_path / "c.tsv", tmp_path / "c.json")[
        "metrics"
    ]["candidate_set_drift"]
    assert drift["retained_pairs"] == n - shift
    assert drift["removed_pairs"] == shift
    assert drift["added_pairs"] == shift + 500
    assert drift["union_pairs"] == n + shift + 500


# Memory-growth measurement.
#
# The traced peak of one comparison is about 80 KB and does not depend on the number of pairs
# (measured flat from 5,000 to 1,000,000 pairs). In one local configuration (Windows, Python
# 3.12, full suite without coverage) a single measurement occasionally included an unrelated
# one-off allocation of about 0.96 MB, giving 1,039,917 bytes. It depended on the process's
# allocation history: changing almost anything (running the test alone, adding coverage,
# disabling a pytest plugin, moving the checkout) made it disappear. So the measurement is
# designed to be insensitive to one-off allocations while still catching per-pair growth:
#
# - ``gc.collect()`` before each measurement, so garbage left by earlier tests is not freed or
#   finalized inside the measured window;
# - each size is measured ``_MEMORY_REPEATS`` times and the minimum is used: a one-off
#   allocation can inflate only one run, while a real streaming regression (for example
#   materializing the pairs) recurs in every run;
# - the gate is on growth between sizes, not on an absolute figure alone.
_MEMORY_REPEATS = 2
_SMALL_PAIRS = 5_000
_LARGE_PAIRS = 100_000
#: Allowed traced-peak growth from the small to the large comparison: about 1 byte per added
#: baseline pair. Retaining pairs costs far more (a list alone stores an 8-byte pointer per pair,
#: plus the tuple and its strings), so any per-pair retention exceeds this budget.
_MAX_PEAK_GROWTH = _LARGE_PAIRS - _SMALL_PAIRS
#: Absolute sanity bound on one streaming comparison's traced peak (normally about 80 KB).
_MAX_PEAK = 1_000_000


def _traced_compare_peak(tmp_path: Path, n: int, repeats: int = _MEMORY_REPEATS) -> int:
    """Return the minimum traced peak (bytes) of ``repeats`` comparisons of two n-pair artifacts."""
    work = tmp_path / str(n)
    work.mkdir()
    b_meta = _write_sorted_artifact(work / "b.tsv", _seq_pairs(0, n))
    c_meta = _write_sorted_artifact(work / "c.tsv", _seq_pairs(n // 3, n + n // 3))
    peaks: list[int] = []
    for _ in range(repeats):
        gc.collect()
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            start, _ = tracemalloc.get_traced_memory()
            counts = cs.compare_candidate_set_artifacts(work / "b.tsv", work / "c.tsv", b_meta, c_meta)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert counts == (n - n // 3, n // 3, n // 3)
        peaks.append(peak - start)
    return min(peaks)


def test_comparison_memory_does_not_grow_with_set_size(tmp_path: Path) -> None:
    small = _traced_compare_peak(tmp_path, _SMALL_PAIRS)
    large = _traced_compare_peak(tmp_path, _LARGE_PAIRS)
    # 20x more pairs; streaming keeps the traced peak essentially flat (a set would add ~10+ MB).
    assert large - small < _MAX_PEAK_GROWTH, (small, large)
    assert large < _MAX_PEAK, (small, large)


def test_memory_measurement_detects_materialized_pairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: the measurement above must flag a comparison that retains every pair."""
    streaming_merge = cs.merge_sorted_pair_streams

    def materializing_merge(baseline: Iterator[Pair], current: Iterator[Pair]) -> tuple[int, int, int]:
        return streaming_merge(iter(list(baseline)), iter(list(current)))

    monkeypatch.setattr(cs, "merge_sorted_pair_streams", materializing_merge)
    small = _traced_compare_peak(tmp_path, _SMALL_PAIRS, repeats=1)
    large = _traced_compare_peak(tmp_path, _LARGE_PAIRS, repeats=1)
    assert large - small >= _MAX_PEAK_GROWTH, (small, large)


# --------------------------------------------------------------------------------------------
# Threshold behavior
# --------------------------------------------------------------------------------------------

FOUR_OF_FIVE = ([("a", "1"), ("a", "2"), ("b", "1"), ("c", "1")], [("a", "1"), ("a", "2"), ("b", "1"), ("c", "1"), ("d", "1")])
NINE_OF_TEN = ([(f"k{i}", "x") for i in range(9)], [(f"k{i}", "x") for i in range(10)])


@pytest.mark.parametrize(
    ("sets", "threshold", "passed"),
    [
        pytest.param(FOUR_OF_FIVE, 0.8, True, id="equal-0.8-passes"),
        pytest.param(NINE_OF_TEN, 0.9, True, id="equal-0.9-passes"),
        pytest.param(FOUR_OF_FIVE, 0.75, True, id="above-passes"),
        pytest.param(FOUR_OF_FIVE, 0.8000001, False, id="just-below-fails"),
        pytest.param(FOUR_OF_FIVE, 0.0, True, id="zero-threshold"),
        pytest.param(([("a", "1")], [("b", "1")]), 0.0, True, id="zero-threshold-disjoint"),
        pytest.param(FOUR_OF_FIVE, 1.0, False, id="one-threshold-fails-on-any-change"),
        pytest.param(([("a", "1")], [("a", "1")]), 1.0, True, id="one-threshold-identical"),
        pytest.param(([], []), 1.0, True, id="one-threshold-both-empty"),
        pytest.param(FOUR_OF_FIVE, 1, False, id="integer-one"),
    ],
)
def test_min_candidate_jaccard_gate(tmp_path: Path, sets: tuple[list[Pair], list[Pair]], threshold: float, passed: bool) -> None:
    report = _drift(tmp_path, sets[0], sets[1], min_candidate_jaccard=threshold)
    assert report["quality_gate"]["passed"] is passed
    assert report["thresholds"]["min_candidate_jaccard"] == threshold
    if not passed:
        (failure,) = report["quality_gate"]["failures"]
        assert failure.startswith("candidate-set Jaccard similarity")
        assert "below required minimum" in failure and "retained of" in failure


def test_drift_without_threshold_is_diagnostic_only(tmp_path: Path) -> None:
    report = _drift(tmp_path, [("a", "1")], [("b", "2")])
    assert report["metrics"]["candidate_set_drift"]["jaccard_similarity"] == 0.0
    assert report["quality_gate"] == {"passed": True, "failures": []}
    assert report["thresholds"]["min_candidate_jaccard"] is None


def test_jaccard_gate_combines_with_existing_gates(tmp_path: Path) -> None:
    report = _drift(tmp_path, [("a", "1"), ("b", "1")], [("c", "1"), ("d", "1"), ("e", "1")],
                    min_candidate_jaccard=0.5, max_pair_growth=0.1)
    failures = report["quality_gate"]["failures"]
    assert len(failures) == 2
    assert failures[0].startswith("candidate row growth") and failures[1].startswith("candidate-set Jaccard")


@pytest.mark.parametrize("bad", [-0.01, 1.0000001, 2, float("nan"), float("inf"), float("-inf"), True, False, "0.9", [0.9]])
def test_invalid_min_candidate_jaccard_rejected_before_reading_files(tmp_path: Path, bad: Any) -> None:
    with pytest.raises(AuditError, match="min_candidate_jaccard must be a finite number between 0 and 1"):
        compare_reports(
            baseline_path=tmp_path / "missing.json",
            current_path=tmp_path / "missing.json",
            baseline_candidate_set_path=tmp_path / "b.tsv",
            current_candidate_set_path=tmp_path / "c.tsv",
            min_candidate_jaccard=bad,
        )


def test_threshold_and_artifacts_must_be_supplied_together(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(AuditError, match="requires both --baseline-candidate-set and --current-candidate-set"):
        compare_reports(baseline_path=missing, current_path=missing, min_candidate_jaccard=0.9)
    with pytest.raises(AuditError, match="the current candidate-set artifact was not supplied"):
        compare_reports(baseline_path=missing, current_path=missing, baseline_candidate_set_path=tmp_path / "b.tsv")
    with pytest.raises(AuditError, match="the baseline candidate-set artifact was not supplied"):
        compare_reports(baseline_path=missing, current_path=missing, current_candidate_set_path=tmp_path / "c.tsv")


# --------------------------------------------------------------------------------------------
# Reports, rendering, CLI
# --------------------------------------------------------------------------------------------

def test_compare_report_structure_with_drift(valid_pair: dict[str, Path]) -> None:
    report = _run(valid_pair, min_candidate_jaccard=0.25)
    assert report["schema_version"] == "1.0"
    assert set(report["metrics"]["candidate_set_drift"]) == {
        "baseline_unique_pairs", "current_unique_pairs", "retained_pairs", "added_pairs", "removed_pairs",
        "union_pairs", "jaccard_similarity", "pair_churn", "removed_fraction_of_baseline", "added_fraction_of_current",
    }
    assert report["baseline"]["candidate_set_path"] == str(valid_pair["b_art"])
    assert report["current"]["candidate_set_fingerprint"].startswith("sha256:")
    assert report["metrics"]["candidate_set_drift"]["jaccard_similarity"] == 0.4


def test_render_compare_text_with_drift(valid_pair: dict[str, Path]) -> None:
    text = render_compare_text(_run(valid_pair, min_candidate_jaccard=0.5))
    section = text[text.index("Candidate-set drift (unique pairs)"):]
    assert section.splitlines() == [
        "Candidate-set drift (unique pairs)",
        "----------------------------------",
        "Baseline unique pairs:          4",
        "Current unique pairs:           3",
        "Retained pairs:                 2",
        "Removed from baseline:          2 (50.00%)",
        "Added in current:               1 (33.33%)",
        "Union of candidate pairs:       5",
        "Jaccard similarity:             40.0000%",
        "Pair churn:                     60.0000%",
        "Minimum Jaccard similarity:     50.0000%",
        "",
        "Quality gate: FAIL",
        "  - candidate-set Jaccard similarity 0.40000000 (40.0000%; 2 retained of 5 union pairs) "
        "is below required minimum 0.50000000 (50.0000%)",
    ]


def test_render_compare_text_with_empty_sides_shows_na(tmp_path: Path) -> None:
    text = render_compare_text(_drift(tmp_path, [], []))
    assert "Removed from baseline:          0 (n/a)" in text
    assert "Added in current:               0 (n/a)" in text
    assert "Jaccard similarity:             100.0000%" in text
    assert "Minimum Jaccard similarity" not in text


def test_cli_end_to_end_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _write_delimited(tmp_path / "base.tsv", [("A1", "B1"), ("A1", "B2"), ("A2", "B3"), ("A3", "B4")], header=("source_id", "candidate_id"))
    curr = _write_delimited(tmp_path / "curr.tsv", [("A1", "B1"), ("A1", "B9"), ("A2", "B8"), ("A3", "B7")], header=("source_id", "candidate_id"))
    for name, path in (("base", base), ("curr", curr)):
        assert main([
            "audit", "--candidates", str(path), "--left-id", "source_id", "--right-id", "candidate_id",
            "--candidate-set-out", str(tmp_path / "artifacts" / f"{name}.pairs.tsv"),
            "--format", "json", "--output", str(tmp_path / f"{name}.json"),
        ]) == 0
    common = ["compare", "--baseline", str(tmp_path / "base.json"), "--current", str(tmp_path / "curr.json")]
    artifacts = ["--baseline-candidate-set", str(tmp_path / "artifacts" / "base.pairs.tsv"),
                 "--current-candidate-set", str(tmp_path / "artifacts" / "curr.pairs.tsv")]
    assert main(common) == 0  # unchanged count-only comparison passes
    assert main(common + artifacts + ["--min-candidate-jaccard", "0.10"]) == 0
    capsys.readouterr()
    assert main(common + artifacts + ["--min-candidate-jaccard", "0.90", "--format", "json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["metrics"]["candidate_set_drift"]["jaccard_similarity"] == 0.14285714
    assert main(common + artifacts + ["--min-candidate-jaccard", "nan"]) == 2
    assert main(common + ["--min-candidate-jaccard", "0.5"]) == 2
    assert main(common + artifacts[:2]) == 2
    _rewrite(tmp_path / "artifacts" / "curr.pairs.tsv", lambda t: t.replace("B9", "B0"))
    assert main(common + artifacts) == 2
    assert "Current candidate-set artifact" in capsys.readouterr().err


def test_cli_rejects_candidate_set_out_equal_to_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidates = _write_delimited(tmp_path / "c.tsv", CROSS_ROWS)
    out = tmp_path / "same.json"
    code = main(["audit", "--candidates", str(candidates), "--left-id", "l", "--right-id", "r",
                 "--candidate-set-out", str(out), "--format", "json", "--output", str(out)])
    assert code == 2
    assert "must be different files" in capsys.readouterr().err
    assert not out.exists()


def test_cli_audit_gate_failure_still_exports_artifact(tmp_path: Path) -> None:
    candidates = _write_delimited(tmp_path / "c.tsv", CROSS_ROWS)
    out = tmp_path / "c.pairs.tsv"
    assert main(["audit", "--candidates", str(candidates), "--left-id", "l", "--right-id", "r",
                 "--max-pairs", "1", "--candidate-set-out", str(out)]) == 1
    assert out.is_file()


# --------------------------------------------------------------------------------------------
# Backward compatibility
# --------------------------------------------------------------------------------------------

GOLDEN_COMPARE_TEXT_NO_GATES = (
    "CandGuard — Regression Comparison Audit\n"
    "==========================================\n"
    "Baseline report:                BASE\n"
    "Current report:                 CURR\n"
    "Linkage type:                   cross\n"
    "\n"
    "Candidate rows:                 5 -> 6 (+20.00%)\n"
    "Unique candidate pairs:         4 -> 4 (+0.00%)\n"
    "Duplicate candidate rows:       1 -> 2 (+100.00%)\n"
    "Candidate recall:               66.6667% -> 66.6667% (drop: 0.0000)\n"
    "\n"
    "Quality gate: PASS"
)
# The reports are produced by the running package, so the version fields follow
# candguard.__version__; every other field must match the golden output exactly.
GOLDEN_SIDE = {
    "candguard_version": candguard_version,
    "candidate_recall": 0.66666667,
    "ground_truth_fingerprint": "sha256:2e3c100e15eac48452552ee384f1b10eb8bf8e7bf23264c489042ad4ae3505c2",
    "ground_truth_fingerprint_version": "v2",
    "linkage_type": "cross",
    "unique_candidate_pairs": 4,
}


def _golden_reports(tmp_path: Path, export: bool) -> tuple[Path, Path, Path, Path]:
    truth = _write_delimited(tmp_path / "t.tsv", [("A1", "B1"), ("A2", "B3"), ("A3", "B7")])
    _, b_rep, b_art = _audit(tmp_path, "c1", [("A1", "B1"), ("A1", "B2"), ("A2", "B3"), ("A3", "B4"), ("A1", "B1")],
                             truth_path=truth, export=export)
    _, c_rep, c_art = _audit(tmp_path, "c2", [("A1", "B1"), ("A1", "B9"), ("A2", "B8"), ("A3", "B7"), ("A3", "B7"), ("A3", "B7")],
                             truth_path=truth, export=export)
    return b_rep, c_rep, b_art, c_art


@pytest.mark.parametrize("export", [False, True])
def test_legacy_compare_output_unchanged_from_main(tmp_path: Path, export: bool) -> None:
    """Golden outputs produced by main @ 7d77822 for the same reports (paths normalized)."""
    b_rep, c_rep, _, _ = _golden_reports(tmp_path, export)
    report = compare_reports(baseline_path=b_rep, current_path=c_rep)
    report["baseline"]["path"], report["current"]["path"] = "BASE", "CURR"
    assert report == {
        "tool": "candguard",
        "version": candguard_version,
        "command": "compare",
        "schema_version": "1.0",
        "baseline": {**GOLDEN_SIDE, "path": "BASE", "candidate_rows": 5, "duplicate_candidate_rows": 1},
        "current": {**GOLDEN_SIDE, "path": "CURR", "candidate_rows": 6, "duplicate_candidate_rows": 2},
        "metrics": {
            "recall_drop": 0.0,
            "candidate_pair_growth": 0.2,
            "candidate_pair_growth_unbounded": False,
            "duplicate_row_growth": 1.0,
            "duplicate_row_growth_unbounded": False,
        },
        "thresholds": {"max_recall_drop": None, "max_pair_growth": None, "max_duplicate_growth": None},
        "quality_gate": {"passed": True, "failures": []},
    }
    assert render_compare_text(report) == GOLDEN_COMPARE_TEXT_NO_GATES

    gated = compare_reports(baseline_path=b_rep, current_path=c_rep, max_recall_drop=0.5, max_pair_growth=0.1,
                            max_duplicate_growth=1.0)
    assert gated["quality_gate"] == {"passed": False, "failures": ["candidate row growth 20.00% exceeds limit 10.00%"]}
    assert gated["thresholds"] == {"max_recall_drop": 0.5, "max_pair_growth": 0.1, "max_duplicate_growth": 1.0}


def test_same_reports_with_artifacts_detect_the_hidden_composition_change(tmp_path: Path) -> None:
    b_rep, c_rep, b_art, c_art = _golden_reports(tmp_path, export=True)
    legacy = compare_reports(baseline_path=b_rep, current_path=c_rep, max_recall_drop=0.0, max_pair_growth=0.25)
    assert legacy["quality_gate"]["passed"]  # counts and recall see no regression
    drift_report = _compare_files(b_art, b_rep, c_art, c_rep, max_recall_drop=0.0, max_pair_growth=0.25,
                                  min_candidate_jaccard=0.5)
    drift = drift_report["metrics"]["candidate_set_drift"]
    assert (drift["retained_pairs"], drift["removed_pairs"], drift["added_pairs"]) == (1, 3, 3)
    assert not drift_report["quality_gate"]["passed"]
    for key in ("recall_drop", "candidate_pair_growth", "duplicate_row_growth"):
        assert drift_report["metrics"][key] == legacy["metrics"][key]


def test_compare_without_artifacts_never_touches_artifact_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import candguard.compare as compare_module

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("candidate-set code must not run without artifacts")

    monkeypatch.setattr(compare_module, "compare_candidate_set_artifacts", forbidden)
    monkeypatch.setattr(compare_module, "validate_candidate_set_metadata", forbidden)
    b_rep, c_rep, _, _ = _golden_reports(tmp_path, export=True)
    report = compare_reports(baseline_path=b_rep, current_path=c_rep)
    assert "candidate_set_drift" not in report["metrics"]
    assert "min_candidate_jaccard" not in report["thresholds"]
    assert "candidate_set_path" not in report["baseline"]
    assert "Candidate-set drift" not in render_compare_text(report)


def test_public_signatures_are_backward_compatible() -> None:
    audit_params = inspect.signature(audit_candidates).parameters
    assert list(audit_params)[:15] == [
        "candidates_path", "left_id", "right_id", "truth_path", "truth_left_id", "truth_right_id", "delimiter",
        "truth_delimiter", "linkage_type", "left_records", "right_records", "min_recall", "max_pairs",
        "max_duplicate_rows", "missed_pairs_path",
    ]
    assert list(audit_params)[15:] == ["candidate_set_path"]
    compare_params = inspect.signature(compare_reports).parameters
    assert list(compare_params) == [
        "baseline_path", "current_path", "max_recall_drop", "max_pair_growth", "max_duplicate_growth",
        "baseline_candidate_set_path", "current_candidate_set_path", "min_candidate_jaccard",
    ]
    for params, new in ((audit_params, ["candidate_set_path"]),
                        (compare_params, ["baseline_candidate_set_path", "current_candidate_set_path", "min_candidate_jaccard"])):
        for name in new:
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert params[name].default is None


def test_drift_metrics_are_finite_and_rounded(valid_pair: dict[str, Path]) -> None:
    drift = _run(valid_pair)["metrics"]["candidate_set_drift"]
    for key in ("jaccard_similarity", "pair_churn", "removed_fraction_of_baseline", "added_fraction_of_current"):
        assert math.isfinite(drift[key]) and drift[key] == round(drift[key], 8)

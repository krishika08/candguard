import json
from pathlib import Path
import pytest

from candguard.cli import main


def test_cli_exit_code_0_on_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidates = tmp_path / "cand.tsv"
    candidates.write_text("s\tc\na\tb\n", encoding="utf-8")
    code = main(["audit", "--candidates", str(candidates), "--left-id", "s", "--right-id", "c"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Quality gate: PASS" in captured.out


def test_cli_exit_code_1_on_quality_gate_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidates = tmp_path / "cand.tsv"
    candidates.write_text("s\tc\na\tb\n", encoding="utf-8")
    truth = tmp_path / "truth.tsv"
    truth.write_text("s\tc\na\tb\nx\ty\n", encoding="utf-8")

    code = main([
        "audit",
        "--candidates", str(candidates),
        "--truth", str(truth),
        "--left-id", "s",
        "--right-id", "c",
        "--min-recall", "0.99",
    ])
    assert code == 1
    captured = capsys.readouterr()
    assert "Quality gate: FAIL" in captured.out


def test_cli_exit_code_2_on_invalid_configuration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidates = tmp_path / "cand.tsv"
    candidates.write_text("s\tc\na\tb\n", encoding="utf-8")

    # --min-recall without --truth is invalid config (exit code 2)
    code = main([
        "audit",
        "--candidates", str(candidates),
        "--left-id", "s",
        "--right-id", "c",
        "--min-recall", "0.90",
    ])
    assert code == 2
    captured = capsys.readouterr()
    assert "candguard: error:" in captured.err


def test_cli_exit_code_2_on_missing_column(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidates = tmp_path / "cand.tsv"
    candidates.write_text("colA\tcolB\na\tb\n", encoding="utf-8")

    code = main([
        "audit",
        "--candidates", str(candidates),
        "--left-id", "wrong_col",
        "--right-id", "colB",
    ])
    assert code == 2
    captured = capsys.readouterr()
    assert "Missing column(s)" in captured.err


def test_cli_json_format_and_output_file(tmp_path: Path) -> None:
    candidates = tmp_path / "cand.tsv"
    candidates.write_text("s\tc\na\tb\n", encoding="utf-8")
    out_file = tmp_path / "report.json"

    code = main([
        "audit",
        "--candidates", str(candidates),
        "--left-id", "s",
        "--right-id", "c",
        "--format", "json",
        "--output", str(out_file),
    ])
    assert code == 0
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["tool"] == "candguard"
    assert data["quality_gate"]["passed"] is True


def test_cli_output_write_error_returns_code_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidates = tmp_path / "cand.tsv"
    candidates.write_text("s\tc\na\tb\n", encoding="utf-8")

    # Target an invalid path that cannot be written (file as parent directory)
    dummy_file = tmp_path / "dummy_file"
    dummy_file.write_text("content", encoding="utf-8")
    bad_out = dummy_file / "sub" / "report.txt"

    code = main([
        "audit",
        "--candidates", str(candidates),
        "--left-id", "s",
        "--right-id", "c",
        "--output", str(bad_out),
    ])
    assert code == 2
    captured = capsys.readouterr()
    assert "Could not write output" in captured.err


def test_cli_delimiter_override_tab(tmp_path: Path) -> None:
    candidates = tmp_path / "cand.txt"
    candidates.write_text("s\tc\na\tb\n", encoding="utf-8")

    code = main([
        "audit",
        "--candidates", str(candidates),
        "--left-id", "s",
        "--right-id", "c",
        "--delimiter", r"\t",
    ])
    assert code == 0


def _sample_report_dict(
    rows: int = 100,
    dupes: int = 10,
    recall: float = 0.90,
    fingerprint: str = "sha256:fixedfingerprint123",
) -> dict:
    return {
        "tool": "candguard",
        "version": "0.1.0",
        "schema_version": "1.0",
        "inputs": {
            "candidates": "cand.tsv",
            "truth": "truth.tsv",
            "candidate_rows": rows,
            "unique_candidate_pairs": rows - dupes,
            "duplicate_candidate_rows": dupes,
        },
        "coverage": {"left_records_represented": 50, "right_records_represented": 50},
        "candidate_distribution_per_left_record": {
            "min": 1, "p50": 2, "mean": 2.0, "p95": 3, "p99": 3, "max": 3, "sources_with_candidates": 50,
        },
        "efficiency": {"linkage_type": "cross", "possible_pairs": 1000, "reduction_ratio": 0.90},
        "ground_truth": {
            "rows": 50, "unique_pairs": 50, "recovered_pairs": 45, "missed_pairs": 5,
            "candidate_recall": recall, "fingerprint": fingerprint, "fingerprint_version": "v2",
        },
        "quality_gate": {"passed": True, "failures": []},
    }


def test_cli_compare_exit_code_0_on_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rep = _sample_report_dict()
    base_f = tmp_path / "base.json"
    curr_f = tmp_path / "curr.json"
    base_f.write_text(json.dumps(rep), encoding="utf-8")
    curr_f.write_text(json.dumps(rep), encoding="utf-8")

    code = main([
        "compare",
        "--baseline", str(base_f),
        "--current", str(curr_f),
        "--max-recall-drop", "0.01",
        "--max-pair-growth", "0.10",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "Quality gate: PASS" in captured.out


def test_cli_compare_exit_code_1_on_gate_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base_f = tmp_path / "base.json"
    curr_f = tmp_path / "curr.json"
    # Current has recall drop from 0.90 to 0.80 (0.10 drop)
    base_f.write_text(json.dumps(_sample_report_dict(recall=0.90)), encoding="utf-8")
    curr_f.write_text(json.dumps(_sample_report_dict(recall=0.80)), encoding="utf-8")

    code = main([
        "compare",
        "--baseline", str(base_f),
        "--current", str(curr_f),
        "--max-recall-drop", "0.05",
    ])
    assert code == 1
    captured = capsys.readouterr()
    assert "Quality gate: FAIL" in captured.out
    assert "candidate recall drop" in captured.out


def test_cli_compare_exit_code_2_on_invalid_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base_f = tmp_path / "base.json"
    curr_f = tmp_path / "curr.json"
    # Mismatched fingerprint with recall drop requested -> exit code 2
    base_f.write_text(json.dumps(_sample_report_dict(fingerprint="sha256:111")), encoding="utf-8")
    curr_f.write_text(json.dumps(_sample_report_dict(fingerprint="sha256:222")), encoding="utf-8")

    code = main([
        "compare",
        "--baseline", str(base_f),
        "--current", str(curr_f),
        "--max-recall-drop", "0.05",
    ])
    assert code == 2
    captured = capsys.readouterr()
    assert "candguard: error: Incompatible ground truth" in captured.err


def test_cli_compare_json_format_and_output(tmp_path: Path) -> None:
    rep = _sample_report_dict()
    base_f = tmp_path / "base.json"
    curr_f = tmp_path / "curr.json"
    out_f = tmp_path / "compare_out.json"
    base_f.write_text(json.dumps(rep), encoding="utf-8")
    curr_f.write_text(json.dumps(rep), encoding="utf-8")

    code = main([
        "compare",
        "--baseline", str(base_f),
        "--current", str(curr_f),
        "--format", "json",
        "--output", str(out_f),
    ])
    assert code == 0
    assert out_f.exists()
    data = json.loads(out_f.read_text(encoding="utf-8"))
    assert data["command"] == "compare"
    assert data["quality_gate"]["passed"] is True


# --- compare --output must never resolve to an input file -----------------------------------

def _drift_inputs(tmp_path: Path) -> dict[str, Path]:
    """Create two audit reports with candidate-set sidecars via the CLI."""
    paths: dict[str, Path] = {}
    for name, rows in (("baseline", "a\tb\na\tc\n"), ("current", "a\tb\nd\te\n")):
        candidates = tmp_path / f"{name}_candidates.tsv"
        candidates.write_text("s\tc\n" + rows, encoding="utf-8")
        paths[name] = tmp_path / f"{name}.json"
        paths[f"{name}_set"] = tmp_path / f"{name}.pairs.tsv"
        assert main([
            "audit", "--candidates", str(candidates), "--left-id", "s", "--right-id", "c",
            "--candidate-set-out", str(paths[f"{name}_set"]), "--format", "json", "--output", str(paths[name]),
        ]) == 0
    return paths


def _compare_args(paths: dict[str, Path], output: Path | str, *, with_sets: bool = True) -> list[str]:
    args = ["compare", "--baseline", str(paths["baseline"]), "--current", str(paths["current"])]
    if with_sets:
        args += ["--baseline-candidate-set", str(paths["baseline_set"]),
                 "--current-candidate-set", str(paths["current_set"])]
    return args + ["--format", "json", "--output", str(output)]


def _snapshot(paths: dict[str, Path]) -> dict[str, bytes]:
    return {name: path.read_bytes() for name, path in paths.items()}


def _assert_rejected(
    paths: dict[str, Path],
    output: Path | str,
    flag: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import candguard.cli as cli_module

    def must_not_run(**kwargs: object) -> dict[str, object]:
        raise AssertionError("compare_reports() must not run when --output collides with an input")

    monkeypatch.setattr(cli_module, "compare_reports", must_not_run)
    before = _snapshot(paths)
    capsys.readouterr()
    assert main(_compare_args(paths, output)) == 2
    captured = capsys.readouterr()
    assert f"resolves to the same file as {flag} (" in captured.err
    assert "--output must point to a different file" in captured.err
    assert captured.out == ""  # no report was rendered or written anywhere
    assert _snapshot(paths) == before  # every input is byte-for-byte unchanged


@pytest.mark.parametrize(
    ("key", "flag"),
    [
        ("baseline", "--baseline"),
        ("current", "--current"),
        ("baseline_set", "--baseline-candidate-set"),
        ("current_set", "--current-candidate-set"),
    ],
)
def test_cli_compare_output_cannot_overwrite_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, key: str, flag: str
) -> None:
    paths = _drift_inputs(tmp_path)
    _assert_rejected(paths, paths[key], flag, capsys, monkeypatch)


def test_cli_compare_output_dotdot_alias_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _drift_inputs(tmp_path)
    (tmp_path / "sub").mkdir()
    alias = tmp_path / "sub" / ".." / paths["baseline_set"].name
    _assert_rejected(paths, alias, "--baseline-candidate-set", capsys, monkeypatch)


def test_cli_compare_output_relative_alias_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _drift_inputs(tmp_path)
    monkeypatch.chdir(tmp_path)
    _assert_rejected(paths, Path(".") / paths["current"].name, "--current", capsys, monkeypatch)


def test_cli_compare_output_symlink_alias_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _drift_inputs(tmp_path)
    link = tmp_path / "link.json"
    try:
        link.symlink_to(paths["baseline"])
    except (OSError, NotImplementedError) as exc:  # e.g. Windows without symlink privilege
        pytest.skip(f"symlink creation not supported here: {exc}")
    _assert_rejected(paths, link, "--baseline", capsys, monkeypatch)
    assert link.is_symlink()


@pytest.mark.parametrize("with_sets", [True, False])
def test_cli_compare_separate_output_still_written(tmp_path: Path, with_sets: bool) -> None:
    paths = _drift_inputs(tmp_path)
    before = _snapshot(paths)
    out = tmp_path / "reports" / "compare.json"
    assert main(_compare_args(paths, out, with_sets=with_sets)) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["command"] == "compare"
    assert ("candidate_set_drift" in data["metrics"]) is with_sets
    assert _snapshot(paths) == before


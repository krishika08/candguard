import json
import os
import sys
from pathlib import Path
from typing import Any

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


# --- audit --output must never resolve to an input or another audit output ------------------

def _audit_files(tmp_path: Path) -> dict[str, Path]:
    files = {
        "candidates": tmp_path / "candidates.tsv",
        "truth": tmp_path / "truth.tsv",
        "missed": tmp_path / "missed.tsv",
        "cset": tmp_path / "candidates.pairs.tsv",
    }
    files["candidates"].write_text("s\tc\na\tb\na\tc\nd\te\n", encoding="utf-8")
    files["truth"].write_text("s\tc\na\tb\nx\ty\n", encoding="utf-8")
    files["missed"].write_text("previous missed-pairs artifact\n", encoding="utf-8")
    files["cset"].write_text("previous candidate-set artifact\n", encoding="utf-8")
    return files


def _audit_cli_args(
    files: dict[str, Path], output: Path | str, *, truth: bool = True, candidate_set: bool = False
) -> list[str]:
    args = ["audit", "--candidates", str(files["candidates"]), "--left-id", "s", "--right-id", "c"]
    if truth:
        args += ["--truth", str(files["truth"]), "--missed-pairs-out", str(files["missed"])]
    if candidate_set:
        args += ["--candidate-set-out", str(files["cset"])]
    return args + ["--format", "json", "--output", str(output)]


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _assert_audit_rejected(
    tmp_path: Path,
    args: list[str],
    expected_err: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import candguard.cli as cli_module

    def must_not_run(**kwargs: object) -> dict[str, object]:
        raise AssertionError("audit_candidates() must not run when --output collides with another path")

    monkeypatch.setattr(cli_module, "audit_candidates", must_not_run)
    before = _tree_snapshot(tmp_path)
    capsys.readouterr()
    assert main(args) == 2
    captured = capsys.readouterr()
    for fragment in expected_err:
        assert fragment in captured.err
    assert captured.out == ""  # no report rendered
    assert _tree_snapshot(tmp_path) == before  # no file overwritten, truncated or created


@pytest.mark.parametrize(
    ("key", "flag"),
    [("candidates", "--candidates"), ("truth", "--truth"), ("missed", "--missed-pairs-out")],
)
def test_cli_audit_output_cannot_overwrite_input_or_missed_pairs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, key: str, flag: str
) -> None:
    files = _audit_files(tmp_path)
    _assert_audit_rejected(
        tmp_path,
        _audit_cli_args(files, files[key]),
        [f"--output ({files[key]}) resolves to the same file as {flag} (",
         f"--output must point to a different file so {flag} is not overwritten"],
        capsys,
        monkeypatch,
    )


def test_cli_audit_output_cannot_equal_candidate_set_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _audit_files(tmp_path)
    _assert_audit_rejected(
        tmp_path,
        _audit_cli_args(files, files["cset"], candidate_set=True),
        ["--candidate-set-out (", "and --output (", "must be different files"],
        capsys,
        monkeypatch,
    )


def test_cli_audit_output_relative_alias_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _audit_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    _assert_audit_rejected(
        tmp_path, _audit_cli_args(files, Path(".") / "candidates.tsv"),
        ["resolves to the same file as --candidates ("], capsys, monkeypatch,
    )


def test_cli_audit_output_dotdot_alias_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _audit_files(tmp_path)
    (tmp_path / "sub").mkdir()
    _assert_audit_rejected(
        tmp_path, _audit_cli_args(files, tmp_path / "sub" / ".." / "truth.tsv"),
        ["resolves to the same file as --truth ("], capsys, monkeypatch,
    )


def test_cli_audit_output_symlink_alias_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _audit_files(tmp_path)
    link = tmp_path / "report.json"
    try:
        link.symlink_to(files["candidates"])
    except (OSError, NotImplementedError) as exc:  # e.g. Windows without symlink privilege
        pytest.skip(f"symlink creation not supported here: {exc}")
    _assert_audit_rejected(
        tmp_path, _audit_cli_args(files, link),
        ["resolves to the same file as --candidates ("], capsys, monkeypatch,
    )
    assert link.is_symlink()


def test_cli_audit_separate_output_text_report(tmp_path: Path) -> None:
    files = _audit_files(tmp_path)
    out = tmp_path / "reports" / "audit.txt"
    inputs = {k: files[k].read_bytes() for k in ("candidates", "truth")}
    code = main(["audit", "--candidates", str(files["candidates"]), "--left-id", "s", "--right-id", "c",
                 "--output", str(out)])
    assert code == 0
    assert "Quality gate: PASS" in out.read_text(encoding="utf-8")
    assert {k: files[k].read_bytes() for k in ("candidates", "truth")} == inputs


def test_cli_audit_separate_output_without_truth(tmp_path: Path) -> None:
    files = _audit_files(tmp_path)
    out = tmp_path / "audit.json"
    before = files["candidates"].read_bytes()
    assert main(_audit_cli_args(files, out, truth=False)) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ground_truth"] is None
    assert data["inputs"]["unique_candidate_pairs"] == 3
    assert files["candidates"].read_bytes() == before


def test_cli_audit_separate_output_with_truth_missed_pairs_and_candidate_set(tmp_path: Path) -> None:
    files = _audit_files(tmp_path)
    out = tmp_path / "audit.json"
    inputs = {k: files[k].read_bytes() for k in ("candidates", "truth")}
    assert main(_audit_cli_args(files, out, candidate_set=True)) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ground_truth"]["missed_pairs"] == 1
    assert data["ground_truth"]["missed_pairs_file"] == str(files["missed"])
    assert data["candidate_set"]["unique_pairs"] == 3
    assert files["missed"].read_text(encoding="utf-8") == "s\tc\nx\ty\n"
    assert files["cset"].read_text(encoding="utf-8").startswith("left_id\tright_id\n")
    assert {k: files[k].read_bytes() for k in ("candidates", "truth")} == inputs



# --- --missed-pairs-out must never resolve to an input ---------------------------------------

def _missed_cli_args(candidates: Path | str, truth: Path | str, missed: Path | str) -> list[str]:
    return ["audit", "--candidates", str(candidates), "--truth", str(truth),
            "--left-id", "s", "--right-id", "c", "--missed-pairs-out", str(missed)]


def _forbid_input_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if audit_candidates() builds an input reader (i.e. starts reading inputs)."""
    import candguard.audit as audit_module

    def must_not_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("inputs must not be read when an output path collides with another file")

    monkeypatch.setattr(audit_module, "get_reader", must_not_read)


def _assert_missed_rejected(
    tmp_path: Path,
    args: list[str],
    input_label: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_input_reads(monkeypatch)
    before = _tree_snapshot(tmp_path)
    capsys.readouterr()
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("candguard: error: Missed-pairs output path ")
    assert f"resolves to the {input_label} (" in captured.err
    assert "choose a different --missed-pairs-out path" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
    # The colliding input (and every other file) keeps its exact bytes; nothing is created.
    assert _tree_snapshot(tmp_path) == before


_MISSED_TARGETS = pytest.mark.parametrize(
    ("key", "label"), [("candidates", "candidate input file"), ("truth", "ground-truth input file")]
)


@_MISSED_TARGETS
def test_cli_missed_pairs_out_cannot_overwrite_input_absolute_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, key: str, label: str
) -> None:
    files = _audit_files(tmp_path)
    assert files[key].is_absolute()
    _assert_missed_rejected(
        tmp_path, _missed_cli_args(files["candidates"], files["truth"], files[key]), label, capsys, monkeypatch
    )


@_MISSED_TARGETS
@pytest.mark.parametrize("form", ["all-relative", "relative-output", "relative-inputs"])
def test_cli_missed_pairs_out_relative_and_absolute_aliases_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, key: str, label: str, form: str
) -> None:
    files = _audit_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    rel = {k: Path(files[k].name) for k in ("candidates", "truth")}
    if form == "all-relative":
        candidates, truth, missed = rel["candidates"], rel["truth"], Path(".") / files[key].name
    elif form == "relative-output":
        candidates, truth, missed = files["candidates"], files["truth"], rel[key]
    else:  # relative inputs, absolute output
        candidates, truth, missed = rel["candidates"], rel["truth"], files[key]
    _assert_missed_rejected(tmp_path, _missed_cli_args(candidates, truth, missed), label, capsys, monkeypatch)


@_MISSED_TARGETS
def test_cli_missed_pairs_out_dotdot_alias_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, key: str, label: str
) -> None:
    files = _audit_files(tmp_path)
    (tmp_path / "sub").mkdir()
    alias = tmp_path / "sub" / ".." / files[key].name
    _assert_missed_rejected(
        tmp_path, _missed_cli_args(files["candidates"], files["truth"], alias), label, capsys, monkeypatch
    )


@_MISSED_TARGETS
def test_cli_missed_pairs_out_symlink_alias_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, key: str, label: str
) -> None:
    files = _audit_files(tmp_path)
    link = tmp_path / "missed_link.tsv"
    try:
        link.symlink_to(files[key])
    except (OSError, NotImplementedError) as exc:  # e.g. Windows without symlink privilege
        pytest.skip(f"symlink creation not supported here: {exc}")
    _assert_missed_rejected(
        tmp_path, _missed_cli_args(files["candidates"], files["truth"], link), label, capsys, monkeypatch
    )
    assert link.is_symlink()


def test_api_missed_pairs_path_cannot_overwrite_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from candguard import AuditError, audit_candidates

    files = _audit_files(tmp_path)
    _forbid_input_reads(monkeypatch)
    before = _tree_snapshot(tmp_path)
    for target in (files["candidates"], files["truth"]):
        with pytest.raises(AuditError, match="Missed-pairs output path .* resolves to the"):
            audit_candidates(
                candidates_path=files["candidates"], truth_path=files["truth"],
                left_id="s", right_id="c", missed_pairs_path=target,
            )
    assert _tree_snapshot(tmp_path) == before


def test_cli_missed_pairs_out_separate_file_still_written(tmp_path: Path) -> None:
    files = _audit_files(tmp_path)
    inputs = {k: files[k].read_bytes() for k in ("candidates", "truth")}
    missed = tmp_path / "out" / "missed_pairs.tsv"
    assert main(_missed_cli_args(files["candidates"], files["truth"], missed)) == 0
    assert missed.read_text(encoding="utf-8") == "s\tc\nx\ty\n"
    assert {k: files[k].read_bytes() for k in ("candidates", "truth")} == inputs


# Every audit output is checked against every input and every other output: --output in the CLI,
# --missed-pairs-out and --candidate-set-out in audit_candidates() (which the CLI calls).
_AUDIT_PATH_FLAGS = {
    "candidates": "--candidates",
    "truth": "--truth",
    "missed": "--missed-pairs-out",
    "cset": "--candidate-set-out",
    "report": "--output",
}


@pytest.mark.parametrize(
    ("output_key", "other_key"),
    [(o, k) for o in ("missed", "cset", "report") for k in _AUDIT_PATH_FLAGS if k != o],
)
def test_cli_every_audit_output_is_checked_against_every_other_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, output_key: str, other_key: str
) -> None:
    files = _audit_files(tmp_path)
    files["report"] = tmp_path / "report.json"
    files["report"].write_text("previous report\n", encoding="utf-8")
    paths = dict(files)
    paths[output_key] = files[other_key]  # the output under test aliases another path
    args = ["audit", "--left-id", "s", "--right-id", "c", "--format", "json"]
    for key, flag in _AUDIT_PATH_FLAGS.items():
        args += [flag, str(paths[key])]
    _forbid_input_reads(monkeypatch)
    before = _tree_snapshot(tmp_path)
    capsys.readouterr()
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("candguard: error: ")
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert _tree_snapshot(tmp_path) == before


# --- unreadable inputs and reports exit 2 without a traceback ---------------------------------

def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _fail_path_method(monkeypatch: pytest.MonkeyPatch, method: str, target: Path, make_result: Any) -> None:
    """Make ``Path.<method>`` misbehave for ``target`` only (deterministic on every OS)."""
    original = getattr(Path, method)

    def patched(self: Path, *args: Any, **kwargs: Any) -> Any:
        if _same_file(self, target):
            return make_result()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, patched)


def _raise_permission_denied() -> Any:
    raise PermissionError(13, "Permission denied")


class _StreamFailingAfterHeader:
    """A text stream that yields the header line and then fails with an I/O error."""

    def __init__(self) -> None:
        self._lines = ["s\tc\n"]

    def __enter__(self) -> "_StreamFailingAfterHeader":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> "_StreamFailingAfterHeader":
        return self

    def __next__(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        raise OSError(5, "Input/output error")


@pytest.mark.parametrize("key", ["candidates", "truth"])
@pytest.mark.parametrize(
    ("failure", "make_result"),
    [("open", _raise_permission_denied), ("read", _StreamFailingAfterHeader)],
    ids=["permission-denied-on-open", "io-error-while-reading"],
)
def test_cli_unreadable_delimited_input_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    key: str, failure: str, make_result: Any,
) -> None:
    files = _audit_files(tmp_path)
    _fail_path_method(monkeypatch, "open", files[key], make_result)
    code = main(["audit", "--candidates", str(files["candidates"]), "--truth", str(files["truth"]),
                 "--left-id", "s", "--right-id", "c"])
    captured = capsys.readouterr()
    assert code == 2
    expected = "Permission denied" if failure == "open" else "Input/output error"
    assert captured.err.startswith(f"candguard: error: Could not read input file {files[key]}: ")
    assert expected in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("side", ["baseline", "current"])
def test_cli_unreadable_compare_report_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    reports = {name: tmp_path / f"{name}.json" for name in ("baseline", "current")}
    for path in reports.values():
        path.write_text(json.dumps(_sample_report_dict()), encoding="utf-8")
    _fail_path_method(monkeypatch, "read_text", reports[side], _raise_permission_denied)
    code = main(["compare", "--baseline", str(reports["baseline"]), "--current", str(reports["current"])])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith(f"candguard: error: Could not read {side.capitalize()} file ({reports[side]}): ")
    assert "Permission denied" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_cli_malformed_compare_report_message_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The new OSError handling must not change how malformed JSON is reported."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_sample_report_dict()), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["compare", "--baseline", str(good), "--current", str(bad)]) == 2
    assert capsys.readouterr().err.startswith(f"candguard: error: Could not parse Current JSON report ({bad}): ")


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="real permission bits need a non-root POSIX user",
)
@pytest.mark.parametrize("target", ["candidates", "truth", "baseline", "current"])
def test_cli_real_unreadable_files_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    files = _audit_files(tmp_path)
    reports = {name: tmp_path / f"{name}.json" for name in ("baseline", "current")}
    for path in reports.values():
        path.write_text(json.dumps(_sample_report_dict()), encoding="utf-8")
    victim = {**files, **reports}[target]
    victim.chmod(0)
    try:
        if target in reports:
            args = ["compare", "--baseline", str(reports["baseline"]), "--current", str(reports["current"])]
        else:
            args = ["audit", "--candidates", str(files["candidates"]), "--truth", str(files["truth"]),
                    "--left-id", "s", "--right-id", "c"]
        code = main(args)
    finally:
        victim.chmod(0o644)
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("candguard: error: Could not read ")
    assert "Traceback" not in captured.err

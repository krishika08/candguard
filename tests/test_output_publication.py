"""Regression tests for audit output staging, publication, and recovery."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

import candguard.audit as audit_module
import candguard.cli as cli_module
from candguard.audit import AuditError, audit_candidates
from candguard.metrics import compute_candidate_set_fingerprint_v1


_OUTPUTS = ("missed", "pairs", "report")
_FLAGS = {"missed": "--missed-pairs-out", "pairs": "--candidate-set-out", "report": "--output"}


def _files(root, existing=(), *, separate=False):
    paths = {
        "candidates": root / "input.tsv",
        "truth": root / "truth.tsv",
    }
    paths["candidates"].write_bytes(b"l\tr\nb\t3\na\t2\na\t2\n")
    paths["truth"].write_bytes(b"l\tr\na\t2\nm\t9\n")
    for name in _OUTPUTS:
        directory = root / name if separate else root
        directory.mkdir(exist_ok=True)
        paths[name] = directory / f"{name}.out"
        if name in existing:
            paths[name].write_bytes(f"previous {name}\r\n".encode())
    return paths


def _snapshot(root):
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _cli(paths, outputs=_OUTPUTS, extra=()):
    args = [
        "audit", "--left-id", "l", "--right-id", "r", "--format", "json",
        "--candidates", str(paths["candidates"]), "--truth", str(paths["truth"]),
    ]
    for name in outputs:
        args.extend([_FLAGS[name], str(paths[name])])
    return cli_module.main(args + list(extra))


def _api(paths, outputs=("missed", "pairs")):
    return audit_candidates(
        candidates_path=paths["candidates"], truth_path=paths["truth"],
        left_id="l", right_id="r",
        missed_pairs_path=paths["missed"] if "missed" in outputs else None,
        candidate_set_path=paths["pairs"] if "pairs" in outputs else None,
    )


def _cli_error(capsys, *fragments):
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("candguard: error:")
    assert len(captured.err.splitlines()) == 1
    assert "Traceback" not in captured.err
    assert all(str(fragment) in captured.err for fragment in fragments)
    return captured.err


@pytest.mark.parametrize("position", [1, 2, 3], ids=_OUTPUTS)
def test_cli_staging_failure_keeps_all_destinations(tmp_path, monkeypatch, capsys, position):
    paths = _files(tmp_path, ("missed", "report"), separate=True)
    before = _snapshot(tmp_path)
    real_fsync = os.fsync
    count = 0

    def fail_stage(fd):
        nonlocal count
        count += 1
        if count == position:
            assert all(path.read_bytes() == data for path, data in before.items())
            assert not paths["pairs"].exists()
            raise OSError("injected staging sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_stage)
    assert _cli(paths) == 2
    _cli_error(capsys, paths[_OUTPUTS[position - 1]], "injected staging sync failure")
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("report_file", [True, False])
def test_cli_render_failure_never_publishes_sidecars(tmp_path, monkeypatch, capsys, report_file):
    paths = _files(tmp_path, ("missed", "report"))
    before = _snapshot(tmp_path)

    def fail_render(report):
        raise OSError("injected report render failure")

    monkeypatch.setattr(cli_module, "report_json", fail_render)
    assert _cli(paths, _OUTPUTS if report_file else _OUTPUTS[:2]) == 2
    _cli_error(capsys, "render", "injected report render failure")
    assert _snapshot(tmp_path) == before


def test_cli_report_directory_does_not_leave_partial_outputs(tmp_path, capsys):
    paths = _files(tmp_path, ("missed",))
    paths["report"].mkdir()
    before = _snapshot(tmp_path)
    assert _cli(paths) == 2
    _cli_error(capsys, paths["report"])
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("position", [1, 2, 3], ids=_OUTPUTS)
@pytest.mark.parametrize("existing", [(), _OUTPUTS, ("missed", "report")], ids=["new", "existing", "mixed"])
@pytest.mark.parametrize("separate", [False, True], ids=["same-dir", "separate-dirs"])
def test_cli_replacement_failure_restores_original_state(tmp_path, monkeypatch, capsys, position, existing, separate):
    paths = _files(tmp_path, existing, separate=separate)
    before = _snapshot(tmp_path)
    real_replace = os.replace
    attempts = []
    restored = []

    def fail_replace(src, dst):
        src, dst = Path(src), Path(dst)
        assert src.parent == dst.parent
        if len(attempts) == position:
            restored.append(dst)
        else:
            attempts.append(dst)
            if len(attempts) == position:
                raise PermissionError("injected locked destination")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_replace)
    assert _cli(paths) == 2
    _cli_error(capsys, paths[_OUTPUTS[position - 1]], "injected locked destination")
    assert attempts == [paths[name] for name in _OUTPUTS[:position]]
    assert restored == [paths[name] for name in reversed(_OUTPUTS[:position - 1]) if name in existing]
    assert _snapshot(tmp_path) == before


def test_cli_backup_failure_happens_before_any_replace(tmp_path, monkeypatch, capsys):
    paths = _files(tmp_path, _OUTPUTS)
    before = _snapshot(tmp_path)
    real_copy = shutil.copyfileobj
    attempted_sources = []

    def fail_backup(source, target, *args, **kwargs):
        attempted_sources.append(Path(source.name))
        if len(attempted_sources) == 2:
            raise OSError("injected backup copy failure")
        return real_copy(source, target, *args, **kwargs)

    def forbid_replace(*args):
        pytest.fail("all backups must complete before publication")

    monkeypatch.setattr(shutil, "copyfileobj", fail_backup)
    monkeypatch.setattr(os, "replace", forbid_replace)
    assert _cli(paths) == 2
    _cli_error(capsys, paths["pairs"], "backup", "injected backup copy failure")
    assert attempted_sources == [paths["missed"], paths["pairs"]]
    assert _snapshot(tmp_path) == before


def test_cli_rollback_failure_keeps_each_recovery_backup(tmp_path, monkeypatch, capsys):
    paths = _files(tmp_path, _OUTPUTS, separate=True)
    before = _snapshot(tmp_path)
    real_replace = os.replace
    backups = {}
    publication_failed = False

    def fail_replace(src, dst):
        nonlocal publication_failed
        dst = Path(dst)
        if dst == paths["report"]:
            publication_failed = True
            raise PermissionError("original report replacement failure")
        if publication_failed:
            backups[dst] = Path(src)
            raise PermissionError("injected rollback failure")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_replace)
    assert _cli(paths) == 2
    error = _cli_error(capsys, "original report replacement failure", "injected rollback failure", paths["report"])
    assert list(backups) == [paths["pairs"], paths["missed"]]
    for destination, backup in backups.items():
        assert str(destination) in error
        assert str(backup) in error
        assert backup.parent == destination.parent
        assert backup.read_bytes() == before[destination]
    after = _snapshot(tmp_path)
    assert set(after) == set(before) | set(backups.values())
    for name in ("candidates", "truth", "report"):
        assert after[paths[name]] == before[paths[name]]


def test_cli_failed_removal_during_rollback_names_new_destination(tmp_path, monkeypatch, capsys):
    paths = _files(tmp_path, ("missed", "report"))
    before = _snapshot(tmp_path)
    real_replace = os.replace
    real_unlink = Path.unlink

    def fail_report(src, dst):
        if Path(dst) == paths["report"]:
            raise PermissionError("original report failure")
        return real_replace(src, dst)

    def fail_unlink(path, *args, **kwargs):
        if path == paths["pairs"]:
            raise PermissionError("cannot remove new sidecar")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_report)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    assert _cli(paths) == 2
    _cli_error(capsys, paths["report"], "original report failure", paths["pairs"], "cannot remove new sidecar")
    after = _snapshot(tmp_path)
    assert after.pop(paths["pairs"]).startswith(b"left_id\tright_id\n")
    assert after == before


def test_cli_cleanup_failure_keeps_successfully_published_outputs(tmp_path, monkeypatch, caplog):
    paths = _files(tmp_path, ("missed",))
    before = _snapshot(tmp_path)
    real_unlink = Path.unlink
    retained = []

    def fail_backup_cleanup(path, *args, **kwargs):
        if path.exists() and path not in paths.values() and path.read_bytes() == before[paths["missed"]]:
            retained.append(path)
            raise PermissionError("injected backup cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_cleanup)
    assert _cli(paths) == 0
    assert len(retained) == 1
    assert str(retained[0]) in caplog.text
    assert "injected backup cleanup failure" in caplog.text
    assert paths["missed"].read_bytes() == b"l\tr\nm\t9\n"
    assert paths["pairs"].is_file()
    assert json.loads(paths["report"].read_bytes())["quality_gate"]["passed"] is True


@pytest.mark.parametrize("outputs", [("missed",), ("pairs",), ("missed", "pairs")])
@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
def test_api_publishes_requested_sidecars(tmp_path, outputs, existing):
    paths = _files(tmp_path, outputs if existing else ())
    before = _snapshot(tmp_path)
    report = _api(paths, outputs)
    _assert_artifacts(paths, outputs, report)
    assert _snapshot(tmp_path).keys() == before.keys() | {paths[name] for name in outputs}
    assert all(paths[name].read_bytes() == before[paths[name]] for name in ("candidates", "truth"))


@pytest.mark.parametrize("phase", ["stage", "publish"])
def test_api_later_sidecar_failure_keeps_existing_missed_pairs(tmp_path, monkeypatch, phase):
    paths = _files(tmp_path, ("missed",))
    before = _snapshot(tmp_path)
    if phase == "stage":
        real_fsync = os.fsync
        synced = 0

        def fail_second(fd):
            nonlocal synced
            synced += 1
            if synced == 2:
                raise OSError("failed second sidecar")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_second)
    else:
        real_replace = os.replace

        def fail_second(src, dst):
            if Path(dst) == paths["pairs"]:
                raise PermissionError("failed second sidecar")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", fail_second)

    with pytest.raises(AuditError, match="failed second sidecar"):
        _api(paths)
    assert _snapshot(tmp_path) == before


def _assert_artifacts(paths, outputs, report):
    if "missed" in outputs:
        assert report["ground_truth"]["missed_pairs_file"] == str(paths["missed"])
        assert paths["missed"].read_bytes() == b"l\tr\nm\t9\n"
    else:
        assert "missed_pairs_file" not in report["ground_truth"]
    if "pairs" in outputs:
        metadata = report["candidate_set"]
        assert metadata["unique_pairs"] == 2
        assert metadata["fingerprint_version"] == "v1"
        assert metadata["fingerprint"] == compute_candidate_set_fingerprint_v1([("a", "2"), ("b", "3")], "cross")
        assert paths["pairs"].read_bytes() == b"left_id\tright_id\na\t2\nb\t3\n"
    else:
        assert "candidate_set" not in report
    assert report["schema_version"] == "1.0"
    assert report["ground_truth"]["fingerprint_version"] == "v2"


@pytest.mark.parametrize("outputs", [_OUTPUTS, ("missed", "report"), ("pairs", "report"), ("report",), _OUTPUTS[:2]])
@pytest.mark.parametrize("gate_passes", [True, False])
def test_cli_publishes_complete_audits_even_when_gate_fails(tmp_path, capsys, outputs, gate_passes):
    paths = _files(tmp_path, outputs)
    before = _snapshot(tmp_path)
    assert _cli(paths, outputs, ("--min-recall", "0.5" if gate_passes else "1")) == int(not gate_passes)
    captured = capsys.readouterr()
    if "report" in outputs:
        assert captured.out == ""
        report = json.loads(paths["report"].read_bytes())
    else:
        report = json.loads(captured.out)
    assert report["quality_gate"]["passed"] is gate_passes
    _assert_artifacts(paths, outputs, report)
    assert _snapshot(tmp_path).keys() == before.keys()
    assert all(paths[name].read_bytes() == before[paths[name]] for name in ("candidates", "truth"))


@pytest.mark.parametrize("kind", ["directory", "symlink", "broken-symlink", "hard-link", "ancestor"])
def test_cli_preflight_rejects_unsafe_report_paths(tmp_path, monkeypatch, capsys, kind):
    paths = _files(tmp_path, ("pairs",))
    report_file = paths["report"]
    if kind == "hard-link":
        os.link(paths["pairs"], report_file)
    elif kind == "ancestor":
        paths["missed"] = report_file / "missed.tsv"
    elif kind == "directory":
        report_file.mkdir()
    else:
        target = paths["candidates"] if kind == "symlink" else tmp_path / "missing"
        try:
            report_file.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")

    before = _snapshot(tmp_path)

    def forbid_input(*args, **kwargs):
        pytest.fail("unsafe output paths must fail before input is read")

    monkeypatch.setattr(audit_module, "get_reader", forbid_input)
    assert _cli(paths) == 2
    _cli_error(capsys, report_file)
    assert _snapshot(tmp_path) == before
    if kind == "ancestor":
        assert not report_file.exists()
    elif "symlink" in kind:
        assert report_file.is_symlink()


def test_cli_stages_and_backs_up_every_output_before_first_replace(tmp_path, monkeypatch):
    paths = _files(tmp_path, _OUTPUTS, separate=True)
    before = _snapshot(tmp_path)
    real_temporary = tempfile.NamedTemporaryFile
    real_replace = os.replace
    staged = []
    backups = []
    order = []

    def track_temporary(*args, **kwargs):
        stream = real_temporary(*args, **kwargs)
        if kwargs.get("suffix") == ".stage":
            staged.append(stream)
        elif kwargs.get("suffix") == ".backup":
            backups.append(stream)
        return stream

    def inspect_replace(source, target):
        target = Path(target)
        if not order:
            assert all(path.read_bytes() == value for path, value in before.items())
            assert len(staged) == len(backups) == len(_OUTPUTS)
            for name, stage, backup in zip(_OUTPUTS, staged, backups):
                assert stage.closed and backup.closed
                assert Path(stage.name).parent == Path(backup.name).parent == paths[name].parent
                assert Path(backup.name).read_bytes() == before[paths[name]]
            assert Path(staged[0].name).read_bytes() == b"l\tr\nm\t9\n"
            assert Path(staged[1].name).read_bytes() == b"left_id\tright_id\na\t2\nb\t3\n"
            assert json.loads(Path(staged[2].name).read_bytes())["quality_gate"]["passed"] is True
        order.append(target)
        return real_replace(source, target)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", track_temporary)
    monkeypatch.setattr(os, "replace", inspect_replace)
    assert _cli(paths) == 0
    assert order == [paths[name] for name in _OUTPUTS]
    assert _snapshot(tmp_path).keys() == before.keys()


def test_cli_stdout_write_failure_leaves_complete_sidecars(tmp_path, monkeypatch, capsys):
    paths = _files(tmp_path)

    def fail_stdout(*args, **kwargs):
        if kwargs.get("file") is None:
            assert paths["missed"].read_bytes() == b"l\tr\nm\t9\n"
            assert paths["pairs"].is_file()
            raise OSError("injected stdout write failure")
        print(*args, **kwargs)

    monkeypatch.setattr(cli_module, "print", fail_stdout, raising=False)
    assert _cli(paths, _OUTPUTS[:2]) == 2
    _cli_error(capsys, "stdout", "injected stdout write failure")
    assert set(_snapshot(tmp_path)) == {paths[name] for name in ("candidates", "truth", "missed", "pairs")}

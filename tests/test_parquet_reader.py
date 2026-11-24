"""Tests for optional Apache Parquet input support.

Tests that need PyArrow are skipped when it is not installed (the default, dependency-free
test environment). Tests for the missing-dependency path, delimiter misuse, and import
isolation run in every environment.

All Parquet fixtures are small, synthetic, and generated at test time.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import pytest

from candguard import AuditError, audit_candidates
from candguard.cli import main
from candguard.readers import (
    PARQUET_INSTALL_HINT,
    PARQUET_RELEASE_INSTALL_COMMAND,
    PARQUET_SOURCE_INSTALL_COMMAND,
    CandidateReader,
    DelimitedReader,
    ParquetReader,
    get_reader,
)

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - exercised in the dependency-free CI job
    pa = None
    pq = None

needs_pyarrow = pytest.mark.skipif(pa is None, reason="optional dependency pyarrow is not installed")

HEADER = ("source_id", "candidate_id")
SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def write_delimited(path: Path, rows: Iterable[tuple[Any, Any]], delimiter: str, header: tuple[str, str] = HEADER) -> Path:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter=delimiter, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    return path


def write_parquet(
    path: Path,
    rows: list[tuple[Any, Any]],
    header: tuple[str, str] = HEADER,
    *,
    types: tuple[Any, Any] | None = None,
    extra_columns: dict[str, Any] | None = None,
    **write_kwargs: Any,
) -> Path:
    left_type, right_type = types if types is not None else (pa.string(), pa.string())
    columns: dict[str, Any] = {}
    if extra_columns:
        columns.update(extra_columns)
    columns[header[0]] = pa.array([r[0] for r in rows], type=left_type)
    columns[header[1]] = pa.array([r[1] for r in rows], type=right_type)
    pq.write_table(pa.table(columns), path, **write_kwargs)
    return path


def write_format(path_stem: Path, fmt: str, rows: list[tuple[Any, Any]]) -> Path:
    if fmt == "csv":
        return write_delimited(path_stem.with_suffix(".csv"), rows, ",")
    if fmt == "tsv":
        return write_delimited(path_stem.with_suffix(".tsv"), rows, "\t")
    if fmt == "parquet":
        return write_parquet(path_stem.with_suffix(".parquet"), rows)
    raise ValueError(fmt)


def logical(report: dict[str, Any]) -> dict[str, Any]:
    """Strip file-path fields so reports from different input formats can be compared."""
    result = json.loads(json.dumps(report))
    result["inputs"].pop("candidates")
    result["inputs"].pop("truth")
    if result["ground_truth"] is not None:
        result["ground_truth"].pop("missed_pairs_file", None)
    return result


@pytest.fixture
def sqlite_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CandGuard's temporary SQLite databases to an isolated, inspectable directory."""
    directory = tmp_path / "sqlite_tmp"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


# Logical datasets shared by the cross-format parity tests. They deliberately include
# duplicate rows, duplicates that only appear after trimming, reversed pairs, Unicode,
# and a delimiter character inside an identifier.
CROSS_CANDIDATES = [
    ("A1", "B1"),
    ("A1", "B2"),
    (" A1 ", "B1"),
    ("A2", "B3"),
    ("B3", "A2"),
    ("Ä3", "東京"),
    ("A4", "B4"),
    ("A4", "B4"),
    ("A,7", "B7"),
    ("A5", "B9"),
]
CROSS_TRUTH = [("A1", "B1"), ("A2", "B3"), ("Ä3", "東京"), ("A6", "B6"), ("A1", "B1")]

DEDUPE_CANDIDATES = [("E1", "E2"), ("E2", "E1"), ("E1", "E3"), (" E4", "E5 "), ("E5", "E4"), ("E6", "E7"), ("É8", "E9")]
DEDUPE_TRUTH = [("E2", "E1"), ("E4", "E5"), ("E10", "E11"), ("E9", "É8")]


# ---------------------------------------------------------------------------
# Dependency handling (runs with or without PyArrow)
# ---------------------------------------------------------------------------


def test_importing_candguard_does_not_import_pyarrow() -> None:
    code = (
        "import sys; import candguard, candguard.cli, candguard.readers, candguard.audit; "
        "print('pyarrow' in sys.modules)"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize(("suffix", "delimiter"), [(".csv", ","), (".tsv", "\t")])
def test_delimited_audit_works_when_pyarrow_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, delimiter: str
) -> None:
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    cand = write_delimited(tmp_path / f"c{suffix}", [("a", "b")], delimiter)
    truth = write_delimited(tmp_path / f"t{suffix}", [("a", "b")], delimiter)
    report = audit_candidates(candidates_path=cand, truth_path=truth, left_id="source_id", right_id="candidate_id")
    assert report["inputs"]["candidate_rows"] == 1
    assert report["ground_truth"]["candidate_recall"] == 1.0


def test_install_hint_labels_release_and_source_checkout_commands() -> None:
    assert PARQUET_RELEASE_INSTALL_COMMAND == 'python -m pip install "candguard[parquet]"'
    assert PARQUET_SOURCE_INSTALL_COMMAND == 'python -m pip install -e ".[parquet]"'
    hint = PARQUET_INSTALL_HINT
    assert "\n" not in hint  # one line, like every other AuditError message
    release_part, _, source_part = hint.partition("; from a source checkout")
    assert source_part, hint
    # Each command appears, delimited by backticks, only in the part labeled for it.
    assert f"`{PARQUET_RELEASE_INSTALL_COMMAND}`" in release_part
    assert f"`{PARQUET_SOURCE_INSTALL_COMMAND}`" in source_part
    assert PARQUET_SOURCE_INSTALL_COMMAND not in release_part
    assert PARQUET_RELEASE_INSTALL_COMMAND not in source_part
    # The release command is limited to releases that include Parquet; 0.1.0 is explicitly excluded.
    assert "published CandGuard release that includes Parquet support (CandGuard 0.1.0 does not)" in release_part
    # The editable install must be run from the repository root.
    assert source_part.endswith(f"`{PARQUET_SOURCE_INSTALL_COMMAND}` from the repository root.")


@pytest.mark.parametrize("factory", ["get_reader", "ParquetReader"])
def test_missing_pyarrow_raises_install_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory: str) -> None:
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    path = tmp_path / "pairs.parquet"
    with pytest.raises(AuditError) as excinfo:
        if factory == "get_reader":
            get_reader(path, "l", "r")
        else:
            ParquetReader(path, "l", "r")
    message = str(excinfo.value)
    assert message.startswith("Reading Parquet input requires the optional PyArrow dependency")
    assert message.endswith(PARQUET_INSTALL_HINT)
    assert PARQUET_RELEASE_INSTALL_COMMAND in message
    assert PARQUET_SOURCE_INSTALL_COMMAND in message
    assert "repository root" in message
    assert isinstance(excinfo.value.__cause__, ImportError)


@pytest.mark.parametrize("flag", ["--candidates", "--truth"])
def test_cli_missing_pyarrow_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    tsv = write_delimited(tmp_path / "pairs.tsv", [("a", "b")], "\t")
    parquet_path = tmp_path / "pairs.parquet"
    parquet_path.write_bytes(b"placeholder")
    candidates = parquet_path if flag == "--candidates" else tsv
    argv = ["audit", "--candidates", str(candidates), "--left-id", "source_id", "--right-id", "candidate_id"]
    if flag == "--truth":
        argv += ["--truth", str(parquet_path)]
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("candguard: error: Reading Parquet input requires the optional PyArrow dependency")
    assert PARQUET_INSTALL_HINT in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_cli_subprocess_missing_pyarrow_has_no_traceback(tmp_path: Path) -> None:
    """End-to-end: a real interpreter with PyArrow blocked prints the guidance and exits 2."""
    parquet_path = tmp_path / "pairs.parquet"
    parquet_path.write_bytes(b"placeholder")
    code = (
        "import sys; sys.modules['pyarrow'] = None; sys.modules['pyarrow.parquet'] = None; "
        "from candguard.cli import main; "
        f"sys.exit(main(['audit', '--candidates', {str(parquet_path)!r}, "
        "'--left-id', 'source_id', '--right-id', 'candidate_id']))"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
    assert PARQUET_RELEASE_INSTALL_COMMAND in result.stderr
    assert PARQUET_SOURCE_INSTALL_COMMAND in result.stderr
    assert f"`{PARQUET_SOURCE_INSTALL_COMMAND}` from the repository root" in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1  # a single "candguard: error: ..." line


def test_get_reader_rejects_delimiter_override_for_parquet(tmp_path: Path) -> None:
    with pytest.raises(AuditError, match="delimiters apply only to CSV/TSV files"):
        get_reader(tmp_path / "pairs.parquet", "l", "r", delimiter=",")
    with pytest.raises(AuditError, match="delimiters apply only to CSV/TSV files"):
        get_reader(tmp_path / "PAIRS.PARQUET", "l", "r", delimiter="\\t")


@pytest.mark.parametrize("flag", ["--delimiter", "--truth-delimiter"])
def test_cli_delimiter_misuse_with_parquet_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    parquet_path = tmp_path / "pairs.parquet"
    parquet_path.write_bytes(b"placeholder")
    tsv = write_delimited(tmp_path / "pairs.tsv", [("a", "b")], "\t")
    # Only the Parquet side carries the misplaced delimiter; the other input is a valid TSV.
    candidates, truth = (parquet_path, tsv) if flag == "--delimiter" else (tsv, parquet_path)
    argv = [
        "audit", "--candidates", str(candidates), "--truth", str(truth),
        "--left-id", "source_id", "--right-id", "candidate_id", flag, ",",
    ]
    assert main(argv) == 2
    assert "delimiters apply only to CSV/TSV files" in capsys.readouterr().err


def test_delimited_dispatch_is_unchanged(tmp_path: Path) -> None:
    for name, expected in [("a.csv", ","), ("a.tsv", "\t"), ("a.tab", "\t"), ("a.TSV", "\t"), ("a.txt", ",")]:
        reader = get_reader(tmp_path / name, "l", "r")
        assert isinstance(reader, DelimitedReader)
        assert reader.delimiter == expected
    assert get_reader(tmp_path / "a.tab", "l", "r", delimiter="|").delimiter == "|"


# ---------------------------------------------------------------------------
# Reader behavior
# ---------------------------------------------------------------------------


@needs_pyarrow
@pytest.mark.parametrize("name", ["pairs.parquet", "PAIRS.PARQUET", "pairs.Parquet"])
def test_get_reader_dispatches_parquet_case_insensitively(tmp_path: Path, name: str) -> None:
    path = write_parquet(tmp_path / name, [("a", "b")])
    reader = get_reader(path, "source_id", "candidate_id")
    assert isinstance(reader, ParquetReader)
    assert isinstance(reader, CandidateReader)
    assert reader.path == path
    assert reader.left_col == "source_id"
    assert reader.right_col == "candidate_id"
    assert list(reader.iter_pairs()) == [("a", "b")]


@needs_pyarrow
def test_string_identifiers(tmp_path: Path) -> None:
    rows = [("s1", "t1"), ("s2", "t2"), ("s1", "t1")]
    path = write_parquet(tmp_path / "p.parquet", rows)
    assert list(get_reader(path, *HEADER).iter_pairs()) == rows


@needs_pyarrow
def test_large_string_and_dictionary_string_identifiers(tmp_path: Path) -> None:
    path = tmp_path / "p.parquet"
    table = pa.table({
        "l": pa.array(["  a", "b ", "a"], type=pa.large_string()),
        "r": pa.array([" x ", "y", "x"]).dictionary_encode(),
    })
    pq.write_table(table, path)
    assert list(get_reader(path, "l", "r").iter_pairs()) == [("a", "x"), ("b", "y"), ("a", "x")]


@needs_pyarrow
@pytest.mark.parametrize(
    "arrow_type,values,expected",
    [
        ("int8", [-128, 0, 127], ["-128", "0", "127"]),
        ("int16", [-1, 32767], ["-1", "32767"]),
        ("int32", [7, -2147483648], ["7", "-2147483648"]),
        ("int64", [9223372036854775807, -5], ["9223372036854775807", "-5"]),
        ("uint8", [0, 255], ["0", "255"]),
        ("uint16", [65535, 1], ["65535", "1"]),
        ("uint32", [4294967295, 2], ["4294967295", "2"]),
        ("uint64", [18446744073709551615, 3], ["18446744073709551615", "3"]),
    ],
)
def test_integer_identifiers_become_decimal_text(
    tmp_path: Path, arrow_type: str, values: list[int], expected: list[str]
) -> None:
    t = getattr(pa, arrow_type)()
    rows = list(zip(values, values))
    path = write_parquet(tmp_path / "p.parquet", rows, types=(t, t))
    assert list(get_reader(path, *HEADER).iter_pairs()) == list(zip(expected, expected))


@needs_pyarrow
def test_mixed_integer_and_string_and_dictionary_integer_columns(tmp_path: Path) -> None:
    path = tmp_path / "p.parquet"
    table = pa.table({
        "l": pa.array([10, 20, 10], type=pa.int64()).dictionary_encode(),
        "r": pa.array(["  r1", "r2", "r1"]),
    })
    pq.write_table(table, path)
    assert list(get_reader(path, "l", "r").iter_pairs()) == [("10", "r1"), ("20", "r2"), ("10", "r1")]


WHITESPACE_AND_UNICODE = [
    ("  padded  ", "\ttabbed\t"),
    (" nbsp ", "　ideographic　"),
    ("inner  space", "line\nbreak"),
    ("café", "東京"),
    ("é", "é"),  # decomposed vs composed: no Unicode normalization is applied
    ("😀", "Ω"),
    ("A,7", '"quoted"'),
]


@needs_pyarrow
def test_whitespace_and_unicode_normalization_matches_delimited_reader(tmp_path: Path) -> None:
    pq_path = write_parquet(tmp_path / "p.parquet", WHITESPACE_AND_UNICODE)
    csv_path = write_delimited(tmp_path / "p.csv", WHITESPACE_AND_UNICODE, ",")
    tsv_path = write_delimited(tmp_path / "p.tsv", WHITESPACE_AND_UNICODE, "\t")
    from_parquet = list(get_reader(pq_path, *HEADER).iter_pairs())
    assert from_parquet == list(get_reader(csv_path, *HEADER).iter_pairs())
    assert from_parquet == list(get_reader(tsv_path, *HEADER).iter_pairs())
    assert from_parquet[0] == ("padded", "tabbed")
    assert from_parquet[1] == ("nbsp", "ideographic")
    assert from_parquet[2] == ("inner  space", "line\nbreak")
    assert from_parquet[4] == ("é", "é")
    assert from_parquet[4][0] != from_parquet[4][1]


@needs_pyarrow
def test_column_projection_reads_only_identifier_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "p.parquet"
    # Identifier columns are out of schema order and surrounded by columns of unsupported types.
    table = pa.table({
        "candidate_id": ["t1", "t2"],
        "score": [0.5, 0.9],
        "tags": pa.array([["x"], ["y", "z"]], type=pa.list_(pa.string())),
        "source_id": ["s1", "s2"],
        "payload": pa.array([b"\x00\x01", b"\xff"], type=pa.binary()),
    })
    pq.write_table(table, path)

    requested: list[Any] = []
    original = pq.ParquetFile.iter_batches

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        requested.append(kwargs.get("columns"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", spy)
    assert list(get_reader(path, "source_id", "candidate_id").iter_pairs()) == [("s1", "t1"), ("s2", "t2")]
    assert requested == [["source_id", "candidate_id"]]


@needs_pyarrow
def test_row_order_is_preserved_across_row_groups_and_read_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import random

    ids = [f"ID_{i:05d}" for i in range(5000)]
    random.Random(7).shuffle(ids)
    rows = [(ids[i], ids[-1 - i]) for i in range(len(ids))]
    path = write_parquet(tmp_path / "p.parquet", rows, row_group_size=333)
    assert pq.ParquetFile(path).metadata.num_row_groups > 1
    monkeypatch.setattr(ParquetReader, "read_rows", 128)
    reader = get_reader(path, *HEADER)
    assert list(reader.iter_pairs()) == rows
    assert [pair for batch in reader.iter_batches(batch_size=1000) for pair in batch] == rows


@needs_pyarrow
@pytest.mark.parametrize("read_rows", [1, 3, 4, 7, 65_536])
@pytest.mark.parametrize(
    "batch_size,expected_sizes",
    [(1, [1] * 10), (4, [4, 4, 2]), (5, [5, 5]), (10, [10]), (50_000, [10])],
)
def test_batch_sizes_are_exact_regardless_of_row_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_rows: int, batch_size: int, expected_sizes: list[int]
) -> None:
    rows = [(f"s{i}", f"t{i}") for i in range(10)]
    path = write_parquet(tmp_path / "p.parquet", rows, row_group_size=3)
    monkeypatch.setattr(ParquetReader, "read_rows", read_rows)
    batches = list(get_reader(path, *HEADER).iter_batches(batch_size=batch_size))
    assert [len(b) for b in batches] == expected_sizes
    assert [pair for b in batches for pair in b] == rows


@needs_pyarrow
def test_default_batch_size_and_matching_delimited_batches(tmp_path: Path) -> None:
    rows = [(f"s{i}", f"t{i}") for i in range(23)]
    pq_reader = get_reader(write_parquet(tmp_path / "p.parquet", rows), *HEADER)
    tsv_reader = get_reader(write_delimited(tmp_path / "p.tsv", rows, "\t"), *HEADER)
    assert list(pq_reader.iter_batches()) == [rows]
    assert list(pq_reader.iter_batches(batch_size=5)) == list(tsv_reader.iter_batches(batch_size=5))


@needs_pyarrow
def test_empty_valid_parquet_file_yields_no_pairs(tmp_path: Path) -> None:
    path = write_parquet(tmp_path / "empty.parquet", [])
    reader = get_reader(path, *HEADER)
    assert list(reader.iter_pairs()) == []
    assert list(reader.iter_batches(batch_size=3)) == []


@needs_pyarrow
@pytest.mark.parametrize("codec", ["none", "snappy", "gzip", "brotli", "zstd", "lz4"])
def test_compression_codecs(tmp_path: Path, codec: str) -> None:
    if codec != "none" and not pa.Codec.is_available(codec):
        pytest.skip(f"codec {codec} not available in this PyArrow build")
    rows = [(f"s{i % 17}", f"t{i}") for i in range(500)]
    path = write_parquet(tmp_path / f"p_{codec}.parquet", rows, compression=codec)
    assert list(get_reader(path, *HEADER).iter_pairs()) == rows


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@needs_pyarrow
def test_missing_left_column(tmp_path: Path) -> None:
    path = write_parquet(tmp_path / "p.parquet", [("a", "b")])
    with pytest.raises(AuditError, match=r"Missing column\(s\) in .*: nope\. Available columns: source_id, candidate_id"):
        list(get_reader(path, "nope", "candidate_id").iter_pairs())


@needs_pyarrow
def test_missing_right_column(tmp_path: Path) -> None:
    path = write_parquet(tmp_path / "p.parquet", [("a", "b")])
    with pytest.raises(AuditError, match=r"Missing column\(s\) in .*: nope\."):
        list(get_reader(path, "source_id", "nope").iter_pairs())


@needs_pyarrow
def test_identical_left_and_right_columns(tmp_path: Path) -> None:
    path = write_parquet(tmp_path / "p.parquet", [("a", "b")])
    with pytest.raises(AuditError, match="cannot be identical"):
        list(get_reader(path, "source_id", "source_id").iter_pairs())


@needs_pyarrow
def test_duplicate_column_names_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "p.parquet"
    table = pa.Table.from_arrays([pa.array(["a"]), pa.array(["b"]), pa.array(["c"])], names=["l", "l", "r"])
    try:
        pq.write_table(table, path)
    except pa.ArrowException:  # pragma: no cover - depends on writer support
        pytest.skip("this PyArrow version cannot write duplicate column names")
    with pytest.raises(AuditError, match="Duplicate column 'l'"):
        list(get_reader(path, "l", "r").iter_pairs())


@needs_pyarrow
@pytest.mark.parametrize("side", ["left", "right"])
def test_null_identifiers_are_rejected_with_row_number(tmp_path: Path, side: str) -> None:
    rows: list[tuple[Any, Any]] = [("a", "b"), ("c", "d"), ("e", "f")]
    rows[2] = (None, "f") if side == "left" else ("e", None)
    path = write_parquet(tmp_path / "p.parquet", rows)
    column = "source_id" if side == "left" else "candidate_id"
    with pytest.raises(AuditError, match=rf"Null identifier in column '{column}' at .* row 3;"):
        list(get_reader(path, *HEADER).iter_pairs())


@needs_pyarrow
def test_null_integer_identifier_is_rejected(tmp_path: Path) -> None:
    path = write_parquet(tmp_path / "p.parquet", [(1, 2), (None, 3)], types=(pa.int64(), pa.int64()))
    with pytest.raises(AuditError, match=r"Null identifier in column 'source_id' at .* row 2;"):
        list(get_reader(path, *HEADER).iter_pairs())


@needs_pyarrow
@pytest.mark.parametrize("value", ["", "   ", "\t\n", "　"])
@pytest.mark.parametrize("side", ["left", "right"])
def test_empty_identifiers_are_rejected(tmp_path: Path, value: str, side: str) -> None:
    rows = [("a", "b"), (value, "d") if side == "left" else ("c", value)]
    path = write_parquet(tmp_path / "p.parquet", rows)
    column = "source_id" if side == "left" else "candidate_id"
    with pytest.raises(AuditError, match=rf"Empty identifier in column '{column}' at .* row 2;"):
        list(get_reader(path, *HEADER).iter_pairs())


@needs_pyarrow
def test_null_reported_with_global_row_number_across_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[tuple[Any, Any]] = [(f"s{i}", f"t{i}") for i in range(10)]
    rows[8] = ("s8", None)
    path = write_parquet(tmp_path / "p.parquet", rows, row_group_size=3)
    monkeypatch.setattr(ParquetReader, "read_rows", 2)
    with pytest.raises(AuditError, match=r"row 9;"):
        list(get_reader(path, *HEADER).iter_batches(batch_size=2))


def _unsupported_type_cases() -> list[Any]:
    if pa is None:
        return []
    return [
        pytest.param(pa.float64(), [1.0, 2.0], id="float64"),
        pytest.param(pa.float32(), [1.0, 2.0], id="float32"),
        pytest.param(pa.bool_(), [True, False], id="bool"),
        pytest.param(pa.binary(), [b"a", b"b"], id="binary"),
        pytest.param(pa.large_binary(), [b"a", b"b"], id="large_binary"),
        pytest.param(pa.decimal128(10, 2), [Decimal("1.00"), Decimal("2.00")], id="decimal"),
        pytest.param(pa.date32(), [0, 1], id="date32"),
        pytest.param(pa.timestamp("ms"), [0, 1], id="timestamp"),
        pytest.param(pa.list_(pa.string()), [["a"], ["b"]], id="list"),
        pytest.param(pa.struct([("x", pa.string())]), [{"x": "a"}, {"x": "b"}], id="struct"),
        pytest.param(pa.null(), [None, None], id="null"),
    ]


@needs_pyarrow
@pytest.mark.parametrize("arrow_type,values", _unsupported_type_cases())
def test_unsupported_identifier_types_are_rejected(tmp_path: Path, arrow_type: Any, values: list[Any]) -> None:
    path = tmp_path / "p.parquet"
    pq.write_table(pa.table({"l": pa.array(values, type=arrow_type), "r": pa.array(["x", "y"])}), path)
    with pytest.raises(AuditError, match=r"Unsupported Parquet type .* for identifier column 'l'"):
        list(get_reader(path, "l", "r").iter_pairs())


@needs_pyarrow
@pytest.mark.parametrize("batch_size", [0, -1, -50_000, True, False, 2.5, "10", None])
def test_invalid_batch_sizes_are_rejected(tmp_path: Path, batch_size: Any) -> None:
    path = write_parquet(tmp_path / "p.parquet", [("a", "b")])
    with pytest.raises(AuditError, match="batch_size must be a positive integer"):
        list(get_reader(path, *HEADER).iter_batches(batch_size=batch_size))


@needs_pyarrow
def test_corrupted_and_non_parquet_files(tmp_path: Path) -> None:
    good = write_parquet(tmp_path / "good.parquet", [(f"s{i}", f"t{i}") for i in range(100)])
    data = good.read_bytes()
    cases = {
        "garbage.parquet": b"this is not a parquet file",
        "zero.parquet": b"",
        "truncated.parquet": data[: len(data) // 2],
        "bad_footer.parquet": data[:-8] + b"XXXXXXXX",
    }
    for name, content in cases.items():
        path = tmp_path / name
        path.write_bytes(content)
        with pytest.raises(AuditError, match=r"Could not read Parquet file"):
            list(get_reader(path, *HEADER).iter_pairs())


@needs_pyarrow
def test_corrupted_data_page_detected_when_checksums_present(tmp_path: Path) -> None:
    ids = [f"ID_{i:06d}" for i in range(2000)]
    path = write_parquet(
        tmp_path / "ck.parquet", list(zip(ids, ids)), compression="none", use_dictionary=False, write_page_checksum=True
    )
    data = bytearray(path.read_bytes())
    data[data.find(b"ID_001000") + 4] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(AuditError, match=r"Could not read Parquet file .*checksum"):
        list(get_reader(path, *HEADER).iter_pairs())


@needs_pyarrow
def test_missing_file_and_directory_inputs(tmp_path: Path) -> None:
    with pytest.raises(AuditError, match="Input file does not exist"):
        list(get_reader(tmp_path / "missing.parquet", *HEADER).iter_pairs())
    directory = tmp_path / "dataset.parquet"
    directory.mkdir()
    with pytest.raises(AuditError, match="Partitioned Parquet dataset directories are not supported"):
        list(get_reader(directory, *HEADER).iter_pairs())


@needs_pyarrow
def test_file_handle_is_released_after_partial_iteration(tmp_path: Path) -> None:
    path = write_parquet(tmp_path / "p.parquet", [(f"s{i}", f"t{i}") for i in range(10)])
    iterator = get_reader(path, *HEADER).iter_batches(batch_size=2)
    next(iterator)
    iterator.close()
    os.remove(path)  # fails on Windows if the handle is still open
    assert not path.exists()


# ---------------------------------------------------------------------------
# End-to-end integration with the audit engine
# ---------------------------------------------------------------------------

AUDIT_GATES: dict[str, Any] = {"min_recall": 0.7, "max_pairs": 100, "max_duplicate_rows": 0}


def _audit(cand: Path, truth: Path | None, linkage_type: str, missed: Path | None = None, **gates: Any) -> dict[str, Any]:
    return audit_candidates(
        candidates_path=cand,
        truth_path=truth,
        left_id="source_id",
        right_id="candidate_id",
        linkage_type=linkage_type,
        missed_pairs_path=missed,
        **gates,
    )


@needs_pyarrow
@pytest.mark.parametrize(
    "linkage_type,candidates,truth",
    [("cross", CROSS_CANDIDATES, CROSS_TRUTH), ("dedupe", DEDUPE_CANDIDATES, DEDUPE_TRUTH)],
)
def test_cross_format_audit_parity(
    tmp_path: Path, sqlite_tmp: Path, linkage_type: str, candidates: list[tuple[str, str]], truth: list[tuple[str, str]]
) -> None:
    reports: dict[str, dict[str, Any]] = {}
    missed_files: dict[str, str] = {}
    for fmt in ("csv", "tsv", "parquet"):
        cand = write_format(tmp_path / f"cand_{fmt}", fmt, candidates)
        tru = write_format(tmp_path / f"truth_{fmt}", fmt, truth)
        missed = tmp_path / f"missed_{fmt}.tsv"
        reports[fmt] = logical(_audit(cand, tru, linkage_type, missed, **AUDIT_GATES))
        missed_files[fmt] = missed.read_text(encoding="utf-8")

    assert reports["csv"] == reports["tsv"] == reports["parquet"]
    assert missed_files["csv"] == missed_files["tsv"] == missed_files["parquet"]

    report = reports["parquet"]
    assert report["schema_version"] == "1.0"
    assert report["inputs"]["candidate_rows"] == len(candidates)
    assert report["inputs"]["duplicate_candidate_rows"] > 0
    assert report["ground_truth"]["fingerprint"]
    assert report["ground_truth"]["fingerprint_version"] == "v2"
    assert report["quality_gate"]["passed"] is False  # max_duplicate_rows=0 must fail
    assert list(sqlite_tmp.iterdir()) == []


@needs_pyarrow
def test_cross_parity_expected_values(tmp_path: Path) -> None:
    cand = write_format(tmp_path / "cand", "parquet", CROSS_CANDIDATES)
    tru = write_format(tmp_path / "truth", "parquet", CROSS_TRUTH)
    report = _audit(cand, tru, "cross")
    assert report["inputs"]["candidate_rows"] == 10
    assert report["inputs"]["unique_candidate_pairs"] == 8  # " A1 " trims to "A1"; (A4,B4) repeated
    assert report["inputs"]["duplicate_candidate_rows"] == 2
    assert report["ground_truth"]["unique_pairs"] == 4
    assert report["ground_truth"]["recovered_pairs"] == 3
    assert report["ground_truth"]["candidate_recall"] == 0.75


@needs_pyarrow
@pytest.mark.parametrize(
    "linkage_type,candidates,truth",
    [("cross", CROSS_CANDIDATES, CROSS_TRUTH), ("dedupe", DEDUPE_CANDIDATES, DEDUPE_TRUTH)],
)
@pytest.mark.parametrize("parquet_side", ["candidates", "truth"])
def test_parquet_as_candidate_or_truth_input_independently(
    tmp_path: Path, linkage_type: str, candidates: list[tuple[str, str]], truth: list[tuple[str, str]], parquet_side: str
) -> None:
    baseline = logical(_audit(
        write_format(tmp_path / "cand_base", "tsv", candidates),
        write_format(tmp_path / "truth_base", "tsv", truth),
        linkage_type,
        **AUDIT_GATES,
    ))
    cand_fmt, truth_fmt = ("parquet", "tsv") if parquet_side == "candidates" else ("tsv", "parquet")
    mixed = logical(_audit(
        write_format(tmp_path / "cand_mixed", cand_fmt, candidates),
        write_format(tmp_path / "truth_mixed", truth_fmt, truth),
        linkage_type,
        **AUDIT_GATES,
    ))
    assert mixed == baseline


@needs_pyarrow
def test_integer_parquet_ids_match_decimal_text_csv(tmp_path: Path) -> None:
    int_rows = [(1, 10), (2, 20), (1, 10), (3, 30)]
    pq_cand = write_parquet(tmp_path / "c.parquet", int_rows, types=(pa.int64(), pa.int32()))
    pq_truth = write_parquet(tmp_path / "t.parquet", [(1, 10), (4, 40)], types=(pa.uint16(), pa.int64()))
    csv_cand = write_delimited(tmp_path / "c.csv", int_rows, ",")
    csv_truth = write_delimited(tmp_path / "t.csv", [(1, 10), (4, 40)], ",")
    assert logical(_audit(pq_cand, pq_truth, "cross")) == logical(_audit(csv_cand, csv_truth, "cross"))


@needs_pyarrow
def test_empty_parquet_matches_header_only_csv(tmp_path: Path) -> None:
    pq_report = logical(_audit(write_parquet(tmp_path / "c.parquet", []), None, "cross"))
    csv_report = logical(_audit(write_delimited(tmp_path / "c.csv", [], ","), None, "cross"))
    assert pq_report == csv_report
    assert pq_report["inputs"]["candidate_rows"] == 0


@needs_pyarrow
def test_empty_parquet_truth_with_min_recall_is_invalid_like_csv(tmp_path: Path) -> None:
    cand = write_delimited(tmp_path / "c.tsv", [("a", "b")], "\t")
    for truth in (write_parquet(tmp_path / "t.parquet", []), write_delimited(tmp_path / "t.csv", [], ",")):
        with pytest.raises(AuditError, match="Ground-truth file contains no pairs"):
            _audit(cand, truth, "cross", min_recall=0.5)


@needs_pyarrow
def test_dedupe_self_pair_rejected_for_parquet(tmp_path: Path) -> None:
    cand = write_parquet(tmp_path / "c.parquet", [("x", "y"), (" z", "z ")])
    with pytest.raises(AuditError, match="Self-pair"):
        _audit(cand, None, "dedupe")


@needs_pyarrow
def test_sqlite_temp_files_cleaned_up_after_success(tmp_path: Path, sqlite_tmp: Path) -> None:
    cand = write_parquet(tmp_path / "c.parquet", CROSS_CANDIDATES)
    tru = write_parquet(tmp_path / "t.parquet", CROSS_TRUTH)
    _audit(cand, tru, "cross")
    assert list(sqlite_tmp.iterdir()) == []
    os.remove(cand)
    os.remove(tru)


@needs_pyarrow
@pytest.mark.parametrize("failure", ["null_mid_candidates", "corrupt_truth", "unsupported_truth_type", "missing_truth_column"])
def test_sqlite_temp_files_cleaned_up_after_failure(
    tmp_path: Path, sqlite_tmp: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(ParquetReader, "read_rows", 2)
    rows: list[tuple[Any, Any]] = [(f"s{i}", f"t{i}") for i in range(10)]
    if failure == "null_mid_candidates":
        rows[7] = (None, "t7")  # fails after earlier batches have been inserted into SQLite
    cand = write_parquet(tmp_path / "c.parquet", rows, row_group_size=3)
    truth = tmp_path / "t.parquet"
    if failure == "corrupt_truth":
        truth.write_bytes(b"corrupt")
    elif failure == "unsupported_truth_type":
        write_parquet(truth, [(1.0, 2.0)], types=(pa.float64(), pa.float64()))
    else:
        write_parquet(truth, [("a", "b")], header=("source_id", "other"))

    with pytest.raises(AuditError):
        _audit(cand, truth, "cross")
    assert list(sqlite_tmp.iterdir()) == []
    os.remove(cand)  # input handles must be released, including on Windows
    os.remove(truth)


@needs_pyarrow
def test_cli_parquet_exit_codes_and_json_parity(
    tmp_path: Path, sqlite_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pq_cand = write_parquet(tmp_path / "c.parquet", CROSS_CANDIDATES)
    pq_truth = write_parquet(tmp_path / "t.parquet", CROSS_TRUTH)
    tsv_cand = write_delimited(tmp_path / "c.tsv", CROSS_CANDIDATES, "\t")
    tsv_truth = write_delimited(tmp_path / "t.tsv", CROSS_TRUTH, "\t")
    base = ["audit", "--left-id", "source_id", "--right-id", "candidate_id"]

    def run(cand: Path, truth: Path, *extra: str) -> int:
        return main(base + ["--candidates", str(cand), "--truth", str(truth), *extra])

    assert run(pq_cand, pq_truth, "--min-recall", "0.75") == 0
    assert run(pq_cand, pq_truth, "--min-recall", "0.7501") == 1
    assert run(pq_cand, pq_truth, "--max-duplicate-rows", "1") == 1
    assert run(pq_cand, pq_truth, "--max-duplicate-rows", "2") == 0
    assert run(pq_cand, pq_truth, "--truth-left-id", "missing") == 2
    capsys.readouterr()

    pq_json, tsv_json = tmp_path / "pq.json", tmp_path / "tsv.json"
    assert run(pq_cand, pq_truth, "--format", "json", "--output", str(pq_json)) == 0
    assert run(tsv_cand, tsv_truth, "--format", "json", "--output", str(tsv_json)) == 0
    pq_report = json.loads(pq_json.read_text(encoding="utf-8"))
    tsv_report = json.loads(tsv_json.read_text(encoding="utf-8"))
    assert logical(pq_report) == logical(tsv_report)
    assert pq_report["inputs"]["candidates"] == str(pq_cand)

    assert run(pq_cand, pq_truth) == 0
    text = capsys.readouterr().out
    assert "Candidate recall:                75.0000%" in text
    assert list(sqlite_tmp.iterdir()) == []

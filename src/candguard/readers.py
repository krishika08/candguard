"""Input readers and reader protocol for CandGuard.

Separates input file reading from the processing and storage engine:

- ``DelimitedReader`` reads CSV/TSV text files using only the standard library.
- ``ParquetReader`` reads Apache Parquet files through the optional ``pyarrow``
  dependency. Install it from a source checkout with
  ``python -m pip install -e ".[parquet]"`` (run from the repository root), or, for a
  published CandGuard release that includes Parquet support, with
  ``python -m pip install "candguard[parquet]"``. The published 0.1.0 release does not
  include Parquet support. PyArrow is imported lazily, so importing CandGuard never
  requires it.
"""
from __future__ import annotations

import codecs
import csv
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from .models import AuditError

PARQUET_SUFFIXES = frozenset({".parquet"})

#: Installs the optional extra from PyPI. Only works for a published release that declares the
#: ``parquet`` extra; the published 0.1.0 release does not.
PARQUET_RELEASE_INSTALL_COMMAND = 'python -m pip install "candguard[parquet]"'
#: Installs the optional extra into an editable source checkout; run from the repository root.
PARQUET_SOURCE_INSTALL_COMMAND = 'python -m pip install -e ".[parquet]"'

# A single line, like every other AuditError message; backticks delimit each command.
PARQUET_INSTALL_HINT = (
    "Install it with the command for how CandGuard was installed: "
    "from a published CandGuard release that includes Parquet support (CandGuard 0.1.0 does not), "
    f"run `{PARQUET_RELEASE_INSTALL_COMMAND}`; "
    f"from a source checkout, run `{PARQUET_SOURCE_INSTALL_COMMAND}` from the repository root."
)


def _validate_batch_size(batch_size: Any) -> None:
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise AuditError(f"batch_size must be a positive integer, got {batch_size!r}.")


@runtime_checkable
class CandidateReader(Protocol):
    """Protocol for candidate-pair and ground-truth input readers."""

    @property
    def path(self) -> Path:
        """The source file path."""
        ...

    @property
    def left_col(self) -> str:
        """Name of the left/source ID column."""
        ...

    @property
    def right_col(self) -> str:
        """Name of the right/target ID column."""
        ...

    def iter_pairs(self) -> Iterator[tuple[str, str]]:
        """Yield pair identifier tuples as (left_id, right_id)."""
        ...

    def iter_batches(self, batch_size: int = 50_000) -> Iterator[list[tuple[str, str]]]:
        """Yield batches of pair tuples for bulk ingestion."""
        ...


def resolve_delimiter(path: Path, override: str | None) -> str:
    """Resolve file delimiter from an optional override or file suffix."""
    if override is not None:
        try:
            delim = codecs.decode(override, "unicode_escape")
        except (ValueError, UnicodeDecodeError) as exc:
            raise AuditError(f"Invalid delimiter escape sequence {override!r}: {exc}") from exc
        if len(delim) != 1:
            raise AuditError(f"Delimiter must be a single character, got {override!r}")
        return delim
    if path.suffix.lower() in {".tsv", ".tab"}:
        return "\t"
    return ","


class DelimitedReader:
    """Reader for delimited text candidate-pair files (CSV and TSV)."""

    def __init__(
        self,
        path: Path | str,
        left_col: str,
        right_col: str,
        delimiter: str | None = None,
    ) -> None:
        self._path = Path(path)
        self._left_col = left_col
        self._right_col = right_col
        self._delimiter_override = delimiter

    @property
    def path(self) -> Path:
        return self._path

    @property
    def left_col(self) -> str:
        return self._left_col

    @property
    def right_col(self) -> str:
        return self._right_col

    @property
    def delimiter(self) -> str:
        return resolve_delimiter(self._path, self._delimiter_override)

    def iter_pairs(self) -> Iterator[tuple[str, str]]:
        if not self._path.exists():
            raise AuditError(f"Input file does not exist: {self._path}")
        if not self._path.is_file():
            raise AuditError(f"Input path is not a file: {self._path}")

        sep = self.delimiter
        try:
            with self._path.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream, delimiter=sep)
                try:
                    header = next(reader)
                except StopIteration:
                    raise AuditError(f"Input file is empty or has no header: {self._path}")

                if not header:
                    raise AuditError(f"Input file is empty or has no header: {self._path}")

                valid_fieldnames = [name for name in header if name is not None]
                missing = [name for name in (self._left_col, self._right_col) if name not in valid_fieldnames]
                if missing:
                    raise AuditError(
                        f"Missing column(s) in {self._path}: {', '.join(missing)}. "
                        f"Available columns: {', '.join(valid_fieldnames)}"
                    )

                if self._left_col == self._right_col:
                    raise AuditError(
                        f"Left and right identifier columns cannot be identical ('{self._left_col}') in {self._path}."
                    )
                if valid_fieldnames.count(self._left_col) > 1:
                    raise AuditError(
                        f"Duplicate column '{self._left_col}' in {self._path} header makes identifier selection ambiguous."
                    )
                if valid_fieldnames.count(self._right_col) > 1:
                    raise AuditError(
                        f"Duplicate column '{self._right_col}' in {self._path} header makes identifier selection ambiguous."
                    )

                left_idx = header.index(self._left_col)
                right_idx = header.index(self._right_col)
                expected_len = len(header)

                for line_no, row in enumerate(reader, start=2):
                    if not row or all(not field.strip() for field in row):
                        raise AuditError(
                            f"Blank line encountered at {self._path}:{line_no}; remove empty lines from delimited input."
                        )
                    if len(row) > expected_len:
                        raise AuditError(f"Unexpected extra field at {self._path}:{line_no}; check the delimiter.")
                    if len(row) <= left_idx or len(row) <= right_idx:
                        raise AuditError(
                            f"Truncated row with missing column(s) at {self._path}:{line_no}; "
                            f"expected at least {max(left_idx, right_idx) + 1} fields, got {len(row)}."
                        )
                    left = row[left_idx]
                    right = row[right_idx]
                    if not left.strip() or not right.strip():
                        raise AuditError(
                            f"Missing/empty pair identifier at {self._path}:{line_no}; "
                            f"columns '{self._left_col}' and '{self._right_col}' must both be populated."
                        )
                    yield left.strip(), right.strip()
        except UnicodeDecodeError as exc:
            raise AuditError(f"Could not read {self._path} as UTF-8 text: {exc}") from exc
        except csv.Error as exc:
            raise AuditError(f"Could not parse delimited file {self._path}: {exc}") from exc

    def iter_batches(self, batch_size: int = 50_000) -> Iterator[list[tuple[str, str]]]:
        """Yield batches of pair tuples for bulk ingestion.

        Args:
            batch_size: Positive maximum number of pairs per batch.

        Yields:
            Lists of (left_id, right_id) pair tuples.

        Raises:
            AuditError: If batch_size is less than or equal to zero.
        """
        _validate_batch_size(batch_size)
        batch: list[tuple[str, str]] = []
        for pair in self.iter_pairs():
            batch.append(pair)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch


def _import_pyarrow_parquet() -> tuple[Any, Any]:
    """Import PyArrow lazily, raising an informative AuditError when it is unavailable."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise AuditError(
            "Reading Parquet input requires the optional PyArrow dependency, which is not installed "
            f"or could not be imported ({exc}). {PARQUET_INSTALL_HINT}"
        ) from exc
    return pa, pq


def _identifier_kind(pa: Any, arrow_type: Any) -> str | None:
    """Classify an Arrow type as a supported identifier kind ("string"/"integer") or None."""
    value_type = arrow_type.value_type if pa.types.is_dictionary(arrow_type) else arrow_type
    if (
        pa.types.is_string(value_type)
        or pa.types.is_large_string(value_type)
        or (hasattr(pa.types, "is_string_view") and pa.types.is_string_view(value_type))
    ):
        return "string"
    if pa.types.is_integer(value_type):
        return "integer"
    return None


class ParquetReader:
    """Reader for Apache Parquet candidate-pair and ground-truth files.

    Requires the optional ``pyarrow`` dependency. From a source checkout, install it with
    ``python -m pip install -e ".[parquet]"`` run from the repository root. From a published
    CandGuard release that includes Parquet support (CandGuard 0.1.0 does not), use
    ``python -m pip install "candguard[parquet]"``. See ``PARQUET_INSTALL_HINT``.

    Data is read incrementally with ``pyarrow.parquet.ParquetFile.iter_batches()``, projecting
    only the two identifier columns, and yielded in file row order.

    Identifier policy:

    - String columns (``string``, ``large_string``, ``string_view``, or dictionary-encoded
      strings) are trimmed with ``str.strip()``, exactly like CSV/TSV identifiers.
    - Integer columns (signed or unsigned, any width, or dictionary-encoded integers) are
      converted to base-10 text with ``str(int)``, e.g. ``42`` -> ``"42"``, ``-7`` -> ``"-7"``.
    - Null identifiers and identifiers that are empty after trimming are rejected.
    - All other types (floats, booleans, binary, decimals, dates/timestamps, nested types)
      are rejected rather than silently stringified.
    """

    #: Number of rows requested from PyArrow per record batch. Output batches are re-chunked
    #: to the caller's ``batch_size`` independently of this value and of row-group boundaries.
    read_rows: int = 65_536

    def __init__(self, path: Path | str, left_col: str, right_col: str) -> None:
        self._path = Path(path)
        self._left_col = left_col
        self._right_col = right_col
        self._pa, self._pq = _import_pyarrow_parquet()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def left_col(self) -> str:
        return self._left_col

    @property
    def right_col(self) -> str:
        return self._right_col

    def _validate_schema(self, schema: Any) -> tuple[str, str]:
        names = list(schema.names)
        missing = [name for name in (self._left_col, self._right_col) if name not in names]
        if missing:
            raise AuditError(
                f"Missing column(s) in {self._path}: {', '.join(missing)}. "
                f"Available columns: {', '.join(names)}"
            )
        if self._left_col == self._right_col:
            raise AuditError(
                f"Left and right identifier columns cannot be identical ('{self._left_col}') in {self._path}."
            )
        kinds: list[str] = []
        for name in (self._left_col, self._right_col):
            if names.count(name) > 1:
                raise AuditError(
                    f"Duplicate column '{name}' in {self._path} schema makes identifier selection ambiguous."
                )
            arrow_type = schema.field(name).type
            kind = _identifier_kind(self._pa, arrow_type)
            if kind is None:
                raise AuditError(
                    f"Unsupported Parquet type '{arrow_type}' for identifier column '{name}' in {self._path}. "
                    "Identifier columns must be string or integer typed; cast other types to string "
                    "before writing the file."
                )
            kinds.append(kind)
        return kinds[0], kinds[1]

    def _iter_pair_chunks(self) -> Iterator[list[tuple[str, str]]]:
        """Yield lists of normalized pairs, one list per Arrow record batch read from the file."""
        if not self._path.exists():
            raise AuditError(f"Input file does not exist: {self._path}")
        if not self._path.is_file():
            raise AuditError(
                f"Input path is not a file: {self._path}. "
                "Partitioned Parquet dataset directories are not supported; supply a single Parquet file."
            )

        pa = self._pa
        left_col, right_col = self._left_col, self._right_col
        try:
            with self._path.open("rb") as handle:
                # Page checksums are verified when the writer stored them (opt-in for most writers).
                parquet_file = self._pq.ParquetFile(handle, page_checksum_verification=True)
                left_kind, right_kind = self._validate_schema(parquet_file.schema_arrow)
                left_is_str = left_kind == "string"
                right_is_str = right_kind == "string"
                row_offset = 0
                for record_batch in parquet_file.iter_batches(
                    batch_size=self.read_rows, columns=[left_col, right_col]
                ):
                    left_idx = record_batch.schema.get_field_index(left_col)
                    right_idx = record_batch.schema.get_field_index(right_col)
                    if left_idx < 0 or right_idx < 0:
                        raise AuditError(
                            f"Could not project identifier columns '{left_col}' and '{right_col}' "
                            f"from {self._path}."
                        )
                    lefts = record_batch.column(left_idx).to_pylist()
                    rights = record_batch.column(right_idx).to_pylist()
                    pairs: list[tuple[str, str]] = []
                    append = pairs.append
                    for offset, (left, right) in enumerate(zip(lefts, rights)):
                        if left is None or right is None:
                            raise AuditError(
                                f"Null identifier in column '{left_col if left is None else right_col}' "
                                f"at {self._path} row {row_offset + offset + 1}; columns '{left_col}' "
                                f"and '{right_col}' must both be populated."
                            )
                        left_text = left.strip() if left_is_str else str(left)
                        right_text = right.strip() if right_is_str else str(right)
                        if not left_text or not right_text:
                            raise AuditError(
                                f"Empty identifier in column '{right_col if left_text else left_col}' "
                                f"at {self._path} row {row_offset + offset + 1}; columns '{left_col}' "
                                f"and '{right_col}' must both be populated."
                            )
                        append((left_text, right_text))
                    row_offset += len(lefts)
                    if pairs:
                        yield pairs
        except (pa.ArrowException, OSError) as exc:
            raise AuditError(f"Could not read Parquet file {self._path}: {exc}") from exc

    def iter_pairs(self) -> Iterator[tuple[str, str]]:
        for chunk in self._iter_pair_chunks():
            yield from chunk

    def iter_batches(self, batch_size: int = 50_000) -> Iterator[list[tuple[str, str]]]:
        """Yield batches of pair tuples for bulk ingestion.

        Every batch except possibly the last contains exactly ``batch_size`` pairs,
        regardless of Parquet row-group or record-batch boundaries.

        Raises:
            AuditError: If batch_size is not a positive integer.
        """
        _validate_batch_size(batch_size)
        pending: list[tuple[str, str]] = []
        for chunk in self._iter_pair_chunks():
            if pending:
                pending.extend(chunk)
            else:
                pending = chunk
            start = 0
            while len(pending) - start >= batch_size:
                yield pending[start : start + batch_size]
                start += batch_size
            pending = pending[start:]
        if pending:
            yield pending


def get_reader(
    path: str | Path,
    left_col: str,
    right_col: str,
    delimiter: str | None = None,
) -> CandidateReader:
    """Return an appropriate CandidateReader for the given input path.

    ``.parquet`` files (case-insensitive) use ``ParquetReader``; all other paths use
    ``DelimitedReader`` (tab-delimited for ``.tsv``/``.tab``, comma otherwise).
    A delimiter override is rejected for Parquet input because it would have no effect.
    """
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix in PARQUET_SUFFIXES:
        if delimiter is not None:
            raise AuditError(
                f"A delimiter override ({delimiter!r}) was supplied for Parquet input {file_path}, "
                "but delimiters apply only to CSV/TSV files. Remove --delimiter/--truth-delimiter "
                "for Parquet inputs."
            )
        return ParquetReader(path=file_path, left_col=left_col, right_col=right_col)
    return DelimitedReader(
        path=file_path,
        left_col=left_col,
        right_col=right_col,
        delimiter=delimiter,
    )

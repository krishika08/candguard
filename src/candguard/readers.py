"""Input readers and reader protocol for CandGuard.

Separates delimited file reading (CSV/TSV) from the processing and storage engine.
Designed to be extended with columnar format readers (e.g., Parquet via pyarrow).
"""
from __future__ import annotations

import codecs
import csv
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

from .models import AuditError


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
        batch: list[tuple[str, str]] = []
        for pair in self.iter_pairs():
            batch.append(pair)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch


def get_reader(
    path: str | Path,
    left_col: str,
    right_col: str,
    delimiter: str | None = None,
) -> CandidateReader:
    """Return an appropriate CandidateReader for the given input path."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".parquet":
        raise AuditError(
            f"Parquet support is not yet installed or enabled for {file_path}. "
            "Delimited CSV/TSV formats are currently supported."
        )
    return DelimitedReader(
        path=file_path,
        left_col=left_col,
        right_col=right_col,
        delimiter=delimiter,
    )

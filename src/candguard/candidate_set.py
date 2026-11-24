"""Canonical candidate-set sidecar artifacts and exact streaming candidate-set comparison.

An audit can export its unique, normalized candidate pairs to a sorted TSV sidecar
(``candguard-pairs-tsv-v1``). ``candguard compare`` validates two such artifacts against the
``candidate_set`` metadata recorded in their audit reports and merges them in one streaming
pass, holding at most one pair from each side in memory.

Ordering is ascending by ``(left_id, right_id)`` using code-point string comparison, which is
identical to SQLite ``BINARY`` collation over UTF-8 text (UTF-8 byte order equals Unicode
code-point order).
"""
from __future__ import annotations

import csv
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .metrics import CANDIDATE_SET_FINGERPRINT_VERSION, CandidateSetFingerprintV1
from .models import AuditError

CANDIDATE_SET_FORMAT = "candguard-pairs-tsv-v1"
CANDIDATE_SET_HEADER = ("left_id", "right_id")
_HEADER_ROW = list(CANDIDATE_SET_HEADER)

_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Python < 3.11 csv readers reject NUL characters, so such artifacts could not be read back.
_CSV_SUPPORTS_NUL = sys.version_info >= (3, 11)
_REPR_LIMIT = 80


def _short(value: str) -> str:
    text = repr(value)
    return text if len(text) <= _REPR_LIMIT else text[: _REPR_LIMIT - 3] + "..."


def _pair_text(pair: tuple[str, str]) -> str:
    return f"({_short(pair[0])}, {_short(pair[1])})"


def same_path(first: str | Path, second: str | Path) -> bool:
    """Return True when two paths refer to the same file (following links, ignoring case on Windows)."""
    a, b = Path(first), Path(second)
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def ensure_distinct_output(out_path: Path, others: Mapping[str, str | Path | None]) -> None:
    """Reject an output path that resolves to an input or another output file."""
    for label, other in others.items():
        if other is not None and same_path(out_path, other):
            raise AuditError(
                f"Candidate-set output path {out_path} resolves to the {label} ({other}); "
                "choose a different --candidate-set-out path."
            )
    if out_path.exists() and not out_path.is_file():
        raise AuditError(f"Candidate-set output path is not a regular file: {out_path}")


def write_candidate_set_artifact(
    pairs: Iterable[tuple[str, str]],
    out_path: str | Path,
    *,
    linkage_type: str,
    expected_pairs: int,
) -> dict[str, Any]:
    """Atomically write sorted unique pairs as a ``candguard-pairs-tsv-v1`` artifact.

    Pairs are streamed to a temporary file in the destination directory, checked for strict
    ascending order (and canonical ``left < right`` order in dedupe mode), hashed, and the
    temporary file atomically replaces ``out_path`` only after every pair was written and the
    count matches ``expected_pairs``. On any failure the temporary file is removed and an
    existing file at ``out_path`` is left untouched. Returns the report metadata.
    """
    target = Path(out_path)
    parent = target.parent
    is_dedupe = linkage_type == "dedupe"
    hasher = CandidateSetFingerprintV1(linkage_type)
    try:
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
    except OSError as exc:
        raise AuditError(f"Could not create candidate-set artifact {target}: {exc}") from exc

    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t", quotechar='"', lineterminator="\n")
            writer.writerow(CANDIDATE_SET_HEADER)
            previous: tuple[str, str] | None = None
            for pair in pairs:
                left, right = pair
                if not left or not right:
                    raise AuditError(f"Cannot export candidate pair with an empty identifier: {_pair_text(pair)}.")
                if previous is not None and not previous < pair:
                    raise AuditError(
                        "Candidate pairs were not produced in strict ascending order "
                        f"({_pair_text(previous)} then {_pair_text(pair)}); refusing to write {target}."
                    )
                if is_dedupe and not left < right:
                    raise AuditError(f"Deduplication pair {_pair_text(pair)} is not canonical (left_id < right_id).")
                if not _CSV_SUPPORTS_NUL and ("\x00" in left or "\x00" in right):
                    raise AuditError(
                        f"Candidate identifier {_pair_text(pair)} contains a NUL character, which the csv "
                        "module of Python < 3.11 cannot read back; use Python 3.11+ to export this candidate set."
                    )
                writer.writerow(pair)
                hasher.update(left, right)
                previous = pair
            stream.flush()
            os.fsync(stream.fileno())
        if hasher.count != expected_pairs:
            raise AuditError(
                f"Candidate-set export wrote {hasher.count} pair(s) but the audit counted {expected_pairs} "
                f"unique pair(s); refusing to write {target}."
            )
        os.replace(tmp_path, target)
    except OSError as exc:
        raise AuditError(f"Could not write candidate-set artifact to {target}: {exc}") from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return {
        "format": CANDIDATE_SET_FORMAT,
        "unique_pairs": hasher.count,
        "fingerprint": hasher.hexdigest(),
        "fingerprint_version": CANDIDATE_SET_FINGERPRINT_VERSION,
        "linkage_type": linkage_type,
    }


def validate_candidate_set_metadata(report: Mapping[str, Any], label: str, report_path: str | Path) -> dict[str, Any]:
    """Return the validated ``candidate_set`` section of an audit report, or raise AuditError."""
    metadata = report.get("candidate_set")
    where = f"{label} report ({report_path})"
    if metadata is None:
        raise AuditError(
            f"{where} has no 'candidate_set' metadata, so its candidate-set artifact cannot be verified. "
            "Re-run both audits with --candidate-set-out and compare the reports produced alongside the artifacts."
        )
    if not isinstance(metadata, dict):
        raise AuditError(f"Invalid 'candidate_set' section in {where}: expected an object.")

    fmt = metadata.get("format")
    if fmt != CANDIDATE_SET_FORMAT:
        raise AuditError(
            f"Unsupported candidate-set artifact format in {where}: expected {CANDIDATE_SET_FORMAT!r}, got {fmt!r}."
        )
    version = metadata.get("fingerprint_version")
    if version != CANDIDATE_SET_FINGERPRINT_VERSION:
        raise AuditError(
            f"Unsupported candidate-set fingerprint version in {where}: "
            f"expected {CANDIDATE_SET_FINGERPRINT_VERSION!r}, got {version!r}."
        )
    unique_pairs = metadata.get("unique_pairs")
    if isinstance(unique_pairs, bool) or not isinstance(unique_pairs, int) or unique_pairs < 0:
        raise AuditError(
            f"Invalid 'candidate_set.unique_pairs' in {where}: expected a non-negative integer, got {unique_pairs!r}."
        )
    reported_unique = report["inputs"]["unique_candidate_pairs"]
    if unique_pairs != reported_unique:
        raise AuditError(
            f"Inconsistent {where}: candidate_set.unique_pairs={unique_pairs} does not match "
            f"inputs.unique_candidate_pairs={reported_unique}."
        )
    fingerprint = metadata.get("fingerprint")
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.match(fingerprint):
        raise AuditError(
            f"Invalid 'candidate_set.fingerprint' in {where}: expected 'sha256:' followed by 64 hex digits, "
            f"got {fingerprint!r}."
        )
    linkage_type = metadata.get("linkage_type")
    report_linkage = report["efficiency"]["linkage_type"]
    if linkage_type != report_linkage:
        raise AuditError(
            f"Candidate-set linkage type {linkage_type!r} in {where} does not match the report linkage type "
            f"{report_linkage!r}."
        )
    return dict(metadata)


class CandidateSetArtifactReader:
    """Stream and validate a ``candguard-pairs-tsv-v1`` artifact against its report metadata.

    The path and header are validated on construction. Iteration yields one ``(left_id,
    right_id)`` pair at a time and raises AuditError as soon as a row is malformed, empty,
    duplicated, out of order, non-canonical (dedupe) or exceeds the expected count. When the
    file is exhausted the pair count and fingerprint are verified before StopIteration is
    raised, so a fully consumed reader has verified the whole artifact. Use as a context manager
    to guarantee the file is closed.
    """

    def __init__(self, path: str | Path, metadata: Mapping[str, Any], *, label: str) -> None:
        self.path = Path(path)
        self.label = label
        self._name = f"{label} candidate-set artifact ({self.path})"
        self._linkage_type = str(metadata["linkage_type"])
        self._expected_pairs = int(metadata["unique_pairs"])
        self._expected_fingerprint = str(metadata["fingerprint"])
        self._is_dedupe = self._linkage_type == "dedupe"
        self._hasher = CandidateSetFingerprintV1(self._linkage_type)
        self._previous: tuple[str, str] | None = None
        self._verified = False
        self._stream: Any = None

        if not self.path.exists():
            raise AuditError(f"{label} candidate-set artifact does not exist: {self.path}")
        if not self.path.is_file():
            raise AuditError(f"{label} candidate-set artifact is not a regular file: {self.path}")
        try:
            self._stream = open(self.path, "r", encoding="utf-8", newline="")
        except OSError as exc:
            raise AuditError(f"Could not open {self._name}: {exc}") from exc
        try:
            self._reader = csv.reader(self._stream, delimiter="\t", quotechar='"', doublequote=True, strict=True)
            header = self._read_row()
            if header is None:
                raise AuditError(f"{self._name} is empty; expected the header 'left_id<TAB>right_id'.")
            if header != _HEADER_ROW:
                hint = " (the file starts with a UTF-8 byte-order mark)" if header and header[0].startswith("﻿") else ""
                raise AuditError(
                    f"{self._name} has an invalid header {header!r}{hint}; "
                    f"expected exactly ['left_id', 'right_id'] separated by a tab ({CANDIDATE_SET_FORMAT})."
                )
        except BaseException:
            self.close()
            raise

    @property
    def verified(self) -> bool:
        """True once the artifact was read to the end and its count and fingerprint matched."""
        return self._verified

    def _read_row(self) -> list[str] | None:
        try:
            return next(self._reader)
        except StopIteration:
            return None
        except UnicodeDecodeError as exc:
            raise AuditError(
                f"{self._name} is not valid UTF-8 (decoding failed after line {self._reader.line_num}): {exc}"
            ) from exc
        except csv.Error as exc:
            raise AuditError(f"{self._name} is malformed at line {self._reader.line_num}: {exc}") from exc
        except OSError as exc:
            raise AuditError(f"Could not read {self._name}: {exc}") from exc

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return self

    def __next__(self) -> tuple[str, str]:
        if self._stream is None:
            if self._verified:
                raise StopIteration
            raise AuditError(f"{self._name} is closed.")
        row = self._read_row()
        if row is None:
            self._verify_complete()
            raise StopIteration
        line = self._reader.line_num
        if len(row) != 2:
            raise AuditError(f"{self._name} line {line}: expected 2 tab-separated fields, found {len(row)}.")
        left, right = row
        pair = (left, right)
        if not left or not right:
            raise AuditError(f"{self._name} line {line}: empty identifier in pair {_pair_text(pair)}.")
        previous = self._previous
        if previous is not None and not previous < pair:
            if previous == pair:
                raise AuditError(f"{self._name} line {line}: duplicate pair {_pair_text(pair)}.")
            raise AuditError(
                f"{self._name} line {line}: pairs are not in ascending (left_id, right_id) order; "
                f"{_pair_text(pair)} follows {_pair_text(previous)}."
            )
        if self._is_dedupe and not left < right:
            kind = "self-pair" if left == right else "non-canonical pair"
            raise AuditError(
                f"{self._name} line {line}: {kind} {_pair_text(pair)}; deduplication artifacts must store "
                "each pair once with left_id < right_id."
            )
        self._hasher.update(left, right)
        if self._hasher.count > self._expected_pairs:
            raise AuditError(
                f"{self._name} contains more pairs than the {self._expected_pairs} recorded in its report "
                "(candidate_set.unique_pairs)."
            )
        self._previous = pair
        return pair

    def _verify_complete(self) -> None:
        if self._verified:
            return
        count = self._hasher.count
        if count != self._expected_pairs:
            raise AuditError(
                f"{self._name} contains {count} pair(s) but its report records "
                f"candidate_set.unique_pairs={self._expected_pairs}; the artifact is truncated or does not "
                "belong to this report."
            )
        actual = self._hasher.hexdigest()
        if actual != self._expected_fingerprint:
            raise AuditError(
                f"{self._name} fingerprint {actual} does not match its report fingerprint "
                f"{self._expected_fingerprint}; the artifact was modified or belongs to a different audit."
            )
        self._verified = True
        self.close()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            finally:
                self._stream = None

    def __enter__(self) -> CandidateSetArtifactReader:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def merge_sorted_pair_streams(
    baseline: Iterator[tuple[str, str]],
    current: Iterator[tuple[str, str]],
) -> tuple[int, int, int]:
    """Return ``(retained, added, removed)`` for two strictly ascending unique pair streams.

    Exact sorted merge: holds at most the current pair from each side and drains both streams
    completely, so validating iterators finish their end-of-stream checks.
    """
    retained = added = removed = 0
    b = next(baseline, None)
    c = next(current, None)
    while b is not None and c is not None:
        if b == c:
            retained += 1
            b = next(baseline, None)
            c = next(current, None)
        elif b < c:
            removed += 1
            b = next(baseline, None)
        else:
            added += 1
            c = next(current, None)
    while b is not None:
        removed += 1
        b = next(baseline, None)
    while c is not None:
        added += 1
        c = next(current, None)
    return retained, added, removed


def compare_candidate_set_artifacts(
    baseline_path: str | Path,
    current_path: str | Path,
    baseline_metadata: Mapping[str, Any],
    current_metadata: Mapping[str, Any],
) -> tuple[int, int, int]:
    """Validate two artifacts while merging them; return ``(retained, added, removed)``."""
    with CandidateSetArtifactReader(baseline_path, baseline_metadata, label="Baseline") as baseline:
        with CandidateSetArtifactReader(current_path, current_metadata, label="Current") as current:
            counts = merge_sorted_pair_streams(baseline, current)
            if not (baseline.verified and current.verified):  # pragma: no cover - defensive invariant
                raise AuditError("Candidate-set artifacts were not fully verified.")
    return counts

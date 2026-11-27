"""Private coordination for staging and publishing audit files."""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TextIO

from .models import AuditError

_LOG = logging.getLogger(__name__)


def _regular_destination(path: Path, description: str) -> os.stat_result | None:
    """Return metadata for an existing regular destination and reject unsupported file types."""
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError) as exc:
        raise AuditError(f"Could not write {description} to {path}: {exc}") from exc

    reparse_point = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if not stat.S_ISREG(info.st_mode) or reparse_point:
        raise AuditError(f"Could not write {description} to {path}: destination is not a regular file.")
    return info


def _open_new_stage(parent: Path, newline: str | None) -> TextIO:
    # Exclusive creation applies the OS's normal 0666 creation mode without changing umask.
    for _ in range(10):
        path = parent / f".candguard-{secrets.token_hex(16)}.stage"
        try:
            return path.open("x", encoding="utf-8", newline=newline)
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not allocate a unique staging file in {parent}.")


@dataclass
class _StagedOutput:
    target: Path
    staged: Path
    description: str
    backup: Path | None = None


class _OutputPublication:
    """Stage files together and attempt reverse-order recovery after a failed replacement."""

    def __init__(self) -> None:
        self._outputs: list[_StagedOutput] = []
        self._cleanup_paths: set[Path] = set()

    def __enter__(self) -> _OutputPublication:
        return self

    def __exit__(self, *exc_info: object) -> None:
        for path in sorted(self._cleanup_paths):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                # Cleanup is best effort. It cannot undo a successfully published group.
                _LOG.warning("Could not remove temporary audit file %s: %s", path, exc)

    @contextmanager
    def stage(
        self, target: Path, description: str, *, newline: str | None = None, create_action: str = "write"
    ) -> Iterator[TextIO]:
        original = _regular_destination(target, description) if os.name == "posix" else None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.name == "posix" and original is None:
                stream = _open_new_stage(target.parent, newline)
            else:
                stream = tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline=newline, dir=target.parent,
                    prefix=".candguard-", suffix=".stage", delete=False,
                )
        except (OSError, UnicodeError) as exc:
            raise AuditError(f"Could not {create_action} {description} at {target}: {exc}") from exc

        staged = Path(stream.name)
        self._cleanup_paths.add(staged)
        try:
            with stream:
                yield stream
                stream.flush()
                if original is not None:
                    mode = stat.S_IMODE(original.st_mode)
                    os.fchmod(stream.fileno(), mode)
                    if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != mode:
                        raise OSError("The staged file could not retain the destination's permission mode.")
                os.fsync(stream.fileno())
        except (OSError, UnicodeError) as exc:
            raise AuditError(f"Could not write {description} to {target}: {exc}") from exc
        self._outputs.append(_StagedOutput(target, staged, description))

    def _backup(self, output: _StagedOutput) -> None:
        original = _regular_destination(output.target, output.description)
        if original is None:
            return
        try:
            with output.target.open("rb") as source, tempfile.NamedTemporaryFile(
                mode="wb", dir=output.target.parent, prefix=".candguard-", suffix=".backup", delete=False,
            ) as stream:
                backup = Path(stream.name)
                self._cleanup_paths.add(backup)
                shutil.copyfileobj(source, stream)
                stream.flush()
                os.fsync(stream.fileno())
            shutil.copystat(output.target, backup)
            if os.name == "posix" and stat.S_IMODE(backup.stat().st_mode) != stat.S_IMODE(original.st_mode):
                raise OSError("The backup could not retain the destination's permission mode.")
        except OSError as exc:
            raise AuditError(f"Could not back up {output.description} at {output.target}: {exc}") from exc
        output.backup = backup

    def publish(self) -> None:
        for output in self._outputs:
            self._backup(output)

        replaced: list[_StagedOutput] = []
        for output in self._outputs:
            try:
                os.replace(output.staged, output.target)
            except OSError as exc:
                failures = self._rollback(replaced)
                message = f"Could not publish {output.description} to {output.target}: {exc}"
                if failures:
                    message += ". Rollback failed: " + " | ".join(failures)
                raise AuditError(message) from exc
            self._cleanup_paths.discard(output.staged)
            replaced.append(output)

    def _rollback(self, replaced: list[_StagedOutput]) -> list[str]:
        failures: list[str] = []
        for output in reversed(replaced):
            backup = output.backup
            try:
                if backup is None:
                    output.target.unlink(missing_ok=True)
                else:
                    os.replace(backup, output.target)
                    self._cleanup_paths.discard(backup)
            except OSError as exc:
                if backup is None:
                    failures.append(f"could not remove new output {output.target}: {exc}")
                else:
                    self._cleanup_paths.discard(backup)
                    failures.append(f"could not restore {output.target}: {exc} (recovery file: {backup})")
        return failures

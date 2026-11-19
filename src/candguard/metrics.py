"""Metric computation and evaluation functions for CandGuard."""
from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable

from .models import AuditError


def compute_possible_pairs(
    linkage_type: str,
    left_records: int | None,
    right_records: int | None = None,
) -> int | None:
    """Calculate the theoretical maximum possible pairs for the linkage mode."""
    if linkage_type == "cross":
        if left_records is not None and right_records is not None:
            return left_records * right_records
        return None
    if linkage_type == "dedupe":
        if left_records is not None:
            return left_records * (left_records - 1) // 2
        return None
    raise AuditError(f"Unsupported linkage_type: {linkage_type!r}")


def compute_reduction_ratio(
    *,
    unique_pairs: int,
    possible_pairs: int | None,
) -> float | None:
    """Compute reduction ratio against total possible pairs."""
    if possible_pairs is None:
        return None
    if possible_pairs == 0:
        return None
    return round(1.0 - (unique_pairs / possible_pairs), 8)


def compute_truth_fingerprint_v2(
    pairs: Iterable[tuple[str, str]],
    linkage_type: str,
) -> str:
    """Compute deterministic SHA-256 v2 length-prefixed digest for ground-truth pairs."""
    hasher = hashlib.sha256()
    hasher.update(f"candguard:truth:v2:{linkage_type}\n".encode("utf-8"))
    for left, right in pairs:
        l_bytes = left.encode("utf-8")
        r_bytes = right.encode("utf-8")
        hasher.update(
            f"{len(l_bytes)}:".encode("ascii")
            + l_bytes
            + b"\t"
            + f"{len(r_bytes)}:".encode("ascii")
            + r_bytes
            + b"\n"
        )
    return f"sha256:{hasher.hexdigest()}"


def compute_relative_growth(base: int, curr: int) -> float:
    """Compute relative growth ratio ((curr - base) / base)."""
    if base == 0:
        return 0.0 if curr == 0 else float("inf")
    return (curr - base) / base

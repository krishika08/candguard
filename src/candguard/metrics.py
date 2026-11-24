"""Metric computation and evaluation functions for CandGuard."""
from __future__ import annotations

import hashlib
import math
from fractions import Fraction
from typing import Any, Iterable

from .models import AuditError

#: Decimal places used for every ratio/float in the advanced coverage and degree metrics.
METRIC_PRECISION = 8

#: Nearest-rank percentiles reported by the degree analysis, as exact fractions.
DEGREE_PERCENTILES: tuple[tuple[str, int, int], ...] = (
    ("p50", 50, 100),
    ("p95", 95, 100),
    ("p99", 99, 100),
    ("p99_9", 999, 1000),
)


def _round(value: float) -> float:
    return round(value, METRIC_PRECISION)


def nearest_rank(numerator: int, denominator: int, population: int) -> int:
    """Return the 1-based nearest rank ``max(1, ceil(numerator / denominator * population))``.

    Uses exact integer arithmetic so ranks never drift because of floating-point error.
    """
    if population <= 0:
        raise ValueError("population must be positive")
    return max(1, -(-numerator * population // denominator))


def compute_mean_degree_ratio(left: dict[str, Any], right: dict[str, Any]) -> float | None:
    """Return right mean degree / left mean degree from two degree summaries, computed exactly.

    Uses each summary's population (universe when supplied, else represented records) and
    total degree. Returns None when either population is empty or the left mean is zero.
    """
    def population(summary: dict[str, Any]) -> int:
        universe = summary["records_in_universe"]
        return int(universe if universe is not None else summary["records_with_candidates"])

    left_n, right_n = population(left), population(right)
    left_s, right_s = int(left["total_degree"]), int(right["total_degree"])
    if left_n == 0 or right_n == 0 or left_s == 0:
        return None
    return _round(float(Fraction(right_s * left_n, right_n * left_s)))


def compute_record_coverage(
    linkage_type: str,
    coverage: dict[str, int],
    left_records: int | None,
    right_records: int | None,
) -> dict[str, int | float | None]:
    """Return record-universe coverage fields to add to the coverage report.

    Fields are always present; they are None when the corresponding total was not supplied,
    and the coverage ratio is None when the supplied total is zero. These are counts only:
    the IDs of records without candidates cannot be derived from a total count.
    """
    def side(represented: int, total: int | None) -> tuple[int | None, int | None, float | None]:
        if total is None:
            return None, None, None
        if represented > total:
            raise AuditError(
                f"Record total {total} is smaller than the {represented} records represented in candidate output."
            )
        return total, total - represented, compute_ratio(represented, total)

    if linkage_type == "dedupe":
        records = coverage.get("records_represented", coverage["left_records_represented"])
        total, without, ratio = side(records, left_records)
        return {"records_total": total, "records_without_candidates": without, "record_coverage": ratio}

    l_total, l_without, l_ratio = side(coverage["left_records_represented"], left_records)
    r_total, r_without, r_ratio = side(coverage["right_records_represented"], right_records)
    return {
        "left_records_total": l_total,
        "right_records_total": r_total,
        "left_records_without_candidates": l_without,
        "right_records_without_candidates": r_without,
        "left_record_coverage": l_ratio,
        "right_record_coverage": r_ratio,
    }


def compute_ratio(numerator: int, denominator: int | None) -> float | None:
    """Return ``numerator / denominator`` rounded to METRIC_PRECISION, or None for a missing/zero denominator."""
    if denominator is None or denominator == 0:
        return None
    return _round(numerator / denominator)


def summarize_degree_histogram(
    histogram: Iterable[tuple[int, int]],
    *,
    represented_records: int,
    total_degree: int,
    universe_records: int | None,
) -> dict[str, Any]:
    """Summarize a degree distribution from an ascending ``(degree, record_count)`` histogram.

    ``histogram`` must yield each distinct positive degree once, in ascending order, with the
    number of represented records having that degree. It is consumed in a single streaming
    pass; nothing is materialized, so Python memory is constant and work is proportional to
    the number of distinct degree values.

    When ``universe_records`` is given, ``universe_records - represented_records`` additional
    zero-degree records are included in every statistic. Otherwise statistics cover only the
    represented records and the universe fields are None.

    Definitions (N = statistical population, S = total degree, x sorted ascending):

    - percentile p: x at nearest rank ``max(1, ceil(p * N))``.
    - mean: ``S / N``.
    - gini_coefficient: ``(2 * sum(i * x_i) - (N + 1) * S) / (N * S)`` for i = 1..N
      (equivalent to the mean absolute difference over twice the mean); None when S = 0.
    - top_1_percent_candidate_share: sum of the ``k = ceil(N / 100)`` largest degrees divided
      by S; None when S = 0. Ties at the cut-off do not change the value.
    """
    if represented_records < 0 or total_degree < 0:
        raise AuditError("Degree summary received negative counts.")
    if universe_records is not None and universe_records < represented_records:
        raise AuditError(
            f"Record universe ({universe_records}) is smaller than the {represented_records} "
            "records represented in candidate output."
        )

    zero_records = universe_records - represented_records if universe_records is not None else 0
    population = represented_records + zero_records

    result: dict[str, Any] = {
        "statistics_basis": "record_universe" if universe_records is not None else "represented_records",
        "records_in_universe": universe_records,
        "records_with_candidates": represented_records,
        "records_without_candidates": zero_records if universe_records is not None else None,
        "total_degree": total_degree,
        "min": None,
        "p50": None,
        "p95": None,
        "p99": None,
        "p99_9": None,
        "mean": None,
        "max": None,
        "gini_coefficient": None,
        "top_1_percent_candidate_share": None,
    }

    # Ranks to resolve during the pass.
    percentile_ranks = (
        {name: nearest_rank(num, den, population) for name, num, den in DEGREE_PERCENTILES}
        if population
        else {}
    )
    percentile_values: dict[str, int] = {}
    for name, rank in percentile_ranks.items():
        if rank <= zero_records:
            percentile_values[name] = 0
    top_k = -(-population // 100)  # ceil(N / 100)
    bottom_count = population - top_k  # ranks summed to obtain the top-k share by subtraction

    cumulative = zero_records  # records at ranks before the current group
    weighted_rank_sum = 0  # exact sum(i * x_i) over ascending ranks i = 1..N
    bottom_sum = 0  # sum of the smallest `bottom_count` degrees (zeros contribute nothing)
    seen_records = 0
    seen_degree = 0
    first_degree: int | None = None
    last_degree: int | None = None
    previous_degree = 0

    for degree, count in histogram:
        degree = int(degree)
        count = int(count)
        if degree <= previous_degree or count <= 0:
            raise AuditError("Degree histogram must have strictly ascending positive degrees and positive counts.")
        previous_degree = degree
        if first_degree is None:
            first_degree = degree
        last_degree = degree

        group_start = cumulative + 1
        group_end = cumulative + count
        # ranks group_start..group_end all have value `degree`
        weighted_rank_sum += degree * (count * cumulative + count * (count + 1) // 2)
        for name, rank in percentile_ranks.items():
            if name not in percentile_values and group_start <= rank <= group_end:
                percentile_values[name] = degree
        if cumulative < bottom_count:
            bottom_sum += degree * (min(group_end, bottom_count) - cumulative)

        cumulative = group_end
        seen_records += count
        seen_degree += degree * count

    if seen_records != represented_records or seen_degree != total_degree:
        raise AuditError(
            "Internal consistency check failed while summarizing degrees: "
            f"histogram covers {seen_records} records / {seen_degree} degree, expected "
            f"{represented_records} / {total_degree}."
        )

    if population == 0:
        return result

    result["min"] = 0 if zero_records else (first_degree if first_degree is not None else 0)
    result["max"] = last_degree if last_degree is not None else 0
    for name, _, _ in DEGREE_PERCENTILES:
        result[name] = percentile_values[name]
    result["mean"] = _round(total_degree / population)
    if total_degree > 0:
        gini = Fraction(2 * weighted_rank_sum - (population + 1) * total_degree, population * total_degree)
        result["gini_coefficient"] = _round(float(gini))
        result["top_1_percent_candidate_share"] = _round((total_degree - bottom_sum) / total_degree)
    return result


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


#: Version tag recorded as ``candidate_set.fingerprint_version`` in audit reports.
CANDIDATE_SET_FINGERPRINT_VERSION = "v1"


class CandidateSetFingerprintV1:
    """Incremental SHA-256 fingerprint of a canonical, sorted, unique candidate-pair set.

    Domain-separated by the prefix ``candguard:candidate-set:v1:<linkage_type>`` plus a newline,
    so a candidate-set digest can never collide with a ground-truth digest or with a set of the
    other linkage type. Each pair is encoded unambiguously as ``<len>:<left>``, a tab,
    ``<len>:<right>`` and a newline, where ``<len>`` is the decimal UTF-8 byte length, so
    identifiers containing tabs, newlines or digits cannot be confused with the separators.
    The digest depends on pair order; callers feed pairs in canonical ascending
    ``(left_id, right_id)`` order. ``count`` tracks the number of pairs hashed.
    """

    def __init__(self, linkage_type: str) -> None:
        if linkage_type not in {"cross", "dedupe"}:
            raise AuditError(f"linkage_type must be 'cross' or 'dedupe', got {linkage_type!r}.")
        self._hasher = hashlib.sha256()
        self._hasher.update(f"candguard:candidate-set:v1:{linkage_type}\n".encode("utf-8"))
        self.count = 0

    def update(self, left: str, right: str) -> None:
        l_bytes = left.encode("utf-8")
        r_bytes = right.encode("utf-8")
        self._hasher.update(
            b"%d:%b\t%d:%b\n" % (len(l_bytes), l_bytes, len(r_bytes), r_bytes)
        )
        self.count += 1

    def hexdigest(self) -> str:
        return f"sha256:{self._hasher.hexdigest()}"


def compute_candidate_set_fingerprint_v1(
    pairs: Iterable[tuple[str, str]],
    linkage_type: str,
) -> str:
    """Return the v1 candidate-set fingerprint of ``pairs`` (canonical sorted unique pairs).

    This is an integrity check for candidate-set artifacts, not a substitute for comparing the
    sets themselves. ``compute_truth_fingerprint_v2`` is unrelated and unchanged.
    """
    hasher = CandidateSetFingerprintV1(linkage_type)
    for left, right in pairs:
        hasher.update(left, right)
    return hasher.hexdigest()


def compute_candidate_set_drift(*, retained: int, added: int, removed: int) -> dict[str, int | float | None]:
    """Return candidate-set drift metrics from exact retained/added/removed unique-pair counts.

    With B the baseline set, C the current set, R = |B ∩ C|, A = |C − B|, D = |B − C| and
    U = |B ∪ C| = R + A + D:

    - ``jaccard_similarity`` = R / U, defined as 1.0 when both sets are empty;
    - ``pair_churn`` = (A + D) / U, defined as 0.0 when both sets are empty;
    - ``removed_fraction_of_baseline`` = D / |B|, None when the baseline set is empty;
    - ``added_fraction_of_current`` = A / |C|, None when the current set is empty.

    Fractions are rounded independently to METRIC_PRECISION decimal places.
    """
    for name, value in (("retained", retained), ("added", added), ("removed", removed)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AuditError(f"{name} pair count must be a non-negative integer, got {value!r}.")
    baseline = retained + removed
    current = retained + added
    union = retained + added + removed
    return {
        "baseline_unique_pairs": baseline,
        "current_unique_pairs": current,
        "retained_pairs": retained,
        "added_pairs": added,
        "removed_pairs": removed,
        "union_pairs": union,
        "jaccard_similarity": _round(retained / union) if union else 1.0,
        "pair_churn": _round((added + removed) / union) if union else 0.0,
        "removed_fraction_of_baseline": compute_ratio(removed, baseline),
        "added_fraction_of_current": compute_ratio(added, current),
    }

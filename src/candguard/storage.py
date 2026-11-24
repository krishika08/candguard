"""Storage and execution engine for CandGuard candidate auditing.

Streams candidate and ground-truth pairs into an isolated, disk-backed SQLite
database to evaluate coverage, distribution percentiles, reduction ratio,
and recall without exceeding RAM limits.
"""
from __future__ import annotations

import csv
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterator, Protocol

from .metrics import compute_mean_degree_ratio, compute_truth_fingerprint_v2, summarize_degree_histogram
from .models import AuditError
from .readers import CandidateReader


def compute_nearest_rank_percentile(
    conn: sqlite3.Connection,
    total_sources: int,
    percentile: float,
) -> int:
    """Return a nearest-rank percentile of per-left candidate counts from source_candidate_counts.

    Directly queries the caller's connection without creating, mutating, or closing databases.
    """
    if total_sources <= 0:
        return 0
    rank = max(0, math.ceil(percentile * total_sources) - 1)
    row = conn.execute(
        "SELECT candidate_count FROM source_candidate_counts "
        "ORDER BY candidate_count, left_id LIMIT 1 OFFSET ?",
        (rank,),
    ).fetchone()
    return int(row[0]) if row else 0


class StorageEngine(Protocol):
    """Storage engine protocol for candidate-pair auditing."""

    def ingest_candidates(
        self,
        reader: CandidateReader,
        linkage_type: str = "cross",
        batch_size: int = 50_000,
    ) -> tuple[int, int]:
        ...

    def ingest_truth(
        self,
        reader: CandidateReader,
        linkage_type: str = "cross",
        batch_size: int = 50_000,
    ) -> tuple[int, int]:
        ...

    def close(self) -> None:
        ...


class SQLiteStorageEngine:
    """Disk-backed SQLite processing engine."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            tmp = tempfile.NamedTemporaryFile(prefix="candguard-", suffix=".sqlite3", delete=False)
            self._db_path = Path(tmp.name)
            tmp.close()
            self._owns_db = True
        else:
            self._db_path = Path(db_path)
            self._owns_db = False

        self._conn: sqlite3.Connection | None = None
        self._init_connection()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise AuditError("Database connection is closed.")
        return self._conn

    def _init_connection(self) -> None:
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("PRAGMA journal_mode = OFF")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("PRAGMA temp_store = FILE")
            conn.execute("PRAGMA cache_size = -65536")  # ~64 MiB cache
            conn.execute("PRAGMA mmap_size = 268435456")  # 256 MiB mmap
            self._conn = conn
        except sqlite3.Error as exc:
            self.close()
            raise AuditError(f"SQLite failed while initializing database: {exc}") from exc

    def __enter__(self) -> SQLiteStorageEngine:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close SQLite connection and remove temporary files."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._owns_db and self._db_path.exists():
            try:
                os.unlink(self._db_path)
            except OSError:
                pass

    def ingest_candidates(
        self,
        reader: CandidateReader,
        linkage_type: str = "cross",
        batch_size: int = 50_000,
    ) -> tuple[int, int]:
        """Ingest candidate pairs. Return (total_rows, unique_pairs)."""
        conn = self.connection
        try:
            conn.execute("CREATE TABLE candidate_raw (left_id TEXT NOT NULL, right_id TEXT NOT NULL)")
            insert_sql = "INSERT INTO candidate_raw (left_id, right_id) VALUES (?, ?)"
            count = 0
            is_dedupe = linkage_type == "dedupe"

            for raw_batch in reader.iter_batches(batch_size=batch_size):
                processed_batch: list[tuple[str, str]] = []
                for left, right in raw_batch:
                    if is_dedupe:
                        if left == right:
                            raise AuditError(
                                f"Self-pair ({left!r}, {right!r}) found in {reader.path}; "
                                "deduplication pairs must connect distinct records."
                            )
                        if left > right:
                            left, right = right, left
                    processed_batch.append((left, right))
                count += len(processed_batch)
                conn.executemany(insert_sql, processed_batch)

            conn.commit()
            conn.execute(
                "CREATE TABLE candidate_pairs AS "
                "SELECT left_id, right_id FROM candidate_raw GROUP BY left_id, right_id"
            )
            conn.execute("DROP TABLE candidate_raw")
            conn.commit()
            conn.execute("CREATE UNIQUE INDEX idx_candidates_pair ON candidate_pairs(left_id, right_id)")
            unique_pairs = int(conn.execute("SELECT COUNT(*) FROM candidate_pairs").fetchone()[0])
            return count, unique_pairs
        except sqlite3.Error as exc:
            raise AuditError(f"SQLite failed while auditing candidate pairs: {exc}") from exc

    def ingest_truth(
        self,
        reader: CandidateReader,
        linkage_type: str = "cross",
        batch_size: int = 50_000,
    ) -> tuple[int, int]:
        """Ingest ground-truth pairs. Return (total_rows, unique_pairs)."""
        conn = self.connection
        try:
            conn.execute("CREATE TABLE truth_raw (left_id TEXT NOT NULL, right_id TEXT NOT NULL)")
            insert_sql = "INSERT INTO truth_raw (left_id, right_id) VALUES (?, ?)"
            count = 0
            is_dedupe = linkage_type == "dedupe"

            for raw_batch in reader.iter_batches(batch_size=batch_size):
                processed_batch: list[tuple[str, str]] = []
                for left, right in raw_batch:
                    if is_dedupe:
                        if left == right:
                            raise AuditError(
                                f"Self-pair ({left!r}, {right!r}) found in {reader.path}; "
                                "deduplication pairs must connect distinct records."
                            )
                        if left > right:
                            left, right = right, left
                    processed_batch.append((left, right))
                count += len(processed_batch)
                conn.executemany(insert_sql, processed_batch)

            conn.commit()
            conn.execute(
                "CREATE TABLE truth_pairs AS SELECT left_id, right_id FROM truth_raw GROUP BY left_id, right_id"
            )
            conn.execute("DROP TABLE truth_raw")
            conn.commit()
            conn.execute("CREATE UNIQUE INDEX idx_truth_pair ON truth_pairs(left_id, right_id)")
            unique_truth = int(conn.execute("SELECT COUNT(*) FROM truth_pairs").fetchone()[0])
            return count, unique_truth
        except sqlite3.Error as exc:
            raise AuditError(f"SQLite failed while auditing ground-truth pairs: {exc}") from exc

    def compute_coverage_and_distribution(
        self,
        linkage_type: str,
    ) -> tuple[dict[str, int], dict[str, Any]]:
        """Compute coverage records and per-source candidate distribution."""
        conn = self.connection
        try:
            if linkage_type == "dedupe":
                conn.execute(
                    "CREATE TABLE source_candidate_counts AS "
                    "SELECT record_id AS left_id, COUNT(*) AS candidate_count FROM ("
                    "SELECT left_id AS record_id FROM candidate_pairs "
                    "UNION ALL "
                    "SELECT right_id AS record_id FROM candidate_pairs"
                    ") GROUP BY record_id"
                )
                conn.execute("CREATE INDEX idx_source_counts ON source_candidate_counts(candidate_count, left_id)")
                count_row = conn.execute(
                    "SELECT COUNT(*), COALESCE(MIN(candidate_count), 0), "
                    "COALESCE(AVG(candidate_count), 0), COALESCE(MAX(candidate_count), 0) "
                    "FROM source_candidate_counts"
                ).fetchone()
                unique_records = int(count_row[0])
                coverage = {
                    "records_represented": unique_records,
                    "left_records_represented": unique_records,
                    "right_records_represented": unique_records,
                }
            else:
                unique_left = int(conn.execute("SELECT COUNT(DISTINCT left_id) FROM candidate_pairs").fetchone()[0])
                unique_right = int(conn.execute("SELECT COUNT(DISTINCT right_id) FROM candidate_pairs").fetchone()[0])
                coverage = {
                    "left_records_represented": unique_left,
                    "right_records_represented": unique_right,
                }
                conn.execute(
                    "CREATE TABLE source_candidate_counts AS "
                    "SELECT left_id, COUNT(*) AS candidate_count FROM candidate_pairs GROUP BY left_id"
                )
                conn.execute("CREATE INDEX idx_source_counts ON source_candidate_counts(candidate_count, left_id)")
                count_row = conn.execute(
                    "SELECT COUNT(*), COALESCE(MIN(candidate_count), 0), "
                    "COALESCE(AVG(candidate_count), 0), COALESCE(MAX(candidate_count), 0) "
                    "FROM source_candidate_counts"
                ).fetchone()

            sources_with_candidates = int(count_row[0])
            distribution = {
                "sources_with_candidates": sources_with_candidates,
                "min": int(count_row[1]),
                "mean": round(float(count_row[2]), 4),
                "p50": self._percentile(sources_with_candidates, 0.50),
                "p95": self._percentile(sources_with_candidates, 0.95),
                "p99": self._percentile(sources_with_candidates, 0.99),
                "max": int(count_row[3]),
            }
            return coverage, distribution
        except sqlite3.Error as exc:
            raise AuditError(f"SQLite failed while computing coverage metrics: {exc}") from exc

    def _summarize_degree_table(self, table: str, universe_records: int | None) -> dict[str, Any]:
        """Summarize a ``(<id>, candidate_count)`` degree table via an ordered SQL histogram.

        Only aggregate rows ``(degree, record_count)`` are streamed to Python, one distinct
        degree at a time; record IDs and per-record degrees are never fetched.
        """
        conn = self.connection
        represented, total_degree = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(candidate_count), 0) FROM {table}"
        ).fetchone()
        histogram = conn.execute(
            f"SELECT candidate_count, COUNT(*) FROM {table} GROUP BY candidate_count ORDER BY candidate_count"
        )
        try:
            return summarize_degree_histogram(
                histogram,
                represented_records=int(represented),
                total_degree=int(total_degree),
                universe_records=universe_records,
            )
        finally:
            # Finalize the statement even if summarizing fails part-way. An unfinalized statement
            # kept alive by a traceback defers the real connection close, which on Windows keeps
            # the temporary database file open and prevents its removal.
            histogram.close()

    def compute_degree_analysis(
        self,
        linkage_type: str,
        left_records: int | None = None,
        right_records: int | None = None,
    ) -> dict[str, Any]:
        """Compute degree distributions, skew, and concentration over unique candidate pairs.

        Must be called after ``compute_coverage_and_distribution()``, which builds the
        per-record degree table reused here. Degrees count unique pairs only, so duplicate
        raw candidate rows never add degree.

        - cross: independent distributions for left/source and right/target records, plus
          ``right_to_left_mean_degree_ratio``. ``left_records``/``right_records`` add the
          corresponding zero-degree records to each distribution.
        - dedupe: one undirected distribution over all records (both endpoints of every
          unique pair). ``left_records`` is the total record count.
        """
        conn = self.connection
        try:
            if linkage_type == "dedupe":
                return {
                    "linkage_type": "dedupe",
                    "records": self._summarize_degree_table("source_candidate_counts", left_records),
                }

            conn.execute(
                "CREATE TABLE IF NOT EXISTS target_candidate_counts AS "
                "SELECT right_id, COUNT(*) AS candidate_count FROM candidate_pairs GROUP BY right_id"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_target_counts ON target_candidate_counts(candidate_count, right_id)"
            )
            left = self._summarize_degree_table("source_candidate_counts", left_records)
            right = self._summarize_degree_table("target_candidate_counts", right_records)
            return {
                "linkage_type": "cross",
                "left": left,
                "right": right,
                "right_to_left_mean_degree_ratio": compute_mean_degree_ratio(left, right),
            }
        except sqlite3.Error as exc:
            raise AuditError(f"SQLite failed while computing degree analysis: {exc}") from exc

    def _percentile(self, total_sources: int, percentile: float) -> int:
        """Return a nearest-rank percentile of per-left candidate counts."""
        return compute_nearest_rank_percentile(self.connection, total_sources, percentile)

    def evaluate_ground_truth(
        self,
        truth_unique: int,
        linkage_type: str,
    ) -> tuple[int, int, float | None, str]:
        """Compute recovered pairs, missed pairs, recall, and v2 fingerprint."""
        conn = self.connection
        try:
            recovered = int(conn.execute(
                "SELECT COUNT(*) FROM truth_pairs t JOIN candidate_pairs c "
                "ON c.left_id = t.left_id AND c.right_id = t.right_id"
            ).fetchone()[0])
            missed = truth_unique - recovered
            recall = recovered / truth_unique if truth_unique else None

            cursor = conn.execute("SELECT left_id, right_id FROM truth_pairs ORDER BY left_id, right_id")
            fingerprint = compute_truth_fingerprint_v2(cursor, linkage_type)
            return recovered, missed, (round(recall, 8) if recall is not None else None), fingerprint
        except sqlite3.Error as exc:
            raise AuditError(f"SQLite failed while evaluating ground truth: {exc}") from exc

    def export_missed_pairs(
        self,
        out_path: Path,
        truth_left_header: str,
        truth_right_header: str,
    ) -> None:
        """Export missed ground-truth pairs to TSV."""
        conn = self.connection
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8", newline="") as out_stream:
                writer = csv.writer(out_stream, delimiter="\t", lineterminator="\n")
                writer.writerow([truth_left_header, truth_right_header])
                cursor = conn.execute(
                    "SELECT t.left_id, t.right_id FROM truth_pairs t "
                    "LEFT JOIN candidate_pairs c ON c.left_id=t.left_id AND c.right_id=t.right_id "
                    "WHERE c.left_id IS NULL ORDER BY t.left_id, t.right_id"
                )
                writer.writerows(cursor)
        except OSError as exc:
            raise AuditError(f"Could not write missed pairs to {out_path}: {exc}") from exc
        except sqlite3.Error as exc:
            raise AuditError(f"SQLite failed while exporting missed pairs: {exc}") from exc

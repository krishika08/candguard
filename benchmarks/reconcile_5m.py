"""Reconciliation benchmark harness for 5M-row deduplication workload.

Runs interleaved, strictly comparable measurements between the baseline
(Phase 2) implementation and the current optimized implementation using
the exact same input files, RNG seed, and environment conditions.

Each measurement runs in an isolated subprocess to ensure true per-process
peak working set measurement without parent-process cumulative high-water contamination.
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import math
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Ensure src/ and workspace root are in sys.path
root_dir = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(Path(root_dir) / "src"))
sys.path.insert(0, root_dir)

from benchmarks.generator import generate_dataset
from benchmarks.system_info import get_peak_memory_mb, get_system_info
from candguard.audit import audit_candidates, _rows, _delimiter, _percentile, AuditError


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Baseline (Phase 2 commit 7880fc8) implementation of dedupe audit
# ---------------------------------------------------------------------------

def _baseline_rows(path: Path, left_col: str, right_col: str, delimiter: str | None) -> Any:
    sep = _delimiter(path, delimiter)
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=sep)
        for row in reader:
            left = row.get(left_col)
            right = row.get(right_col)
            if left is None or right is None or not left.strip() or not right.strip():
                raise AuditError("Missing identifier")
            yield left.strip(), right.strip()


def _baseline_load_pairs(conn: sqlite3.Connection, path: Path, table: str, left_col: str, right_col: str) -> int:
    conn.execute(f"CREATE TABLE {table} (left_id TEXT NOT NULL, right_id TEXT NOT NULL)")
    insert_sql = f"INSERT INTO {table} (left_id, right_id) VALUES (?, ?)"
    batch: list[tuple[str, str]] = []
    count = 0
    for left, right in _baseline_rows(path, left_col, right_col, None):
        if left == right:
            raise AuditError("Self-pair in dedupe")
        if left > right:
            left, right = right, left
        batch.append((left, right))
        count += 1
        if len(batch) >= 20_000:
            conn.executemany(insert_sql, batch)
            batch.clear()
    if batch:
        conn.executemany(insert_sql, batch)
    return count


def baseline_dedupe_audit(cand_path: Path, truth_path: Path) -> dict[str, Any]:
    tmp = tempfile.NamedTemporaryFile(prefix="baseline-5m-", suffix=".sqlite3", delete=False)
    db_path = tmp.name
    tmp.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = FILE")
        conn.execute("PRAGMA cache_size = -65536")

        candidate_rows = _baseline_load_pairs(conn, cand_path, "candidate_raw", "source_id", "candidate_id")
        conn.commit()

        conn.execute(
            "CREATE TABLE candidate_pairs AS "
            "SELECT left_id, right_id FROM candidate_raw GROUP BY left_id, right_id"
        )
        conn.execute("CREATE UNIQUE INDEX idx_candidates_pair ON candidate_pairs(left_id, right_id)")
        conn.execute("CREATE INDEX idx_candidates_left ON candidate_pairs(left_id)")
        conn.execute("CREATE INDEX idx_candidates_right ON candidate_pairs(right_id)")
        unique_candidate_pairs = int(conn.execute("SELECT COUNT(*) FROM candidate_pairs").fetchone()[0])
        duplicate_rows = candidate_rows - unique_candidate_pairs

        unique_records = int(conn.execute(
            "SELECT COUNT(DISTINCT record_id) FROM ("
            "SELECT left_id AS record_id FROM candidate_pairs "
            "UNION "
            "SELECT right_id AS record_id FROM candidate_pairs"
            ")"
        ).fetchone()[0])

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
        sources_with_candidates = int(count_row[0])
        distribution = {
            "sources_with_candidates": sources_with_candidates,
            "min": int(count_row[1]),
            "mean": round(float(count_row[2]), 4),
            "p50": _percentile(conn, sources_with_candidates, 0.50),
            "p95": _percentile(conn, sources_with_candidates, 0.95),
            "p99": _percentile(conn, sources_with_candidates, 0.99),
            "max": int(count_row[3]),
        }

        truth_rows = _baseline_load_pairs(conn, truth_path, "truth_raw", "source_id", "candidate_id")
        conn.commit()
        conn.execute(
            "CREATE TABLE truth_pairs AS SELECT left_id, right_id FROM truth_raw GROUP BY left_id, right_id"
        )
        conn.execute("CREATE UNIQUE INDEX idx_truth_pair ON truth_pairs(left_id, right_id)")
        truth_unique = int(conn.execute("SELECT COUNT(*) FROM truth_pairs").fetchone()[0])

        recovered = int(conn.execute(
            "SELECT COUNT(*) FROM truth_pairs t JOIN candidate_pairs c "
            "ON c.left_id = t.left_id AND c.right_id = t.right_id"
        ).fetchone()[0])
        missed = truth_unique - recovered
        recall = recovered / truth_unique if truth_unique else None

        hasher = hashlib.sha256()
        hasher.update("candguard:truth:v1:dedupe\n".encode("utf-8"))
        for left, right in conn.execute(
            "SELECT left_id, right_id FROM truth_pairs ORDER BY left_id, right_id"
        ):
            hasher.update(f"{left}\t{right}\n".encode("utf-8"))
        fp = f"sha256:{hasher.hexdigest()}"

        return {
            "candidate_rows": candidate_rows,
            "unique_candidate_pairs": unique_candidate_pairs,
            "duplicate_candidate_rows": duplicate_rows,
            "records_represented": unique_records,
            "distribution": distribution,
            "truth_rows": truth_rows,
            "truth_unique_pairs": truth_unique,
            "recovered_pairs": recovered,
            "missed_pairs": missed,
            "candidate_recall": round(recall, 8) if recall is not None else None,
            "fingerprint": fp,
            "fingerprint_version": "v1",
        }
    finally:
        conn.close()
        if os.path.exists(db_path):
            os.unlink(db_path)


def run_optimized_audit(cand_path: Path, truth_path: Path) -> dict[str, Any]:
    rep = audit_candidates(
        candidates_path=cand_path,
        left_id="source_id",
        right_id="candidate_id",
        truth_path=truth_path,
        linkage_type="dedupe",
    )
    return {
        "candidate_rows": rep["inputs"]["candidate_rows"],
        "unique_candidate_pairs": rep["inputs"]["unique_candidate_pairs"],
        "duplicate_candidate_rows": rep["inputs"]["duplicate_candidate_rows"],
        "records_represented": rep["coverage"]["records_represented"],
        "distribution": rep["candidate_distribution_per_left_record"],
        "truth_rows": rep["ground_truth"]["rows"],
        "truth_unique_pairs": rep["ground_truth"]["unique_pairs"],
        "recovered_pairs": rep["ground_truth"]["recovered_pairs"],
        "missed_pairs": rep["ground_truth"]["missed_pairs"],
        "candidate_recall": rep["ground_truth"]["candidate_recall"],
        "fingerprint": rep["ground_truth"]["fingerprint"],
        "fingerprint_version": rep["ground_truth"].get("fingerprint_version", "v2"),
    }


def worker_main(impl: str, cand_path_str: str, truth_path_str: str) -> None:
    """Worker process: runs a single audit and prints JSON metrics to stdout."""
    cand = Path(cand_path_str)
    truth = Path(truth_path_str)

    t0 = time.perf_counter()
    if impl == "baseline":
        res = baseline_dedupe_audit(cand, truth)
    else:
        res = run_optimized_audit(cand, truth)
    elapsed = time.perf_counter() - t0
    peak_mem = get_peak_memory_mb()

    output_payload = {
        "elapsed_sec": elapsed,
        "peak_mem_mb": peak_mem,
        "result": res,
    }
    print(json.dumps(output_payload))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-baseline", nargs=2, metavar=("CAND", "TRUTH"))
    parser.add_argument("--worker-optimized", nargs=2, metavar=("CAND", "TRUTH"))
    args = parser.parse_args()

    if args.worker_baseline:
        worker_main("baseline", args.worker_baseline[0], args.worker_baseline[1])
        return 0
    if args.worker_optimized:
        worker_main("optimized", args.worker_optimized[0], args.worker_optimized[1])
        return 0

    data_dir = Path("benchmarks_reconcile_data")
    data_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("CandGuard Phase 3.2: 5M Deduplication Reconciliation Benchmark")
    print("=" * 70)

    sys_info = get_system_info()
    print(f"OS:             {sys_info['os']}")
    print(f"Processor:      {sys_info['processor']}")
    print(f"CPU count:      {sys_info['cpu_count']}")
    print(f"RAM:            {sys_info['total_ram_gb']} GiB (Free: {sys_info['free_ram_gb']} GiB)")
    print(f"Disk Free:      {sys_info['disk_free_gb']} GiB")
    print(f"Python:         {sys_info['python_version']}")
    print(f"SQLite:         {sys_info['sqlite_version']}")
    print("-" * 70)

    # 1. Generate single deterministic dataset
    print("Generating 5,000,000 row deduplication dataset (seed=42)...", flush=True)
    t_gen_start = time.perf_counter()
    cand_path, truth_path = generate_dataset(
        data_dir,
        num_rows=5_000_000,
        duplicate_rate=0.05,
        linkage_type="dedupe",
        truth_pairs=250_000,
        recall_target=0.85,
        seed=42,
    )
    gen_time = time.perf_counter() - t_gen_start
    cand_size_mb = round(cand_path.stat().st_size / (1024 * 1024), 2)
    truth_size_mb = round(truth_path.stat().st_size / (1024 * 1024), 2)
    cand_sha256 = sha256_file(cand_path)
    truth_sha256 = sha256_file(truth_path)

    print(f"  Candidates file: {cand_path.name} ({cand_size_mb} MB) | SHA-256: {cand_sha256}")
    print(f"  Truth file:      {truth_path.name} ({truth_size_mb} MB) | SHA-256: {truth_sha256}")
    print(f"  Generation time: {gen_time:.2f}s")
    print("-" * 70)

    # 2. Interleaved execution protocol in isolated child processes: 4 runs each (8 total runs)
    # Order: [Baseline, Optimized, Baseline, Optimized, Baseline, Optimized, Baseline, Optimized]
    schedule = [
        ("Baseline", 1),
        ("Optimized", 1),
        ("Baseline", 2),
        ("Optimized", 2),
        ("Baseline", 3),
        ("Optimized", 3),
        ("Baseline", 4),
        ("Optimized", 4),
    ]

    baseline_times: list[float] = []
    optimized_times: list[float] = []
    baseline_peaks: list[float] = []
    optimized_peaks: list[float] = []

    results_log = []
    baseline_logical = None
    optimized_logical = None

    script_path = str(Path(__file__).resolve())

    for idx, (impl, run_num) in enumerate(schedule, start=1):
        print(f"[{idx}/8] Running {impl} (iteration {run_num}) in child process...", end="", flush=True)
        time.sleep(1.0)  # Brief pause between runs to let disk I/O settle

        flag = "--worker-baseline" if impl == "Baseline" else "--worker-optimized"
        cmd = [sys.executable, script_path, flag, str(cand_path), str(truth_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=root_dir)
        data = json.loads(proc.stdout.strip())

        elapsed = data["elapsed_sec"]
        peak = data["peak_mem_mb"]
        res = data["result"]

        if impl == "Baseline":
            baseline_times.append(elapsed)
            baseline_peaks.append(peak)
            baseline_logical = res
        else:
            optimized_times.append(elapsed)
            optimized_peaks.append(peak)
            optimized_logical = res

        print(f" done in {elapsed:.3f}s (peak memory: {peak:.2f} MB)")
        results_log.append({
            "step": idx,
            "implementation": impl,
            "iteration": run_num,
            "elapsed_sec": round(elapsed, 3),
            "peak_mem_mb": round(peak, 2),
        })

    print("=" * 70)
    print("VERIFICATION OF LOGICAL RESULTS EQUIVALENCE:")
    assert baseline_logical is not None and optimized_logical is not None

    keys_to_check = [
        "candidate_rows",
        "unique_candidate_pairs",
        "duplicate_candidate_rows",
        "records_represented",
        "truth_rows",
        "truth_unique_pairs",
        "recovered_pairs",
        "missed_pairs",
        "candidate_recall",
    ]
    all_match = True
    for k in keys_to_check:
        b_val = baseline_logical[k]
        o_val = optimized_logical[k]
        matches = (b_val == o_val)
        if not matches:
            all_match = False
        print(f"  {k:28s}: Baseline={b_val} | Optimized={o_val} | MATCH={matches}")

    dist_match = (baseline_logical["distribution"] == optimized_logical["distribution"])
    print(f"  {'distribution (min..max)':28s}: Baseline={baseline_logical['distribution']} | Optimized={optimized_logical['distribution']} | MATCH={dist_match}")
    if not dist_match:
        all_match = False

    print(f"Logical equivalence verified: {all_match}")
    print("-" * 70)

    print("INTERLEAVED BENCHMARK SUMMARY (5M rows deduplication):")
    b_med = statistics.median(baseline_times)
    o_med = statistics.median(optimized_times)
    b_mean = statistics.mean(baseline_times)
    o_mean = statistics.mean(optimized_times)
    b_std = statistics.stdev(baseline_times) if len(baseline_times) > 1 else 0.0
    o_std = statistics.stdev(optimized_times) if len(optimized_times) > 1 else 0.0

    b_mem_med = statistics.median(baseline_peaks)
    o_mem_med = statistics.median(optimized_peaks)

    print(f"Baseline timings (4 runs) : {[round(t, 3) for t in baseline_times]}")
    print(f"  Median: {b_med:.3f}s | Mean: {b_mean:.3f}s | StdDev: {b_std:.3f}s | Median Peak RAM: {b_mem_med:.1f} MB")
    print(f"Optimized timings (4 runs): {[round(t, 3) for t in optimized_times]}")
    print(f"  Median: {o_med:.3f}s | Mean: {o_mean:.3f}s | StdDev: {o_std:.3f}s | Median Peak RAM: {o_mem_med:.1f} MB")

    if b_med > 0 and o_med > 0:
        speedup_ratio = b_med / o_med
        time_reduction_pct = ((b_med - o_med) / b_med) * 100
        print(f"Speedup Ratio (Baseline / Optimized): {speedup_ratio:.2f}x")
        print(f"Median Elapsed-Time Reduction:       {time_reduction_pct:.1f}%")

    # Clean up generated test dataset
    try:
        cand_path.unlink()
        truth_path.unlink()
        data_dir.rmdir()
    except Exception:
        pass

    out_file = Path("reconciliation_results.json")
    out_file.write_text(json.dumps({
        "system_info": sys_info,
        "input_dataset": {
            "num_rows": 5_000_000,
            "linkage_type": "dedupe",
            "duplicate_rate": 0.05,
            "seed": 42,
            "candidate_file_mb": cand_size_mb,
            "truth_file_mb": truth_size_mb,
            "candidate_sha256": cand_sha256,
            "truth_sha256": truth_sha256,
        },
        "runs": results_log,
        "summary": {
            "baseline": {
                "runs": [round(t, 3) for t in baseline_times],
                "median_sec": round(b_med, 3),
                "mean_sec": round(b_mean, 3),
                "stddev_sec": round(b_std, 3),
                "median_peak_mem_mb": round(b_mem_med, 2),
                "peak_mem_runs": [round(p, 2) for p in baseline_peaks],
            },
            "optimized": {
                "runs": [round(t, 3) for t in optimized_times],
                "median_sec": round(o_med, 3),
                "mean_sec": round(o_mean, 3),
                "stddev_sec": round(o_std, 3),
                "median_peak_mem_mb": round(o_mem_med, 2),
                "peak_mem_runs": [round(p, 2) for p in optimized_peaks],
            },
            "speedup_ratio": round(speedup_ratio, 2),
            "time_reduction_percent": round(time_reduction_pct, 1),
            "logical_equivalence_verified": all_match,
        }
    }, indent=2), encoding="utf-8")
    print(f"\nDetailed reconciliation results saved to {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

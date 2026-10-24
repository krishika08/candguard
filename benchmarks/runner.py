"""Benchmark harness for CandGuard performance and scale evaluation.

Measures elapsed time, rows/sec, peak memory, and temporary disk usage across
various dataset sizes and representative workflows.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Ensure src/ is accessible when runner is executed directly or via python -m
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from .generator import generate_dataset
from .system_info import get_peak_memory_mb, get_system_info
from candguard.audit import audit_candidates, report_json
from candguard.compare import compare_reports



def run_benchmark_workflow(
    work_dir: Path,
    *,
    workflow_name: str,
    num_rows: int,
    linkage_type: str = "cross",
    has_truth: bool = True,
    duplicate_rate: float = 0.05,
    seed: int = 42,
) -> dict[str, Any]:
    """Execute a single benchmark workflow run and record metrics."""
    data_dir = work_dir / f"data_{workflow_name}_{num_rows}"
    data_dir.mkdir(parents=True, exist_ok=True)

    truth_pairs = max(50, int(num_rows * 0.05)) if has_truth else None

    # Step 1: Generate dataset
    gen_start = time.perf_counter()
    cand_path, truth_path = generate_dataset(
        data_dir,
        num_rows=num_rows,
        duplicate_rate=duplicate_rate,
        linkage_type=linkage_type,
        truth_pairs=truth_pairs,
        recall_target=0.85,
        seed=seed,
    )
    gen_time = time.perf_counter() - gen_start
    file_size_mb = round(cand_path.stat().st_size / (1024 * 1024), 2)
    truth_size_mb = round(truth_path.stat().st_size / (1024 * 1024), 2) if truth_path else 0.0

    # Step 2: Measure audit execution
    temp_dir = tempfile.gettempdir()
    dbs_before = set(glob.glob(os.path.join(temp_dir, "candguard-*.sqlite3*")))

    start_mem = get_peak_memory_mb()
    t0 = time.perf_counter()

    report = audit_candidates(
        candidates_path=cand_path,
        left_id="source_id",
        right_id="candidate_id",
        truth_path=truth_path,
        linkage_type=linkage_type,
    )

    elapsed = time.perf_counter() - t0
    peak_mem = get_peak_memory_mb()
    dbs_after = set(glob.glob(os.path.join(temp_dir, "candguard-*.sqlite3*")))
    leaked_dbs = len(dbs_after - dbs_before)

    json_str = report_json(report)
    report_size_kb = round(len(json_str.encode("utf-8")) / 1024, 2)
    rows_per_sec = round(num_rows / elapsed, 1) if elapsed > 0 else 0.0

    result = {
        "workflow": workflow_name,
        "rows": num_rows,
        "linkage_type": linkage_type,
        "has_truth": has_truth,
        "candidate_file_mb": file_size_mb,
        "truth_file_mb": truth_size_mb,
        "elapsed_sec": round(elapsed, 3),
        "rows_per_sec": rows_per_sec,
        "peak_mem_mb": peak_mem,
        "report_size_kb": report_size_kb,
        "leaked_temp_dbs": leaked_dbs,
        "unique_pairs": report["inputs"]["unique_candidate_pairs"],
        "duplicate_rows": report["inputs"]["duplicate_candidate_rows"],
        "recall": report["ground_truth"]["candidate_recall"] if report.get("ground_truth") else None,
        "exit_code": 0 if report["quality_gate"]["passed"] else 1,
    }

    # Step 3: Clean up data directory
    try:
        if cand_path.exists():
            cand_path.unlink()
        if truth_path and truth_path.exists():
            truth_path.unlink()
        data_dir.rmdir()
    except Exception:
        pass

    return result


def run_compare_benchmark(work_dir: Path, num_rows: int = 100_000) -> dict[str, Any]:
    """Benchmark the candguard compare command between two audit reports."""
    data_dir = work_dir / f"compare_{num_rows}"
    data_dir.mkdir(parents=True, exist_ok=True)

    cand1, truth = generate_dataset(data_dir, num_rows=num_rows, truth_pairs=1000, seed=1)
    cand2, _ = generate_dataset(data_dir, num_rows=int(num_rows * 1.1), truth_pairs=1000, seed=2)

    rep1 = audit_candidates(candidates_path=cand1, truth_path=truth, left_id="source_id", right_id="candidate_id")
    rep2 = audit_candidates(candidates_path=cand2, truth_path=truth, left_id="source_id", right_id="candidate_id")

    base_json = data_dir / "base.json"
    curr_json = data_dir / "curr.json"
    base_json.write_text(report_json(rep1), encoding="utf-8")
    curr_json.write_text(report_json(rep2), encoding="utf-8")

    t0 = time.perf_counter()
    cmp_res = compare_reports(
        baseline_path=base_json,
        current_path=curr_json,
        max_recall_drop=0.05,
        max_pair_growth=0.20,
    )
    elapsed = time.perf_counter() - t0

    # Cleanup
    try:
        cand1.unlink()
        cand2.unlink()
        truth.unlink()
        base_json.unlink()
        curr_json.unlink()
        data_dir.rmdir()
    except Exception:
        pass

    return {
        "workflow": "compare_regression",
        "rows": num_rows,
        "elapsed_sec": round(elapsed, 4),
        "quality_gate_passed": cmp_res["quality_gate"]["passed"],
        "exit_code": 0 if cmp_res["quality_gate"]["passed"] else 1,
    }


def run_all_benchmarks(
    sizes: list[int],
    work_dir: Path | None = None,
    output_json: Path | None = None,
) -> dict[str, Any]:
    """Run full benchmark matrix across sizes and workflows."""
    cleanup_work = False
    if work_dir is None:
        temp_obj = tempfile.TemporaryDirectory(prefix="candguard-bench-")
        work_dir = Path(temp_obj.name)
        cleanup_work = True
    else:
        temp_obj = None
        work_dir.mkdir(parents=True, exist_ok=True)

    sys_info = get_system_info()
    print("=" * 60)
    print("CandGuard Benchmark Suite")
    print("=" * 60)
    print(f"Platform:       {sys_info['os']}")
    print(f"Python:         {sys_info['python_version']}")
    print(f"SQLite:         {sys_info['sqlite_version']}")
    print(f"CPU count:      {sys_info['cpu_count']}")
    print(f"RAM:            {sys_info['total_ram_gb']} GiB (Free: {sys_info['free_ram_gb']} GiB)")
    print(f"Disk Free:      {sys_info['disk_free_gb']} GiB")
    print("=" * 60)

    results: list[dict[str, Any]] = []

    for size in sizes:
        print(f"\n--- Running benchmarks for size: {size:,} rows ---")

        # 1. Cross without truth
        print("  Running: cross without truth...", end="", flush=True)
        r1 = run_benchmark_workflow(work_dir, workflow_name="cross_no_truth", num_rows=size, linkage_type="cross", has_truth=False)
        print(f" done ({r1['elapsed_sec']}s, {r1['rows_per_sec']:,} rows/s, peak mem: {r1['peak_mem_mb']} MB)")
        results.append(r1)

        # 2. Cross with truth (recall calculation & fingerprinting)
        print("  Running: cross with truth (recall + fingerprint)...", end="", flush=True)
        r2 = run_benchmark_workflow(work_dir, workflow_name="cross_with_truth", num_rows=size, linkage_type="cross", has_truth=True)
        print(f" done ({r2['elapsed_sec']}s, {r2['rows_per_sec']:,} rows/s, recall: {r2['recall']:.2%})")
        results.append(r2)

        # 3. Dedupe mode
        print("  Running: dedupe with truth...", end="", flush=True)
        r3 = run_benchmark_workflow(work_dir, workflow_name="dedupe_with_truth", num_rows=size, linkage_type="dedupe", has_truth=True)
        print(f" done ({r3['elapsed_sec']}s, {r3['rows_per_sec']:,} rows/s)")
        results.append(r3)

    print("\n--- Running compare command benchmark ---")
    cmp_bench = run_compare_benchmark(work_dir, num_rows=100_000)
    print(f"  Compare (100k rows audit reports): {cmp_bench['elapsed_sec']}s")

    benchmark_data = {
        "system_info": sys_info,
        "audit_benchmarks": results,
        "compare_benchmark": cmp_bench,
    }

    if output_json:
        output_json.write_text(json.dumps(benchmark_data, indent=2), encoding="utf-8")
        print(f"\nResults saved to {output_json}")

    if cleanup_work and temp_obj:
        temp_obj.cleanup()

    return benchmark_data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CandGuard benchmark harness")
    parser.add_argument(
        "--sizes",
        default="100000,1000000",
        help="Comma-separated row counts to benchmark (e.g. 100000,1000000,5000000)",
    )
    parser.add_argument("--output", help="Save benchmark JSON results to this file")
    args = parser.parse_args(argv)

    sizes = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]
    output_path = Path(args.output) if args.output else None
    run_all_benchmarks(sizes=sizes, output_json=output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Scale validation harness for 10M-row datasets using corrected generator.

Validates that CandGuard operates within documented scale boundaries on 10,000,000 candidate pairs
across cross-linkage without truth, cross-linkage with truth, and deduplication with truth.

Runs each audit in an isolated subprocess to measure true per-process peak working set.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
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
from candguard.audit import audit_candidates


def sha256_file(path: Path) -> str:
    """Compute SHA-256 checksum of a file in chunks."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def count_lines(path: Path) -> int:
    """Count lines in file minus header."""
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        header = f.readline()
        if not header:
            return 0
        for _ in f:
            count += 1
    return count


def validate_truth_data(truth_path: Path, linkage_type: str) -> dict[str, Any]:
    """Verify ground truth file uniqueness, formatting, and canonicity."""
    seen_pairs: set[tuple[str, str]] = set()
    total_rows = 0
    non_canonical = 0
    self_pairs = 0

    with truth_path.open("r", encoding="utf-8") as f:
        header = f.readline().strip().split("\t")
        assert header == ["source_id", "candidate_id"], f"Unexpected truth header: {header}"
        for line in f:
            parts = line.strip().split("\t")
            assert len(parts) == 2, f"Malformed row: {line}"
            left, right = parts[0].strip(), parts[1].strip()
            total_rows += 1
            if linkage_type == "dedupe":
                if left == right:
                    self_pairs += 1
                elif left > right:
                    non_canonical += 1
            seen_pairs.add((left, right))

    return {
        "total_rows": total_rows,
        "unique_pairs": len(seen_pairs),
        "duplicate_rows": total_rows - len(seen_pairs),
        "non_canonical": non_canonical,
        "self_pairs": self_pairs,
    }


def worker_audit(
    candidates_path: Path,
    truth_path: Path | None,
    linkage_type: str,
) -> None:
    """Worker process: runs audit in isolated memory space and outputs JSON."""
    start_mem = get_peak_memory_mb()
    t0 = time.perf_counter()

    report = audit_candidates(
        candidates_path=candidates_path,
        truth_path=truth_path,
        left_id="source_id",
        right_id="candidate_id",
        linkage_type=linkage_type,
    )

    elapsed = time.perf_counter() - t0
    peak_mem = get_peak_memory_mb()

    result = {
        "elapsed_sec": round(elapsed, 3),
        "start_mem_mb": round(start_mem, 2),
        "peak_mem_mb": round(peak_mem, 2),
        "report": report,
    }
    print(json.dumps(result))


def run_workflow_benchmark(
    work_dir: Path,
    *,
    workflow_name: str,
    num_rows: int = 10_000_000,
    linkage_type: str = "cross",
    has_truth: bool = True,
    truth_pairs: int | None = 500_000,
    recall_target: float = 0.85,
    duplicate_rate: float = 0.05,
    seed: int = 42,
) -> dict[str, Any]:
    """Execute complete generation, verification, and benchmark for one 10M workflow."""
    print(f"\n{'='*70}")
    print(f"Workflow: {workflow_name} ({num_rows:,} rows, linkage={linkage_type})")
    print(f"{'='*70}")

    wf_dir = work_dir / f"wf_{workflow_name}"
    wf_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate Dataset
    print(f"  [1/4] Generating synthetic dataset (seed={seed}, duplicate_rate={duplicate_rate})...", end="", flush=True)
    t_gen_start = time.perf_counter()
    cand_path, truth_path = generate_dataset(
        wf_dir,
        num_rows=num_rows,
        duplicate_rate=duplicate_rate,
        linkage_type=linkage_type,
        truth_pairs=truth_pairs if has_truth else None,
        recall_target=recall_target,
        seed=seed,
    )
    t_gen = time.perf_counter() - t_gen_start
    print(f" done in {t_gen:.2f}s")

    cand_bytes = cand_path.stat().st_size
    cand_mb = round(cand_bytes / (1024 * 1024), 2)
    print(f"        Candidate file: {cand_path.name} ({cand_mb} MB)")
    cand_sha256 = sha256_file(cand_path)
    print(f"        Candidate SHA-256: {cand_sha256}")

    truth_mb = 0.0
    truth_sha256 = None
    truth_val: dict[str, Any] | None = None
    if truth_path and truth_path.exists():
        truth_bytes = truth_path.stat().st_size
        truth_mb = round(truth_bytes / (1024 * 1024), 2)
        truth_sha256 = sha256_file(truth_path)
        print(f"        Truth file: {truth_path.name} ({truth_mb} MB)")
        print(f"        Truth SHA-256: {truth_sha256}")

    # 2. Validate Ground-Truth Correctness (if present)
    print("  [2/4] Validating input integrity...", end="", flush=True)
    cand_raw_count = count_lines(cand_path)
    assert cand_raw_count == num_rows, f"Expected {num_rows} candidate rows, found {cand_raw_count}"

    if truth_path and truth_path.exists():
        truth_val = validate_truth_data(truth_path, linkage_type)
        assert truth_val["total_rows"] == truth_pairs, f"Expected {truth_pairs} truth rows, got {truth_val['total_rows']}"
        assert truth_val["unique_pairs"] == truth_pairs, f"Duplicate truth rows detected! Unique={truth_val['unique_pairs']}, expected={truth_pairs}"
        assert truth_val["duplicate_rows"] == 0, f"Duplicate rows: {truth_val['duplicate_rows']}"
        assert truth_val["non_canonical"] == 0, f"Non-canonical dedupe pairs: {truth_val['non_canonical']}"
        assert truth_val["self_pairs"] == 0, f"Self-pairs in dedupe: {truth_val['self_pairs']}"
    print(" PASSED (100% unique truth pairs, valid headers, correct row counts)")

    # 3. Benchmark in isolated child process
    print("  [3/4] Running audit in isolated child process...", end="", flush=True)
    temp_dir = tempfile.gettempdir()
    dbs_before = set(glob.glob(os.path.join(temp_dir, "candguard-*.sqlite3*")))

    script_path = str(Path(__file__).resolve())
    cmd = [
        sys.executable,
        script_path,
        "--worker",
        "--candidates",
        str(cand_path),
        "--linkage-type",
        linkage_type,
    ]
    if truth_path and truth_path.exists():
        cmd.extend(["--truth", str(truth_path)])

    proc = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=root_dir)
    worker_data = json.loads(proc.stdout.strip())
    elapsed = worker_data["elapsed_sec"]
    peak_mem = worker_data["peak_mem_mb"]
    rep = worker_data["report"]
    print(f" done in {elapsed:.3f}s (peak memory: {peak_mem:.2f} MB)")

    # Check for leaked temporary SQLite databases
    dbs_after = set(glob.glob(os.path.join(temp_dir, "candguard-*.sqlite3*")))
    leaked_dbs = len(dbs_after - dbs_before)
    assert leaked_dbs == 0, f"Leaked {leaked_dbs} temporary SQLite database files!"

    rows_per_sec = round(num_rows / elapsed, 1) if elapsed > 0 else 0.0

    # 4. Extract and verify audit metrics
    print("  [4/4] Verifying audit report metrics:")
    cand_input_rows = rep["inputs"]["candidate_rows"]
    cand_unique_pairs = rep["inputs"]["unique_candidate_pairs"]
    cand_dup_rows = rep["inputs"]["duplicate_candidate_rows"]
    print(f"        Candidate Rows: {cand_input_rows:,} | Unique Pairs: {cand_unique_pairs:,} | Duplicate Rows: {cand_dup_rows:,}")
    assert cand_input_rows == num_rows
    assert cand_unique_pairs + cand_dup_rows == num_rows

    truth_metrics = None
    if rep.get("ground_truth"):
        gt = rep["ground_truth"]
        t_rows = gt["rows"]
        t_unique = gt["unique_pairs"]
        recovered = gt["recovered_pairs"]
        missed = gt["missed_pairs"]
        recall = gt["candidate_recall"]
        truth_metrics = {
            "rows": t_rows,
            "unique_pairs": t_unique,
            "recovered_pairs": recovered,
            "missed_pairs": missed,
            "recall": recall,
            "fingerprint": gt["fingerprint"],
            "fingerprint_version": gt["fingerprint_version"],
        }
        print(f"        Truth Rows: {t_rows:,} | Unique Pairs: {t_unique:,} | Recovered: {recovered:,} | Missed: {missed:,} | Recall: {recall:.6f}")
        assert t_rows == truth_pairs
        assert t_unique == truth_pairs
        assert recovered + missed == truth_pairs
        assert abs(recall - (recovered / t_unique)) < 1e-9

    workflow_summary = {
        "workflow": workflow_name,
        "num_rows": num_rows,
        "linkage_type": linkage_type,
        "has_truth": has_truth,
        "candidate_file": cand_path.name,
        "candidate_bytes": cand_bytes,
        "candidate_mb": cand_mb,
        "candidate_sha256": cand_sha256,
        "truth_file": truth_path.name if truth_path else None,
        "truth_bytes": truth_bytes if truth_path and truth_path.exists() else 0,
        "truth_mb": truth_mb,
        "truth_sha256": truth_sha256,
        "elapsed_sec": elapsed,
        "rows_per_sec": rows_per_sec,
        "peak_mem_mb": peak_mem,
        "leaked_temp_dbs": leaked_dbs,
        "candidate_rows": cand_input_rows,
        "unique_candidate_pairs": cand_unique_pairs,
        "duplicate_candidate_rows": cand_dup_rows,
        "truth_metrics": truth_metrics,
        "quality_gate_passed": rep["quality_gate"]["passed"],
    }

    # Clean up generated data files to keep disk footprint minimal
    try:
        if cand_path.exists():
            cand_path.unlink()
        if truth_path and truth_path.exists():
            truth_path.unlink()
        wf_dir.rmdir()
    except Exception as e:
        print(f"  Warning: cleanup encountered {e}")

    return workflow_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CandGuard 10M-Row Scale Benchmark")
    parser.add_argument("--worker", action="store_true", help="Internal worker mode")
    parser.add_argument("--candidates", type=Path, help="Path to candidates file for worker")
    parser.add_argument("--truth", type=Path, default=None, help="Path to truth file for worker")
    parser.add_argument("--linkage-type", default="cross", help="Linkage type for worker")
    parser.add_argument("--work-dir", type=Path, default=None, help="Directory for benchmark files")
    parser.add_argument("--output", type=Path, default=None, help="Path to write JSON benchmark report")

    args = parser.parse_args(argv)

    if args.worker:
        assert args.candidates is not None, "--candidates required in worker mode"
        worker_audit(args.candidates, args.truth, args.linkage_type)
        return 0

    print("=" * 70)
    print("CandGuard Phase 3.3 — Ten-Million-Row Scale Validation")
    print("=" * 70)
    sys_info = get_system_info()
    print(f"Platform:       {sys_info['os']}")
    print(f"Python:         {sys_info['python_version']}")
    print(f"SQLite:         {sys_info['sqlite_version']}")
    print(f"CPU:            {sys_info['cpu_count']} cores")
    print(f"RAM:            {sys_info['total_ram_gb']} GiB (Free: {sys_info['free_ram_gb']} GiB)")
    print(f"Disk Free:      {sys_info['disk_free_gb']} GiB")
    print("=" * 70)

    work_dir = args.work_dir
    cleanup_work = False
    if work_dir is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="candguard-10m-")
        work_dir = Path(temp_dir.name)
        cleanup_work = True
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []

    try:
        # 1. Cross without truth
        r1 = run_workflow_benchmark(
            work_dir,
            workflow_name="cross_no_truth",
            num_rows=10_000_000,
            linkage_type="cross",
            has_truth=False,
            seed=42,
        )
        results.append(r1)

        # 2. Cross with truth
        r2 = run_workflow_benchmark(
            work_dir,
            workflow_name="cross_with_truth",
            num_rows=10_000_000,
            linkage_type="cross",
            has_truth=True,
            truth_pairs=500_000,
            recall_target=0.85,
            seed=42,
        )
        results.append(r2)

        # 3. Dedupe with truth
        r3 = run_workflow_benchmark(
            work_dir,
            workflow_name="dedupe_with_truth",
            num_rows=10_000_000,
            linkage_type="dedupe",
            has_truth=True,
            truth_pairs=500_000,
            recall_target=0.85,
            seed=42,
        )
        results.append(r3)

    finally:
        if cleanup_work:
            temp_dir.cleanup()

    print("\n" + "=" * 70)
    print("SUMMARY OF 10M SCALE BENCHMARK RESULTS")
    print("=" * 70)
    print(f"{'Workflow':<20} | {'Elapsed':<9} | {'Throughput':<15} | {'Peak RAM':<10} | {'Recall':<10}")
    print("-" * 72)
    for r in results:
        rec_str = f"{r['truth_metrics']['recall']:.2%}" if r["truth_metrics"] else "N/A"
        print(f"{r['workflow']:<20} | {r['elapsed_sec']:>7.3f}s | {r['rows_per_sec']:>12,.0f} r/s | {r['peak_mem_mb']:>7.2f} MB | {rec_str:<10}")
    print("=" * 70)

    final_payload = {
        "system_info": sys_info,
        "workflows": results,
    }

    if args.output:
        args.output.write_text(json.dumps(final_payload, indent=2), encoding="utf-8")
        print(f"Results saved to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

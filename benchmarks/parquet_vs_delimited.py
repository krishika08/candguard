"""Reproducible CSV vs TSV vs Parquet input benchmark for CandGuard.

Generates one synthetic logical dataset (candidates + ground truth) with the existing
deterministic generator, writes it as TSV, CSV, and Parquet, and audits each format in an
isolated child process. Per run it records:

- input size (candidate + truth files),
- wall-clock audit time and candidate rows per second,
- peak resident memory of the child process (Windows PeakWorkingSetSize, ru_maxrss elsewhere),
- peak bytes in the child's private temporary directory (SQLite database and SQLite temp files),
- leaked temporary files after the audit,
- a SHA-256 of the logical report (file paths removed), to verify cross-format equivalence.

Requires the optional Parquet dependency: pip install -e ".[parquet]"

Usage:
    python -m benchmarks.parquet_vs_delimited --rows 1000000 --repeats 3 \
        --work-dir /path/outside/git --output results.json

Generated datasets are written to --work-dir (a temporary directory by default) and deleted
afterwards unless --keep-data is given. Do not commit generated data.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))

from benchmarks.generator import generate_dataset  # noqa: E402
from benchmarks.system_info import get_peak_memory_mb, get_system_info  # noqa: E402

FORMATS = ("tsv", "csv", "parquet")


def logical_report_digest(report: dict[str, Any]) -> str:
    data = json.loads(json.dumps(report))
    data["inputs"].pop("candidates", None)
    data["inputs"].pop("truth", None)
    if data.get("ground_truth"):
        data["ground_truth"].pop("missed_pairs_file", None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def tsv_to_csv(src: Path, dst: Path) -> Path:
    with src.open("r", encoding="utf-8", newline="") as fin, dst.open("w", encoding="utf-8", newline="") as fout:
        writer = csv.writer(fout, lineterminator="\n")
        writer.writerows(csv.reader(fin, delimiter="\t"))
    return dst


def tsv_to_parquet(src: Path, dst: Path, *, compression: str, row_group_size: int, chunk: int = 100_000) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    with src.open("r", encoding="utf-8", newline="") as fin:
        reader = csv.reader(fin, delimiter="\t")
        header = next(reader)
        schema = pa.schema([(header[0], pa.string()), (header[1], pa.string())])
        with pq.ParquetWriter(dst, schema, compression=compression) as writer:
            lefts: list[str] = []
            rights: list[str] = []
            for row in reader:
                lefts.append(row[0])
                rights.append(row[1])
                if len(lefts) >= chunk:
                    writer.write_table(pa.table([lefts, rights], schema=schema), row_group_size=row_group_size)
                    lefts, rights = [], []
            if lefts:
                writer.write_table(pa.table([lefts, rights], schema=schema), row_group_size=row_group_size)
    return dst


def _dir_bytes(directory: Path) -> int:
    total = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        try:
            # os.stat() on the path, not DirEntry.stat(): on Windows the directory entry's
            # cached size is not updated while SQLite holds the file open.
            total += os.stat(entry.path).st_size
        except OSError:
            pass
    return total


def worker(candidates: Path, truth: Path | None, linkage_type: str, temp_dir: Path) -> None:
    """Child process: audit once and print a JSON result line."""
    tempfile.tempdir = str(temp_dir)
    from candguard.audit import audit_candidates

    peak_temp = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak_temp
        while not stop.is_set():
            peak_temp = max(peak_temp, _dir_bytes(temp_dir))
            stop.wait(0.005)

    sampler = threading.Thread(target=sample, daemon=True)
    start_mem = get_peak_memory_mb()
    sampler.start()
    t0 = time.perf_counter()
    report = audit_candidates(
        candidates_path=candidates,
        truth_path=truth,
        left_id="source_id",
        right_id="candidate_id",
        linkage_type=linkage_type,
    )
    elapsed = time.perf_counter() - t0
    stop.set()
    sampler.join()
    peak_temp = max(peak_temp, _dir_bytes(temp_dir))
    print(json.dumps({
        "elapsed_sec": elapsed,
        "start_mem_mb": start_mem,
        "peak_mem_mb": get_peak_memory_mb(),
        "peak_temp_bytes": peak_temp,
        "leaked_temp_files": len(list(temp_dir.iterdir())),
        "candidate_rows": report["inputs"]["candidate_rows"],
        "unique_candidate_pairs": report["inputs"]["unique_candidate_pairs"],
        "duplicate_candidate_rows": report["inputs"]["duplicate_candidate_rows"],
        "candidate_recall": (report["ground_truth"] or {}).get("candidate_recall"),
        "truth_fingerprint": (report["ground_truth"] or {}).get("fingerprint"),
        "logical_digest": logical_report_digest(report),
    }))


def run_child(candidates: Path, truth: Path | None, linkage_type: str, temp_dir: Path) -> dict[str, Any]:
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TMP": str(temp_dir), "TEMP": str(temp_dir), "TMPDIR": str(temp_dir), "SQLITE_TMPDIR": str(temp_dir)}
    cmd = [sys.executable, "-m", "benchmarks.parquet_vs_delimited", "--worker",
           "--candidates", str(candidates), "--linkage-type", linkage_type, "--temp-dir", str(temp_dir)]
    if truth is not None:
        cmd += ["--truth", str(truth)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT_DIR, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"worker failed ({proc.returncode}): {proc.stderr.strip()}")
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    try:
        temp_dir.rmdir()  # succeeds only when empty; leaked files are left in place as evidence
    except OSError:
        pass
    return result


def run_benchmark(
    *, rows: int, repeats: int, linkage_types: list[str], work_dir: Path,
    compression: str, row_group_size: int, keep_data: bool,
) -> dict[str, Any]:
    import pyarrow

    results: dict[str, Any] = {
        "system": get_system_info(),
        "pyarrow_version": pyarrow.__version__,
        "config": {"rows": rows, "repeats": repeats, "parquet_compression": compression,
                   "parquet_row_group_size": row_group_size, "reader_batch_size": 50_000},
        "workloads": [],
    }
    for linkage_type in linkage_types:
        data_dir = work_dir / f"data_{linkage_type}_{rows}"
        tsv_cand, tsv_truth = generate_dataset(
            data_dir, num_rows=rows, linkage_type=linkage_type,
            truth_pairs=max(50, rows // 20), recall_target=0.85, duplicate_rate=0.05, seed=42,
        )
        assert tsv_truth is not None
        inputs = {
            "tsv": (tsv_cand, tsv_truth),
            "csv": (tsv_to_csv(tsv_cand, data_dir / "candidates.csv"), tsv_to_csv(tsv_truth, data_dir / "truth.csv")),
            "parquet": (
                tsv_to_parquet(tsv_cand, data_dir / "candidates.parquet", compression=compression, row_group_size=row_group_size),
                tsv_to_parquet(tsv_truth, data_dir / "truth.parquet", compression=compression, row_group_size=row_group_size),
            ),
        }
        runs: dict[str, list[dict[str, Any]]] = {fmt: [] for fmt in FORMATS}
        for rep in range(repeats):
            order = FORMATS[rep % len(FORMATS):] + FORMATS[:rep % len(FORMATS)]  # rotate to spread cache effects
            for fmt in order:
                cand, truth = inputs[fmt]
                result = run_child(cand, truth, linkage_type, work_dir / f"tmp_{linkage_type}_{fmt}_{rep}")
                runs[fmt].append(result)
                print(f"  {linkage_type:6s} {fmt:7s} run {rep + 1}: {result['elapsed_sec']:.3f}s, "
                      f"peak {result['peak_mem_mb']:.1f} MB, temp {result['peak_temp_bytes'] / 1048576:.1f} MiB", flush=True)

        digests = {r["logical_digest"] for fmt_runs in runs.values() for r in fmt_runs}
        summary = []
        for fmt in FORMATS:
            cand, truth = inputs[fmt]
            elapsed = [r["elapsed_sec"] for r in runs[fmt]]
            median_elapsed = statistics.median(elapsed)
            summary.append({
                "format": fmt,
                "candidate_file_mib": round(cand.stat().st_size / 1048576, 2),
                "truth_file_mib": round(truth.stat().st_size / 1048576, 2),
                "elapsed_sec_median": round(median_elapsed, 3),
                "elapsed_sec_min": round(min(elapsed), 3),
                "elapsed_sec_max": round(max(elapsed), 3),
                "rows_per_sec_median": round(rows / median_elapsed),
                "peak_mem_mb_median": round(statistics.median(r["peak_mem_mb"] for r in runs[fmt]), 2),
                "peak_mem_mb_max": round(max(r["peak_mem_mb"] for r in runs[fmt]), 2),
                "peak_temp_mib_max": round(max(r["peak_temp_bytes"] for r in runs[fmt]) / 1048576, 2),
                "leaked_temp_files": sum(r["leaked_temp_files"] for r in runs[fmt]),
            })
        first = runs["tsv"][0]
        results["workloads"].append({
            "linkage_type": linkage_type,
            "rows": rows,
            "logical_results_identical": len(digests) == 1,
            "logical_digest": sorted(digests),
            "candidate_rows": first["candidate_rows"],
            "unique_candidate_pairs": first["unique_candidate_pairs"],
            "duplicate_candidate_rows": first["duplicate_candidate_rows"],
            "candidate_recall": first["candidate_recall"],
            "truth_fingerprint": first["truth_fingerprint"],
            "summary": summary,
            "runs": runs,
        })
        if not keep_data:
            for path in data_dir.iterdir():
                path.unlink()
            data_dir.rmdir()
    return results


def print_table(results: dict[str, Any]) -> None:
    print("\n| Linkage | Format | Input MiB (cand + truth) | Median time (s) | Rows/s (median) | Peak RSS MB (median / max) | Peak temp MiB | Leaked |")
    print("|:--|:--|--:|--:|--:|--:|--:|--:|")
    for wl in results["workloads"]:
        for s in wl["summary"]:
            print(f"| {wl['linkage_type']} | {s['format']} | {s['candidate_file_mib']} + {s['truth_file_mib']} | "
                  f"{s['elapsed_sec_median']} ({s['elapsed_sec_min']}-{s['elapsed_sec_max']}) | {s['rows_per_sec_median']:,} | "
                  f"{s['peak_mem_mb_median']} / {s['peak_mem_mb_max']} | {s['peak_temp_mib_max']} | {s['leaked_temp_files']} |")
        print(f"\n{wl['linkage_type']}: logical results identical across formats and runs: {wl['logical_results_identical']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--candidates", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--truth", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--temp-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--linkage-type", choices=["cross", "dedupe"], default="cross")
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--linkage-types", default="cross,dedupe")
    parser.add_argument("--compression", default="snappy")
    parser.add_argument("--row-group-size", type=int, default=100_000)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args(argv)

    if args.worker:
        worker(args.candidates, args.truth, args.linkage_type, args.temp_dir)
        return 0

    tmp_obj = None
    work_dir = args.work_dir
    if work_dir is None:
        tmp_obj = tempfile.TemporaryDirectory(prefix="candguard-pqbench-")
        work_dir = Path(tmp_obj.name)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        results = run_benchmark(
            rows=args.rows, repeats=args.repeats, linkage_types=args.linkage_types.split(","),
            work_dir=work_dir, compression=args.compression, row_group_size=args.row_group_size,
            keep_data=args.keep_data,
        )
    finally:
        if tmp_obj is not None:
            tmp_obj.cleanup()
    print_table(results)
    if args.output:
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nResults written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Reproducible benchmark for CandGuard's advanced degree and coverage metrics.

Generates a synthetic cross-linkage candidate file with an exact number of unique pairs and
heavy-tailed degrees on both sides, plus 5% duplicate rows. Each run executes in an isolated
child process that ingests the file, computes the existing coverage/distribution, then times
``SQLiteStorageEngine.compute_degree_analysis()`` on its own. Per run it records:

- ingest + existing-metrics wall-clock time, and degree-analysis wall-clock time,
- peak resident memory before and after the degree analysis (process peak),
- SQLite database size before and after the analysis, and peak bytes in the private temp dir,
- whether the analysis matches an independent reference computed by the parent.

The reference degrees are derived arithmetically from the generator parameters, so the parent
never builds the edge set. Usage:

    python -m benchmarks.degree_metrics --pairs 100000,1000000 --repeats 3 --output results.json

Generated files live in a temporary directory (or --work-dir) and are deleted afterwards.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
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

from benchmarks.system_info import get_peak_memory_mb, get_system_info  # noqa: E402


def generate(path: Path, unique_pairs: int, seed: int = 42, duplicate_rate: float = 0.05) -> dict[str, Any]:
    """Write a TSV with exactly ``unique_pairs`` unique pairs; return generator parameters.

    Left record i links to targets ``(offset_i + t) mod R`` for t < d_i, so pairs are unique by
    construction. d_i is heavy-tailed; offsets are concentrated near 0 to create target hubs.
    """
    rng = random.Random(seed)
    num_right = max(1000, unique_pairs // 5)
    max_degree = min(num_right, 5000)
    degrees: list[int] = []
    offsets: list[int] = []
    remaining = unique_pairs
    while remaining > 0:
        d = min(remaining, max_degree, 1 + int(rng.paretovariate(1.2)))
        degrees.append(d)
        offsets.append(int(num_right * rng.random() ** 4))
        remaining -= d
    num_left = len(degrees)

    rows = 0
    dup_rng = random.Random(seed + 1)
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write("source_id\tcandidate_id\n")
        buffer: list[str] = []
        for i, (d, offset) in enumerate(zip(degrees, offsets)):
            for t in range(d):
                line = f"L{i:08d}\tR{(offset + t) % num_right:08d}\n"
                buffer.append(line)
                rows += 1
                if dup_rng.random() < duplicate_rate:
                    buffer.append(line)
                    rows += 1
            if len(buffer) >= 50_000:
                stream.writelines(buffer)
                buffer.clear()
        stream.writelines(buffer)
    return {"num_left": num_left, "num_right": num_right, "degrees": degrees, "offsets": offsets, "rows": rows}


def reference(params: dict[str, Any], left_universe: int) -> dict[str, dict[str, Any]]:
    """Exact reference statistics from generator parameters (independent of SQLite)."""
    num_right = params["num_right"]
    diff = [0] * (num_right + 1)
    for d, offset in zip(params["degrees"], params["offsets"]):
        end = offset + d
        if end <= num_right:
            diff[offset] += 1
            diff[end] -= 1
        else:
            diff[offset] += 1
            diff[num_right] -= 1
            diff[0] += 1
            diff[end - num_right] -= 1
    right_degrees, running = [], 0
    for r in range(num_right):
        running += diff[r]
        right_degrees.append(running)
    left_degrees = params["degrees"] + [0] * (left_universe - params["num_left"])

    def stats(values: list[int]) -> dict[str, Any]:
        xs = sorted(values)
        n, s = len(xs), sum(xs)
        cumulative, area = 0, 0.0
        for x in xs:  # Lorenz-curve area; a different computation from the rank-weighted formula
            area += (2 * cumulative + x) / (2 * s)
            cumulative += x
        return {
            "p50": xs[math.ceil(0.5 * n) - 1], "p95": xs[math.ceil(0.95 * n) - 1],
            "p99": xs[math.ceil(0.99 * n) - 1], "p99_9": xs[math.ceil(0.999 * n) - 1],
            "min": xs[0], "max": xs[-1], "mean": round(s / n, 8),
            "gini_coefficient": 1 - 2 * area / n,
            "top_1_percent_candidate_share": sum(xs[-math.ceil(n / 100):]) / s,
        }

    return {"left": stats(left_degrees), "right": stats(right_degrees)}


def _dir_bytes(directory: Path) -> int:
    total = 0
    for entry in list(os.scandir(directory)):
        try:
            total += os.stat(entry.path).st_size  # DirEntry.stat() is stale for open files on Windows
        except OSError:
            pass
    return total


def worker(candidates: Path, left_records: int, right_records: int, temp_dir: Path) -> None:
    tempfile.tempdir = str(temp_dir)
    from candguard.readers import get_reader
    from candguard.storage import SQLiteStorageEngine

    peak_temp = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak_temp
        while not stop.is_set():
            peak_temp = max(peak_temp, _dir_bytes(temp_dir))
            stop.wait(0.005)

    threading.Thread(target=sample, daemon=True).start()
    t0 = time.perf_counter()
    with SQLiteStorageEngine() as engine:
        engine.ingest_candidates(get_reader(candidates, "source_id", "candidate_id"), linkage_type="cross")
        engine.compute_coverage_and_distribution("cross")
        t1 = time.perf_counter()
        mem_before = get_peak_memory_mb()
        db_before = os.stat(engine.db_path).st_size
        analysis = engine.compute_degree_analysis("cross", left_records=left_records, right_records=right_records)
        t2 = time.perf_counter()
        mem_after = get_peak_memory_mb()
        db_after = os.stat(engine.db_path).st_size
    stop.set()
    peak_temp = max(peak_temp, db_after)  # the sampler can miss the final growth between samples
    print(json.dumps({
        "ingest_and_existing_metrics_sec": t1 - t0,
        "degree_analysis_sec": t2 - t1,
        "peak_mem_mb_before_analysis": mem_before,
        "peak_mem_mb_after_analysis": mem_after,
        "db_bytes_before_analysis": db_before,
        "db_bytes_after_analysis": db_after,
        "peak_temp_bytes": peak_temp,
        "leaked_temp_files": len(list(temp_dir.iterdir())),
        "analysis": analysis,
    }))


def run(pairs_list: list[int], repeats: int, work_dir: Path) -> dict[str, Any]:
    results: dict[str, Any] = {"system": get_system_info(), "workloads": []}
    for unique_pairs in pairs_list:
        path = work_dir / f"degree_{unique_pairs}.tsv"
        params = generate(path, unique_pairs)
        left_universe = params["num_left"] + params["num_left"] // 10
        ref = reference(params, left_universe)
        runs = []
        for rep in range(repeats):
            temp_dir = work_dir / f"tmp_{unique_pairs}_{rep}"
            temp_dir.mkdir()
            env = {**os.environ, "TMP": str(temp_dir), "TEMP": str(temp_dir), "TMPDIR": str(temp_dir)}
            proc = subprocess.run(
                [sys.executable, "-m", "benchmarks.degree_metrics", "--worker", "--candidates", str(path),
                 "--left-records", str(left_universe), "--right-records", str(params["num_right"]),
                 "--temp-dir", str(temp_dir)],
                capture_output=True, text=True, cwd=ROOT_DIR, env=env,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr)
            out = json.loads(proc.stdout.strip().splitlines()[-1])
            mismatches = [
                f"{side}.{key}" for side in ("left", "right") for key, value in ref[side].items()
                if abs(out["analysis"][side][key] - value) > 1e-8
            ]
            out["matches_reference"] = not mismatches
            out["mismatches"] = mismatches
            runs.append(out)
            try:
                temp_dir.rmdir()
            except OSError:
                pass
            print(f"  {unique_pairs:>9,} pairs run {rep + 1}: analysis {out['degree_analysis_sec']:.3f}s, "
                  f"ingest {out['ingest_and_existing_metrics_sec']:.3f}s, peak {out['peak_mem_mb_after_analysis']:.1f} MB, "
                  f"matches reference: {out['matches_reference']}", flush=True)
        a = runs[0]["analysis"]
        results["workloads"].append({
            "unique_pairs": unique_pairs,
            "candidate_rows": params["rows"],
            "candidate_file_mib": round(path.stat().st_size / 1048576, 2),
            "left_universe": left_universe,
            "right_universe": params["num_right"],
            "left_represented": a["left"]["records_with_candidates"],
            "right_represented": a["right"]["records_with_candidates"],
            "analysis_sec_median": round(statistics.median(r["degree_analysis_sec"] for r in runs), 3),
            "analysis_sec_range": [round(min(r["degree_analysis_sec"] for r in runs), 3), round(max(r["degree_analysis_sec"] for r in runs), 3)],
            "ingest_and_existing_sec_median": round(statistics.median(r["ingest_and_existing_metrics_sec"] for r in runs), 3),
            "peak_mem_mb_before_analysis_median": round(statistics.median(r["peak_mem_mb_before_analysis"] for r in runs), 2),
            "peak_mem_mb_after_analysis_median": round(statistics.median(r["peak_mem_mb_after_analysis"] for r in runs), 2),
            "db_mib_before_analysis": round(runs[0]["db_bytes_before_analysis"] / 1048576, 2),
            "db_mib_after_analysis": round(runs[0]["db_bytes_after_analysis"] / 1048576, 2),
            "peak_temp_mib_max": round(max(r["peak_temp_bytes"] for r in runs) / 1048576, 2),
            "leaked_temp_files": sum(r["leaked_temp_files"] for r in runs),
            "all_runs_match_reference": all(r["matches_reference"] for r in runs),
            "left_gini": a["left"]["gini_coefficient"], "right_gini": a["right"]["gini_coefficient"],
            "left_top1": a["left"]["top_1_percent_candidate_share"], "right_top1": a["right"]["top_1_percent_candidate_share"],
            "runs": runs,
        })
        path.unlink()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--candidates", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--left-records", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--right-records", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--temp-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--pairs", default="100000,1000000")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.worker:
        worker(args.candidates, args.left_records, args.right_records, args.temp_dir)
        return 0

    tmp = None
    work_dir = args.work_dir
    if work_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="candguard-degbench-")
        work_dir = Path(tmp.name)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        results = run([int(p) for p in args.pairs.split(",")], args.repeats, work_dir)
    finally:
        if tmp is not None:
            tmp.cleanup()
    print("\n| Unique pairs | Rows | Analysis s (median, min-max) | Ingest + existing metrics s | Peak RSS MB before / after analysis | DB MiB before / after | Peak temp MiB | Leaked | Matches reference |")
    print("|--:|--:|--:|--:|--:|--:|--:|--:|:--|")
    for w in results["workloads"]:
        print(f"| {w['unique_pairs']:,} | {w['candidate_rows']:,} | {w['analysis_sec_median']} ({w['analysis_sec_range'][0]}-{w['analysis_sec_range'][1]}) | "
              f"{w['ingest_and_existing_sec_median']} | {w['peak_mem_mb_before_analysis_median']} / {w['peak_mem_mb_after_analysis_median']} | "
              f"{w['db_mib_before_analysis']} / {w['db_mib_after_analysis']} | {w['peak_temp_mib_max']} | {w['leaked_temp_files']} | {w['all_runs_match_reference']} |")
    if args.output:
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

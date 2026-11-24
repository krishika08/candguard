"""Reproducible benchmark for candidate-set sidecar export and exact drift comparison.

For each size N, the baseline candidate file holds unique pairs with ids ``[0, N)``; each
scenario's current file holds ids from a shifted range, so retained/added/removed counts are
known arithmetically (pair id ``j`` maps to ``("L<j mod 997>", "R<j>")``, an injective mapping
whose sorted order interleaves both sets). Rows are written in a scrambled order with 5%
duplicate rows, so ingestion has to sort and deduplicate.

Scenarios (current id range):

- identical:        ``[0, N)``             100% overlap
- partial:          ``[0.1N, 1.15N)``      90% of baseline retained, current 5% larger
- low-overlap:      ``[0.9N, 1.9N)``       10% retained, equal sizes
- equal-high-churn: ``[0.5N, 1.5N)``       50% retained, equal sizes

Every audit (with ``--candidate-set-out``) and every comparison runs in an isolated child
process. Per audit it records the export time, sidecar size, peak RSS and peak temporary disk
(SQLite database in a private temp dir plus the temporary sidecar in the artifact dir). Per
comparison it records wall-clock time and peak RSS before/after the comparison. A small run is
also checked against a Python-set reference built from the generated rows. Usage:

    python -m benchmarks.candidate_drift --pairs 100000,1000000 --repeats 3 --output results.json

Generated files live in a temporary directory (or --work-dir) and are deleted afterwards.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
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

from benchmarks.system_info import get_peak_memory_mb, get_system_info  # noqa: E402

SCENARIOS: dict[str, tuple[float, float]] = {
    "identical": (0.0, 1.0),
    "partial": (0.10, 1.15),
    "low-overlap": (0.90, 1.90),
    "equal-high-churn": (0.50, 1.50),
}


def pair(j: int) -> tuple[str, str]:
    return f"L{j % 997:04d}", f"R{j:010d}"


def generate(path: Path, start: int, stop: int, duplicate_every: int = 20) -> int:
    """Write ids ``[start, stop)`` in scrambled order with ~5% duplicate rows; return row count."""
    n = stop - start
    stride = 7_919  # scrambled order: k -> (k * stride) mod n is a permutation when gcd(stride, n) == 1
    while n and math.gcd(stride, n) != 1:
        stride += 2
    rows = 0
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write("source_id\tcandidate_id\n")
        buffer: list[str] = []
        for k in range(n):
            left, right = pair(start + (k * stride) % n)
            line = f"{left}\t{right}\n"
            buffer.append(line)
            rows += 1
            if k % duplicate_every == 0:
                buffer.append(line)
                rows += 1
            if len(buffer) >= 50_000:
                stream.writelines(buffer)
                buffer.clear()
        stream.writelines(buffer)
    return rows


def _dir_bytes(directory: Path) -> int:
    total = 0
    for entry in list(os.scandir(directory)):
        try:
            total += os.stat(entry.path).st_size  # DirEntry.stat() is stale for open files on Windows
        except OSError:
            pass
    return total


def audit_worker(candidates: Path, artifact: Path, report: Path, temp_dir: Path) -> None:
    tempfile.tempdir = str(temp_dir)
    from candguard.audit import audit_candidates
    from candguard.storage import SQLiteStorageEngine

    timings: dict[str, float] = {}
    real_export = SQLiteStorageEngine.export_candidate_set

    def timed_export(self: Any, *args: Any, **kwargs: Any) -> Any:
        timings["db_bytes_at_export"] = os.stat(self.db_path).st_size
        timings["mem_before_export"] = get_peak_memory_mb()
        t0 = time.perf_counter()
        result = real_export(self, *args, **kwargs)
        timings["export_sec"] = time.perf_counter() - t0
        timings["mem_after_export"] = get_peak_memory_mb()
        return result

    SQLiteStorageEngine.export_candidate_set = timed_export  # type: ignore[method-assign]

    peak_temp = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak_temp
        while not stop.is_set():
            peak_temp = max(peak_temp, _dir_bytes(temp_dir) + _dir_bytes(artifact.parent))
            stop.wait(0.005)

    threading.Thread(target=sample, daemon=True).start()
    t0 = time.perf_counter()
    result = audit_candidates(
        candidates_path=candidates, left_id="source_id", right_id="candidate_id", candidate_set_path=artifact
    )
    total = time.perf_counter() - t0
    stop.set()
    artifact_bytes = artifact.stat().st_size
    # The sampler can miss the final moment where the DB and the full temporary sidecar coexist.
    peak_temp = max(peak_temp, int(timings["db_bytes_at_export"]) + artifact_bytes)
    report.write_text(json.dumps(result), encoding="utf-8")
    print(json.dumps({
        "audit_total_sec": total,
        "export_sec": timings["export_sec"],
        "peak_mem_mb_before_export": timings["mem_before_export"],
        "peak_mem_mb_after_export": timings["mem_after_export"],
        "db_bytes_at_export": timings["db_bytes_at_export"],
        "artifact_bytes": artifact_bytes,
        "peak_temp_bytes": peak_temp,
        "leaked_temp_files": len(list(temp_dir.iterdir())),
        "artifact_dir_files": sorted(p.name for p in artifact.parent.iterdir()),
        "candidate_set": result["candidate_set"],
    }))


def compare_worker(base_rep: Path, curr_rep: Path, base_art: Path, curr_art: Path, temp_dir: Path) -> None:
    tempfile.tempdir = str(temp_dir)
    from candguard.compare import compare_reports

    mem_before = get_peak_memory_mb()
    t0 = time.perf_counter()
    report = compare_reports(
        baseline_path=base_rep,
        current_path=curr_rep,
        baseline_candidate_set_path=base_art,
        current_candidate_set_path=curr_art,
    )
    elapsed = time.perf_counter() - t0
    print(json.dumps({
        "compare_sec": elapsed,
        "peak_mem_mb_before_compare": mem_before,
        "peak_mem_mb_after_compare": get_peak_memory_mb(),
        "temp_files_created": len(list(temp_dir.iterdir())),
        "drift": report["metrics"]["candidate_set_drift"],
    }))


def _child(args: list[str], temp_dir: Path) -> dict[str, Any]:
    env = {**os.environ, "TMP": str(temp_dir), "TEMP": str(temp_dir), "TMPDIR": str(temp_dir)}
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.candidate_drift", *args],
        capture_output=True, text=True, cwd=ROOT_DIR, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _fresh_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False)
    return path


def run_audit(work: Path, name: str, candidates: Path) -> tuple[dict[str, Any], Path, Path]:
    temp_dir = _fresh_dir(work / f"tmp_audit_{name}")
    art_dir = _fresh_dir(work / f"art_{name}")
    artifact, report = art_dir / "pairs.tsv", work / f"{name}.json"
    out = _child(["--audit-worker", "--candidates", str(candidates), "--artifact", str(artifact),
                  "--report", str(report), "--temp-dir", str(temp_dir)], temp_dir)
    temp_dir.rmdir()
    return out, artifact, report


def run_compare(work: Path, name: str, base: tuple[Path, Path], curr: tuple[Path, Path]) -> dict[str, Any]:
    temp_dir = _fresh_dir(work / f"tmp_cmp_{name}")
    out = _child(["--compare-worker", "--baseline-report", str(base[1]), "--current-report", str(curr[1]),
                  "--baseline-artifact", str(base[0]), "--current-artifact", str(curr[0]),
                  "--temp-dir", str(temp_dir)], temp_dir)
    temp_dir.rmdir()
    return out


def bounds(n: int, lo: float, hi: float) -> tuple[int, int]:
    return round(lo * n), round(hi * n)


def expected_counts(n: int, lo: float, hi: float) -> dict[str, int]:
    c_start, c_stop = bounds(n, lo, hi)
    retained = max(0, min(n, c_stop) - max(0, c_start))
    return {"retained_pairs": retained, "added_pairs": (c_stop - c_start) - retained, "removed_pairs": n - retained}


def small_reference_check(work: Path) -> dict[str, Any]:
    """Run the full pipeline on 2,000 pairs and compare with Python sets built from the files."""
    n = 2_000
    work = _fresh_dir(work / "reference")
    base_file = work / "base.tsv"
    generate(base_file, 0, n)
    _, b_art, b_rep = run_audit(work, "base", base_file)

    def pairs_of(path: Path) -> set[tuple[str, str]]:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = csv.reader(stream, delimiter="\t")
            next(rows)
            return {(r[0], r[1]) for r in rows}

    base_set = pairs_of(base_file)
    results = {}
    for name, (lo, hi) in SCENARIOS.items():
        curr_file = work / f"{name}.tsv"
        generate(curr_file, *bounds(n, lo, hi))
        _, c_art, c_rep = run_audit(work, name, curr_file)
        drift = run_compare(work, name, (b_art, b_rep), (c_art, c_rep))["drift"]
        curr_set = pairs_of(curr_file)
        ref = {
            "retained_pairs": len(base_set & curr_set),
            "added_pairs": len(curr_set - base_set),
            "removed_pairs": len(base_set - curr_set),
        }
        results[name] = {"reference": ref, "matches": all(drift[k] == v for k, v in ref.items())}
    return results


def run(pairs_list: list[int], repeats: int, work_dir: Path) -> dict[str, Any]:
    results: dict[str, Any] = {"system": get_system_info(), "small_reference": {}, "workloads": []}
    print("Small Python-set reference check (2,000 pairs)...", flush=True)
    results["small_reference"] = small_reference_check(work_dir)
    print(f"  {results['small_reference']}", flush=True)

    for n in pairs_list:
        work = _fresh_dir(work_dir / f"n{n}")
        base_file = work / "base.tsv"
        base_rows = generate(base_file, 0, n)
        base_audits = []
        for rep in range(repeats):
            out, b_art, b_rep = run_audit(work, f"base{rep}", base_file)
            base_audits.append(out)
            print(f"  {n:>9,} baseline audit {rep + 1}: export {out['export_sec']:.3f}s "
                  f"(audit {out['audit_total_sec']:.3f}s), sidecar {out['artifact_bytes'] / 1048576:.2f} MiB, "
                  f"peak RSS {out['peak_mem_mb_after_export']:.1f} MB", flush=True)
        base = (b_art, b_rep)
        workload: dict[str, Any] = {
            "unique_pairs": n,
            "baseline_candidate_rows": base_rows,
            "baseline_candidate_file_mib": round(base_file.stat().st_size / 1048576, 2),
            "baseline_export_sec_median": round(statistics.median(a["export_sec"] for a in base_audits), 3),
            "baseline_export_sec_range": [round(min(a["export_sec"] for a in base_audits), 3),
                                          round(max(a["export_sec"] for a in base_audits), 3)],
            "baseline_audit_total_sec_median": round(statistics.median(a["audit_total_sec"] for a in base_audits), 3),
            "baseline_sidecar_mib": round(base_audits[0]["artifact_bytes"] / 1048576, 2),
            "baseline_peak_mem_mb_before_export_median": round(statistics.median(a["peak_mem_mb_before_export"] for a in base_audits), 2),
            "baseline_peak_mem_mb_after_export_median": round(statistics.median(a["peak_mem_mb_after_export"] for a in base_audits), 2),
            "baseline_db_mib_at_export": round(base_audits[0]["db_bytes_at_export"] / 1048576, 2),
            "baseline_peak_temp_mib_max": round(max(a["peak_temp_bytes"] for a in base_audits) / 1048576, 2),
            "leaked_temp_files": sum(a["leaked_temp_files"] for a in base_audits),
            "baseline_audits": base_audits,
            "scenarios": [],
        }
        for name, (lo, hi) in SCENARIOS.items():
            curr_file = work / f"{name}.tsv"
            generate(curr_file, *bounds(n, lo, hi))
            audit_out, c_art, c_rep = run_audit(work, name, curr_file)
            workload["leaked_temp_files"] += audit_out["leaked_temp_files"]
            compares = [run_compare(work, f"{name}{rep}", base, (c_art, c_rep)) for rep in range(repeats)]
            expected = expected_counts(n, lo, hi)
            drift = compares[0]["drift"]
            matches = all(c["drift"][k] == v for c in compares for k, v in expected.items())
            scenario = {
                "scenario": name,
                "current_unique_pairs": drift["current_unique_pairs"],
                "retained_pairs": drift["retained_pairs"],
                "added_pairs": drift["added_pairs"],
                "removed_pairs": drift["removed_pairs"],
                "jaccard_similarity": drift["jaccard_similarity"],
                "matches_expected_counts": matches,
                "current_export_sec": round(audit_out["export_sec"], 3),
                "current_sidecar_mib": round(audit_out["artifact_bytes"] / 1048576, 2),
                "compare_sec_median": round(statistics.median(c["compare_sec"] for c in compares), 3),
                "compare_sec_range": [round(min(c["compare_sec"] for c in compares), 3),
                                      round(max(c["compare_sec"] for c in compares), 3)],
                "compare_peak_mem_mb_before_median": round(statistics.median(c["peak_mem_mb_before_compare"] for c in compares), 2),
                "compare_peak_mem_mb_after_median": round(statistics.median(c["peak_mem_mb_after_compare"] for c in compares), 2),
                "compare_temp_files_created": sum(c["temp_files_created"] for c in compares),
                "compare_runs": compares,
                "current_audit": audit_out,
            }
            workload["scenarios"].append(scenario)
            print(f"  {n:>9,} {name:<17} retained {drift['retained_pairs']:,} added {drift['added_pairs']:,} "
                  f"removed {drift['removed_pairs']:,} | compare {scenario['compare_sec_median']}s, peak RSS "
                  f"{scenario['compare_peak_mem_mb_after_median']} MB | expected: {matches}", flush=True)
        results["workloads"].append(workload)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audit-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--compare-worker", action="store_true", help=argparse.SUPPRESS)
    for hidden in ("--candidates", "--artifact", "--report", "--temp-dir", "--baseline-report",
                   "--current-report", "--baseline-artifact", "--current-artifact"):
        parser.add_argument(hidden, type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--pairs", default="100000,1000000")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.audit_worker:
        audit_worker(args.candidates, args.artifact, args.report, args.temp_dir)
        return 0
    if args.compare_worker:
        compare_worker(args.baseline_report, args.current_report, args.baseline_artifact,
                       args.current_artifact, args.temp_dir)
        return 0

    tmp = None
    work_dir = args.work_dir
    if work_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="candguard-driftbench-")
        work_dir = Path(tmp.name)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        results = run([int(p) for p in args.pairs.split(",")], args.repeats, work_dir)
    finally:
        if tmp is not None:
            tmp.cleanup()

    print("\n| Unique pairs | Export s (median, min-max) | Sidecar MiB | Peak RSS MB before / after export | "
          "SQLite DB MiB at export | Peak temp MiB | Leaked |")
    print("|--:|--:|--:|--:|--:|--:|--:|")
    for w in results["workloads"]:
        print(f"| {w['unique_pairs']:,} | {w['baseline_export_sec_median']} ({w['baseline_export_sec_range'][0]}-"
              f"{w['baseline_export_sec_range'][1]}) | {w['baseline_sidecar_mib']} | "
              f"{w['baseline_peak_mem_mb_before_export_median']} / {w['baseline_peak_mem_mb_after_export_median']} | "
              f"{w['baseline_db_mib_at_export']} | {w['baseline_peak_temp_mib_max']} | {w['leaked_temp_files']} |")
    print("\n| Unique pairs | Scenario | Current pairs | Retained | Added | Removed | Jaccard | "
          "Compare s (median, min-max) | Peak RSS MB before / after compare | Temp files | Matches expected |")
    print("|--:|:--|--:|--:|--:|--:|--:|--:|--:|--:|:--|")
    for w in results["workloads"]:
        for s in w["scenarios"]:
            print(f"| {w['unique_pairs']:,} | {s['scenario']} | {s['current_unique_pairs']:,} | {s['retained_pairs']:,} | "
                  f"{s['added_pairs']:,} | {s['removed_pairs']:,} | {s['jaccard_similarity']} | "
                  f"{s['compare_sec_median']} ({s['compare_sec_range'][0]}-{s['compare_sec_range'][1]}) | "
                  f"{s['compare_peak_mem_mb_before_median']} / {s['compare_peak_mem_mb_after_median']} | "
                  f"{s['compare_temp_files_created']} | {s['matches_expected_counts']} |")
    print(f"\nSmall Python-set reference: {results['small_reference']}")
    if args.output:
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

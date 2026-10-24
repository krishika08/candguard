"""Deterministic incremental synthetic data generator for CandGuard benchmarks.

Streams candidate pairs and ground truth directly to disk in buffered batches
without constructing large datasets in Python memory.
"""
from __future__ import annotations

import math
import random
from pathlib import Path


def generate_dataset(
    output_dir: Path,
    *,
    num_rows: int,
    num_left: int | None = None,
    num_right: int | None = None,
    duplicate_rate: float = 0.05,
    linkage_type: str = "cross",
    truth_pairs: int | None = None,
    recall_target: float = 0.85,
    seed: int = 42,
    batch_size: int = 20_000,
) -> tuple[Path, Path | None]:
    """Generate synthetic candidate and ground-truth TSV files incrementally.

    Returns (candidates_path, truth_path).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = output_dir / f"candidates_{num_rows}.tsv"
    truth_path = output_dir / f"truth_{num_rows}.tsv" if truth_pairs else None

    rng = random.Random(seed)

    if num_left is None:
        num_left = max(100, int(math.sqrt(num_rows) * 3))
    if linkage_type == "dedupe":
        num_right = num_left
    elif num_right is None:
        num_right = num_left

    # 1. Generate ground truth if requested
    truth_to_recover: list[tuple[str, str]] = []
    if truth_path is not None and truth_pairs and truth_pairs > 0:
        recovered_count = int(truth_pairs * recall_target)
        with truth_path.open("w", encoding="utf-8", newline="") as t_stream:
            t_stream.write("source_id\tcandidate_id\n")
            t_batch: list[str] = []
            for i in range(truth_pairs):
                if linkage_type == "dedupe":
                    stride = 1 + ((i // num_left) % max(1, (num_left - 1) // 2))
                    u = i % num_left
                    v = (u + stride) % num_left
                    if u > v:
                        u, v = v, u
                    left_str = f"ENT_{u:08d}"
                    right_str = f"ENT_{v:08d}"
                else:
                    u = i % num_left
                    v = (i // num_left) % (num_right or 1)
                    left_str = f"SRC_{u:08d}"
                    right_str = f"TGT_{v:08d}"

                t_batch.append(f"{left_str}\t{right_str}\n")
                if i < recovered_count:
                    truth_to_recover.append((left_str, right_str))

                if len(t_batch) >= batch_size:
                    t_stream.writelines(t_batch)
                    t_batch.clear()
            if t_batch:
                t_stream.writelines(t_batch)

    # 2. Generate candidate pairs streaming directly to disk
    with candidates_path.open("w", encoding="utf-8", newline="") as c_stream:
        c_stream.write("source_id\tcandidate_id\n")
        c_batch: list[str] = []
        recent_pairs: list[tuple[str, str]] = []
        max_recent = 500

        rows_written = 0

        # First emit the truth pairs that should be recovered
        for pair in truth_to_recover:
            if rows_written >= num_rows:
                break
            c_batch.append(f"{pair[0]}\t{pair[1]}\n")
            recent_pairs.append(pair)
            if len(recent_pairs) > max_recent:
                recent_pairs.pop(0)
            rows_written += 1
            if len(c_batch) >= batch_size:
                c_stream.writelines(c_batch)
                c_batch.clear()

        # Emit remaining rows up to num_rows
        while rows_written < num_rows:
            if recent_pairs and rng.random() < duplicate_rate:
                pair = rng.choice(recent_pairs)
                c_batch.append(f"{pair[0]}\t{pair[1]}\n")
            else:
                if linkage_type == "dedupe":
                    u = rng.randint(0, num_left - 1)
                    v = rng.randint(0, num_left - 1)
                    if u == v:
                        v = (u + 1) % num_left
                    left_str = f"ENT_{u:08d}"
                    right_str = f"ENT_{v:08d}"
                else:
                    u = rng.randint(0, num_left - 1)
                    v = rng.randint(0, num_right - 1)
                    left_str = f"SRC_{u:08d}"
                    right_str = f"TGT_{v:08d}"

                c_batch.append(f"{left_str}\t{right_str}\n")
                recent_pairs.append((left_str, right_str))
                if len(recent_pairs) > max_recent:
                    recent_pairs.pop(0)

            rows_written += 1
            if len(c_batch) >= batch_size:
                c_stream.writelines(c_batch)
                c_batch.clear()

        if c_batch:
            c_stream.writelines(c_batch)

    return candidates_path, truth_path

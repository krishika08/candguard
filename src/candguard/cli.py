"""Command-line interface for CandGuard."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .audit import AuditError, audit_candidates, render_text, report_json
from .candidate_set import same_path
from .compare import compare_reports, render_compare_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="candguard",
        description="Audit entity-resolution candidate pairs and enforce candidate-quality thresholds.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"candguard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # Subcommand: audit
    audit = sub.add_parser(
        "audit",
        help="Audit a candidate-pair CSV/TSV file",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    audit.add_argument("--candidates", required=True, help="Candidate-pair CSV/TSV with a header, or .parquet (requires candguard[parquet])")
    audit.add_argument("--left-id", required=True, help="Column name for left/source record ID")
    audit.add_argument("--right-id", required=True, help="Column name for right/target record ID")
    audit.add_argument("--truth", help="Optional ground-truth matching-pair CSV/TSV, or .parquet (requires candguard[parquet])")
    audit.add_argument("--truth-left-id", help="Left ID column in truth file; defaults to --left-id")
    audit.add_argument("--truth-right-id", help="Right ID column in truth file; defaults to --right-id")
    audit.add_argument("--delimiter", help=r"Candidate delimiter (default: tab for .tsv, comma otherwise; use '\t' for tab)")
    audit.add_argument("--truth-delimiter", help=r"Truth delimiter (default based on extension; use '\t' for tab)")
    audit.add_argument("--linkage-type", choices=["cross", "dedupe"], default="cross", help="Cross-dataset linking or within-dataset deduplication")
    audit.add_argument("--left-records", type=int, help="Number of records in the left/source dataset")
    audit.add_argument("--right-records", type=int, help="Number of records in the right/target dataset (cross mode only)")
    audit.add_argument("--min-recall", type=float, help="Fail the quality gate if measured candidate recall is below this value")
    audit.add_argument("--max-pairs", type=int, help="Fail the quality gate if unique candidate-pair count exceeds this value")
    audit.add_argument("--max-duplicate-rows", type=int, help="Fail the quality gate if duplicate candidate rows exceed this value")
    audit.add_argument("--missed-pairs-out", help="Write all ground-truth pairs missing from candidates to this TSV file")
    audit.add_argument(
        "--candidate-set-out",
        help="Export the unique normalized candidate pairs to this sorted TSV sidecar (candguard-pairs-tsv-v1) "
        "for candidate-set drift comparison",
    )
    audit.add_argument("--format", choices=["text", "json"], default="text", help="Report format")
    audit.add_argument("--output", help="Write the report to this path instead of stdout")

    # Subcommand: compare
    compare = sub.add_parser(
        "compare",
        help="Compare two candidate audit reports and evaluate regression gates",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    compare.add_argument("--baseline", required=True, help="Baseline audit JSON report")
    compare.add_argument("--current", required=True, help="Current audit JSON report")
    compare.add_argument("--max-recall-drop", type=float, help="Fail gate if candidate recall drop exceeds this absolute amount")
    compare.add_argument("--max-pair-growth", type=float, help="Fail gate if raw candidate row count grows beyond this ratio")
    compare.add_argument("--max-duplicate-growth", type=float, help="Fail gate if duplicate row count grows beyond this ratio")
    compare.add_argument("--baseline-candidate-set", help="Baseline candidate-set sidecar written by audit --candidate-set-out")
    compare.add_argument("--current-candidate-set", help="Current candidate-set sidecar written by audit --candidate-set-out")
    compare.add_argument(
        "--min-candidate-jaccard",
        type=float,
        help="Fail gate if candidate-set Jaccard similarity is below this value (requires both candidate-set artifacts)",
    )
    compare.add_argument("--format", choices=["text", "json"], default="text", help="Report format")
    compare.add_argument("--output", help="Write the report to this path instead of stdout")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "audit":
            if args.candidate_set_out and args.output and same_path(args.candidate_set_out, args.output):
                raise AuditError(
                    f"--candidate-set-out ({args.candidate_set_out}) and --output ({args.output}) "
                    "must be different files."
                )
            report = audit_candidates(
                candidates_path=args.candidates,
                left_id=args.left_id,
                right_id=args.right_id,
                truth_path=args.truth,
                truth_left_id=args.truth_left_id,
                truth_right_id=args.truth_right_id,
                delimiter=args.delimiter,
                truth_delimiter=args.truth_delimiter,
                linkage_type=args.linkage_type,
                left_records=args.left_records,
                right_records=args.right_records,
                min_recall=args.min_recall,
                max_pairs=args.max_pairs,
                max_duplicate_rows=args.max_duplicate_rows,
                missed_pairs_path=args.missed_pairs_out,
                candidate_set_path=args.candidate_set_out,
            )
            rendered = report_json(report) if args.format == "json" else render_text(report)
        elif args.command == "compare":
            report = compare_reports(
                baseline_path=args.baseline,
                current_path=args.current,
                max_recall_drop=args.max_recall_drop,
                max_pair_growth=args.max_pair_growth,
                max_duplicate_growth=args.max_duplicate_growth,
                baseline_candidate_set_path=args.baseline_candidate_set,
                current_candidate_set_path=args.current_candidate_set,
                min_candidate_jaccard=args.min_candidate_jaccard,
            )
            rendered = report_json(report) if args.format == "json" else render_compare_text(report)
        else:
            parser.error(f"Unknown command: {args.command}")
    except AuditError as exc:
        print(f"candguard: error: {exc}", file=sys.stderr)
        return 2

    if args.output:
        try:
            out_file = Path(args.output)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            with out_file.open("w", encoding="utf-8") as stream:
                stream.write(rendered)
                stream.write("\n")
        except OSError as exc:
            print(f"candguard: error: Could not write output to {args.output}: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            print(rendered)
        except UnicodeEncodeError:
            sys.stdout.buffer.write(rendered.encode("utf-8", errors="replace"))
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()

    return 0 if report["quality_gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Local input inspection and real imagery collection commands."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .input import inspect_input
from .sample import sample, verify_sample
from .collection.pipeline import collect as collect_index


def main() -> int:
    parser = argparse.ArgumentParser(description="Bad Apple Earth: real-data technical validation")
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe", help="Inspect local MP4 and make representative previews")
    probe.add_argument("--input", required=True, type=Path)
    probe.add_argument("--output", required=True, type=Path)
    collect = commands.add_parser("sample", help="Read at most 500 real Sentinel-2 windows (P1)")
    collect.add_argument("--config", type=Path, default=Path("config/p1.json"))
    collect.add_argument("--output", required=True, type=Path)
    collect.add_argument("--limit", type=int, default=120)
    verify = commands.add_parser("verify-sample", help="Reextract one sample per scene and scale")
    verify.add_argument("--sample", required=True, type=Path)
    bulk = commands.add_parser("collect", help="Build or resume a reusable Sentinel-2 feature index")
    bulk.add_argument("--config", type=Path, default=Path("config/collect.json"))
    bulk.add_argument("--output", required=True, type=Path)
    bulk.add_argument("--limit", type=int, default=10000)
    bulk.add_argument("--stop-after-blocks", type=int, help="Checkpoint and exit after this many blocks")
    bulk.add_argument("--redistribute", action="store_true", help="Fill unavailable AOI quotas from completed AOIs, preferring the same terrain group")
    bundle_cmd = commands.add_parser("combine", help="Combine index snapshots by reference, excluding duplicate tiles")
    bundle_cmd.add_argument("--index", type=Path, action="append", required=True)
    bundle_cmd.add_argument("--output", type=Path, required=True)
    target_cmd = commands.add_parser("targets", help="Extract fixed targets by nearest actual PTS")
    target_cmd.add_argument("--config", type=Path, default=Path("config/targets.json"))
    target_cmd.add_argument("--output", required=True, type=Path)
    match_cmd = commands.add_parser("match", help="Search exact grayscale Top-K on a fixed index snapshot")
    match_cmd.add_argument("--targets", required=True, type=Path)
    match_cmd.add_argument("--index", required=True, type=Path)
    match_cmd.add_argument("--output", required=True, type=Path)
    match_cmd.add_argument("--top-k", type=int, default=20)
    match_cmd.add_argument("--rotations", default="0,90,180,270", help="Clockwise quarter turns, comma separated; use 0 for baseline")
    match_cmd.add_argument("--photo-cache", type=Path, help="Optional existing unrotated photo directory")
    materialize_cmd = commands.add_parser("materialize", help="Cache selected real photos and Top-5 comparisons")
    materialize_cmd.add_argument("--targets", required=True, type=Path)
    materialize_cmd.add_argument("--index", required=True, type=Path)
    materialize_cmd.add_argument("--run", required=True, type=Path)
    render_cmd = commands.add_parser("render", help="Render color, grayscale and side-by-side silent MP4s")
    render_cmd.add_argument("--targets", required=True, type=Path)
    render_cmd.add_argument("--run", required=True, type=Path)
    compare_cmd = commands.add_parser("compare", help="Compare fixed-target baseline and expanded search runs")
    compare_cmd.add_argument("--baseline", required=True, type=Path)
    compare_cmd.add_argument("--run", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "probe":
            inspect_input(args.input, args.output)
            print(f"Input inspection: {args.output}")
        elif args.command == "sample":
            config = json.loads(args.config.read_text(encoding="utf-8"))
            result = sample(config, args.output, args.limit)
            print(f"{result['status']}: {result['accepted']}/{result['requested']} real samples in {args.output}")
            return 0 if result["status"] == "complete" else 1
        elif args.command == "verify-sample":
            result = verify_sample(args.sample)
            print(f"Reextraction: {len(result['checks'])} checks, passed={result['passed']}")
            return 0 if result["passed"] else 1
        elif args.command == "collect":
            config = json.loads(args.config.read_text(encoding="utf-8"))
            result = collect_index(config, args.output, args.limit, args.stop_after_blocks, args.redistribute)
            print(f"{result['status']}: {result['rows']}/{result['requested']} candidates in {args.output}")
            return 0 if result["status"] in {"complete", "paused"} else 1
        elif args.command == "combine":
            from .collection.bundle import combine
            result = combine(args.index, args.output)
            print(f"{result['rows']} unique places / {len(result['members'])} members: {args.output}")
        elif args.command == "targets":
            from .matching.targets import targets
            result = targets(json.loads(args.config.read_text(encoding="utf-8")), args.output)
            print(f"{result['count']} targets: {args.output}")
        elif args.command == "match":
            from .matching.search import match
            rotations = tuple(int(angle.strip()) for angle in args.rotations.split(","))
            result = match(args.targets, args.index, args.output, args.top_k, rotations, args.photo_cache)
            print(f"{result['target_count']} targets / {result['candidate_count']} places / {result['view_count']} views: {args.output}")
        elif args.command == "materialize":
            from .matching.output import materialize
            result = materialize(args.targets, args.index, args.run)
            print(f"{result['materialized']} selected slots / {result['selected_unique_tiles']} real photos: {args.run}")
        elif args.command == "render":
            from .matching.output import render
            render(args.targets, args.run)
            print(f"Rendered color.mp4, gray.mp4, comparison.mp4: {args.run}")
        else:
            from .matching.comparison import compare
            result = compare(args.baseline, args.run)
            print(f"Mean MSE: {result['baseline_mean_mse']:.6f} -> {result['expanded_mean_mse']:.6f} "
                  f"({result['mean_reduction_percent']:.2f}% reduction), {result['regressed_targets']} regressed targets")
    except ValueError as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 1
    except (OSError, KeyError, subprocess.SubprocessError) as error:
        # Exceptions from remote libraries can include locators; do not echo raw errors.
        print(f"{args.command} failed ({type(error).__name__}). Check inputs, tools and evidence files.", file=sys.stderr)
        return 1
    return 0

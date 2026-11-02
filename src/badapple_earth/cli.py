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
    parallel = commands.add_parser("collect-parallel", help="Collect in independent worker indexes with resumable checkpoints")
    parallel.add_argument("--config", required=True, type=Path)
    parallel.add_argument("--output", required=True, type=Path)
    parallel.add_argument("--limit", required=True, type=int)
    parallel.add_argument("--workers", default=4, type=int)
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
    shape_build = commands.add_parser("shape-index", help="Build/resume full-resolution shape analysis and PHOG/rank features")
    shape_build.add_argument("--index", required=True, type=Path)
    shape_build.add_argument("--output", required=True, type=Path)
    shape_build.add_argument("--workers", default=8, type=int)
    shape_match = commands.add_parser("shape-match", help="Full PHOG/rank retrieval, SDF/Chamfer/region reranking and shape-first selection")
    shape_match.add_argument("--targets", required=True, type=Path)
    shape_match.add_argument("--index", required=True, type=Path)
    shape_match.add_argument("--output", required=True, type=Path)
    shape_match.add_argument("--config", type=Path, default=Path("config/shape-v2.json"))
    shape_match.add_argument("--stop-after-targets", type=int, help="Checkpoint after this many newly completed targets")
    shape_render = commands.add_parser("shape-render", help="Render real shape-selected photos, diagnostics and optional baseline comparison")
    shape_render.add_argument("--targets", required=True, type=Path)
    shape_render.add_argument("--index", required=True, type=Path)
    shape_render.add_argument("--run", required=True, type=Path)
    shape_render.add_argument("--baseline", type=Path)
    production = commands.add_parser("produce", help="Use every source frame and render the complete 16:9 search presentation")
    production.add_argument("--config", type=Path, default=Path("config/production.json"))
    production.add_argument("--output", type=Path, required=True)
    metrics = commands.add_parser("compare-metrics", help="Compare five CPU metrics on cached Top-K and every frame in an interval")
    metrics.add_argument("--run", type=Path, required=True)
    metrics.add_argument("--output", type=Path, required=True)
    metrics.add_argument("--start", type=float, default=38)
    metrics.add_argument("--duration", type=float, default=10)
    metrics.add_argument("--top-k", type=int, default=20)
    metrics.add_argument("--workers", type=int, default=8)
    correlation = commands.add_parser("compare-correlation", help="Search all views by multiscale ZNCC/gradient and compare wide reranking")
    correlation.add_argument("--config", type=Path, default=Path("config/correlation.json"))
    correlation.add_argument("--output", type=Path, required=True)
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
        elif args.command == "collect-parallel":
            from .collection.parallel import collect_parallel
            result = collect_parallel(json.loads(args.config.read_text(encoding="utf-8")), args.output, args.limit, args.workers)
            print(f"{result['status']}: {result['rows']}/{result['requested']} places in {args.output}")
            return 0 if result["status"] == "complete" else 1
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
        elif args.command == "produce":
            from .production.pipeline import produce
            result = produce(json.loads(args.config.read_text(encoding="utf-8")), args.output)
            print(f"Production {result['status']}: {result['source_frames']} original frames; {args.output}")
        elif args.command == "compare-metrics":
            from .production.comparison import compare_metrics
            result = compare_metrics(args.run, args.output, args.start, args.duration, args.top_k, args.workers)
            print(f"Metric comparison {result['status']}: {result['source_frames']} original frames; {args.output}")
        elif args.command == "compare-correlation":
            from .production.correlation.pipeline import compare
            result = compare(json.loads(args.config.read_text(encoding="utf-8")), args.output)
            print(f"Wide correlation comparison {result['status']}: {result['source_frames']} original frames; {args.output}")
        elif args.command == "shape-index":
            from .shape.index import build
            result = build(args.index, args.output, args.workers)
            print(f"Shape index: {result['places']} places / {result['view_count']} views")
        elif args.command == "shape-match":
            from .shape.search import match
            result = match(args.targets, args.index, args.output, json.loads(args.config.read_text(encoding="utf-8")), args.stop_after_targets)
            print(f"Shape match {result['status']}: {result['completed_targets']}/{result['target_count']} targets")
        elif args.command == "shape-render":
            from .shape.output import materialize
            result = materialize(args.targets, args.index, args.run, args.baseline)
            print(f"Shape video: {result['materialized']} frames / {result['selected_unique_tiles']} photos")
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

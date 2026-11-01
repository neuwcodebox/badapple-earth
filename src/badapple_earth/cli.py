"""Only implemented P1 commands are exposed."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .input import inspect_input
from .sample import sample, verify_sample


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
        else:
            result = verify_sample(args.sample)
            print(f"Reextraction: {len(result['checks'])} checks, passed={result['passed']}")
            return 0 if result["passed"] else 1
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        # Exceptions from remote libraries can include locators; do not echo raw errors.
        print(f"{args.command} failed ({type(error).__name__}). Check inputs, tools and evidence files.", file=sys.stderr)
        return 1
    return 0

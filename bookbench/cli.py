from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import load_config
from .runner import Harness, PipelineError


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bookbench", description="Validate and compare the Buchwandler audiobook pipeline")
    p.add_argument("--config", "-c", default="bookbench.toml", help="TOML configuration file")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check tools, profiles, and engine coverage")
    run = sub.add_parser("run", help="Validate the configured RenderProfile matrix")
    run.add_argument("--run-id")
    run.add_argument("--dry-run", action="store_true")
    compare = sub.add_parser(
        "compare",
        help="Run explicit [[voices]] candidates and generate a listening report",
    )
    compare.add_argument("--run-id")
    compare.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(Path(args.config))
        if args.command == "compare" and len(config.voices) < 2:
            raise ValueError("compare requires at least two [[voices]] entries in the TOML config")
        harness = Harness(config, dry_run=getattr(args, "dry_run", False))
        if args.command == "doctor":
            data = harness.doctor()
            print(json.dumps(data, indent=2))
            missing_tools = [name for name, value in data["tools"].items() if value is None]
            missing_engines = data["engine_coverage"]["missing"]
            return 1 if missing_tools or missing_engines or not data["profiles"] else 0
        run_dir, results = harness.run(run_id=args.run_id)
        print(f"Run: {run_dir}")
        print(f"Listening report: {run_dir / 'listen.html'}")
        print(f"{'PROFILE':24} {'ENGINE':18} {'STATUS':8} {'WER':>8} {'CER':>8}")
        for result in results:
            wer = "-" if result.wer is None else f"{result.wer:.4f}"
            cer = "-" if result.cer is None else f"{result.cer:.4f}"
            print(f"{result.profile[:24]:24} {','.join(result.engines)[:18]:18} {result.status:8} {wer:>8} {cer:>8}")
            if result.error:
                print(f"  {result.error}")
        return 0 if results and all(x.status == "pass" for x in results) else 1
    except (OSError, ValueError, PipelineError) as exc:
        print(f"bookbench: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

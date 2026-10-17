from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


def run_doctor(engine: str, evidence_file: Path) -> int:
    command = ["bookbench", "doctor", "--engine", engine]
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        returncode = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except OSError as exc:
        returncode = 127
        stdout = ""
        stderr = str(exc)

    report = {
        "schema": "bookbench.e2e-doctor.v1",
        "engine": engine,
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    try:
        evidence_file.parent.mkdir(parents=True, exist_ok=True)
        evidence_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"Could not save BookBench doctor evidence to {evidence_file}: {exc}", file=sys.stderr)

    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run BookBench doctor and persist its full output.")
    parser.add_argument("--engine", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_doctor(args.engine, args.output)


if __name__ == "__main__":
    raise SystemExit(main())

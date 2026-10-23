from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from scripts.ci.engine_matrix import DEFAULT_ENGINES

PLATFORMS = ("linux-x64", "windows-x64", "macos-arm64")


def merge_evidence(
    engine: str,
    run_id: str,
    attempt: str,
    downloaded_root: Path,
    output_root: Path,
    matrix_result: str,
) -> dict[str, object]:
    if engine not in DEFAULT_ENGINES:
        raise ValueError(f"unsupported engine: {engine}")
    for value, label in ((run_id, "run id"), (attempt, "run attempt")):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ValueError(f"invalid {label}: {value!r}")

    output_root.mkdir(parents=True, exist_ok=True)
    available: list[str] = []
    missing: list[str] = []
    artifact_root = output_root / "platform-artifacts"
    for platform in PLATFORMS:
        artifact_name = f"bookbench-{engine}-{platform}-{run_id}-{attempt}"
        source = downloaded_root / artifact_name
        if not source.is_dir():
            missing.append(platform)
            continue
        destination = artifact_root / platform
        shutil.copytree(source, destination, dirs_exist_ok=True)
        available.append(platform)

    manifest: dict[str, object] = {
        "schema": "bookbench.e2e-merged-evidence.v1",
        "engine": engine,
        "run_id": run_id,
        "run_attempt": attempt,
        "matrix_job_result": matrix_result,
        "available_platforms": available,
        "missing_platforms": missing,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    available_text = ", ".join(f"`{platform}`" for platform in available) or "none"
    missing_text = ", ".join(f"`{platform}`" for platform in missing) or "none"
    readme = f"""# BookBench merged E2E evidence

- Engine: `{engine}`
- Matrix result: `{matrix_result}` (a successful merge does not change this result)
- Evidence available for: {available_text}
- Evidence not uploaded for: {missing_text}
- Manifest: `manifest.json`
- Platform artifacts are preserved under `platform-artifacts/<platform>/`.

The merged artifact retains whichever platform artifacts were uploaded. Missing platform evidence is reported rather than treated as a passing E2E result.
"""
    (output_root / "README.md").write_text(readme, encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge whatever platform E2E evidence artifacts are available.")
    parser.add_argument("--engine", required=True, choices=DEFAULT_ENGINES)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--downloaded-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--matrix-result", required=True)
    args = parser.parse_args()
    merge_evidence(
        args.engine,
        args.run_id,
        args.attempt,
        args.downloaded_root,
        args.output_root,
        args.matrix_result,
    )


if __name__ == "__main__":
    main()

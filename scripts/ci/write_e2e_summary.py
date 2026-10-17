from __future__ import annotations

import argparse
import os
from pathlib import Path


def render_summary(
    mode: str,
    engine: str,
    run_id: str,
    attempt: str,
    artifact_url: str,
    *,
    platform: str | None = None,
    matrix_result: str | None = None,
    job_status: str | None = None,
) -> str:
    if mode == "platform":
        if not platform:
            raise ValueError("platform is required for a platform summary")
        heading = f"### BookBench E2E: {engine} / {platform}"
        details = [f"- Job status: `{job_status or 'unknown'}`"]
    elif mode == "merge":
        heading = f"### BookBench E2E merged evidence: {engine}"
        details = [f"- Matrix result: `{matrix_result or 'unknown'}`"]
    else:
        raise ValueError(f"unsupported summary mode: {mode}")
    details.append(f"- Run: `{run_id}` (attempt `{attempt}`)")
    if artifact_url:
        details.append(f"- [Download evidence artifact]({artifact_url})")
    else:
        details.append("- Evidence artifact URL was not available.")
    return "\n".join((heading, *details, "")) + "\n"


def write_summary(summary: str, output_path: str | None = None) -> None:
    output_path = output_path or os.environ.get("GITHUB_STEP_SUMMARY")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as output:
            output.write(summary)
    else:
        print(summary, end="")


def main() -> None:
    parser = argparse.ArgumentParser(description="Link BookBench E2E artifacts in the GitHub job summary.")
    parser.add_argument("--mode", choices=("platform", "merge"), required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--platform")
    parser.add_argument("--matrix-result")
    parser.add_argument("--job-status")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    parser.add_argument("--attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    parser.add_argument("--artifact-url", default=os.environ.get("BOOKBENCH_ARTIFACT_URL", ""))
    args = parser.parse_args()
    summary = render_summary(
        args.mode,
        args.engine,
        args.run_id,
        args.attempt,
        args.artifact_url,
        platform=args.platform,
        matrix_result=args.matrix_result,
        job_status=args.job_status,
    )
    write_summary(summary)


if __name__ == "__main__":
    main()

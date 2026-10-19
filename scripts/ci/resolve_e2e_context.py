from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import tomllib
from pathlib import Path

try:
    from .engine_matrix import DEFAULT_ENGINES as ENGINES
except ImportError:  # Script execution puts scripts/ci on sys.path.
    from engine_matrix import DEFAULT_ENGINES as ENGINES


ROOT = Path(__file__).resolve().parents[2]


def resolve_context(engine: str, root: Path = ROOT) -> dict[str, str]:
    if engine not in ENGINES:
        raise ValueError(f"unsupported engine: {engine}")

    config = tomllib.loads((root / "bookbench.toml").read_text(encoding="utf-8"))
    speech = config["speechscope"]
    model = speech["model"]
    backend = speech["backend"]
    device = speech["device"]
    if backend != "whispercpp":
        raise ValueError(f"CI expects speechscope.backend=whispercpp, got {backend!r}")
    if not model.startswith("whispercpp:"):
        raise ValueError(f"CI expects a canonical whispercpp model ref, got {model!r}")

    result = subprocess.run(
        ["speechscope", "models", "show", model, "--format", "json"],
        check=True,
        text=True,
        capture_output=True,
    )
    record = json.loads(result.stdout)
    primary = [artifact for artifact in record["artifacts"] if artifact.get("primary")]
    if len(primary) != 1:
        raise ValueError("catalog record must contain exactly one primary artifact")
    digest = primary[0]["sha256"]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError("catalog primary artifact must have a SHA-256 digest")

    slug = model.replace(":", "-").replace("/", "-")
    profile = root / "profiles" / f"{engine}.renderprofile.toml"
    if not profile.is_file():
        raise ValueError(f"missing engine profile: {profile}")
    profile_sha = hashlib.sha256(profile.read_bytes()).hexdigest()
    return {
        "model": model,
        "backend": backend,
        "device": device,
        "architecture": platform.machine(),
        "cache_key": f"speechscope-{platform.machine()}-{slug}-{digest.lower()}",
        "profile_sha": profile_sha,
    }


def write_outputs(values: dict[str, str], output_path: str | None = None) -> None:
    output_path = output_path or os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    if output_path:
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as output:
            output.write(lines)
    else:
        print(lines, end="")


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve and fingerprint BookBench's configured ASR model.")
    parser.add_argument("--engine", required=True, choices=ENGINES)
    args = parser.parse_args()
    write_outputs(resolve_context(args.engine))


if __name__ == "__main__":
    main()

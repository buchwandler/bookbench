# ruff: noqa: N999
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = {
    "linux-x64": "x86_64-unknown-linux-gnu",
    "windows-x64": "x86_64-pc-windows-msvc",
    "macos-arm64": "aarch64-apple-darwin",
}
ENGINES = ("inflect", "kitten", "kokoro", "piper", "pocket", "supertonic")


def main() -> None:
    for platform, python_platform in PLATFORMS.items():
        for engine in ENGINES:
            requirements = ROOT / "requirements" / "e2e" / "engines" / f"{engine}.in"
            lock = ROOT / "requirements" / "e2e" / "locks" / platform / f"{engine}.lock"
            lock.parent.mkdir(parents=True, exist_ok=True)
            command = [
                "uv",
                "pip",
                "compile",
                "--python-version",
                "3.12",
                "--python-platform",
                python_platform,
                "--generate-hashes",
                "--no-annotate",
                "--output-file",
                str(lock.relative_to(ROOT)),
                str(requirements.relative_to(ROOT)),
            ]
            print(f"Compiling {platform}/{engine}", flush=True)
            subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()

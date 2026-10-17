from __future__ import annotations

import argparse
import subprocess


def ensure_model(model: str) -> None:
    verify = ["speechscope", "models", "verify", model]
    installed = subprocess.run(verify, check=False)
    if installed.returncode == 0:
        return

    subprocess.run(["speechscope", "models", "install", model, "--force"], check=True)
    subprocess.run(verify, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify or install the configured SpeechScope model.")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    ensure_model(args.model)


if __name__ == "__main__":
    main()

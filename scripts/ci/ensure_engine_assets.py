from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

try:
    from .engine_matrix import ENGINE_ASSETS
except ImportError:  # Script execution puts scripts/ci on sys.path.
    from engine_matrix import ENGINE_ASSETS

ROOT = Path(__file__).resolve().parents[2]
_SECRET_TEXT = re.compile(
    r"(?i)\b(?:hf_[a-z0-9_-]{8,}|(?:token|password|secret|authorization)\s*[=:]\s*)\S+"
)


def _safe_message(value: object) -> str:
    message = " ".join(str(value).split())
    message = _SECRET_TEXT.sub("[REDACTED]", message)
    if len(message) > 240:
        message = message[:239] + "…"
    return message


def _safe_error(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": _safe_message(exc)}


def ensure_engine_asset(
    engine: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if engine not in ENGINE_ASSETS:
        raise ValueError(f"engine has no explicit model asset policy: {engine}")
    model_ref, quality = ENGINE_ASSETS[engine]
    environment = dict(os.environ if env is None else env)
    cache_value = environment.get("ONNXVOICE_CACHE_DIR")
    cache_dir = str(Path(cache_value).expanduser().resolve()) if cache_value else None
    verify = ["onnxvoice", "verify", model_ref, "--quality", quality]
    install = ["onnxvoice", "install", model_ref, "--quality", quality]
    report: dict[str, Any] = {
        "schema": "bookbench.engine-assets.v1",
        "engine": engine,
        "model_ref": model_ref,
        "quality": quality,
        "cache_dir": cache_dir,
        "cache_hit": False,
        "status": "failed",
        "error": None,
    }

    def run(command: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return runner(
                command,
                capture_output=True,
                check=False,
                env=environment,
                text=True,
                timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            report["error"] = {"stage": command[1], **_safe_error(exc)}
            return None

    initial = run(verify)
    if initial is None:
        return report
    if initial.returncode == 0:
        report["cache_hit"] = True
        report["status"] = "verified"
        return report

    installed = run(install)
    if installed is None:
        return report
    if installed.returncode != 0:
        report["error"] = {
            "stage": "install",
            "returncode": installed.returncode,
            "message": _safe_message(installed.stderr or installed.stdout),
        }
        return report

    verified = run(verify)
    if verified is None:
        return report
    if verified.returncode != 0:
        report["error"] = {
            "stage": "verify_after_install",
            "returncode": verified.returncode,
            "message": _safe_message(verified.stderr or verified.stdout),
        }
        return report
    report["status"] = "installed_and_verified"
    return report


def write_report(report: Mapping[str, Any], output_path: Path | None = None) -> str:
    serialized = json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized, encoding="utf-8")
    return serialized


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ensure an explicitly configured ONNXVoice engine asset and emit safe JSON evidence."
    )
    parser.add_argument("--engine", required=True, choices=tuple(ENGINE_ASSETS))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = ensure_engine_asset(args.engine)
    output_path = args.output
    if output_path is not None and not output_path.is_absolute():
        output_path = ROOT / output_path
    sys.stdout.write(write_report(report, output_path))
    return 0 if report["status"] in {"verified", "installed_and_verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

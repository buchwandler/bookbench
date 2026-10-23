from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from .engine_matrix import ENGINE_COMPONENTS, ENGINE_EXECUTABLES, RuntimeExecutable
except ImportError:  # Script execution puts scripts/ci on sys.path.
    from engine_matrix import ENGINE_COMPONENTS, ENGINE_EXECUTABLES, RuntimeExecutable

ROOT = Path(__file__).resolve().parents[2]
MAX_ERROR_MESSAGE = 240
IMPORT_PROBE = r"""
import contextlib
import importlib
import importlib.metadata
import io
import json
import re
import sys

module_name, distribution_name = sys.argv[1:3]
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        module = importlib.import_module(module_name)
    try:
        version = importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        version = getattr(module, "__version__", None)
    result = {"version": str(version) if version is not None else "unknown"}
    returncode = 0
except Exception as exc:
    error_message = " ".join(str(exc).split())
    error_message = re.sub(
        r"(?i)\b(?:hf_[a-z0-9_-]{8,}|(?:token|password|secret|authorization)\s*[=:]\s*)\S+",
        "[REDACTED]",
        error_message,
    )[:240]
    result = {
        "error_type": type(exc).__name__,
        "error_message": error_message,
        "missing_name": getattr(exc, "name", None),
    }
    returncode = 1
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
sys.exit(returncode)
"""
_SECRET_TEXT = re.compile(r"(?i)\b(?:hf_[a-z0-9_-]{8,}|(?:token|password|secret|authorization)\s*[=:]\s*)\S+")


def _safe_message(value: object) -> str:
    message = " ".join(str(value).split())
    message = _SECRET_TEXT.sub("[REDACTED]", message)
    if len(message) > MAX_ERROR_MESSAGE:
        message = message[: MAX_ERROR_MESSAGE - 1] + "…"
    return message


def _clean_environment() -> dict[str, str]:
    # Runtime imports need executable/DLL lookup and temporary paths, not user credentials.
    safe_keys = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "ESPEAKNG_RUNTIME_EXECUTABLE")
    return {key: os.environ[key] for key in safe_keys if key in os.environ}


def _component_result(module_name: str, distribution_name: str) -> dict[str, Any]:
    command = [
        sys.executable,
        "-I",
        "-c",
        IMPORT_PROBE,
        module_name,
        distribution_name,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=_clean_environment(),
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "broken",
            "error": {"type": type(exc).__name__, "message": _safe_message(exc)},
        }

    try:
        result = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return {
            "status": "broken",
            "error": {"type": "ProbeError", "message": "Import subprocess returned invalid JSON."},
        }

    if completed.returncode == 0 and isinstance(result, dict) and isinstance(result.get("version"), str):
        return {"status": "ready", "version": result["version"]}

    error_type = result.get("error_type") if isinstance(result, dict) else None
    missing_name = result.get("missing_name") if isinstance(result, dict) else None
    if not isinstance(error_type, str):
        error_type = "ProbeError"
    if not isinstance(missing_name, str):
        missing_name = None
    status = "missing" if error_type == "ModuleNotFoundError" and missing_name == module_name else "broken"
    message = result.get("error_message") if isinstance(result, dict) else None
    if not isinstance(message, str):
        message = "Import subprocess failed without a structured error."
    return {"status": status, "error": {"type": error_type, "message": _safe_message(message)}}


EXECUTABLE_PROBE_TIMEOUT = 10


def _executable_result(requirement: RuntimeExecutable) -> dict[str, Any]:
    configured_path = os.environ.get(requirement.env_var) if requirement.env_var is not None else None
    if requirement.env_var is not None and requirement.env_var in os.environ:
        path = configured_path
        source = requirement.env_var
        if not path or not Path(path).is_file():
            return {
                "status": "missing",
                "path": path,
                "source": source,
                "error": {
                    "type": "ExecutableNotFound",
                    "message": "Configured executable path does not point to a file.",
                },
            }
    else:
        path = shutil.which(requirement.command)
        source = "PATH"
        if not path:
            return {
                "status": "missing",
                "path": None,
                "source": source,
                "error": {
                    "type": "ExecutableNotFound",
                    "message": f"Required executable '{requirement.command}' was not found.",
                },
            }
        if not Path(path).is_file():
            return {
                "status": "missing",
                "path": path,
                "source": source,
                "error": {
                    "type": "ExecutableNotFound",
                    "message": f"Resolved executable '{requirement.command}' is not a file.",
                },
            }

    command = [path, *requirement.version_args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=_clean_environment(),
            text=True,
            timeout=EXECUTABLE_PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "broken",
            "path": path,
            "source": source,
            "error": {
                "type": "TimeoutExpired",
                "message": f"Required executable '{requirement.command}' timed out during its readiness check.",
            },
        }
    except OSError as exc:
        return {
            "status": "broken",
            "path": path,
            "source": source,
            "error": {
                "type": type(exc).__name__,
                "message": f"Could not run required executable '{requirement.command}' for its readiness check.",
            },
        }

    if completed.returncode != 0:
        return {
            "status": "broken",
            "path": path,
            "source": source,
            "error": {
                "type": "ExecutableCheckFailed",
                "message": f"Required executable '{requirement.command}' failed its readiness check (exit {completed.returncode}).",
            },
        }
    return {"status": "ready", "path": path, "source": source}


def check_engine_runtime(engine: str) -> dict[str, Any]:
    if engine not in ENGINE_COMPONENTS:
        raise ValueError(f"unsupported engine: {engine}")
    components = {
        module_name: _component_result(module_name, distribution_name)
        for module_name, distribution_name in ENGINE_COMPONENTS[engine]
    }
    executables = {requirement.command: _executable_result(requirement) for requirement in ENGINE_EXECUTABLES[engine]}
    return {
        "engine": engine,
        "ok": all(component["status"] == "ready" for component in components.values())
        and all(executable["status"] == "ready" for executable in executables.values()),
        "components": components,
        "executables": executables,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check engine runtime dependencies without downloading model assets.")
    parser.add_argument("--engine", required=True, choices=tuple(ENGINE_COMPONENTS))
    parser.add_argument("--output", type=Path, help="Optional path for the deterministic JSON report.")
    args = parser.parse_args()

    report = check_engine_runtime(args.engine)
    serialized = json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output is not None:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    sys.stdout.write(serialized)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

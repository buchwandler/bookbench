from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINES = ("inflect", "kitten", "kokoro", "piper", "pocket", "supertonic")
PLATFORMS = ("linux-x64", "windows-x64", "macos-arm64")
SAFE_ENVIRONMENT_KEYS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITHUB_EVENT_NAME",
    "GITHUB_JOB",
    "GITHUB_REF",
    "GITHUB_REPOSITORY",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_RUN_ID",
    "GITHUB_SHA",
    "GITHUB_WORKFLOW",
    "GITHUB_WORKSPACE",
    "RUNNER_ARCH",
    "RUNNER_NAME",
    "RUNNER_OS",
    "SPEECHSCOPE_MODEL_HOME",
)
STEP_OUTCOME_KEYS = {
    "setup_python": "BOOKBENCH_CI_SETUP_PYTHON_OUTCOME",
    "install_dependencies": "BOOKBENCH_CI_INSTALL_DEPS_OUTCOME",
    "install_bookbench": "BOOKBENCH_CI_INSTALL_CHECKOUT_OUTCOME",
    "runtime_cache": "BOOKBENCH_CI_RUNTIME_CACHE_OUTCOME",
    "bootstrap_linux": "BOOKBENCH_CI_BOOTSTRAP_LINUX_OUTCOME",
    "bootstrap_macos": "BOOKBENCH_CI_BOOTSTRAP_MACOS_OUTCOME",
    "bootstrap_windows": "BOOKBENCH_CI_BOOTSTRAP_WINDOWS_OUTCOME",
    "resolve_asr": "BOOKBENCH_CI_RESOLVE_ASR_OUTCOME",
    "model_cache": "BOOKBENCH_CI_MODEL_CACHE_OUTCOME",
    "verify_model": "BOOKBENCH_CI_MODEL_VERIFY_OUTCOME",
    "cold_synthesis_cache": "BOOKBENCH_CI_COLD_CACHE_OUTCOME",
    "doctor": "BOOKBENCH_CI_DOCTOR_OUTCOME",
    "e2e": "BOOKBENCH_CI_E2E_OUTCOME",
    "job": "BOOKBENCH_CI_JOB_STATUS",
}
ENGINE_PACKAGES = {
    "inflect": "inflectsynth",
    "kitten": "kittensynth",
    "kokoro": "pykokoro",
    "piper": "pipersynth",
    "pocket": "pocketsynth",
    "supertonic": "supertonicsynth",
}


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def capture_diagnostics(
    engine: str,
    platform_key: str,
    output_dir: Path,
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    if engine not in ENGINES:
        raise ValueError(f"unsupported engine: {engine}")
    if platform_key not in PLATFORMS:
        raise ValueError(f"unsupported platform: {platform_key}")
    environment = os.environ if env is None else env
    lock = root / "requirements" / "e2e" / "locks" / platform_key / f"{engine}.lock"
    profile = root / "profiles" / f"{engine}.renderprofile.toml"
    speech_config: dict[str, object] = {}
    config_error = None
    try:
        config = tomllib.loads((root / "bookbench.toml").read_text(encoding="utf-8"))
        speech = config["speechscope"]
        speech_config = {key: speech.get(key) for key in ("backend", "model", "device")}
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        config_error = f"{type(exc).__name__}: {exc}"

    whisper_bin = environment.get("SPEECHSCOPE_WHISPERCPP_BIN")
    doctor_path = output_dir / "doctor.json"
    doctor_returncode = None
    if doctor_path.is_file():
        try:
            doctor_returncode = json.loads(doctor_path.read_text(encoding="utf-8")).get("returncode")
        except (OSError, json.JSONDecodeError):
            doctor_returncode = "unreadable"

    report: dict[str, object] = {
        "schema": "bookbench.e2e-ci-diagnostics.v1",
        "engine": engine,
        "platform_key": platform_key,
        "captured_after_job_steps": True,
        "runner": {
            "os": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
        },
        "safe_environment": {key: environment[key] for key in SAFE_ENVIRONMENT_KEYS if key in environment},
        "step_outcomes": {name: environment[key] for name, key in STEP_OUTCOME_KEYS.items() if key in environment},
        "speechscope_config": speech_config,
        "speechscope_config_error": config_error,
        "files": {
            "lock_path": lock.relative_to(root).as_posix(),
            "lock_sha256": _sha256(lock),
            "renderprofile_sha256": _sha256(profile),
            "doctor_output": doctor_path.is_file(),
            "doctor_returncode": doctor_returncode,
            "runtime_cache_present": (root / ".ci-cache" / "whispercpp").is_dir(),
            "speechscope_model_cache_present": Path(
                environment.get("SPEECHSCOPE_MODEL_HOME", root / ".ci-cache" / "speechscope-models")
            ).is_dir(),
        },
        "runtime": {
            "bookbench_executable": shutil.which("bookbench"),
            "speechscope_executable": shutil.which("speechscope"),
            "whispercpp_executable": whisper_bin,
            "whispercpp_executable_exists": bool(whisper_bin and Path(whisper_bin).is_file()),
        },
        "package_versions": {
            name: _package_version(name)
            for name in ("bookbench", "voicerender", "speechscope", ENGINE_PACKAGES[engine])
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "ci-diagnostics.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    readme = f"""# BookBench E2E evidence

- Engine: `{engine}`
- Platform lock: `{platform_key}`
- Hosted runner: `{environment.get("RUNNER_OS", platform.system())}` / `{environment.get("RUNNER_ARCH", platform.machine())}`
- GitHub run: `{environment.get("GITHUB_RUN_ID", "local")}` (attempt `{environment.get("GITHUB_RUN_ATTEMPT", "unknown")}`)
- CI diagnostics: `ci-diagnostics.json` (safe environment fields, tool versions, lock/profile hashes, and step outcomes)
- Doctor report: `doctor.json` when the doctor step was reached; it contains the full command output and exit status.
- BookBench run evidence may be absent when the job failed before synthesis started.

The diagnostic report uses an explicit environment-variable allowlist and does not serialize secrets.
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Write non-secret BookBench E2E CI diagnostics and artifact metadata.")
    parser.add_argument("--engine", required=True, choices=ENGINES)
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    args = parser.parse_args()
    output_dir = ROOT / ".bookbench" / "evidence" / args.platform
    capture_diagnostics(args.engine, args.platform, output_dir)


if __name__ == "__main__":
    main()

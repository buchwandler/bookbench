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

try:
    from .engine_matrix import ENGINE_PACKAGES
    from .engine_matrix import LOCK_ENGINES as ENGINES
except ImportError:  # Script execution puts scripts/ci on sys.path.
    from engine_matrix import ENGINE_PACKAGES
    from engine_matrix import LOCK_ENGINES as ENGINES


ROOT = Path(__file__).resolve().parents[2]
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
    "VOICEFOLIO_CACHE_DIR",
    "ONNXVOICE_CACHE_DIR",
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
    "voicefolio_cache": "BOOKBENCH_CI_VOICEFOLIO_CACHE_OUTCOME",
    "engine_asset_cache": "BOOKBENCH_CI_ENGINE_ASSET_CACHE_OUTCOME",
    "engine_assets": "BOOKBENCH_CI_ENGINE_ASSETS_OUTCOME",
    "reference_voice": "BOOKBENCH_CI_REFERENCE_VOICE_OUTCOME",
    "verify_model": "BOOKBENCH_CI_MODEL_VERIFY_OUTCOME",
    "cold_synthesis_cache": "BOOKBENCH_CI_COLD_CACHE_OUTCOME",
    "engine_runtime": "BOOKBENCH_CI_ENGINE_RUNTIME_OUTCOME",
    "doctor": "BOOKBENCH_CI_DOCTOR_OUTCOME",
    "e2e": "BOOKBENCH_CI_E2E_OUTCOME",
    "job": "BOOKBENCH_CI_JOB_STATUS",
}


CACHE_HIT_KEYS = {
    "voicefolio": "BOOKBENCH_CI_VOICEFOLIO_CACHE_HIT",
    "onnxvoice": "BOOKBENCH_CI_ENGINE_ASSET_CACHE_HIT",
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

    runtime_path = output_dir / "engine-runtime.json"
    engine_runtime_ok: bool | str | None = None
    if runtime_path.is_file():
        try:
            runtime_report = json.loads(runtime_path.read_text(encoding="utf-8"))
            engine_runtime_ok = runtime_report.get("ok") if isinstance(runtime_report, dict) else None
            if not isinstance(engine_runtime_ok, bool):
                engine_runtime_ok = "unreadable"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            engine_runtime_ok = "unreadable"



    asset_path = output_dir / "engine-assets.json"
    engine_asset_status: str | None = None
    engine_asset_cache_hit: bool | None = None
    if asset_path.is_file():
        try:
            asset_report = json.loads(asset_path.read_text(encoding="utf-8"))
            if isinstance(asset_report, dict):
                status = asset_report.get("status")
                cache_hit = asset_report.get("cache_hit")
                engine_asset_status = status if isinstance(status, str) else "unreadable"
                engine_asset_cache_hit = cache_hit if isinstance(cache_hit, bool) else None
            else:
                engine_asset_status = "unreadable"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            engine_asset_status = "unreadable"


    reference_path = output_dir / "reference-voice.json"
    reference_voice_status: str | None = None
    reference_voice_cache_hits: list[bool | None] | None = None
    if reference_path.is_file():
        try:
            reference_report = json.loads(reference_path.read_text(encoding="utf-8"))
            if isinstance(reference_report, dict):
                status = reference_report.get("status")
                references = reference_report.get("references", [])
                reference_voice_status = status if isinstance(status, str) else "unreadable"
                if isinstance(references, list):
                    reference_voice_cache_hits = [
                        item.get("cache_hit")
                        for item in references
                        if isinstance(item, dict)
                        and (item.get("cache_hit") is None or isinstance(item.get("cache_hit"), bool))
                    ]
            else:
                reference_voice_status = "unreadable"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            reference_voice_status = "unreadable"
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
        "cache_hits": {name: environment[key] for name, key in CACHE_HIT_KEYS.items() if key in environment},
        "speechscope_config_error": config_error,
        "files": {
            "lock_path": lock.relative_to(root).as_posix(),
            "lock_sha256": _sha256(lock),
            "renderprofile_sha256": _sha256(profile),
            "doctor_output": doctor_path.is_file(),
            "engine_runtime_output": runtime_path.is_file(),
            "engine_runtime_ok": engine_runtime_ok,
            "engine_assets_output": asset_path.is_file(),
            "engine_assets_status": engine_asset_status,
            "engine_assets_cache_hit": engine_asset_cache_hit,
            "reference_voice_output": reference_path.is_file(),
            "reference_voice_status": reference_voice_status,
            "reference_voice_cache_hits": reference_voice_cache_hits,
            "doctor_returncode": doctor_returncode,
            "runtime_cache_present": (root / ".ci-cache" / "whispercpp").is_dir(),
            "speechscope_model_cache_present": Path(
                environment.get("SPEECHSCOPE_MODEL_HOME", root / ".ci-cache" / "speechscope-models")
            ).is_dir(),
            "voicefolio_cache_present": Path(
                environment.get("VOICEFOLIO_CACHE_DIR", root / ".ci-cache" / "voicefolio")
            ).is_dir(),
            "onnxvoice_cache_present": Path(
                environment.get("ONNXVOICE_CACHE_DIR", root / ".ci-cache" / "onnxvoice")
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
- Engine runtime report: `engine-runtime.json` when the readiness step ran; its success flag and step outcome are recorded in `ci-diagnostics.json`.
- Engine asset report: `engine-assets.json` when ONNXVoice model verification ran; it records verified/cache-hit/install/failure status without command output secrets.
- Reference voice report: `reference-voice.json` when present; it records managed-reference validation/fetch outcome, pinned source identity, cache state, and failure type without a local WAV path.
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

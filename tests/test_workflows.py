from __future__ import annotations

import re
import tomllib
from pathlib import Path

from scripts.ci.engine_matrix import DEFAULT_ENGINES, ENGINE_CI, LOCK_ENGINES

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ENGINES = DEFAULT_ENGINES


def test_default_profiles_and_configs_match_shared_engine_registry():
    profile_ids = {
        path.name.removesuffix(".renderprofile.toml")
        for path in (ROOT / "profiles").glob("*.renderprofile.toml")
    }
    assert profile_ids == set(ENGINES)

    project_config = tomllib.loads((ROOT / "bookbench.toml").read_text(encoding="utf-8"))
    matrix_example = tomllib.loads(
        (ROOT / "bookbench-all-engines.toml.example").read_text(encoding="utf-8")
    )
    assert project_config["required_engines"] == list(ENGINES)
    assert matrix_example["required_engines"] == list(ENGINES)
    assert "qwen3" not in profile_ids
    assert "qwen3" in LOCK_ENGINES
    assert not (WORKFLOWS / "e2e-qwen3.yml").exists()


def test_each_validated_engine_has_one_manual_reusable_workflow():
    for engine in ENGINES:
        path = WORKFLOWS / f"e2e-{engine}.yml"
        assert path.is_file(), f"missing manual workflow for {engine}"
        content = path.read_text(encoding="utf-8")

        assert re.search(r"(?m)^on:\s*$", content)
        assert re.search(r"(?m)^  workflow_dispatch:\s*$", content)
        assert content.count("workflow_dispatch:") == 1
        assert "actions: read" in content
        assert re.findall(r"(?m)^\s+uses:\s+(.+?)\s*$", content) == ["./.github/workflows/_e2e-engine.yml"]
        assert re.findall(r"(?m)^\s+engine:\s*([a-z-]+)\s*$", content) == [engine]


def test_shared_engine_workflow_verifies_model_cache_and_uploads_cold_run_evidence():
    path = WORKFLOWS / "_e2e-engine.yml"
    assert path.is_file()
    content = path.read_text(encoding="utf-8")

    assert "workflow_call:" in content
    assert "fail-fast: false" in content
    for runner, platform in (
        ("ubuntu-24.04", "linux-x64"),
        ("windows-2025", "windows-x64"),
        ("macos-15", "macos-arm64"),
    ):
        assert f"os: {runner}" in content
        assert f"platform: {platform}" in content
    assert "runs-on: ${{ matrix.os }}" in content
    assert "SPEECHSCOPE_MODEL_HOME: ${{ github.workspace }}/.ci-cache/speechscope-models" in content
    assert "SPEECHSCOPE_WHISPERCPP_CACHE: ${{ github.workspace }}/.ci-cache/whispercpp" in content
    assert "runs-on: self-hosted" not in content
    assert 'python-version: "3.12"' in content
    assert 'CUDA_VISIBLE_DEVICES: ""' in content
    assert "requirements/e2e/locks/${{ matrix.platform }}/${{ inputs.engine }}.lock" in content
    assert content.index("python -m pip install --require-hashes") < content.index("scripts/ci/bootstrap-e2e-linux.sh")
    assert "group: bookbench-e2e-${{ inputs.engine }}-${{ matrix.platform }}" in content
    assert "path: .ci-cache/whispercpp" in content

    assert "VOICEFOLIO_CACHE_DIR: ${{ github.workspace }}/.ci-cache/voicefolio" in content
    assert "ONNXVOICE_CACHE_DIR: ${{ github.workspace }}/.ci-cache/onnxvoice" in content
    assert "path: .ci-cache/voicefolio" in content
    assert "path: .ci-cache/onnxvoice" in content
    assert "voicefolio-v1-${{ runner.os }}-${{ runner.arch }}-0.1.1-323332d33f997de8394f24a193e1a76df720e01a-46264e83cb99115c3d210260e029117566d9c64f20266d10daa78107759ede3e" in content
    assert (
        "hashFiles(format('requirements/e2e/locks/{0}/{1}.lock', matrix.platform, inputs.engine), "
        "'scripts/ci/engine_matrix.py', format('profiles/{0}.renderprofile.toml', inputs.engine))" in content
    )
    assert "ensure_engine_assets.py --engine" in content
    assert content.index("ensure_engine_assets.py --engine") < content.index("Check engine runtime readiness")
    assert "prepare_reference_voice.py --engine" in content
    assert content.index("prepare_reference_voice.py --engine") < content.index("BookBench doctor")
    assert "reference-voice.json" in content
    assert "BOOKBENCH_CI_REFERENCE_VOICE_OUTCOME: ${{ steps.reference-voice.outcome }}" in content
    assert "whispercpp-cpu-v1-${{ runner.os }}-${{ runner.arch }}-0c7be696371069e0baa2515ed7aa08d5a5473aa6" in content
    assert "if: runner.os == 'Linux'" in content
    assert "if: runner.os == 'macOS'" in content
    assert "if: runner.os == 'Windows'" in content
    assert "scripts/ci/resolve_e2e_context.py --engine" in content
    assert "${{ runner.os }}-${{ runner.arch }}-${{ steps.asr.outputs.cache_key }}" in content
    assert "scripts/ci/ensure_speechscope_model.py --model" in content
    assert "shutil.rmtree(p) if p.exists() else None" in content
    assert "os.uname()" not in content
    assert "rm -rf" not in content
    assert 'scripts/ci/run_e2e_doctor.py --engine "${{ inputs.engine }}"' in content
    assert '".bookbench/evidence/${{ matrix.platform }}/doctor.json"' in content
    assert 'run: bookbench run --engine "${{ inputs.engine }}"' in content
    assert "if: always()" in content
    assert "scripts/ci/write_e2e_diagnostics.py --engine" in content
    assert "BOOKBENCH_CI_DOCTOR_OUTCOME: ${{ steps.doctor.outcome }}" in content
    assert "uses: actions/upload-artifact@" in content
    assert "name: bookbench-${{ inputs.engine }}-${{ matrix.platform }}-" in content
    assert (
        ".bookbench/runs/gha-${{ github.run_id }}-${{ github.run_attempt }}-${{ inputs.engine }}-${{ matrix.platform }}/"
        in content
    )
    assert ".bookbench/evidence/${{ matrix.platform }}/" in content
    e2e_job, merge_job = content.split("\n  merge:\n", maxsplit=1)
    assert "continue-on-error" not in e2e_job
    assert "id: upload-evidence" in e2e_job
    assert "steps.upload-evidence.outputs.artifact-url" in e2e_job
    assert "actions/download-artifact@v7" in merge_job
    assert "pattern: bookbench-${{ inputs.engine }}-*-${{ github.run_id }}-${{ github.run_attempt }}" in merge_job
    assert "continue-on-error: true" in merge_job
    assert "needs: e2e" in merge_job
    assert "if: always()" in merge_job
    assert "scripts/ci/merge_e2e_evidence.py" in merge_job
    assert '--matrix-result "${{ needs.e2e.result }}"' in merge_job
    assert "name: bookbench-${{ inputs.engine }}-merged-${{ github.run_id }}-${{ github.run_attempt }}" in merge_job
    assert "steps.upload-merged.outputs.artifact-url" in merge_job
    assert "scripts/ci/write_e2e_summary.py --mode merge" in merge_job


def test_hosted_all_engine_aggregate_is_manual_and_calls_every_engine():
    path = WORKFLOWS / "e2e-all-engines.yml"
    assert path.is_file()
    content = path.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in content
    assert "actions: read" in content
    assert "self-hosted" not in content
    assert content.count("./.github/workflows/_e2e-engine.yml") == len(ENGINES)
    assert re.findall(r"(?m)^\s+engine:\s*([a-z-]+)\s*$", content) == list(ENGINES)


def test_unit_ci_runs_on_approved_hosted_runner_matrix():
    content = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")

    assert "fail-fast: false" in content
    assert "runs-on: ${{ matrix.os }}" in content
    for runner in ("ubuntu-24.04", "windows-2025", "macos-15"):
        assert f"- os: {runner}" in content
    assert 'python-version: "3.12"' in content
    assert "pytest -q" in content
    assert "ruff check bookbench tests scripts/ci" in content


def test_no_workflow_uses_self_hosted_runners():
    for path in WORKFLOWS.glob("*.yml"):
        assert "runs-on: self-hosted" not in path.read_text(encoding="utf-8"), path.name


def test_hosted_e2e_lock_and_whisper_bootstrap_are_pinned():
    base = (ROOT / "requirements" / "e2e" / "base.in").read_text(encoding="utf-8")
    assert "text2epub==0.1.4" in base
    assert "utterplan[spacy]==0.6.3" in base
    assert "en_core_web_sm-3.8.0-py3-none-any.whl" in base

    package_names = {engine.id: engine.distribution for engine in ENGINE_CI}
    inflect_requirement = "inflectsynth[cpu]==0.1.2"
    for engine, package in package_names.items():
        engine_input = (ROOT / "requirements" / "e2e" / "engines" / f"{engine}.in").read_text(encoding="utf-8")
        assert "-r ../base.in" in engine_input
        if engine == "inflect":
            assert inflect_requirement in engine_input
        else:
            assert package in engine_input

        unrelated_packages = set(package_names.values()) - {package}
        for platform in ("linux-x64", "windows-x64", "macos-arm64"):
            lock_path = ROOT / "requirements" / "e2e" / "locks" / platform / f"{engine}.lock"
            assert lock_path.is_file(), f"missing {platform}/{engine} lock"
            lock = lock_path.read_text(encoding="utf-8")
            locked_names = {
                line.split("==", maxsplit=1)[0].split(" @ ", maxsplit=1)[0].lower()
                for line in lock.splitlines()
                if "==" in line or " @ " in line
            }
            assert package in locked_names
            assert not (unrelated_packages & locked_names)
            assert "--hash=sha256:" in lock
            if engine == "qwen3":
                assert "huggingface-hub==" not in lock
            if engine == "inflect":
                assert "inflectsynth==0.1.2" in lock
                assert "huggingface-hub==" in lock

    compiler = (ROOT / "scripts" / "ci" / "compile-e2e-locks.py").read_text(encoding="utf-8")
    assert '"3.12"' in compiler
    for target in ("x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc", "aarch64-apple-darwin"):
        assert target in compiler
    assert "--generate-hashes" in compiler

    bootstrap = (ROOT / "scripts" / "ci" / "bootstrap-e2e-linux.sh").read_text(encoding="utf-8")
    assert "0c7be696371069e0baa2515ed7aa08d5a5473aa6" in bootstrap
    assert "SPEECHSCOPE_WHISPERCPP_BIN=" in bootstrap
    assert "SPEECHSCOPE_WHISPERCPP_CACHE" in bootstrap
    assert "CACHED_REVISION" in bootstrap
    assert "--target whisper-cli" in bootstrap
    for prerequisite in ("build-essential", "cmake", "espeak-ng", "ffmpeg", "git"):
        assert prerequisite in bootstrap

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "GitHub-hosted" in readme
    assert "ubuntu-24.04" in readme
    assert "windows-2025" in readme
    assert "macos-15" in readme
    assert "requirements/e2e/locks/<platform>/<engine>.lock" in readme
    assert "E2E · All Engines (hosted)" in readme
    assert "ci-diagnostics.json" in readme
    assert "scripts/ci/bootstrap-e2e-linux.sh" in readme


def test_all_platform_bootstraps_verify_prerequisites_revision_and_executable():
    bootstrap_dir = ROOT / "scripts" / "ci"
    linux = (bootstrap_dir / "bootstrap-e2e-linux.sh").read_text(encoding="utf-8")
    macos = (bootstrap_dir / "bootstrap-e2e-macos.sh").read_text(encoding="utf-8")
    windows = (bootstrap_dir / "bootstrap-e2e-windows.ps1").read_text(encoding="utf-8")
    revision = "0c7be696371069e0baa2515ed7aa08d5a5473aa6"

    for bootstrap in (linux, macos, windows):
        assert revision in bootstrap
        assert "SPEECHSCOPE_WHISPERCPP_CACHE" in bootstrap
        assert "SPEECHSCOPE_WHISPERCPP_BIN" in bootstrap
        assert "whisper-cli" in bootstrap.lower()

    for prerequisite in ("cmake", "espeak-ng", "ffmpeg", "git"):
        assert prerequisite in linux
        assert prerequisite in macos
        assert prerequisite in windows
    assert "build-essential" in linux
    assert "brew install" in macos
    assert "xcode-select -p" in macos
    assert "-DGGML_METAL=OFF" in macos
    assert "choco install" in windows
    assert "CMake build failed" in windows
    assert "Could not discover whisper-cli.exe" in windows

    assert "Get-Command 'espeak-ng' -CommandType Application" in windows
    assert "IsPathFullyQualified($EspeakExecutable)" in windows
    assert "$EspeakExecutable --version" in windows
    assert "Test-Path $EspeakExecutable -PathType Leaf" in windows
    assert "throw \"espeak-ng failed its --version check (exit $LASTEXITCODE).\"" in windows
    assert "$env:ESPEAKNG_RUNTIME_EXECUTABLE = $EspeakExecutable" in windows

    github_env_block = windows.split("if ($env:GITHUB_ENV) {", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert "AppendAllText($env:GITHUB_ENV," in github_env_block
    assert "ESPEAKNG_RUNTIME_EXECUTABLE=$EspeakExecutable" in github_env_block

    github_path_block = windows.split("if ($env:GITHUB_PATH) {", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert "AppendAllText($env:GITHUB_PATH," in github_path_block
    assert "$(Split-Path -Parent $EspeakExecutable)" in github_path_block


def test_hf_token_is_forwarded_only_by_pocket_manual_workflow():
    pocket = (WORKFLOWS / "e2e-pocket.yml").read_text(encoding="utf-8")
    assert "HF_TOKEN: ${{ secrets.HF_TOKEN }}" in pocket

    for engine in ENGINES:
        if engine == "pocket":
            continue
        content = (WORKFLOWS / f"e2e-{engine}.yml").read_text(encoding="utf-8")
        assert "HF_TOKEN" not in content, f"unexpected HF_TOKEN forwarding for {engine}"


def test_reusable_workflow_declares_optional_hf_token_and_scopes_it_to_e2e_step():
    content = (WORKFLOWS / "_e2e-engine.yml").read_text(encoding="utf-8")
    assert re.search(
        r"(?ms)^    secrets:\n"
        r"      HF_TOKEN:\n"
        r"        required: false\s*$",
        content,
    )

    engine_e2e_block = (
        content
        .split("      - name: Run engine E2E\n", maxsplit=1)[1]
        .split("\n      - name:", maxsplit=1)[0]
    )
    assert "HF_TOKEN: ${{ secrets.HF_TOKEN }}" in engine_e2e_block
    assert content.count("HF_TOKEN: ${{ secrets.HF_TOKEN }}") == 1
    assert "secrets: inherit" not in content


def test_aggregate_workflow_forwards_hf_token_only_to_pocket_job():
    content = (WORKFLOWS / "e2e-all-engines.yml").read_text(encoding="utf-8")
    pocket_job = (
        content
        .split("  pocket:\n", maxsplit=1)[1]
        .split("\n  supertonic:\n", maxsplit=1)[0]
    )
    assert "HF_TOKEN: ${{ secrets.HF_TOKEN }}" in pocket_job
    assert content.count("HF_TOKEN: ${{ secrets.HF_TOKEN }}") == 1
    assert "secrets: inherit" not in content



def test_documentation_explains_voice_ownership_caches_and_qwen_transcript_gate():
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    profile_docs = " ".join(
        (ROOT / "profiles" / "README.md").read_text(encoding="utf-8").split()
    )

    assert "The seven validated default engines" in readme
    assert "Pocket and Sopro use VoiceFolio-managed cloning" in readme
    assert "Qwen3 has CPU dependency, runtime, and model-cache infrastructure" in readme
    assert "Do not invent a" in readme and "reference transcript" in readme
    assert ".ci-cache/voicefolio" in readme
    assert ".ci-cache/onnxvoice" in readme
    assert "reference-voice.json" in readme
    assert "The seven manually triggered per-engine workflows" in readme
    assert "The seven validated default profiles" in profile_docs
    assert "a named/predefined voice is not a cloned reference voice" in profile_docs
    assert "no authoritative exact transcript" in profile_docs
    assert "profiles-experimental/" in profile_docs
    assert "six manually triggered" not in readme

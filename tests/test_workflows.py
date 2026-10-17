from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ENGINES = ("inflect", "kitten", "kokoro", "piper", "pocket", "supertonic")


def test_each_validated_engine_has_one_manual_reusable_workflow():
    for engine in ENGINES:
        path = WORKFLOWS / f"e2e-{engine}.yml"
        assert path.is_file(), f"missing manual workflow for {engine}"
        content = path.read_text(encoding="utf-8")

        assert re.search(r"(?m)^on:\s*$", content)
        assert re.search(r"(?m)^  workflow_dispatch:\s*$", content)
        assert content.count("workflow_dispatch:") == 1
        assert re.findall(r"(?m)^\s+uses:\s+(.+?)\s*$", content) == ["./.github/workflows/_e2e-engine.yml"]
        assert re.findall(r"(?m)^\s+engine:\s*([a-z-]+)\s*$", content) == [engine]


def test_shared_engine_workflow_verifies_model_cache_and_uploads_cold_run_evidence():
    path = WORKFLOWS / "_e2e-engine.yml"
    assert path.is_file()
    content = path.read_text(encoding="utf-8")

    assert "workflow_call:" in content
    assert "runs-on: ubuntu-24.04" in content
    assert "SPEECHSCOPE_MODEL_HOME: ${{ github.workspace }}/.ci-cache/speechscope-models" in content
    assert "runs-on: self-hosted" not in content
    assert 'python-version: "3.12"' in content
    assert 'CUDA_VISIBLE_DEVICES: ""' in content
    assert "python -m pip install --require-hashes -r requirements-e2e.lock" in content
    assert "scripts/bootstrap-e2e-runtime.sh" in content
    assert content.index("python -m pip install --require-hashes") < content.index("scripts/bootstrap-e2e-runtime.sh") < content.index('speechscope models verify "$MODEL"') < content.index("bookbench doctor")
    assert "uses: actions/cache@" in content
    assert "path: ${{ env.SPEECHSCOPE_MODEL_HOME }}" in content
    assert "${{ runner.os }}-${{ runner.arch }}-${{ steps.asr.outputs.cache_key }}" in content
    assert 'model = speech["model"]' in content
    assert 'if a.get("primary")' in content
    assert "digest = primary[0][\"sha256\"]" in content
    assert '"speechscope-{os.uname().machine}-{slug}-{digest}"' in content
    assert 'speechscope models verify "$MODEL"' in content
    assert 'speechscope models install "$MODEL" --force' in content
    assert "rm -rf .bookbench/cache/voicerender" in content
    assert 'bookbench doctor --engine "${{ inputs.engine }}"' in content
    assert 'bookbench run \\\n            --engine "${{ inputs.engine }}"' in content
    assert "uses: actions/upload-artifact@" in content
    assert "path: .bookbench/runs/gha-${{ github.run_id }}-${{ github.run_attempt }}-${{ inputs.engine }}/" in content
    assert "continue-on-error" not in content


def test_existing_all_engine_workflow_is_optional_and_manual():
    path = WORKFLOWS / "e2e-self-hosted.yml"
    content = path.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in content
    assert "optional" in content.lower()
    assert "all engines" in content.lower()


def test_hosted_e2e_lock_and_whisper_bootstrap_are_pinned():
    requirements = (ROOT / "requirements-e2e.in").read_text(encoding="utf-8")
    lock = (ROOT / "requirements-e2e.lock").read_text(encoding="utf-8")
    for spec in (
        "text2epub==0.1.4",
        "ssmdconvert==0.2.0",
        "utterplan[spacy]==0.6.3",
        "voicerender==0.2.2",
        "audiocompose[voicerender]==0.3.1",
        "audioexport==0.1.2",
        "speechscope==0.1.1",
        "setuptools==84.0.0",
        "setuptools-scm==9.2.2",
        "inflectsynth[cpu]==0.1.1",
        "kittensynth[cpu]==0.1.5",
        "pykokoro[cpu]==0.10.5",
        "pipersynth[cpu]==0.2.5",
        "pocketsynth[cpu]==0.2.6",
        "supertonicsynth[cpu]==0.1.4",
    ):
        assert spec in requirements
        package = spec.split("==", maxsplit=1)[0].split("[", maxsplit=1)[0]
        lock_lines = lock.splitlines()
        package_line = next(i for i, line in enumerate(lock_lines) if line.startswith(f"{package}=="))
        assert lock_lines[package_line].endswith("\\")
        assert lock_lines[package_line + 1].strip().startswith("--hash=sha256:")

    assert "en_core_web_sm-3.8.0-py3-none-any.whl" in requirements
    model_line = next(i for i, line in enumerate(lock_lines) if line.startswith("en-core-web-sm @"))
    assert lock_lines[model_line + 1].strip().startswith("--hash=sha256:")
    bootstrap = (ROOT / "scripts" / "bootstrap-e2e-runtime.sh").read_text(encoding="utf-8")
    assert "0c7be696371069e0baa2515ed7aa08d5a5473aa6" in bootstrap
    assert "SPEECHSCOPE_WHISPERCPP_BIN=" in bootstrap
    assert "--target whisper-cli" in bootstrap
    for prerequisite in ("build-essential", "cmake", "espeak-ng", "ffmpeg", "git"):
        assert prerequisite in bootstrap

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "GitHub-hosted `ubuntu-24.04`" in readme
    assert "requirements-e2e.lock" in readme
    assert "scripts/bootstrap-e2e-runtime.sh" in readme

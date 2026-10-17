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
    assert "runs-on: self-hosted" in content
    assert "SPEECHSCOPE_MODEL_HOME: ${{ github.workspace }}/.ci-cache/speechscope-models" in content
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

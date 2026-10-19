from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci import run_e2e_doctor


def test_run_doctor_persists_complete_output_and_preserves_failure_status(tmp_path, monkeypatch, capsys):
    completed = subprocess.CompletedProcess(
        ["bookbench", "doctor", "--engine", "inflect"],
        returncode=1,
        stdout='{"ok": false}\n',
        stderr="profile validation failed\n",
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return completed

    monkeypatch.setattr(run_e2e_doctor.subprocess, "run", fake_run)
    evidence_file = tmp_path / "evidence" / "doctor.json"

    assert run_e2e_doctor.run_doctor("inflect", evidence_file) == 1

    assert calls == [
        (
            ["bookbench", "doctor", "--engine", "inflect"],
            {"text": True, "capture_output": True, "check": False},
        )
    ]
    assert json.loads(evidence_file.read_text(encoding="utf-8")) == {
        "schema": "bookbench.e2e-doctor.v1",
        "engine": "inflect",
        "command": ["bookbench", "doctor", "--engine", "inflect"],
        "returncode": 1,
        "stdout": '{"ok": false}\n',
        "stderr": "profile validation failed\n",
    }
    captured = capsys.readouterr()
    assert captured.out == '{"ok": false}\n'
    assert captured.err == "profile validation failed\n"


def test_run_doctor_records_launch_failure_without_raising(tmp_path, monkeypatch, capsys):
    def fail_to_launch(_command, **_kwargs):
        raise FileNotFoundError("bookbench not found")

    monkeypatch.setattr(run_e2e_doctor.subprocess, "run", fail_to_launch)
    evidence_file = tmp_path / "doctor.json"

    assert run_e2e_doctor.run_doctor("inflect", evidence_file) == 127

    report = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert report["returncode"] == 127
    assert report["stdout"] == ""
    assert report["stderr"] == "bookbench not found"
    assert capsys.readouterr().err == "bookbench not found"


def test_resolve_e2e_context_fingerprints_catalog_artifact_and_engine_profile(tmp_path, monkeypatch):
    import hashlib

    from scripts.ci import resolve_e2e_context

    (tmp_path / "bookbench.toml").write_text(
        '[speechscope]\nbackend = "whispercpp"\nmodel = "whispercpp:base"\ndevice = "cpu"\n',
        encoding="utf-8",
    )
    profile = tmp_path / "profiles" / "inflect.renderprofile.toml"
    profile.parent.mkdir()
    profile.write_text("profile = true\n", encoding="utf-8")
    artifact_sha = "a" * 64

    def fake_run(command, **kwargs):
        assert command == ["speechscope", "models", "show", "whispercpp:base", "--format", "json"]
        assert kwargs == {"check": True, "text": True, "capture_output": True}
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"artifacts": [{"primary": True, "sha256": artifact_sha}]}),
            stderr="",
        )

    monkeypatch.setattr(resolve_e2e_context.subprocess, "run", fake_run)
    monkeypatch.setattr(resolve_e2e_context.platform, "machine", lambda: "arm64")

    context = resolve_e2e_context.resolve_context("inflect", tmp_path)

    assert context == {
        "model": "whispercpp:base",
        "backend": "whispercpp",
        "device": "cpu",
        "architecture": "arm64",
        "cache_key": f"speechscope-arm64-whispercpp-base-{artifact_sha}",
        "profile_sha": hashlib.sha256(b"profile = true\n").hexdigest(),
    }


def test_ensure_speechscope_model_installs_and_reverifies_after_cache_miss(monkeypatch):
    from scripts.ci import ensure_speechscope_model

    calls = []
    returncodes = iter((1, 0, 0))

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        returncode = next(returncodes)
        return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(ensure_speechscope_model.subprocess, "run", fake_run)

    ensure_speechscope_model.ensure_model("whispercpp:base")

    assert calls == [
        (["speechscope", "models", "verify", "whispercpp:base"], {"check": False}),
        (["speechscope", "models", "install", "whispercpp:base", "--force"], {"check": True}),
        (["speechscope", "models", "verify", "whispercpp:base"], {"check": True}),
    ]


def test_ci_diagnostics_include_failure_metadata_without_serializing_secrets(tmp_path):
    from scripts.ci import write_e2e_diagnostics

    (tmp_path / "bookbench.toml").write_text(
        '[speechscope]\nbackend = "whispercpp"\nmodel = "whispercpp:base"\ndevice = "cpu"\n',
        encoding="utf-8",
    )
    profile = tmp_path / "profiles" / "inflect.renderprofile.toml"
    profile.parent.mkdir()
    profile.write_text("profile = true\n", encoding="utf-8")
    lock = tmp_path / "requirements/e2e/locks/linux-x64/inflect.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("inflectsynth==0.1.1\\n    --hash=sha256:abcd\\n", encoding="utf-8")
    output_dir = tmp_path / ".bookbench/evidence/linux-x64"
    output_dir.mkdir(parents=True)
    (output_dir / "doctor.json").write_text(
        json.dumps({"returncode": 1, "stderr": "api_incompatible"}),
        encoding="utf-8",
    )
    (output_dir / "engine-runtime.json").write_text(
        json.dumps({"engine": "inflect", "ok": True, "components": {}}, sort_keys=True),
        encoding="utf-8",
    )
    environment = {
        "GITHUB_RUN_ID": "12345",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "BOOKBENCH_CI_BOOTSTRAP_LINUX_OUTCOME": "failure",
        "BOOKBENCH_CI_DOCTOR_OUTCOME": "failure",
        "BOOKBENCH_CI_ENGINE_RUNTIME_OUTCOME": "success",
        "SPEECHSCOPE_WHISPERCPP_BIN": str(tmp_path / "missing-whisper-cli"),
        "GITHUB_TOKEN": "must-not-be-recorded",
    }

    report = write_e2e_diagnostics.capture_diagnostics(
        "inflect",
        "linux-x64",
        output_dir,
        root=tmp_path,
        env=environment,
    )

    report_path = output_dir / "ci-diagnostics.json"
    assert report_path.is_file()
    assert (output_dir / "README.md").is_file()
    serialized_report = report_path.read_text(encoding="utf-8")
    assert "must-not-be-recorded" not in serialized_report
    assert "GITHUB_TOKEN" not in serialized_report
    assert report["step_outcomes"] == {"bootstrap_linux": "failure", "engine_runtime": "success", "doctor": "failure"}
    files = report["files"]
    assert isinstance(files, dict)
    assert files["doctor_output"] is True
    assert files["doctor_returncode"] == 1
    assert files["engine_runtime_output"] is True
    assert files["engine_runtime_ok"] is True
    assert files["lock_sha256"] is not None
    runtime = report["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["whispercpp_executable_exists"] is False
    assert "BookBench E2E evidence" in (output_dir / "README.md").read_text(encoding="utf-8")


def test_sopro_diagnostics_capture_independent_caches_and_asset_status(tmp_path):
    from scripts.ci import write_e2e_diagnostics

    (tmp_path / "bookbench.toml").write_text(
        '[speechscope]\nbackend = "whispercpp"\nmodel = "whispercpp:base"\ndevice = "cpu"\n',
        encoding="utf-8",
    )
    profile = tmp_path / "profiles" / "sopro.renderprofile.toml"
    profile.parent.mkdir()
    profile.write_text("managed = true\n", encoding="utf-8")
    lock = tmp_path / "requirements/e2e/locks/linux-x64/sopro.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("soprosynth==0.1.1\n", encoding="utf-8")
    output_dir = tmp_path / ".bookbench/evidence/linux-x64"
    output_dir.mkdir(parents=True)
    (output_dir / "engine-assets.json").write_text(
        json.dumps({"status": "installed_and_verified", "cache_hit": False}),
        encoding="utf-8",
    )
    (output_dir / "reference-voice.json").write_text(
        json.dumps(
            {
                "status": "verified",
                "references": [{"ref": "kyutai-tts-voices:alba-mackenna/casual", "cache_hit": True}],
            }
        ),
        encoding="utf-8",
    )
    voicefolio_cache = tmp_path / "cache" / "voicefolio"
    onnxvoice_cache = tmp_path / "cache" / "onnxvoice"
    voicefolio_cache.mkdir(parents=True)
    onnxvoice_cache.mkdir(parents=True)
    environment = {
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "VOICEFOLIO_CACHE_DIR": str(voicefolio_cache),
        "ONNXVOICE_CACHE_DIR": str(onnxvoice_cache),
        "BOOKBENCH_CI_VOICEFOLIO_CACHE_OUTCOME": "success",
        "BOOKBENCH_CI_ENGINE_ASSET_CACHE_OUTCOME": "success",
        "BOOKBENCH_CI_ENGINE_ASSETS_OUTCOME": "success",
        "BOOKBENCH_CI_REFERENCE_VOICE_OUTCOME": "success",
        "BOOKBENCH_CI_VOICEFOLIO_CACHE_HIT": "true",
        "BOOKBENCH_CI_ENGINE_ASSET_CACHE_HIT": "false",
        "HF_TOKEN": "must-not-be-recorded",
    }

    report = write_e2e_diagnostics.capture_diagnostics(
        "sopro", "linux-x64", output_dir, root=tmp_path, env=environment
    )

    assert report["step_outcomes"] == {
        "voicefolio_cache": "success",
        "engine_asset_cache": "success",
        "engine_assets": "success",
        "reference_voice": "success",
    }
    assert report["cache_hits"] == {"voicefolio": "true", "onnxvoice": "false"}
    files = report["files"]
    assert files["voicefolio_cache_present"] is True
    assert files["onnxvoice_cache_present"] is True
    assert files["engine_assets_output"] is True
    assert files["engine_assets_status"] == "installed_and_verified"
    assert files["engine_assets_cache_hit"] is False
    assert files["reference_voice_output"] is True
    assert files["reference_voice_status"] == "verified"
    assert files["reference_voice_cache_hits"] == [True]
    assert "must-not-be-recorded" not in (output_dir / "ci-diagnostics.json").read_text(encoding="utf-8")


def test_merge_evidence_retains_available_platforms_and_records_matrix_failure(tmp_path):
    from scripts.ci import merge_e2e_evidence

    downloaded_root = tmp_path / "downloads"
    for platform_key in ("linux-x64", "macos-arm64"):
        artifact = downloaded_root / f"bookbench-inflect-{platform_key}-12345-2"
        artifact.mkdir(parents=True)
        (artifact / "README.md").write_text(f"{platform_key} evidence\n", encoding="utf-8")

    output_root = tmp_path / "merged"
    manifest = merge_e2e_evidence.merge_evidence("inflect", "12345", "2", downloaded_root, output_root, "failure")

    assert manifest["matrix_job_result"] == "failure"
    assert manifest["available_platforms"] == ["linux-x64", "macos-arm64"]
    assert manifest["missing_platforms"] == ["windows-x64"]
    assert (output_root / "platform-artifacts/linux-x64/README.md").is_file()
    assert "a successful merge does not change this result" in (output_root / "README.md").read_text(encoding="utf-8")


def test_merge_evidence_creates_manifest_even_when_no_platform_artifacts_exist(tmp_path):
    from scripts.ci import merge_e2e_evidence

    output_root = tmp_path / "merged-empty"
    manifest = merge_e2e_evidence.merge_evidence(
        "inflect", "12345", "1", tmp_path / "no-downloads", output_root, "cancelled"
    )

    assert manifest["available_platforms"] == []
    assert manifest["matrix_job_result"] == "cancelled"
    assert (output_root / "README.md").is_file()
    assert (output_root / "manifest.json").is_file()


def test_e2e_summary_links_platform_and_merged_artifacts(tmp_path):
    from scripts.ci import write_e2e_summary

    platform_summary = write_e2e_summary.render_summary(
        "platform",
        "inflect",
        "12345",
        "1",
        "https://example.test/artifact/1",
        platform="windows-x64",
        job_status="failure",
    )
    merged_summary = write_e2e_summary.render_summary(
        "merge",
        "inflect",
        "12345",
        "1",
        "https://example.test/artifact/2",
        matrix_result="failure",
    )
    assert "windows-x64" in platform_summary
    assert "Job status: `failure`" in platform_summary
    assert "Matrix result: `failure`" in merged_summary
    assert "https://example.test/artifact/2" in merged_summary

    summary_file = tmp_path / "summary.md"
    write_e2e_summary.write_summary(platform_summary, str(summary_file))
    write_e2e_summary.write_summary(merged_summary, str(summary_file))
    contents = summary_file.read_text(encoding="utf-8")
    assert "BookBench E2E: inflect / windows-x64" in contents
    assert "BookBench E2E merged evidence: inflect" in contents


def test_engine_runtime_probe_writes_kitten_component_json_without_environment_secrets(tmp_path, monkeypatch, capsys):
    from scripts.ci import check_engine_runtime

    monkeypatch.setenv("HF_TOKEN", "hf_fake_token_123456")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        module_name = command[-2]
        return subprocess.CompletedProcess(command, 0, json.dumps({"version": f"{module_name}-1.0"}), "")

    monkeypatch.setattr(check_engine_runtime.subprocess, "run", fake_run)
    output = tmp_path / "engine-runtime.json"
    monkeypatch.setattr(
        check_engine_runtime.sys,
        "argv",
        ["check_engine_runtime.py", "--engine", "kitten", "--output", str(output)],
    )

    assert check_engine_runtime.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["engine"] == "kitten"
    assert report["ok"] is True
    assert set(report["components"]) == {"kittensynth", "onnxvoice", "onnxruntime", "huggingface_hub"}
    assert all(item["status"] == "ready" for item in report["components"].values())
    assert capsys.readouterr().out == output.read_text(encoding="utf-8")
    assert len(calls) == 4
    for command, kwargs in calls:
        assert command[1] == "-I"
        assert kwargs["timeout"] == 30
        assert "HF_TOKEN" not in kwargs["env"]
    assert "hf_fake_token_123456" not in output.read_text(encoding="utf-8")


def test_engine_runtime_probe_distinguishes_missing_and_broken_imports_and_redacts_errors(monkeypatch):
    from scripts.ci import check_engine_runtime

    def fake_run(command, **kwargs):
        module_name = command[-2]
        if module_name == "kittensynth":
            result = {
                "error_type": "ModuleNotFoundError",
                "error_message": "No module named 'kittensynth'",
                "missing_name": "kittensynth",
            }
            return subprocess.CompletedProcess(command, 1, json.dumps(result), "")
        if module_name == "onnxvoice":
            result = {
                "error_type": "ImportError",
                "error_message": "bad import token=hf_FAKE_TOKEN_123456",
                "missing_name": None,
            }
            return subprocess.CompletedProcess(command, 1, json.dumps(result), "")
        return subprocess.CompletedProcess(command, 0, json.dumps({"version": "1.0"}), "")

    monkeypatch.setattr(check_engine_runtime.subprocess, "run", fake_run)
    report = check_engine_runtime.check_engine_runtime("kitten")

    assert report["ok"] is False
    assert report["components"]["kittensynth"]["status"] == "missing"
    assert report["components"]["onnxvoice"]["status"] == "broken"
    serialized = json.dumps(report)
    assert "hf_FAKE_TOKEN_123456" not in serialized
    assert "[REDACTED]" in serialized


def test_missing_huggingface_hub_writes_structured_failure_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    from scripts.ci import check_engine_runtime

    def fake_run(command, **kwargs):
        module_name = command[-2]
        if module_name == "huggingface_hub":
            result = {
                "error_type": "ModuleNotFoundError",
                "error_message": "No module named 'huggingface_hub'",
                "missing_name": "huggingface_hub",
            }
            return subprocess.CompletedProcess(command, 1, json.dumps(result), "")
        return subprocess.CompletedProcess(command, 0, json.dumps({"version": "1.0"}), "")

    monkeypatch.setattr(check_engine_runtime.subprocess, "run", fake_run)
    output = tmp_path / "engine-runtime.json"
    monkeypatch.setattr(
        check_engine_runtime.sys,
        "argv",
        ["check_engine_runtime.py", "--engine", "kitten", "--output", str(output)],
    )

    assert check_engine_runtime.main() == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["components"]["huggingface_hub"] == {
        "status": "missing",
        "error": {"type": "ModuleNotFoundError", "message": "No module named 'huggingface_hub'"},
    }
    assert json.loads(capsys.readouterr().out) == report


@pytest.mark.parametrize("engine", ("piper", "sopro", "qwen3"))
def test_engine_runtime_probe_uses_engine_specific_import_requirements(engine, monkeypatch):
    from scripts.ci import check_engine_runtime, engine_matrix

    imported = []

    def fake_run(command, **kwargs):
        imported.append(command[-2])
        return subprocess.CompletedProcess(command, 0, json.dumps({"version": "1.0"}), "")

    monkeypatch.setattr(check_engine_runtime.subprocess, "run", fake_run)
    report = check_engine_runtime.check_engine_runtime(engine)

    expected = {module for module, _distribution in engine_matrix.ENGINE_COMPONENTS[engine]}
    assert report["ok"] is True
    assert set(imported) == expected
    if engine == "qwen3":
        assert "huggingface_hub" not in imported
    from scripts.ci import check_engine_runtime

    imported = []

    def fake_run(command, **kwargs):
        imported.append(command[-2])
        return subprocess.CompletedProcess(command, 0, json.dumps({"version": "1.0"}), "")

    monkeypatch.setattr(check_engine_runtime.subprocess, "run", fake_run)
    report = check_engine_runtime.check_engine_runtime("piper")

    assert report["ok"] is True
    assert set(imported) == {"pipersynth", "onnxvoice", "onnxruntime", "piperg2p"}
    assert "huggingface_hub" not in imported



def test_engine_ci_metadata_separates_default_matrix_from_qwen_infrastructure():
    from scripts.ci import engine_matrix

    assert engine_matrix.DEFAULT_ENGINES == (
        "inflect",
        "kitten",
        "kokoro",
        "piper",
        "pocket",
        "sopro",
        "supertonic",
    )
    assert engine_matrix.LOCK_ENGINES == (
        "inflect",
        "kitten",
        "kokoro",
        "piper",
        "pocket",
        "qwen3",
        "sopro",
        "supertonic",
    )
    assert engine_matrix.ENGINE_PACKAGES["qwen3"] == "qwen3synth"
    assert engine_matrix.ENGINE_COMPONENTS["sopro"] == (
        ("soprosynth", "soprosynth"),
        ("onnxvoice", "onnxvoice"),
        ("onnxruntime", "onnxruntime"),
    )
    assert engine_matrix.ENGINE_COMPONENTS["qwen3"] == (
        ("qwen3synth", "qwen3synth"),
        ("onnxvoice", "onnxvoice"),
        ("onnxruntime", "onnxruntime"),
    )
    assert "huggingface_hub" not in dict(engine_matrix.ENGINE_COMPONENTS["qwen3"])
    assert engine_matrix.ENGINE_ASSETS == {
        "qwen3": ("qwen3:base-0.6b", "int4"),
        "sopro": ("sopro:v2-turbo", "wasm-uint8"),
    }
    assert "qwen3" not in engine_matrix.DEFAULT_ENGINES
    assert not (Path(__file__).parents[1] / "profiles" / "qwen3.renderprofile.toml").exists()


@pytest.mark.parametrize(
    ("engine", "model_ref", "quality"),
    (
        ("sopro", "sopro:v2-turbo", "wasm-uint8"),
        ("qwen3", "qwen3:base-0.6b", "int4"),
    ),
)
def test_ensure_engine_asset_installs_and_reverifies_cache_miss(
    engine, model_ref, quality, tmp_path
):
    from scripts.ci import ensure_engine_assets

    calls = []
    returncodes = iter((1, 0, 0))
    environment = {"PATH": "/bin", "ONNXVOICE_CACHE_DIR": str(tmp_path / "models")}

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, next(returncodes), "", "")

    report = ensure_engine_assets.ensure_engine_asset(engine, runner=fake_run, env=environment)

    assert [command for command, _kwargs in calls] == [
        ["onnxvoice", "verify", model_ref, "--quality", quality],
        ["onnxvoice", "install", model_ref, "--quality", quality],
        ["onnxvoice", "verify", model_ref, "--quality", quality],
    ]
    assert all(kwargs["env"] == environment for _command, kwargs in calls)
    assert report["status"] == "installed_and_verified"
    assert report["cache_hit"] is False
    assert report["cache_dir"] == str((tmp_path / "models").resolve())


def test_ensure_engine_asset_cache_hit_and_failure_evidence_are_non_secret(tmp_path):
    from scripts.ci import ensure_engine_assets

    calls = []
    environment = {
        "ONNXVOICE_CACHE_DIR": str(tmp_path / "models"),
        "HF_TOKEN": "hf_fake_token_123456",
    }

    def cached_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "verified", "")

    report = ensure_engine_assets.ensure_engine_asset("sopro", runner=cached_run, env=environment)
    serialized = ensure_engine_assets.write_report(report)

    assert [command for command, _kwargs in calls] == [
        ["onnxvoice", "verify", "sopro:v2-turbo", "--quality", "wasm-uint8"]
    ]
    assert report["status"] == "verified"
    assert report["cache_hit"] is True
    assert "hf_fake_token_123456" not in serialized

    returncodes = iter((1, 1))

    def failed_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, next(returncodes), "", "authorization=hf_fake_token_abcdef"
        )

    failure = ensure_engine_assets.ensure_engine_asset("sopro", runner=failed_run, env=environment)
    failure_json = ensure_engine_assets.write_report(failure)
    assert failure["status"] == "failed"
    assert failure["error"] == {"stage": "install", "returncode": 1, "message": "[REDACTED]"}
    assert "hf_fake_token_abcdef" not in failure_json


@pytest.mark.parametrize(
    ("phases", "expected_cache_hit"),
    (
        (("cache_check", "verify", "ready"), True),
        (("cache_check", "download", "verify", "ready"), False),
    ),
)
def test_prepare_reference_voice_reports_cache_state_without_paths_or_profile_mutation(
    phases, expected_cache_hit, tmp_path
):
    from types import SimpleNamespace

    from scripts.ci import prepare_reference_voice

    reference = "kyutai-tts-voices:alba-mackenna/casual"
    profile = tmp_path / "sopro.renderprofile.toml"
    profile.write_text(
        "[roles.narrator.binding.options.voice_source]\n"
        'kind = "managed_reference"\n'
        'resolver = "voicefolio"\n'
        f'value = "{reference}"\n',
        encoding="utf-8",
    )
    original_profile = profile.read_bytes()
    cache_dir = tmp_path / "cache" / "voicefolio"
    entry = SimpleNamespace(ref=reference, sha256="a" * 64, source_revision="b" * 40)

    def validator(source):
        assert source["value"] == reference
        return entry

    def resolver(source, *, cache_dir, offline, progress):
        assert source["value"] == reference
        assert cache_dir == tmp_path / "cache" / "voicefolio"
        assert offline is False
        for phase in phases:
            progress(SimpleNamespace(phase=phase))
        return SimpleNamespace(path=tmp_path / "local-reference.wav")

    report = prepare_reference_voice.prepare_reference_voice(
        "sopro", profile, cache_dir=cache_dir, validator=validator, resolver=resolver
    )
    output = tmp_path / "reference-voice.json"
    serialized = prepare_reference_voice.write_report(report, output)

    assert report["status"] == "verified"
    assert report["profile_sha256"] == hashlib.sha256(original_profile).hexdigest()
    assert report["cache_dir"] == str(cache_dir.resolve())
    assert report["references"] == [
        {
            "role": "narrator",
            "ref": reference,
            "sha256": "a" * 64,
            "source_revision": "b" * 40,
            "cache_hit": expected_cache_hit,
            "status": "verified",
        }
    ]
    assert serialized == prepare_reference_voice.write_report(report)
    assert output.read_text(encoding="utf-8") == serialized
    assert "local-reference.wav" not in serialized
    assert profile.read_bytes() == original_profile


def test_prepare_reference_voice_verification_failure_is_redacted(tmp_path):
    from types import SimpleNamespace

    import voicefolio

    from scripts.ci import prepare_reference_voice

    profile = tmp_path / "pocket.renderprofile.toml"
    profile.write_text(
        '[roles.narrator.binding.options.voice_source]\n'
        'kind = "managed_reference"\n'
        'resolver = "voicefolio"\n'
        'value = "kyutai-tts-voices:alba-mackenna/casual"\n',
        encoding="utf-8",
    )
    original_profile = profile.read_bytes()
    entry = SimpleNamespace(
        ref="kyutai-tts-voices:alba-mackenna/casual",
        sha256="a" * 64,
        source_revision="b" * 40,
    )

    def resolver(_source, *, cache_dir, offline, progress):
        progress(SimpleNamespace(phase="cache_check"))
        progress(SimpleNamespace(phase="verify"))
        raise voicefolio.IntegrityError("authorization=hf_fake_token_123456")

    report = prepare_reference_voice.prepare_reference_voice(
        "pocket",
        profile,
        validator=lambda _source: entry,
        resolver=resolver,
    )
    serialized = prepare_reference_voice.write_report(report)

    assert report["status"] == "failed"
    assert report["references"][0]["error_type"] == "IntegrityError"
    assert report["references"][0]["cache_hit"] is True
    assert "hf_fake_token_123456" not in serialized
    assert profile.read_bytes() == original_profile


def test_prepare_reference_voice_offline_miss_and_unknown_reference_fail_closed(tmp_path):
    from types import SimpleNamespace

    import voicefolio

    from scripts.ci import prepare_reference_voice

    profile = tmp_path / "pocket.renderprofile.toml"
    profile.write_text(
        '[roles.narrator.binding.options.voice_source]\n'
        'kind = "managed_reference"\n'
        'resolver = "voicefolio"\n'
        'value = "kyutai-tts-voices:alba-mackenna/casual"\n',
        encoding="utf-8",
    )
    entry = SimpleNamespace(
        ref="kyutai-tts-voices:alba-mackenna/casual",
        sha256="a" * 64,
        source_revision="b" * 40,
    )

    def offline_resolver(_source, *, cache_dir, offline, progress):
        assert offline is True
        progress(SimpleNamespace(phase="cache_check"))
        raise voicefolio.OfflineError("cache miss")

    offline_report = prepare_reference_voice.prepare_reference_voice(
        "pocket",
        profile,
        offline=True,
        validator=lambda _source: entry,
        resolver=offline_resolver,
    )
    assert offline_report["status"] == "failed"
    assert offline_report["references"][0]["error_type"] == "OfflineError"
    assert offline_report["references"][0]["cache_hit"] is False

    resolver_called = False
    profile.write_text(
        '[roles.narrator.binding.options.voice_source]\n'
        'kind = "managed_reference"\n'
        'resolver = "voicefolio"\n'
        'value = "kyutai-tts-voices:missing"\n',
        encoding="utf-8",
    )

    def unknown_validator(_source):
        assert _source["value"] == "kyutai-tts-voices:missing"
        raise voicefolio.VoiceNotFoundError("unknown ref with token=hf_fake_token_abcdef")

    def unused_resolver(*_args, **_kwargs):
        nonlocal resolver_called
        resolver_called = True
        raise AssertionError("unknown references must not be fetched")

    unknown_report = prepare_reference_voice.prepare_reference_voice(
        "pocket",
        profile,
        validator=unknown_validator,
        resolver=unused_resolver,
    )
    unknown_json = prepare_reference_voice.write_report(unknown_report)
    assert unknown_report["status"] == "failed"
    assert unknown_report["references"][0]["error_type"] == "VoiceNotFoundError"
    assert unknown_report["references"][0]["cache_hit"] is None
    assert resolver_called is False
    assert "hf_fake_token_abcdef" not in unknown_json


def test_prepare_reference_voice_skips_profiles_without_managed_references(tmp_path):
    from scripts.ci import prepare_reference_voice

    profile = tmp_path / "piper.renderprofile.toml"
    profile.write_text(
        '[roles.narrator.binding]\nrenderer = "piper"\nvoice = "en_US-lessac-medium"\n',
        encoding="utf-8",
    )
    report = prepare_reference_voice.prepare_reference_voice(
        "piper",
        profile,
        validator=lambda _source: (_ for _ in ()).throw(AssertionError("unexpected validation")),
        resolver=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected resolution")
        ),
    )
    assert report["status"] == "not_applicable"
    assert report["references"] == []

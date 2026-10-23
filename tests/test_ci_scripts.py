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
    profile.write_bytes(b"profile = true\r\n")
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


def test_ci_diagnostics_include_failure_metadata_without_serializing_secrets(tmp_path, monkeypatch):
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
        "ESPEAKNG_RUNTIME_EXECUTABLE": str(tmp_path / "missing-espeak-ng.exe"),
        "BOOKBENCH_PRIVATE_SECRET": "must-not-be-recorded-either",
    }
    path_espeak = tmp_path / "path-espeak-ng.exe"
    monkeypatch.setattr(
        write_e2e_diagnostics.shutil,
        "which",
        lambda command, path=None: str(path_espeak) if command == "espeak-ng" else None,
    )

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
    assert runtime["espeakng_executable"] == str(tmp_path / "missing-espeak-ng.exe")
    assert runtime["espeakng_executable_exists"] is False
    assert runtime["espeakng_path_lookup"] == str(path_espeak)
    assert report["safe_environment"]["ESPEAKNG_RUNTIME_EXECUTABLE"] == str(tmp_path / "missing-espeak-ng.exe")
    assert "GITHUB_TOKEN" not in report["safe_environment"]
    assert "BOOKBENCH_PRIVATE_SECRET" not in report["safe_environment"]
    assert "BookBench E2E evidence" in (output_dir / "README.md").read_text(encoding="utf-8")


def test_ci_diagnostics_capture_existing_espeak_path_without_leaking_lookup_errors(tmp_path, monkeypatch):
    from scripts.ci import write_e2e_diagnostics

    (tmp_path / "bookbench.toml").write_text(
        '[speechscope]\nbackend = "whispercpp"\nmodel = "whispercpp:base"\ndevice = "cpu"\n',
        encoding="utf-8",
    )
    profile = tmp_path / "profiles" / "inflect.renderprofile.toml"
    profile.parent.mkdir()
    profile.write_text("profile = true\\n", encoding="utf-8")
    lock = tmp_path / "requirements/e2e/locks/linux-x64/inflect.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("inflectsynth==0.1.2\\n", encoding="utf-8")
    executable = tmp_path / "espeak-ng.exe"
    executable.touch()
    output_dir = tmp_path / ".bookbench/evidence/linux-x64"
    environment = {
        "ESPEAKNG_RUNTIME_EXECUTABLE": str(executable),
        "HF_TOKEN": "hf_fake_secret_123456",
        "BOOKBENCH_PRIVATE_SECRET": "also-secret",
        "PATH": "",
    }

    def fail_lookup(command, path=None):
        if command == "espeak-ng":
            raise OSError("authorization=hf_fake_secret_123456")

    monkeypatch.setattr(write_e2e_diagnostics.shutil, "which", fail_lookup)
    report = write_e2e_diagnostics.capture_diagnostics(
        "inflect", "linux-x64", output_dir, root=tmp_path, env=environment
    )
    serialized = (output_dir / "ci-diagnostics.json").read_text(encoding="utf-8")

    assert report["runtime"]["espeakng_executable"] == str(executable)
    assert report["runtime"]["espeakng_executable_exists"] is True
    assert report["runtime"]["espeakng_path_lookup"] is None
    assert report["safe_environment"]["ESPEAKNG_RUNTIME_EXECUTABLE"] == str(executable)
    assert "HF_TOKEN" not in report["safe_environment"]
    assert "BOOKBENCH_PRIVATE_SECRET" not in report["safe_environment"]
    assert "hf_fake_secret_123456" not in serialized
    assert "also-secret" not in serialized
    assert "authorization=" not in serialized


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

    report = write_e2e_diagnostics.capture_diagnostics("sopro", "linux-x64", output_dir, root=tmp_path, env=environment)

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


def test_merge_evidence_accepts_every_default_engine(tmp_path):
    from scripts.ci import merge_e2e_evidence
    from scripts.ci.engine_matrix import DEFAULT_ENGINES

    downloaded_root = tmp_path / "downloads"
    for engine in DEFAULT_ENGINES:
        artifact = downloaded_root / f"bookbench-{engine}-linux-x64-12345-1"
        artifact.mkdir(parents=True)
        (artifact / "README.md").write_text(f"{engine} evidence\n", encoding="utf-8")

    for engine in DEFAULT_ENGINES:
        output_root = tmp_path / "merged" / engine
        manifest = merge_e2e_evidence.merge_evidence(engine, "12345", "1", downloaded_root, output_root, "success")
        assert manifest["engine"] == engine
        assert manifest["available_platforms"] == ["linux-x64"]


def _make_pages_input_tree(
    tmp_path,
    *,
    missing_engine=None,
):
    import hashlib
    import json

    from scripts.ci.engine_matrix import DEFAULT_ENGINES

    input_root = tmp_path / "pages-input"
    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir()
    for engine in DEFAULT_ENGINES:
        profile_id = hashlib.sha256(f"{engine}-profile".encode()).hexdigest()
        target_id = f"{engine}-target"
        profile_text = f'''default_role = "narrator"
format = "renderprofile"
profile_id = "sha256:{profile_id}"
schema_version = 1

[roles.narrator]
language = "en-US"

[roles.narrator.binding]
renderer = "{engine}"
target_id = "{target_id}"
'''
        if engine not in {"pocket", "sopro"}:
            profile_text += f'voice = "{engine}-voice"\n'
        else:
            profile_text += """
[roles.narrator.binding.options.voice_source]
kind = "managed_reference"
value = "kyutai-tts-voices:alba-mackenna/casual"
dataset = "alba-mackenna"
license = "CC-BY-4.0"
resolver = "voicefolio"
source_path = "alba-mackenna/casual.wav"
"""
        profile_bytes = profile_text.encode()
        (profiles_root / f"{engine}.renderprofile.toml").write_bytes(profile_bytes)
        original_sha = hashlib.sha256(profile_bytes).hexdigest()
        variant = f"{engine}--{profile_id[:12]}"
        if engine == missing_engine:
            continue
        artifact = input_root / f"bookbench-{engine}-linux-x64-12345-1"
        run_id = f"gha-12345-1-{engine}-linux-x64"
        run_dir = artifact / ".bookbench" / "runs" / run_id
        exports = run_dir / "exports"
        exports.mkdir(parents=True)
        (exports / f"{engine}.listen.mp3").write_bytes(f"mp3-{engine}".encode())
        result = {
            "profile": engine,
            "variant": variant,
            "engines": [engine],
            "status": "pass",
            "export": f"/runner/work/bookbench/{run_id}/exports/{engine}.m4b",
            "compound_wer": 0.0,
            "wer": 0.0,
            "cer": 0.0,
            "word_boundary_match_count": 0,
        }
        (run_dir / "summary.json").write_text(
            json.dumps({"run_id": run_id, "source": "/runner/work/private/fixture.md", "results": [result]}),
            encoding="utf-8",
        )
        (run_dir / "profiles.json").write_text(
            json.dumps(
                [
                    {
                        "candidate_label": engine,
                        "profile_label": engine,
                        "original_path": f"/runner/work/bookbench/profiles/{engine}.renderprofile.toml",
                        "original_sha256": original_sha,
                        "profile_id": f"sha256:{profile_id}",
                        "variant_label": engine,
                        "variant": variant,
                        "engines": [engine],
                        "targets": [{"engine_id": engine, "target_id": target_id}],
                        "staged_path": f"/runner/work/private/{engine}.toml",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (run_dir / "versions.json").write_text(json.dumps({"voicerender": "0.2.3"}), encoding="utf-8")
        (run_dir / "engines.json").write_text(
            json.dumps(
                {"selected_targets": [{"candidate_label": engine, "engine_id": engine, "target_id": target_id}]}
            ),
            encoding="utf-8",
        )
    return input_root, profiles_root


def _pages_run_dir(input_root, engine):
    run_id = f"gha-12345-1-{engine}-linux-x64"
    return input_root / f"bookbench-{engine}-linux-x64-12345-1" / ".bookbench" / "runs" / run_id


def _build_test_pages_site(input_root, profiles_root, output_root):
    from scripts.ci.build_pages_site import build_pages_site

    return build_pages_site(
        input_root=input_root,
        output_root=output_root,
        repository="buchwandler/bookbench",
        commit="a" * 40,
        run_id="12345",
        attempt="1",
        profiles_root=profiles_root,
    )


def test_pages_builder_complete_valid_input_succeeds_and_sanitizes_output(tmp_path):
    import json

    from scripts.ci.engine_matrix import DEFAULT_ENGINES

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    output_root = _build_test_pages_site(input_root, profiles_root, tmp_path / "site")
    manifest_text = (output_root / "gallery.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert [sample["engine"] for sample in manifest["samples"]] == list(DEFAULT_ENGINES)
    assert len(list((output_root / "samples").rglob("*.mp3"))) == len(DEFAULT_ENGINES)
    assert {path.name for path in output_root.iterdir()} == {"index.html", "gallery.json", "assets", "samples"}
    assert "/runner/work/" not in manifest_text
    assert str(tmp_path) not in manifest_text
    assert all("source_path" not in json.dumps(sample["bindings"]) for sample in manifest["samples"])
    for engine in ("pocket", "sopro"):
        sample = next(item for item in manifest["samples"] if item["engine"] == engine)
        source = sample["bindings"][0]["voice_source"]
        assert source["value"] == "kyutai-tts-voices:alba-mackenna/casual"
        assert source["license"] == "CC-BY-4.0"
        assert source["resolver"] == "voicefolio"
    page = (output_root / "index.html").read_text(encoding="utf-8")
    assert "voicefolio" in page  # resolver metadata is rendered from the tested profile
    assert "/runner/work/" not in page


def test_pages_builder_missing_engine_fails(tmp_path):
    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path, missing_engine="sopro")
    with pytest.raises(GalleryBuildError, match="missing expected engine artifact"):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "site")


def test_pages_builder_duplicate_engine_fails(tmp_path):
    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    duplicate = input_root / "bookbench-piper-linux-x64-12345-1-duplicate"
    duplicate.mkdir()
    with pytest.raises(GalleryBuildError, match="duplicate artifact for engine piper"):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "site")


def test_pages_builder_failed_result_fails(tmp_path):
    import json

    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    run_dir = _pages_run_dir(input_root, "sopro")
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["results"][0]["status"] = "fail"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(GalleryBuildError, match="result is not pass"):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "site")


@pytest.mark.parametrize("audio_state", ("missing", "empty"))
def test_pages_builder_missing_or_empty_audio_fails(tmp_path, audio_state):
    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    audio_path = _pages_run_dir(input_root, "piper") / "exports" / "piper.listen.mp3"
    if audio_state == "missing":
        audio_path.unlink()
        expected_error = "listening MP3 is missing or unsafe"
    else:
        audio_path.write_bytes(b"")
        expected_error = "listening MP3 is empty"
    with pytest.raises(GalleryBuildError, match=expected_error):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "site")


def test_pages_builder_mismatched_run_id_fails(tmp_path):
    import json

    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    run_dir = _pages_run_dir(input_root, "piper")
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["run_id"] = "gha-99999-1-piper-linux-x64"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(GalleryBuildError, match="BookBench run id does not match"):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "site")


def test_pages_builder_profile_sha_mismatch_fails(tmp_path):
    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    profile_path = profiles_root / "piper.renderprofile.toml"
    profile_path.write_text(profile_path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    with pytest.raises(GalleryBuildError, match="SHA-256 differs from tested profile"):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "site")


def test_pages_builder_traversal_profile_name_fails(tmp_path):
    import json

    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    run_dir = _pages_run_dir(input_root, "piper")
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["results"][0]["profile"] = "../x"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(GalleryBuildError, match="one safe path component"):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "site")


@pytest.mark.parametrize("tamper", ("profile_id", "engine"))
def test_pages_builder_rejects_malformed_identity_and_engine_mismatch(tmp_path, tamper):
    import json

    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    run_dir = _pages_run_dir(input_root, "piper")
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    profiles_path = run_dir / "profiles.json"
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
    if tamper == "profile_id":
        profiles[0]["profile_id"] = "sha256:invalid"
        expected_error = "profile id is missing or malformed"
        profiles_path.write_text(json.dumps(profiles), encoding="utf-8")
    else:
        summary["results"][0]["engines"] = ["sopro"]
        expected_error = "result engine does not match artifact engine"
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(GalleryBuildError, match=expected_error):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "site")


def test_pages_builder_rejects_symlinks_and_sample_size_over_limit(tmp_path, monkeypatch):
    from scripts.ci import build_pages_site
    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not follow", encoding="utf-8")
    symlink_path = _pages_run_dir(input_root, "piper") / "outside-link"
    try:
        symlink_path.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(GalleryBuildError, match="contains a symlink"):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "symlink-site")
    symlink_path.unlink()

    monkeypatch.setattr(build_pages_site, "MAX_SAMPLE_BYTES", 100)
    audio_path = _pages_run_dir(input_root, "piper") / "exports" / "piper.listen.mp3"
    audio_path.write_bytes(b"x" * 101)
    with pytest.raises(GalleryBuildError, match="per-sample limit"):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "large-site")


def test_pages_builder_size_guard_preserves_existing_output(tmp_path, monkeypatch):
    from scripts.ci import build_pages_site
    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    output_root = tmp_path / "site"
    output_root.mkdir()
    (output_root / "index.html").write_text("last known good", encoding="utf-8")
    monkeypatch.setattr(build_pages_site, "MAX_SITE_BYTES", 1)
    with pytest.raises(GalleryBuildError, match="generated site exceeds"):
        _build_test_pages_site(input_root, profiles_root, output_root)
    assert (output_root / "index.html").read_text(encoding="utf-8") == "last known good"


@pytest.mark.parametrize("missing_file", ("summary.json", "profiles.json"))
def test_pages_builder_missing_required_metadata_fails(tmp_path, missing_file):
    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    (_pages_run_dir(input_root, "piper") / missing_file).unlink()
    with pytest.raises(GalleryBuildError, match=f"{missing_file} is missing"):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "site")


def test_pages_builder_total_sample_budget_fails_closed(tmp_path, monkeypatch):
    from scripts.ci import build_pages_site
    from scripts.ci.build_pages_site import GalleryBuildError

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    monkeypatch.setattr(build_pages_site, "MAX_TOTAL_SAMPLE_BYTES", 10)
    with pytest.raises(GalleryBuildError, match="total sample payload exceeds"):
        _build_test_pages_site(input_root, profiles_root, tmp_path / "site")


def test_pages_builder_discovers_artifacts_with_upload_root_stripping_bookbench(tmp_path):
    from scripts.ci.engine_matrix import DEFAULT_ENGINES

    input_root, profiles_root = _make_pages_input_tree(tmp_path)
    for engine in DEFAULT_ENGINES:
        artifact = input_root / f"bookbench-{engine}-linux-x64-12345-1"
        (artifact / ".bookbench" / "runs").rename(artifact / "runs")
        (artifact / ".bookbench").rmdir()
    output_root = _build_test_pages_site(input_root, profiles_root, tmp_path / "site")
    assert (output_root / "gallery.json").is_file()


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


def test_inflect_runtime_probe_accepts_explicit_espeak_path(tmp_path, monkeypatch):
    from scripts.ci import check_engine_runtime

    executable = tmp_path / "espeak-ng"
    executable.touch()
    monkeypatch.setenv("ESPEAKNG_RUNTIME_EXECUTABLE", str(executable))
    monkeypatch.setattr(
        check_engine_runtime,
        "_component_result",
        lambda _module, _distribution: {"status": "ready", "version": "1.0"},
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "eSpeak NG", "")

    monkeypatch.setattr(check_engine_runtime.subprocess, "run", fake_run)
    report = check_engine_runtime.check_engine_runtime("inflect")

    assert report["ok"] is True
    assert report["executables"]["espeak-ng"] == {
        "status": "ready",
        "path": str(executable),
        "source": "ESPEAKNG_RUNTIME_EXECUTABLE",
    }
    assert calls[0][0] == [str(executable), "--version"]
    assert calls[0][1]["timeout"] == check_engine_runtime.EXECUTABLE_PROBE_TIMEOUT
    assert calls[0][1]["env"]["ESPEAKNG_RUNTIME_EXECUTABLE"] == str(executable)


def test_inflect_runtime_probe_rejects_invalid_explicit_espeak_path_without_path_fallback(tmp_path, monkeypatch):
    from scripts.ci import check_engine_runtime

    configured_path = tmp_path / "missing-espeak-ng"
    monkeypatch.setenv("ESPEAKNG_RUNTIME_EXECUTABLE", str(configured_path))
    monkeypatch.setattr(
        check_engine_runtime,
        "_component_result",
        lambda _module, _distribution: {"status": "ready", "version": "1.0"},
    )
    monkeypatch.setattr(
        check_engine_runtime.shutil,
        "which",
        lambda _command: pytest.fail("invalid explicit path must not fall back to PATH"),
    )
    monkeypatch.setattr(
        check_engine_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid explicit path must not run"),
    )

    report = check_engine_runtime.check_engine_runtime("inflect")

    assert report["ok"] is False
    assert report["executables"]["espeak-ng"]["status"] == "missing"
    assert report["executables"]["espeak-ng"]["source"] == "ESPEAKNG_RUNTIME_EXECUTABLE"
    assert report["executables"]["espeak-ng"]["error"]["type"] == "ExecutableNotFound"


def test_inflect_runtime_probe_resolves_espeak_from_path_when_no_override(tmp_path, monkeypatch):
    from scripts.ci import check_engine_runtime

    executable = tmp_path / "espeak-ng"
    executable.touch()
    monkeypatch.delenv("ESPEAKNG_RUNTIME_EXECUTABLE", raising=False)
    monkeypatch.setattr(
        check_engine_runtime,
        "_component_result",
        lambda _module, _distribution: {"status": "ready", "version": "1.0"},
    )
    monkeypatch.setattr(check_engine_runtime.shutil, "which", lambda command: str(executable))
    monkeypatch.setattr(
        check_engine_runtime.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "eSpeak NG", ""),
    )

    report = check_engine_runtime.check_engine_runtime("inflect")

    assert report["ok"] is True
    assert report["executables"]["espeak-ng"]["status"] == "ready"
    assert report["executables"]["espeak-ng"]["source"] == "PATH"
    assert report["executables"]["espeak-ng"]["path"] == str(executable)


def test_inflect_runtime_probe_reports_structured_missing_espeak(tmp_path, monkeypatch):
    from scripts.ci import check_engine_runtime

    monkeypatch.delenv("ESPEAKNG_RUNTIME_EXECUTABLE", raising=False)
    monkeypatch.setattr(
        check_engine_runtime,
        "_component_result",
        lambda _module, _distribution: {"status": "ready", "version": "1.0"},
    )
    monkeypatch.setattr(check_engine_runtime.shutil, "which", lambda _command: None)

    report = check_engine_runtime.check_engine_runtime("inflect")

    assert report["ok"] is False
    assert report["executables"]["espeak-ng"] == {
        "status": "missing",
        "path": None,
        "source": "PATH",
        "error": {
            "type": "ExecutableNotFound",
            "message": "Required executable 'espeak-ng' was not found.",
        },
    }


def test_inflect_executable_probe_does_not_leak_exception_messages(tmp_path, monkeypatch):
    from scripts.ci import check_engine_runtime

    executable = tmp_path / "espeak-ng"
    executable.touch()
    monkeypatch.setenv("ESPEAKNG_RUNTIME_EXECUTABLE", str(executable))
    monkeypatch.setattr(
        check_engine_runtime,
        "_component_result",
        lambda _module, _distribution: {"status": "ready", "version": "1.0"},
    )

    def fail_to_run(_command, **_kwargs):
        raise OSError("authorization=hf_fake_secret_123456")

    monkeypatch.setattr(check_engine_runtime.subprocess, "run", fail_to_run)
    report = check_engine_runtime.check_engine_runtime("inflect")
    serialized = json.dumps(report)

    assert report["ok"] is False
    assert report["executables"]["espeak-ng"]["status"] == "broken"
    assert "hf_fake_secret_123456" not in serialized
    assert "authorization=" not in serialized


@pytest.mark.parametrize("engine", ("kitten", "piper", "sopro", "qwen3"))
def test_non_inflect_engines_have_no_espeak_runtime_requirement(engine, monkeypatch):
    from scripts.ci import check_engine_runtime

    monkeypatch.setattr(
        check_engine_runtime,
        "_component_result",
        lambda _module, _distribution: {"status": "ready", "version": "1.0"},
    )

    report = check_engine_runtime.check_engine_runtime(engine)

    assert report["ok"] is True
    assert report["executables"] == {}


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
def test_ensure_engine_asset_installs_and_reverifies_cache_miss(engine, model_ref, quality, tmp_path):
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
        return subprocess.CompletedProcess(command, next(returncodes), "", "authorization=hf_fake_token_abcdef")

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
        "[roles.narrator.binding.options.voice_source]\n"
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
        "[roles.narrator.binding.options.voice_source]\n"
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
        "[roles.narrator.binding.options.voice_source]\n"
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
        resolver=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected resolution")),
    )
    assert report["status"] == "not_applicable"
    assert report["references"] == []

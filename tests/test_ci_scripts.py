from __future__ import annotations

import json
import subprocess

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
    environment = {
        "GITHUB_RUN_ID": "12345",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "BOOKBENCH_CI_BOOTSTRAP_LINUX_OUTCOME": "failure",
        "BOOKBENCH_CI_DOCTOR_OUTCOME": "failure",
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
    assert report["step_outcomes"] == {"bootstrap_linux": "failure", "doctor": "failure"}
    assert report["step_outcomes"] == {"bootstrap_linux": "failure", "doctor": "failure"}
    files = report["files"]
    assert isinstance(files, dict)
    assert files["doctor_output"] is True
    assert files["doctor_returncode"] == 1
    assert files["lock_sha256"] is not None
    runtime = report["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["whispercpp_executable_exists"] is False
    assert "BookBench E2E evidence" in (output_dir / "README.md").read_text(encoding="utf-8")


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

import hashlib
import json
import posixpath
from pathlib import Path

import pytest

from bookbench.config import Config, VoiceCandidate
from bookbench.profiles import ProfileCase, inspect_profile
from bookbench.runner import CommandResult, Harness, PipelineError

PROFILE_ID = "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"


def _write_profile(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f'# Exact profile bytes\nprofile_id = "{PROFILE_ID}"\n\n[roles.narrator.binding]\nrenderer = "piper"\n'
    ).encode()
    path.write_bytes(content)
    return content


def _config(tmp_path: Path, profile_path: Path) -> Config:
    source = tmp_path / "source.md"
    source.write_text("# Smoke fixture\n", encoding="utf-8")
    config_path = tmp_path / "bookbench.toml"
    config_path.write_text(
        f"source = {json.dumps(str(source))}\n"
        f"profiles_dir = {json.dumps(str(profile_path.parent))}\n"
        f"work_root = {json.dumps(str(tmp_path / 'runs'))}\n"
        f"cache_dir = {json.dumps(str(tmp_path / 'cache'))}\n"
        'required_engines = ["piper"]\n\n'
        '[[voices]]\nname = "voice-a-candidate"\n'
        f"profile = {json.dumps(str(profile_path))}\n",
        encoding="utf-8",
    )
    return Config(
        source=source,
        profiles_dir=profile_path.parent,
        work_root=tmp_path / "runs",
        cache_dir=tmp_path / "cache",
        required_engines=("piper",),
        voices=(VoiceCandidate(name="voice-a-candidate", profile=profile_path),),
        config_path=config_path,
    )


def test_stage_profile_is_byte_exact_inside_renderplan_and_idempotent(tmp_path):
    profile_path = tmp_path / "profiles" / "My Voice!.renderprofile.toml"
    original = _write_profile(profile_path)
    case = ProfileCase("candidate", inspect_profile(profile_path))
    book = tmp_path / "book.ssmdbook"
    harness = Harness(Config())

    staged = harness._stage_profile(book, case)

    assert staged.is_relative_to(book / "renderplan")
    assert staged.read_bytes() == original
    assert case.variant in staged.parts

    variant_dir = book / "renderplan" / case.variant
    provenance = posixpath.relpath(staged, variant_dir)
    assert not Path(provenance).is_absolute()
    assert "\\" not in provenance
    assert posixpath.normpath(provenance) == provenance
    assert (variant_dir / provenance).resolve() == staged.resolve()
    try:
        from renderplan import validate_posix_relative_reference
    except ImportError:
        pass
    else:
        assert validate_posix_relative_reference(provenance, allow_parent=True) == provenance
    assert harness._stage_profile(book, case) == staged
    assert staged.read_bytes() == original


def test_stage_profile_rejects_a_different_existing_file(tmp_path):
    profile_path = tmp_path / "My Voice!.renderprofile.toml"
    _write_profile(profile_path)
    case = ProfileCase("candidate", inspect_profile(profile_path))
    book = tmp_path / "book.ssmdbook"
    staged = book / "renderplan" / "_bookbench_profiles" / case.variant / profile_path.name
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"different profile")

    with pytest.raises(PipelineError, match="Staged RenderProfile collision"):
        Harness(Config())._stage_profile(book, case)


def test_external_renderprofile_is_staged_before_utterplan(tmp_path, monkeypatch):
    profile_path = tmp_path / "profiles" / "My Voice!.renderprofile.toml"
    _write_profile(profile_path)
    profile = inspect_profile(profile_path)
    harness = Harness(_config(tmp_path, profile_path), dry_run=True)
    original_run = harness._run
    render_attempts = 0

    def run_with_replan(argv, *, check=True):
        nonlocal render_attempts
        result = original_run(argv, check=check)
        if result.argv[:2] == ["voicerender", "render-book"]:
            render_attempts += 1
            if render_attempts == 1:
                result.returncode = 1
        return result

    monkeypatch.setattr(harness, "_run", run_with_replan)
    run_dir, results = harness.run(run_id="dry-run")

    assert results[0].status == "pass"
    assert render_attempts == 2
    utterplan = next(result.argv for result in harness.commands if result.argv[:2] == ["utterplan", "render-book"])
    staged = run_dir / "book.ssmdbook" / "renderplan" / "_bookbench_profiles" / profile.variant / profile_path.name
    assert utterplan[utterplan.index("--profile") + 1] == str(staged)
    assert utterplan[utterplan.index("--profile") + 1] != str(profile_path)
    assert utterplan[utterplan.index("--variant") + 1] == profile.variant_label

    downstream = [
        result.argv
        for result in harness.commands
        if tuple(result.argv[:2])
        in {
            ("voicerender", "render-book"),
            ("utterplan", "replan-book"),
            ("audiocompose", "compose-book"),
            ("audioexport", "export-book"),
        }
    ]
    assert {args[args.index("--variant") + 1] for args in downstream} == {profile.variant}


def test_renderplan_expected_text_lookup_uses_full_variant(tmp_path, monkeypatch):
    profile_path = tmp_path / "profiles" / "My Voice!.renderprofile.toml"
    _write_profile(profile_path)
    profile = inspect_profile(profile_path)
    harness = Harness(_config(tmp_path, profile_path))
    monkeypatch.setattr(harness, "versions", dict)
    looked_up = []

    def renderplan_text(variant_dir):
        looked_up.append(Path(variant_dir))
        return "Expected spoken text.\n"

    import bookbench.runner as runner_module

    monkeypatch.setattr(runner_module, "renderplan_text", renderplan_text)

    def fake_run(argv, *, check=True):
        args = [str(value) for value in argv]
        result = CommandResult(args, 0, 0.0, "", "")
        harness.commands.append(result)
        if args[:2] == ["speechscope", "analyze"]:
            report = Path(args[args.index("--json") + 1])
            report.write_text(json.dumps({"metrics": {}, "evaluation": {"status": "pass"}}))
        return result

    monkeypatch.setattr(harness, "_run", fake_run)
    run_dir, results = harness.run(run_id="reference-test")

    assert results[0].status == "pass"
    assert looked_up == [run_dir / "book.ssmdbook" / "renderplan" / profile.variant]

    run_data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    profile_data = json.loads((run_dir / "profiles.json").read_text(encoding="utf-8"))[0]
    assert run_data["bookbench_version"]
    assert run_data["python_version"]
    assert run_data["platform"]
    assert run_data["config_path"] == str(harness.config.config_path)
    assert run_data["config_sha256"] == hashlib.sha256(harness.config.config_path.read_bytes()).hexdigest()
    assert run_data["source_sha256"] == hashlib.sha256(harness.config.source.read_bytes()).hexdigest()
    assert run_data["selected_chapters"] == "all"
    assert run_data["dry_run"] is False
    assert run_data["start_timestamp"] and run_data["end_timestamp"]
    assert profile_data["candidate_label"] == "voice-a-candidate"
    assert profile_data["original_path"] == str(profile_path.resolve())
    assert profile_data["original_sha256"] == hashlib.sha256(profile_path.read_bytes()).hexdigest()
    assert profile_data["profile_id"] == profile.profile_id
    assert profile_data["variant_label"] == profile.variant_label
    assert profile_data["variant"] == profile.variant
    assert profile_data["engines"] == ["piper"]
    assert profile_data["staged_sha256"] == profile_data["original_sha256"]
    assert Path(profile_data["staged_path"]).read_bytes() == profile_path.read_bytes()


def test_profile_failure_records_stage_separately(tmp_path, monkeypatch):
    profile_path = tmp_path / "profiles" / "piper.renderprofile.toml"
    _write_profile(profile_path)
    harness = Harness(_config(tmp_path, profile_path), dry_run=True)
    original_run = harness._run

    def fail_utterplan(argv, *, check=True):
        if [str(value) for value in argv][:2] == ["utterplan", "render-book"]:
            raise PipelineError("source_profile must be a normalized relative POSIX reference")
        return original_run(argv, check=check)

    monkeypatch.setattr(harness, "_run", fail_utterplan)
    run_dir, results = harness.run(run_id="failed-stage")

    assert results[0].failed_stage == "renderplan"
    assert "source_profile" in results[0].error
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["results"][0]["failed_stage"] == "renderplan"


def test_commands_log_records_argv_outputs_exit_and_duration(tmp_path, monkeypatch):
    import subprocess

    import bookbench.runner as runner_module

    harness = Harness(Config())
    harness.run_dir = tmp_path

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 7, "command output\n", "command error\n")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    result = harness._run(["sample-tool", "--flag"], check=False)
    monkeypatch.setattr(runner_module.shutil, "which", lambda tool: "/tools/text2epub" if tool == "text2epub" else None)
    harness.versions()
    log = (tmp_path / "commands.log").read_text(encoding="utf-8")

    assert result.returncode == 7
    assert "$ sample-tool --flag" in log
    assert "[stdout]\ncommand output\n" in log
    assert "[stderr]\ncommand error\n" in log
    assert "[exit=7 duration=" in log
    assert "$ text2epub version" in log
    assert len(harness.commands) == 2


def test_versions_use_tool_specific_probes_and_expose_nonzero_exit(monkeypatch):
    import subprocess

    import bookbench.runner as runner_module

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "text2epub":
            return subprocess.CompletedProcess(argv, 2, "usage output", "unsupported version flag\n")
        return subprocess.CompletedProcess(argv, 0, f"{argv[0]} version 1\n", "")

    monkeypatch.setattr(runner_module.shutil, "which", lambda tool: f"/tools/{tool}")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    versions = Harness(Config()).versions()

    assert calls[0] == ("text2epub", "version")
    assert versions["text2epub"] == "probe_failed(exit=2): unsupported version flag"
    assert ("ffmpeg", "-version") in calls
    assert all((tool, "--version") in calls for tool in runner_module.TOOLS if tool != "ffmpeg" and tool != "text2epub")


def test_doctor_reports_profile_validation_duplicates_source_and_workspace(tmp_path, monkeypatch):
    import subprocess

    import bookbench.runner as runner_module

    first_path = tmp_path / "profiles-a" / "same.renderprofile.toml"
    second_path = tmp_path / "profiles-b" / "same.renderprofile.toml"
    _write_profile(first_path)
    _write_profile(second_path)
    source = tmp_path / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    config = Config(
        source=source,
        work_root=tmp_path / "new-work-root",
        required_engines=("piper",),
        voices=(
            VoiceCandidate(name="candidate-a", profile=first_path),
            VoiceCandidate(name="candidate-b", profile=second_path),
        ),
    )
    harness = Harness(config)
    validation_commands = []
    monkeypatch.setattr(runner_module.shutil, "which", lambda tool: f"/tools/{tool}")
    monkeypatch.setattr(harness, "versions", lambda: {tool: "version 1" for tool in runner_module.TOOLS})

    def fake_run(argv, **kwargs):
        args = [str(value) for value in argv]
        validation_commands.append(args)
        return subprocess.CompletedProcess(args, 0, "profile valid", "")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    diagnostics = harness.doctor()

    assert diagnostics["profiles"][0]["validation"]["status"] == "passed"
    assert diagnostics["profiles"][0]["staged_profile_target"].endswith(
        "/renderplan/_bookbench_profiles/same--1234567890ab/same.renderprofile.toml"
    )
    assert len(diagnostics["duplicate_variants"]) == 1
    assert diagnostics["duplicate_variants"][0]["variant"] == "same--1234567890ab"
    assert diagnostics["source"]["exists"] is True
    assert diagnostics["work_root"]["writable"] is True
    assert diagnostics["ok"] is False
    assert len(validation_commands) == 2
    assert all(command[:3] == ["voicerender", "profile", "validate"] for command in validation_commands)

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
        f'# Exact profile bytes\nprofile_id = "{PROFILE_ID}"\n\n[roles.narrator.binding]\nrenderer = "piper"\ntarget_id = "en_US-amy-medium"\n'
    ).encode()
    path.write_bytes(content)
    return content


def _config(tmp_path: Path, profile_path: Path, *, max_replans: int = 3) -> Config:
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
        max_replans=max_replans,
        config_path=config_path,
    )




def _failure_item(
    *,
    chapter_id="chapter-0001",
    render_plan_id="render-plan-0001",
    error_code="preflight.reject",
    error_message="unknown capacity",
    replan_required=False,
):
    return {
        "id": chapter_id,
        "render_plan_id": render_plan_id,
        "status": "failed",
        "error_code": error_code,
        "error_message": error_message,
        "replan_required": replan_required,
        "preflight_report": "chapter-0001/preflight.toml",
    }


def _run_voice_render_scenario(
    tmp_path,
    monkeypatch,
    render_results,
    *,
    max_replans=3,
    replan_returncode=0,
):
    import bookbench.runner as runner_module

    profile_path = tmp_path / "profiles" / "piper.renderprofile.toml"
    _write_profile(profile_path)
    harness = Harness(_config(tmp_path, profile_path, max_replans=max_replans))
    monkeypatch.setattr(harness, "versions", dict)
    monkeypatch.setattr(runner_module, "renderplan_text", lambda _variant_dir: "Expected speech.\n")
    queued_results = iter(render_results)
    commands = []

    def fake_run(argv, *, check=True):
        args = [str(value) for value in argv]
        if args[:2] == ["voicerender", "render-book"]:
            response = next(queued_results, (0, "", ""))
            result = CommandResult(args, response[0], 0.0, response[1], response[2])
        elif args[:2] == ["utterplan", "replan-book"]:
            result = CommandResult(args, replan_returncode, 0.0, "", "replan rejected" if replan_returncode else "")
        else:
            result = CommandResult(args, 0, 0.0, "", "")
        if args[:2] == ["speechscope", "analyze"]:
            report_path = Path(args[args.index("--json") + 1])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps({"metrics": {}, "evaluation": {"status": "pass"}}), encoding="utf-8")
        harness.commands.append(result)
        commands.append(result)
        return result

    monkeypatch.setattr(harness, "_run", fake_run)
    run_dir, results = harness.run(run_id="strict-voice-render")
    return harness, run_dir, results, commands


def _render_failure(items):
    return 1, json.dumps({"items": items}), ""

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
                result.stdout = json.dumps({"items": [_failure_item(error_code="preflight.replan", error_message="capacity overflow", replan_required=True)]})
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





def test_hard_voicerender_reject_is_not_replanned_and_preserves_diagnostics(tmp_path, monkeypatch):
    item = _failure_item(error_message="capacity unknown; no safe replan exists")
    _, run_dir, results, commands = _run_voice_render_scenario(tmp_path, monkeypatch, [_render_failure([item])])

    result = results[0]
    assert result.status == "error"
    assert result.failed_stage == "voicerender"
    assert not any(command.argv[:2] == ["utterplan", "replan-book"] for command in commands)
    for field in (
        '"error_code": "preflight.reject"',
        '"error_message": "capacity unknown; no safe replan exists"',
        '"replan_required": false',
        '"preflight_report": "chapter-0001/preflight.toml"',
        '"chapter_id": "chapter-0001"',
        '"render_plan_id": "render-plan-0001"',
    ):
        assert field in result.error
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["results"][0]["failed_stage"] == "voicerender"
    report = (run_dir / "listen.html").read_text(encoding="utf-8")
    assert "hard VoiceRender preflight reject" in report
    assert "capacity unknown" in report


def test_valid_voicerender_replan_retries_once_then_succeeds(tmp_path, monkeypatch):
    item = _failure_item(
        error_code="preflight.replan", error_message="capacity overflow", replan_required=True
    )
    _, _, results, commands = _run_voice_render_scenario(
        tmp_path, monkeypatch, [_render_failure([item])], max_replans=1
    )

    assert results[0].status == "pass"
    assert sum(command.argv[:2] == ["voicerender", "render-book"] for command in commands) == 2
    assert sum(command.argv[:2] == ["utterplan", "replan-book"] for command in commands) == 1


def test_mixed_voicerender_failure_set_is_not_replanned(tmp_path, monkeypatch):
    replan = _failure_item(
        chapter_id="chapter-replan",
        error_code="preflight.replan",
        error_message="capacity overflow",
        replan_required=True,
    )
    reject = _failure_item(
        chapter_id="chapter-reject",
        error_code="preflight.reject",
        error_message="capacity unknown",
        replan_required=False,
    )
    _, _, results, commands = _run_voice_render_scenario(
        tmp_path, monkeypatch, [_render_failure([replan, reject])]
    )

    assert results[0].failed_stage == "voicerender"
    assert "chapter-replan" in results[0].error
    assert "chapter-reject" in results[0].error
    assert not any(command.argv[:2] == ["utterplan", "replan-book"] for command in commands)


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        "[]",
        json.dumps({"status": "failed"}),
        json.dumps({"items": [{"status": "failed", "error_code": "preflight.replan", "replan_required": True}]}),
    ],
    ids=["missing-stdout", "invalid-json", "wrong-payload-type", "missing-items", "incomplete-failed-item"],
)
def test_malformed_voicerender_output_fails_closed(tmp_path, monkeypatch, stdout):
    _, _, results, commands = _run_voice_render_scenario(tmp_path, monkeypatch, [(1, stdout, "")])

    assert results[0].failed_stage == "voicerender"
    assert "not safely replanable" in results[0].error
    assert not any(command.argv[:2] == ["utterplan", "replan-book"] for command in commands)


def test_replan_budget_exhaustion_preserves_last_voicerender_diagnostic(tmp_path, monkeypatch):
    first = _failure_item(
        chapter_id="chapter-first",
        error_code="preflight.replan",
        error_message="first capacity overflow",
        replan_required=True,
    )
    last = _failure_item(
        chapter_id="chapter-last",
        render_plan_id="render-plan-last",
        error_code="preflight.replan",
        error_message="last capacity overflow",
        replan_required=True,
    )
    _, _, results, commands = _run_voice_render_scenario(
        tmp_path,
        monkeypatch,
        [_render_failure([first]), _render_failure([last])],
        max_replans=1,
    )

    assert results[0].failed_stage == "voicerender"
    assert "last capacity overflow" in results[0].error
    assert "render-plan-last" in results[0].error
    assert sum(command.argv[:2] == ["voicerender", "render-book"] for command in commands) == 2
    assert sum(command.argv[:2] == ["utterplan", "replan-book"] for command in commands) == 1


def test_replan_command_failure_is_attributed_to_replan(tmp_path, monkeypatch):
    item = _failure_item(
        error_code="preflight.replan", error_message="capacity overflow", replan_required=True
    )
    _, _, results, _ = _run_voice_render_scenario(
        tmp_path, monkeypatch, [_render_failure([item])], replan_returncode=1
    )

    assert results[0].failed_stage == "replan"
    assert "capacity overflow" in results[0].error
    assert "replan rejected" in results[0].error
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

    monkeypatch.setattr(
        harness,
        "_probe_engine_inventory",
        lambda: ([{"id": "piper", "package": "pipersynth", "installed": True, "installed_version": "0.2.5"}], None),
    )
    monkeypatch.setattr(
        runner_module,
        "_installed_piper_package_versions",
        lambda: {"voicerender": "0.1.1", "pipersynth": "0.2.5", "onnxvoice": "0.2.9", "piperg2p": "0.1.7"},
    )
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
    assert profile_data["targets"] == [{"engine_id": "piper", "target_id": "en_US-amy-medium"}]
    assert profile_data["staged_sha256"] == profile_data["original_sha256"]
    assert Path(profile_data["staged_path"]).read_bytes() == profile_path.read_bytes()
    engine_data = json.loads((run_dir / "engines.json").read_text(encoding="utf-8"))
    assert engine_data["schema"] == "bookbench.engine-provenance.v1"
    assert engine_data["status"] == "captured"
    assert engine_data["engines"][0]["id"] == "piper"
    assert engine_data["package_versions"] == {
        "voicerender": "0.1.1",
        "pipersynth": "0.2.5",
        "onnxvoice": "0.2.9",
        "piperg2p": "0.1.7",
    }
    assert engine_data["selected_targets"] == [
        {"candidate_label": "voice-a-candidate", "engine_id": "piper", "target_id": "en_US-amy-medium"}
    ]


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
    monkeypatch.setattr(
        harness,
        "_probe_engine_inventory",
        lambda: ([{"id": "piper", "package": "pipersynth", "installed": True, "installed_version": "0.2.5"}], None),
    )
    monkeypatch.setattr(
        runner_module,
        "_installed_piper_package_versions",
        lambda: {"voicerender": "0.1.1", "pipersynth": "0.2.5", "onnxvoice": "0.2.9", "piperg2p": "0.1.7"},
    )
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
    engine_provenance = diagnostics["engine_provenance"]
    assert engine_provenance["status"] == "captured"
    assert engine_provenance["package_versions"] == {
        "voicerender": "0.1.1",
        "pipersynth": "0.2.5",
        "onnxvoice": "0.2.9",
        "piperg2p": "0.1.7",
    }
    assert engine_provenance["selected_targets"] == [
        {"candidate_label": "candidate-a", "engine_id": "piper", "target_id": "en_US-amy-medium"},
        {"candidate_label": "candidate-b", "engine_id": "piper", "target_id": "en_US-amy-medium"},
    ]
    assert len(validation_commands) == 2
    assert all(command[:3] == ["voicerender", "profile", "validate"] for command in validation_commands)

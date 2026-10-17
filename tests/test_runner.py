import hashlib
import json
import posixpath
from pathlib import Path

import pytest

from bookbench.config import Config, SpeechScopeConfig, UtterPlanConfig, VoiceCandidate
from bookbench.profiles import ProfileCase, ProfileSelection, inspect_profile
from bookbench.runner import CommandResult, Harness, PipelineError, ProfileRunContext

PROFILE_ID = "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"


def _write_profile(
    path: Path,
    *,
    engine: str = "piper",
    target_id: str = "en_US-amy-medium",
    token_fields: tuple[str, ...] = (),
) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f'# Exact profile bytes\nprofile_id = "{PROFILE_ID}"\n\n'
        f'[roles.narrator.binding]\nrenderer = "{engine}"\ntarget_id = "{target_id}"\n'
    )
    if token_fields:
        content += f"\n[roles.narrator.requirements]\ntoken_fields = {json.dumps(token_fields)}\n"
    content_bytes = content.encode()
    path.write_bytes(content_bytes)
    return content_bytes

def _config(
    tmp_path: Path,
    profile_path: Path,
    *,
    max_replans: int = 3,
    speechscope: SpeechScopeConfig | None = None,
) -> Config:
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
        speechscope=speechscope if speechscope is not None else SpeechScopeConfig(),
    )



def _selection_config(
    tmp_path: Path,
    *,
    entries: tuple[tuple[str, str], ...] = (("piper", "piper"), ("kitten", "kitten")),
    required_engines: tuple[str, ...] = ("piper", "kitten"),
    token_fields_by_label: dict[str, tuple[str, ...]] | None = None,
    utterplan: UtterPlanConfig | None = None,
) -> Config:
    source = tmp_path / "source.md"
    source.write_text("# Smoke fixture\n", encoding="utf-8")
    profiles_dir = tmp_path / "profiles"
    voices = []
    token_fields_by_label = token_fields_by_label or {}
    for label, engine in entries:
        profile_path = profiles_dir / f"{label}.renderprofile.toml"
        _write_profile(
            profile_path,
            engine=engine,
            target_id=f"{engine}-target",
            token_fields=token_fields_by_label.get(label, ()),
        )
        voices.append(VoiceCandidate(name=label, profile=profile_path))
    return Config(
        source=source,
        profiles_dir=profiles_dir,
        work_root=tmp_path / "runs",
        cache_dir=tmp_path / "cache",
        required_engines=required_engines,
        voices=tuple(voices),
        utterplan=utterplan or UtterPlanConfig(),
    )



def _speechscope_run_context(tmp_path: Path) -> ProfileRunContext:
    profile_path = tmp_path / "profiles" / "piper.renderprofile.toml"
    _write_profile(profile_path)
    profile = ProfileCase("candidate", inspect_profile(profile_path))
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    return ProfileRunContext(
        profile=profile,
        staged_profile=profile_path,
        variant_dir=tmp_path / "variant",
        export_path=export_dir / "candidate.m4b",
        listening_path=export_dir / "candidate.listen.mp3",
        validation_wav=export_dir / "candidate.validation.wav",
        expected_text=export_dir / "candidate.expected.txt",
        policy_path=export_dir / "candidate.speechscope-policy.json",
        speechscope_report=export_dir / "candidate.speechscope.json",
    )


@pytest.mark.parametrize(
    ("strict_wer", "expected_keys"),
    [(None, {"max_compound_wer", "max_cer"}), (0.0, {"max_compound_wer", "max_wer", "max_cer"})],
)
def test_speechscope_policy_serializes_enabled_thresholds(tmp_path, strict_wer, expected_keys):
    context = _speechscope_run_context(tmp_path)
    config = Config(speechscope=SpeechScopeConfig(max_wer=strict_wer))
    Harness(config, dry_run=True)._run_speechscope(context)

    policy = json.loads(context.policy_path.read_text(encoding="utf-8"))
    assert set(policy) == expected_keys | {"schema_version", "missing_timing"}
    assert policy["max_compound_wer"] == 0.0
    assert policy["max_cer"] == 0.0
    if strict_wer is None:
        assert "max_wer" not in policy
    else:
        assert policy["max_wer"] == strict_wer

def test_run_speechscope_passes_configured_backend_device_and_model(tmp_path):
    context = _speechscope_run_context(tmp_path)
    config = Config(
        speechscope=SpeechScopeConfig(
            backend="whispercpp",
            device="cpu",
            model="whispercpp:tiny.en-q5_1",
        )
    )

    checked, _data = Harness(config, dry_run=True)._run_speechscope(context)

    assert checked.argv[checked.argv.index("--backend"):][:2] == ["--backend", "whispercpp"]
    assert checked.argv[checked.argv.index("--device"):][:2] == ["--device", "cpu"]
    assert checked.argv[checked.argv.index("--model"):][:2] == ["--model", "whispercpp:tiny.en-q5_1"]



def test_speechscope_failure_message_names_failed_rules():
    from bookbench.runner import _speechscope_failure_message

    message = _speechscope_failure_message(
        {"rules": [{"name": "max_compound_wer", "observed": 0.0526, "expected": 0.0, "status": "fail"}]}
    )

    assert "max_compound_wer" in message
    assert "observed=0.0526" in message
    assert "expected=0.0" in message



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
    speechscope_payload=None,
    speechscope=None,
    speechscope_returncode=0,
):
    import bookbench.runner as runner_module

    profile_path = tmp_path / "profiles" / "piper.renderprofile.toml"
    _write_profile(profile_path)
    harness = Harness(_config(tmp_path, profile_path, max_replans=max_replans, speechscope=speechscope))
    monkeypatch.setattr(
        harness,
        "_probe_speechscope_capabilities",
        lambda: {
            "status": "supported",
            "command": ["speechscope", "metrics", "list", "--format", "json"],
            "metrics": ["wer", "compound_wer", "cer"],
            "supports_compound_wer": True,
            "supports_max_compound_wer": True,
            "error": None,
        },
    )
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
            report_path.write_text(json.dumps(speechscope_payload or {"metrics": {}, "evaluation": {"status": "pass"}}), encoding="utf-8")
            result.returncode = speechscope_returncode
        harness.commands.append(result)
        commands.append(result)
        return result

    monkeypatch.setattr(harness, "_run", fake_run)
    run_dir, results = harness.run(run_id="strict-voice-render")
    return harness, run_dir, results, commands



def test_piper_boundary_variant_passes_with_nonzero_strict_wer(tmp_path, monkeypatch):
    payload = {
        "metrics": {
            "wer": 2 / 19,
            "compound_wer": 0.0,
            "cer": 0.0,
            "content_match": True,
            "word_boundary_match_count": 1,
        },
        "evaluation": {
            "status": "pass",
            "rules": [
                {"name": "max_compound_wer", "observed": 0.0, "expected": 0.0, "status": "pass"},
                {"name": "max_cer", "observed": 0.0, "expected": 0.0, "status": "pass"},
            ],
        },
    }
    _, run_dir, results, _ = _run_voice_render_scenario(
        tmp_path, monkeypatch, [], speechscope_payload=payload
    )

    result = results[0]
    assert result.status == "pass"
    assert result.failed_stage is None
    assert result.wer == pytest.approx(2 / 19)
    assert result.compound_wer == 0.0
    assert result.cer == 0.0
    assert result.content_match is True
    assert result.word_boundary_match_count == 1

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["speechscope_gate"] == {"max_compound_wer": 0.0, "max_cer": 0.0}
    assert summary["results"][0]["wer"] == pytest.approx(2 / 19)
    assert summary["results"][0]["compound_wer"] == 0.0
    assert summary["results"][0]["content_match"] is True
    assert summary["results"][0]["word_boundary_match_count"] == 1



def test_true_lexical_error_fails_closed_with_rule_details(tmp_path, monkeypatch):
    payload = {
        "metrics": {
            "wer": 1 / 19,
            "compound_wer": 1 / 19,
            "cer": 0.012987012987012988,
            "content_match": False,
            "word_boundary_match_count": 0,
        },
        "evaluation": {
            "status": "fail",
            "rules": [
                {
                    "name": "max_compound_wer",
                    "observed": 1 / 19,
                    "expected": 0.0,
                    "status": "fail",
                }
            ],
        },
    }
    _, _, results, _ = _run_voice_render_scenario(
        tmp_path, monkeypatch, [], speechscope_payload=payload, speechscope_returncode=1
    )

    assert results[0].status == "fail"
    assert results[0].failed_stage == "speechscope"
    assert "max_compound_wer" in results[0].error


def test_explicit_strict_wer_still_fails_boundary_variant(tmp_path, monkeypatch):
    payload = {
        "metrics": {
            "wer": 2 / 19,
            "compound_wer": 0.0,
            "cer": 0.0,
            "content_match": True,
            "word_boundary_match_count": 1,
        },
        "evaluation": {
            "status": "fail",
            "rules": [
                {"name": "max_compound_wer", "observed": 0.0, "expected": 0.0, "status": "pass"},
                {"name": "max_wer", "observed": 2 / 19, "expected": 0.0, "status": "fail"},
                {"name": "max_cer", "observed": 0.0, "expected": 0.0, "status": "pass"},
            ],
        },
    }
    _, run_dir, results, _ = _run_voice_render_scenario(
        tmp_path,
        monkeypatch,
        [],
        speechscope_payload=payload,
        speechscope_returncode=1,
        speechscope=SpeechScopeConfig(max_wer=0.0),
    )

    assert results[0].status == "fail"
    assert results[0].failed_stage == "speechscope"
    assert "max_wer" in results[0].error
    policy_path = run_dir / "exports" / "voice-a-candidate.speechscope-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    assert policy["max_compound_wer"] == 0.0
    assert policy["max_wer"] == 0.0
    assert policy["max_cer"] == 0.0

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
    speechscope = SpeechScopeConfig(
        backend="whispercpp",
        device="cpu",
        model="whispercpp:tiny.en-q5_1",
        normalization="basic-v1",
        reference_mode="renderplan",
    )
    harness = Harness(_config(tmp_path, profile_path, speechscope=speechscope))
    monkeypatch.setattr(
        harness,
        "_probe_speechscope_capabilities",
        lambda: {
            "status": "supported",
            "command": ["speechscope", "metrics", "list", "--format", "json"],
            "metrics": ["compound_wer"],
            "supports_compound_wer": True,
            "supports_max_compound_wer": True,
            "error": None,
        },
    )
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
    assert run_data["speechscope_capabilities"]["supports_max_compound_wer"] is True
    assert run_data["speechscope"] == {
        "backend": "whispercpp",
        "device": "cpu",
        "model": "whispercpp:tiny.en-q5_1",
        "normalization": "basic-v1",
        "reference_mode": "renderplan",
    }
    summary_data = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary_data["speechscope"] == run_data["speechscope"]
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
    monkeypatch.setattr(
        harness,
        "_probe_speechscope_capabilities",
        lambda: {
            "status": "supported",
            "command": ["speechscope", "metrics", "list", "--format", "json"],
            "metrics": ["compound_wer"],
            "supports_compound_wer": True,
            "supports_max_compound_wer": True,
            "error": None,
        },
    )
    monkeypatch.setattr(
        harness,
        "_probe_speechscope_backend",
        lambda: {
            "ok": True,
            "backend": config.speechscope.backend,
            "device": config.speechscope.device,
            "model": config.speechscope.model,
            "runtime": {},
            "model_store": {},
            "error": None,
        },
    )

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
    assert diagnostics["speechscope_capabilities"]["supports_max_compound_wer"] is True
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



def test_speechscope_capability_probe_uses_metric_listing(monkeypatch):
    import subprocess

    import bookbench.runner as runner_module

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(["wer", "compound_wer", "cer"]), "")

    monkeypatch.setattr(runner_module.shutil, "which", lambda tool: "/tools/speechscope" if tool == "speechscope" else None)
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    capabilities = Harness(Config())._probe_speechscope_capabilities()

    assert calls == [["speechscope", "metrics", "list", "--format", "json"]]
    assert capabilities["status"] == "supported"
    assert capabilities["supports_compound_wer"] is True
    assert capabilities["supports_max_compound_wer"] is True

def test_speechscope_backend_probe_reports_configured_readiness(monkeypatch):
    import subprocess

    import bookbench.runner as runner_module

    config = Config(
        speechscope=SpeechScopeConfig(
            backend="whispercpp",
            device="cpu",
            model="whispercpp:tiny.en-q5_1",
        )
    )
    calls = []
    payload = {"ok": True, "runtime": {"available": True}, "model_store": {"verified": True}}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(runner_module.shutil, "which", lambda tool: "/tools/speechscope" if tool == "speechscope" else None)
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    readiness = Harness(config)._probe_speechscope_backend()

    assert calls == [[
        "speechscope", "doctor", "--backend", "whispercpp", "--model", "whispercpp:tiny.en-q5_1",
        "--device", "cpu", "--format", "json",
    ]]
    assert readiness["ok"] is True
    assert readiness["status"] == "ready"
    assert readiness["backend"] == "whispercpp"
    assert readiness["device"] == "cpu"
    assert readiness["model"] == "whispercpp:tiny.en-q5_1"
    assert readiness["runtime"] == {"available": True}
    assert readiness["model_store"] == {"verified": True}


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected_status", "expected_error"),
    [
        (0, '{"ok": false, "error": "model is not installed"}', "", "not_ready", "model is not installed"),
        (1, '{"ok": false, "errors": ["invalid backend/model"]}', "", "not_ready", "invalid backend/model"),
        (0, "not json", "", "probe_failed", "invalid SpeechScope readiness response"),
    ],
)
def test_speechscope_backend_probe_fails_closed(monkeypatch, returncode, stdout, stderr, expected_status, expected_error):
    import subprocess

    import bookbench.runner as runner_module

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(runner_module.shutil, "which", lambda tool: "/tools/speechscope" if tool == "speechscope" else None)
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    readiness = Harness(Config())._probe_speechscope_backend()

    assert readiness["ok"] is False
    assert readiness["status"] == expected_status
    assert readiness["error"]
    assert expected_error in readiness["error"]



def test_run_rejects_unsupported_compound_gate_before_synthesis(tmp_path, monkeypatch):
    profile_path = tmp_path / "profiles" / "piper.renderprofile.toml"
    _write_profile(profile_path)
    harness = Harness(_config(tmp_path, profile_path))
    monkeypatch.setattr(
        harness,
        "_probe_speechscope_capabilities",
        lambda: {
            "status": "unsupported",
            "command": ["speechscope", "metrics", "list", "--format", "json"],
            "metrics": ["wer", "cer"],
            "supports_compound_wer": False,
            "supports_max_compound_wer": False,
            "error": "SpeechScope does not report the compound_wer metric",
        },
    )

    with pytest.raises(PipelineError, match="max_compound_wer.*compound_wer support"):
        harness.run(run_id="unsupported-speechscope")

    assert harness.commands == []
    run_data = json.loads((harness.run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_data["failed_stage"] == "speechscope_capability"
    assert "max_compound_wer" in run_data["error"]



def test_full_matrix_still_requires_every_configured_engine_before_preparation(tmp_path):
    config = _selection_config(
        tmp_path,
        entries=(("piper", "piper"),),
        required_engines=("piper", "kitten"),
    )
    harness = Harness(config, dry_run=True)

    with pytest.raises(PipelineError, match="Missing required engine profiles"):
        harness.run(run_id="incomplete-matrix")

    assert harness.run_dir is None
    assert harness.commands == []
    assert not config.work_root.exists()


def test_selected_run_ignores_unselected_required_engines_and_records_provenance(tmp_path):
    harness = Harness(_selection_config(tmp_path), dry_run=True)
    run_dir, results = harness.run(
        run_id="selected-piper",
        selection=ProfileSelection(engines=("piper",)),
    )

    assert [result.profile for result in results] == ["piper"]
    run_data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    selection = run_data["selection"]
    assert selection == {
        "mode": "engine",
        "requested": ["piper"],
        "selected_profiles": ["piper"],
        "selected_variants": ["piper--1234567890ab"],
        "selected_engines": ["piper"],
        "full_matrix": False,
    }
    profile_data = json.loads((run_dir / "profiles.json").read_text(encoding="utf-8"))
    assert [profile["candidate_label"] for profile in profile_data] == ["piper"]
    assert profile_data[0]["targets"] == [
        {"engine_id": "piper", "target_id": "piper-target"}
    ]
    engine_data = json.loads((run_dir / "engines.json").read_text(encoding="utf-8"))
    assert engine_data["selected_targets"] == [
        {"candidate_label": "piper", "engine_id": "piper", "target_id": "piper-target"}
    ]
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["selection"] == selection
    report = (run_dir / "listen.html").read_text(encoding="utf-8")
    assert "Selection: engine=piper" in report
    assert "Profiles: piper; engines: piper" in report

    compile_args = next(command.argv for command in harness.commands if command.argv[:2] == ["utterplan", "compile-book"])
    export_args = next(command.argv for command in harness.commands if command.argv[:2] == ["ssmdconvert", "txt"])
    assert compile_args[compile_args.index("--spacy") + 1] == "off"
    assert export_args[export_args.index("--spacy") + 1] == "off"
    assert run_data["utterplan_linguistics"] == {
        "required_token_fields": [],
        "requires_spacy": False,
        "configured_spacy": "auto",
        "spacy_policy": "off",
        "reason": "no_rich_token_requirements",
        "status": "ready",
        "error": None,
    }


def test_kokoro_selection_uses_required_spacy_and_records_provenance(tmp_path):
    config = _selection_config(
        tmp_path,
        entries=(("kokoro", "kokoro"),),
        required_engines=("kokoro",),
        token_fields_by_label={"kokoro": ("span", "pos", "tag")},
    )
    harness = Harness(config, dry_run=True)
    run_dir, results = harness.run(run_id="selected-kokoro")

    assert [result.profile for result in results] == ["kokoro"]
    compile_args = next(command.argv for command in harness.commands if command.argv[:2] == ["utterplan", "compile-book"])
    export_args = next(command.argv for command in harness.commands if command.argv[:2] == ["ssmdconvert", "txt"])
    assert compile_args[compile_args.index("--spacy") + 1] == "sm"
    assert export_args[export_args.index("--spacy") + 1] == "off"
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["utterplan_linguistics"] == {
        "required_token_fields": ["pos", "span", "tag"],
        "requires_spacy": True,
        "configured_spacy": "auto",
        "spacy_policy": "sm",
        "reason": "selected_profile_requirements",
        "status": "ready",
        "error": None,
    }



def test_run_json_captures_actual_utterplan_provider_metadata(tmp_path, monkeypatch):
    config = _selection_config(
        tmp_path,
        entries=(("kokoro", "kokoro"),),
        required_engines=("kokoro",),
        token_fields_by_label={"kokoro": ("pos", "tag")},
    )
    harness = Harness(config, dry_run=True)
    original_run = harness._run

    def write_utterplan_metadata(argv, *, check=True):
        result = original_run(argv, check=check)
        if result.argv[:2] == ["utterplan", "compile-book"]:
            plan_dir = Path(result.argv[2]) / "utterplan"
            plan_dir.mkdir(parents=True)
            (plan_dir / "chapter-0001.utterplan.toml").write_text(
                '[[linguistics]]\nlanguage = "en-US"\nprovider = "spacy"\n'
                'model = "en_core_web_sm"\nprovider_version = "3.8.16"\nmodel_version = "3.8.0"\n',
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(harness, "_run", write_utterplan_metadata)
    run_dir, _results = harness.run(run_id="utterplan-provider-provenance")
    linguistics = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["utterplan_linguistics"]

    assert linguistics["plans"] == [
        {
            "plan": "utterplan/chapter-0001.utterplan.toml",
            "language": "en-US",
            "provider": "spacy",
            "model": "en_core_web_sm",
            "provider_version": "3.8.16",
            "model_version": "3.8.0",
        }
    ]


@pytest.mark.parametrize("policy", ("sm", "md", "lg", "trf"))
def test_configured_spacy_tier_is_used_for_rich_profile(tmp_path, policy):
    config = _selection_config(
        tmp_path,
        entries=(("kokoro", "kokoro"),),
        required_engines=("kokoro",),
        token_fields_by_label={"kokoro": ("pos", "tag")},
        utterplan=UtterPlanConfig(spacy=policy),
    )
    harness = Harness(config, dry_run=True)
    harness.run(run_id=f"kokoro-{policy}")

    compile_args = next(command.argv for command in harness.commands if command.argv[:2] == ["utterplan", "compile-book"])
    assert compile_args[compile_args.index("--spacy") + 1] == policy


def test_full_six_profile_matrix_uses_union_of_selected_requirements(tmp_path):
    engines = ("inflect", "kitten", "kokoro", "piper", "pocket", "supertonic")
    config = _selection_config(
        tmp_path,
        entries=tuple((engine, engine) for engine in engines),
        required_engines=engines,
        token_fields_by_label={"kokoro": ("span", "pos", "tag")},
    )
    harness = Harness(config, dry_run=True)
    _run_dir, results = harness.run(run_id="full-six-profile-matrix")

    assert len(results) == 6
    compile_args = next(command.argv for command in harness.commands if command.argv[:2] == ["utterplan", "compile-book"])
    assert compile_args[compile_args.index("--spacy") + 1] == "sm"


def test_spacy_off_conflict_fails_before_compilation(tmp_path):
    config = _selection_config(
        tmp_path,
        entries=(("kokoro", "kokoro"),),
        required_engines=("kokoro",),
        token_fields_by_label={"kokoro": ("pos", "tag")},
        utterplan=UtterPlanConfig(spacy="off"),
    )
    harness = Harness(config, dry_run=True)

    with pytest.raises(PipelineError) as error:
        harness.run(run_id="kokoro-spacy-off")

    assert "selected profiles kokoro" in str(error.value)
    assert "pos, tag" in str(error.value)
    assert 'utterplan.spacy = "off"' in str(error.value)
    assert harness.run_dir is None
    assert harness.commands == []
    assert not config.work_root.exists()


def test_selected_profile_runs_only_the_exact_candidate(tmp_path):
    harness = Harness(_selection_config(tmp_path), dry_run=True)
    run_dir, results = harness.run(
        run_id="selected-kitten-profile",
        selection=ProfileSelection(profiles=("kitten",)),
    )

    assert [result.profile for result in results] == ["kitten"]
    profile_data = json.loads((run_dir / "profiles.json").read_text(encoding="utf-8"))
    assert [profile["candidate_label"] for profile in profile_data] == ["kitten"]
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["selection"]["mode"] == "profile"


def test_unknown_engine_selection_fails_before_run_directory_or_commands(tmp_path):
    config = _selection_config(tmp_path)
    harness = Harness(config, dry_run=True)

    with pytest.raises(ValueError, match="Unknown or unconfigured engine selector"):
        harness.run(run_id="unknown-engine", selection=ProfileSelection(engines=("kokoro",)))

    assert harness.run_dir is None
    assert harness.commands == []
    assert not config.work_root.exists()


def _stub_doctor_dependencies(monkeypatch, harness, *, backend_ok=True):
    import subprocess

    import bookbench.runner as runner_module

    monkeypatch.setattr(runner_module.shutil, "which", lambda tool: f"/tools/{tool}")
    monkeypatch.setattr(harness, "versions", lambda: {tool: "version 1" for tool in runner_module.TOOLS})
    monkeypatch.setattr(
        harness,
        "_probe_speechscope_capabilities",
        lambda: {"supports_max_compound_wer": True},
    )
    speech = harness.config.speechscope
    monkeypatch.setattr(
        harness,
        "_probe_speechscope_backend",
        lambda: {
            "ok": backend_ok,
            "backend": speech.backend,
            "device": speech.device,
            "model": speech.model,
            "runtime": {},
            "model_store": {},
            "error": None if backend_ok else "model is not installed",
        },
    )
    monkeypatch.setattr(harness, "_probe_engine_inventory", lambda: ([{"id": "piper"}], None))

    def validate_profile(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "profile valid", "")

    monkeypatch.setattr(runner_module.subprocess, "run", validate_profile)


def test_doctor_reports_ready_linguistics_for_selected_profiles(tmp_path, monkeypatch):
    harness = Harness(_selection_config(tmp_path))
    _stub_doctor_dependencies(monkeypatch, harness)
    diagnostics = harness.doctor(selection=ProfileSelection(engines=("piper",)))

    assert diagnostics["ok"] is True
    assert diagnostics["speechscope_backend"]["ok"] is True
    assert diagnostics["selection"]["mode"] == "engine"
    assert diagnostics["selection"]["selected_profiles"] == ["piper"]
    assert diagnostics["engine_coverage"] == {
        "found": ["piper"],
        "required": ["piper"],
        "missing": [],
    }
    assert [profile["label"] for profile in diagnostics["profiles"]] == ["piper"]
    assert diagnostics["linguistics"] == {
        "required_token_fields": [],
        "requires_spacy": False,
        "configured_spacy": "auto",
        "spacy_policy": "off",
        "reason": "no_rich_token_requirements",
        "status": "ready",
        "error": None,
    }



def test_doctor_fails_when_speechscope_backend_is_not_ready(tmp_path, monkeypatch):
    harness = Harness(_selection_config(tmp_path))
    _stub_doctor_dependencies(monkeypatch, harness, backend_ok=False)

    diagnostics = harness.doctor(selection=ProfileSelection(engines=("piper",)))

    assert diagnostics["ok"] is False
    assert diagnostics["speechscope_backend"]["ok"] is False
    assert diagnostics["speechscope_backend"]["error"] == "model is not installed"

def test_doctor_fails_on_selected_profile_spacy_conflict_without_downloading_models(tmp_path, monkeypatch):
    config = _selection_config(
        tmp_path,
        entries=(("kokoro", "kokoro"),),
        required_engines=("kokoro",),
        token_fields_by_label={"kokoro": ("span", "pos", "tag")},
        utterplan=UtterPlanConfig(spacy="off"),
    )
    harness = Harness(config)
    _stub_doctor_dependencies(monkeypatch, harness)

    diagnostics = harness.doctor()

    assert diagnostics["ok"] is False
    assert diagnostics["linguistics"] == {
        "required_token_fields": ["pos", "span", "tag"],
        "requires_spacy": True,
        "configured_spacy": "off",
        "spacy_policy": "off",
        "reason": "conflicting_configured_policy",
        "status": "error",
        "error": 'selected profiles kokoro require token fields pos, span, tag, but utterplan.spacy = "off"',
    }
    assert harness.commands == []

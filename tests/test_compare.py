from types import SimpleNamespace

from bookbench.config import load_config
from bookbench.profiles import ProfileCase, inspect_profile
from bookbench.report import build_listening_report


def test_voice_config(tmp_path):
    conf = tmp_path / "test.toml"
    conf.write_text('[[voices]]\nname="test-a"\nprofile="profiles/a.toml"\n')
    loaded = load_config(conf)
    assert loaded.voices[0].name == "test-a"
    assert loaded.voices[0].profile == tmp_path / "profiles/a.toml"


def test_candidate_label_does_not_change_variant(tmp_path):
    profile_path = tmp_path / "piper.renderprofile.toml"
    profile_path.write_text(
        'profile_id = "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"\n'
        '[roles.narrator.binding]\nrenderer = "piper"\n'
    )
    profile = inspect_profile(profile_path)
    case = ProfileCase("amy-ab-test", profile)
    assert case.label == "amy-ab-test"
    assert case.variant == "piper--1234567890ab"


def test_listening_report(tmp_path):
    result = SimpleNamespace(
        profile="voice-a",
        engines=["piper"],
        status="pass",
        wer=0.0,
        cer=0.0,
        error=None,
        variant="voice-a--1234567890ab",
        failed_stage=None,
        speechscope_report=str(tmp_path / "exports/voice-a.speechscope.json"),
        export=str(tmp_path / "exports/voice-a.m4b"),
    )
    path = build_listening_report(tmp_path, [result])
    text = path.read_text()
    assert "exports/voice-a.listen.mp3" in text
    assert "voice-a" in text
    assert "BookBench" in text
    assert "voice-a--1234567890ab" in text
    assert 'href="exports/voice-a.m4b">M4B export</a>' in text
    assert 'href="exports/voice-a.speechscope.json">SpeechScope JSON</a>' in text
    assert 'href="exports/voice-a.expected.txt">Expected transcript</a>' in text
    assert "acoustic transcript agreement" in text
    assert "subjective voice-quality scores" in text


def test_listening_report_orders_candidates_and_escapes_failure_details(tmp_path):
    results = [
        SimpleNamespace(
            profile="Candidate <1>",
            variant="piper--full-identity",
            engines=["piper"],
            status="error",
            wer=None,
            cer=0.125,
            failed_stage="renderplan",
            error="failure <details>",
            export=str(tmp_path / "exports/first.m4b"),
            speechscope_report=str(tmp_path / "exports/first.speechscope.json"),
        ),
        SimpleNamespace(
            profile="Candidate 2",
            variant="other--full-identity",
            engines=["other"],
            status="pass",
            wer=0.0,
            cer=0.0,
            failed_stage=None,
            error=None,
            export=str(tmp_path / "exports/second.m4b"),
            speechscope_report=str(tmp_path / "exports/second.speechscope.json"),
        ),
    ]

    text = build_listening_report(tmp_path, results).read_text(encoding="utf-8")

    assert text.index("Candidate &lt;1&gt;") < text.index("Candidate 2")
    assert "piper--full-identity" in text
    assert "renderplan" in text
    assert "failure &lt;details&gt;" in text
    assert "Candidate <1>" not in text

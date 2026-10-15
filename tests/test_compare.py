from pathlib import Path
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
        export=str(tmp_path / "exports/voice-a.m4b"),
    )
    path = build_listening_report(tmp_path, [result])
    text = path.read_text()
    assert "exports/voice-a.listen.mp3" in text
    assert "voice-a" in text
    assert "BookBench" in text

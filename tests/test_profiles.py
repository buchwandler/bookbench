from pathlib import Path

from bookbench.profiles import coverage, inspect_profile


def test_example_profile_identity_and_variant():
    root = Path(__file__).parents[1]
    info = inspect_profile(root / "profiles" / "piper.renderprofile.toml")
    assert info.engines == ("piper",)
    assert info.targets == (("piper", "en_US-amy-medium"),)
    assert info.variant == "piper--525856299523"


def test_coverage():
    root = Path(__file__).parents[1]
    info = inspect_profile(root / "profiles" / "piper.renderprofile.toml")
    found, missing = coverage([info], ("piper", "kokoro"))
    assert found == {"piper"}
    assert missing == {"kokoro"}


def test_variant_label_matches_utterplan_normalization(tmp_path):
    profile_text = (
        'profile_id = "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"\n'
        '[roles.narrator.binding]\nrenderer = "piper"\n'
    )
    cases = {
        "already-safe": "already-safe",
        "UPPER": "upper",
        "My Voice!": "my-voice",
        "!!!": "profile",
    }
    for raw_label, expected_label in cases.items():
        path = tmp_path / f"{raw_label}.renderprofile.toml"
        path.write_text(profile_text, encoding="utf-8")
        info = inspect_profile(path)
        assert info.variant_label == expected_label
        assert info.variant == f"{expected_label}--1234567890ab"

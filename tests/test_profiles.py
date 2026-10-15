from pathlib import Path

from bookbench.profiles import coverage, inspect_profile


def test_example_profile_identity_and_variant():
    root = Path(__file__).parents[1]
    info = inspect_profile(root / "profiles" / "piper.renderprofile.toml")
    assert info.engines == ("piper",)
    assert info.variant == "piper--525856299523"


def test_coverage():
    root = Path(__file__).parents[1]
    info = inspect_profile(root / "profiles" / "piper.renderprofile.toml")
    found, missing = coverage([info], ("piper", "kokoro"))
    assert found == {"piper"}
    assert missing == {"kokoro"}

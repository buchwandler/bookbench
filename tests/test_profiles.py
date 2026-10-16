from pathlib import Path

from bookbench.profiles import (
    ProfileCase,
    ProfileInfo,
    ProfileSelection,
    coverage,
    inspect_profile,
    select_profiles,
)


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



def _case(label: str, *engines: str) -> ProfileCase:
    profile = ProfileInfo(
        path=Path(f"{label}.renderprofile.toml"),
        label=label,
        profile_id="sha256:1234567890abcdef",
        engines=tuple(engines),
    )
    return ProfileCase(label, profile)


def _selection_cases() -> list[ProfileCase]:
    return [
        _case("piper-amy", "piper"),
        _case("piper-lessac", "piper"),
        _case("kitten", "kitten"),
        _case("kokoro", "kokoro"),
    ]


def test_select_profiles_by_one_engine():
    cases = _selection_cases()
    selected = select_profiles(cases, ProfileSelection(engines=("piper",)))
    assert [case.label for case in selected] == ["piper-amy", "piper-lessac"]


def test_select_profiles_by_multiple_engines_preserves_case_order():
    cases = _selection_cases()
    selected = select_profiles(
        cases, ProfileSelection(engines=("kitten", "piper"))
    )
    assert [case.label for case in selected] == [
        "piper-amy",
        "piper-lessac",
        "kitten",
    ]


def test_select_profiles_by_exact_case_label():
    cases = _selection_cases()
    selected = select_profiles(cases, ProfileSelection(profiles=("piper-amy",)))
    assert [case.label for case in selected] == ["piper-amy"]


def test_select_profiles_rejects_unknown_engine():
    cases = _selection_cases()
    try:
        select_profiles(cases, ProfileSelection(engines=("pipre",)))
    except ValueError as error:
        assert "Unknown or unconfigured engine selector(s): pipre" in str(error)
        assert "Configured engines: kitten, kokoro, piper" in str(error)
    else:
        raise AssertionError("unknown engine selection should fail")


def test_select_profiles_rejects_unknown_profile():
    cases = _selection_cases()
    try:
        select_profiles(cases, ProfileSelection(profiles=("missing",)))
    except ValueError as error:
        assert "Unknown profile selector(s): missing" in str(error)
    else:
        raise AssertionError("unknown profile selection should fail")


def test_profile_selection_rejects_combined_selector_types():
    try:
        ProfileSelection(engines=("piper",), profiles=("piper-amy",))
    except ValueError as error:
        assert str(error) == "engine and profile selectors cannot be combined"
    else:
        raise AssertionError("combined selectors should fail")


def test_inactive_profile_selection_returns_all_cases_in_order():
    cases = _selection_cases()
    assert select_profiles(cases, ProfileSelection()) == cases

import tomllib
from pathlib import Path

import pytest

from bookbench.profiles import (
    ProfileCase,
    ProfileInfo,
    ProfileSelection,
    coverage,
    inspect_profile,
    required_token_fields,
    select_profiles,
)

VOICEFOLIO_REFERENCE = {
    "kind": "managed_reference",
    "resolver": "voicefolio",
    "value": "kyutai-tts-voices:alba-mackenna/casual",
    "sha256": "46264e83cb99115c3d210260e029117566d9c64f20266d10daa78107759ede3e",
    "source_revision": "323332d33f997de8394f24a193e1a76df720e01a",
    "source_repository": "kyutai/tts-voices",
    "source_path": "alba-mackenna/casual.wav",
    "license": "CC-BY-4.0",
    "dataset": "alba-mackenna",
    "variant": "original",
}


def test_example_profile_identity_and_variant():
    root = Path(__file__).parents[1]
    info = inspect_profile(root / "profiles" / "piper.renderprofile.toml")
    assert info.engines == ("piper",)
    assert info.targets == (("piper", "en_US-ljspeech-medium"),)
    assert info.variant == "piper--d3873ffdd3bc"


def test_profile_required_token_fields_and_multi_role_union(tmp_path):
    root = Path(__file__).parents[1]
    assert inspect_profile(root / "profiles" / "piper.renderprofile.toml").required_token_fields == ()
    assert inspect_profile(root / "profiles" / "kokoro.renderprofile.toml").required_token_fields == (
        "pos",
        "span",
        "tag",
    )

    path = tmp_path / "multi-role.renderprofile.toml"
    path.write_text(
        'profile_id = "sha256:1234567890abcdef1234567890abcdef"\n'
        '[roles.narrator.binding]\nrenderer = "piper"\n'
        '[roles.narrator.requirements]\ntoken_fields = ["tag", "span"]\n'
        '[roles.translator.binding]\nrenderer = "inflect"\n'
        '[roles.translator.requirements]\ntoken_fields = ["pos", "tag"]\n',
        encoding="utf-8",
    )
    info = inspect_profile(path)
    case = ProfileCase("multi-role", info)
    assert info.required_token_fields == ("pos", "span", "tag")
    assert case.required_token_fields == ("pos", "span", "tag")
    assert required_token_fields([info, case]) == ("pos", "span", "tag")


@pytest.mark.parametrize("token_fields", ['"pos"', '["pos", 1]', '[""]', "1"])
def test_profile_rejects_malformed_token_fields(tmp_path, token_fields):
    path = tmp_path / "malformed.renderprofile.toml"
    path.write_text(
        'profile_id = "sha256:1234567890abcdef1234567890abcdef"\n'
        '[roles.narrator.binding]\nrenderer = "piper"\n'
        f"[roles.narrator.requirements]\ntoken_fields = {token_fields}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="token_fields must be a list of non-empty strings"):
        inspect_profile(path)


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
    selected = select_profiles(cases, ProfileSelection(engines=("kitten", "piper")))
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


def test_pocket_profile_uses_voicefolio_managed_reference():
    root = Path(__file__).parents[1]
    profile = root / "profiles" / "pocket.renderprofile.toml"
    data = tomllib.loads(profile.read_text(encoding="utf-8"))
    binding = data["roles"]["narrator"]["binding"]

    assert binding["renderer"] == "pocket"
    assert binding["target_id"] == "english_2026-04"
    assert "voice" not in binding
    assert binding["options"]["precision"] == "int8"
    assert binding["options"]["voice_source"] == VOICEFOLIO_REFERENCE
    assert not Path(binding["options"]["voice_source"]["source_path"]).is_absolute()


def test_sopro_profile_uses_same_voicefolio_reference_as_pocket():
    root = Path(__file__).parents[1]
    profile = root / "profiles" / "sopro.renderprofile.toml"
    data = tomllib.loads(profile.read_text(encoding="utf-8"))
    binding = data["roles"]["narrator"]["binding"]
    options = binding["options"]

    assert binding["renderer"] == "sopro"
    assert binding["target_id"] == "v2-turbo"
    assert "voice" not in binding
    assert options["profile"] == "wasm-uint8"
    assert options["providers"] == "cpu"
    assert options["voice_source"] == VOICEFOLIO_REFERENCE
    assert not Path(options["voice_source"]["source_path"]).is_absolute()


def test_clone_comparison_example_uses_the_same_voicefolio_reference():
    root = Path(__file__).parents[1]
    example = root / "bookbench-clone-compare.toml.example"
    content = example.read_text(encoding="utf-8")
    config = tomllib.loads(content)

    voices = config["voices"]
    assert [voice["name"] for voice in voices] == ["pocket-voicefolio", "sopro-voicefolio"]
    source_maps = []
    for voice in voices:
        profile = tomllib.loads((root / voice["profile"]).read_text(encoding="utf-8"))
        source_maps.append(profile["roles"]["narrator"]["binding"]["options"]["voice_source"])
    assert source_maps == [VOICEFOLIO_REFERENCE, VOICEFOLIO_REFERENCE]
    assert "profiles-experimental/kokoro-inno-voicefolio.renderprofile.toml" in content
    assert not (root / "profiles-experimental" / "kokoro-inno-voicefolio.renderprofile.toml").exists()
    assert all("qwen3" not in voice["name"] for voice in voices)

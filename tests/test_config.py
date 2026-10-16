from pathlib import Path

import pytest

from bookbench.config import load_config


def test_load_example_config():
    root = Path(__file__).parents[1]
    cfg = load_config(root / "bookbench.toml")
    assert cfg.config_path == root / "bookbench.toml"
    assert cfg.language == "en-US"
    assert cfg.speechscope.reference_mode == "renderplan"
    assert "piper" in cfg.required_engines

    assert cfg.utterplan.spacy == "auto"


@pytest.mark.parametrize("policy", ("auto", "off", "sm", "md", "lg", "trf"))
def test_load_utterplan_spacy_policy(tmp_path, policy):
    config_path = tmp_path / "bookbench.toml"
    config_path.write_text(f'[utterplan]\nspacy = "{policy}"\n', encoding="utf-8")

    assert load_config(config_path).utterplan.spacy == policy


@pytest.mark.parametrize("policy", ("", "base", "small"))
def test_load_config_rejects_unknown_utterplan_spacy_policy(tmp_path, policy):
    config_path = tmp_path / "bookbench.toml"
    config_path.write_text(f'[utterplan]\nspacy = "{policy}"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="utterplan.spacy must be one of"):
        load_config(config_path)

def _load_config_with_speechscope(tmp_path: Path, speechscope: str):
    config_path = tmp_path / "bookbench.toml"
    config_path.write_text(f"[speechscope]\n{speechscope}", encoding="utf-8")
    return load_config(config_path)


def test_speechscope_threshold_defaults_are_compound_aware(tmp_path):
    config = _load_config_with_speechscope(tmp_path, 'backend = "redux"\n')

    assert config.speechscope.max_compound_wer == 0.0
    assert config.speechscope.max_cer == 0.0
    assert config.speechscope.max_wer is None


def test_explicit_compound_and_strict_thresholds_are_preserved(tmp_path):
    config = _load_config_with_speechscope(
        tmp_path,
        "max_compound_wer = 0.0\nmax_wer = 0.0\nmax_cer = 0.0\n",
    )

    assert config.speechscope.max_compound_wer == 0.0
    assert config.speechscope.max_wer == 0.0
    assert config.speechscope.max_cer == 0.0


def test_legacy_explicit_max_wer_remains_strict(tmp_path):
    config = _load_config_with_speechscope(tmp_path, "max_wer = 0.0\n")

    assert config.speechscope.max_wer == 0.0
    assert config.speechscope.max_compound_wer == 0.0

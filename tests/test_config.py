from pathlib import Path

from bookbench.config import load_config


def test_load_example_config():
    root = Path(__file__).parents[1]
    cfg = load_config(root / "bookbench.toml")
    assert cfg.language == "en-US"
    assert cfg.speechscope.reference_mode == "renderplan"
    assert "piper" in cfg.required_engines

from pathlib import Path

from bookbench.rendertext import renderplan_text


def test_renderplan_text(tmp_path: Path):
    plan = """format = "renderplan"
[[flow]]
kind = "speech"
text = "Hello world."
[[flow]]
kind = "pause"
[[flow]]
kind = "speech"
text = "Second sentence."
"""
    (tmp_path / "chapter-0001.renderplan.toml").write_text(plan, encoding="utf-8")
    assert renderplan_text(tmp_path) == "Hello world.\nSecond sentence.\n"

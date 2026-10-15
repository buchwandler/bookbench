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


def test_renderplan_set_order_overrides_filename_order(tmp_path: Path):
    first = """format = "renderplan"
[[flow]]
kind = "speech"
text = "First filename."
"""
    second = """format = "renderplan"
[[flow]]
kind = "speech"
text = "Second filename."
"""
    (tmp_path / "chapter-0001.renderplan.toml").write_text(first, encoding="utf-8")
    (tmp_path / "chapter-0002.renderplan.toml").write_text(second, encoding="utf-8")
    (tmp_path / "renderplan-set.toml").write_text(
        'format = "renderplan-set"\n'
        '[[chapter]]\nid = "chapter-0002"\nrenderplan = "chapter-0002.renderplan.toml"\n'
        '[[chapter]]\nid = "chapter-0001"\nrenderplan = "chapter-0001.renderplan.toml"\n',
        encoding="utf-8",
    )

    assert renderplan_text(tmp_path) == "Second filename.\nFirst filename.\n"

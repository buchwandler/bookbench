from pathlib import Path
from types import SimpleNamespace

import pytest

from bookbench import cli
from bookbench.profiles import ProfileSelection


def test_run_parses_repeatable_engine_selectors():
    args = cli._parser().parse_args(["run", "--engine", "piper", "--engine", "kitten"])
    assert args.engine == ["piper", "kitten"]
    assert args.profile == []


def test_run_and_doctor_parse_repeatable_profile_selectors():
    for command in ("run", "doctor"):
        args = cli._parser().parse_args([command, "--profile", "piper", "--profile", "piper-amy"])
        assert args.profile == ["piper", "piper-amy"]
        assert args.engine == []


def test_engine_and_profile_selectors_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["run", "--engine", "piper", "--profile", "piper"])


@pytest.mark.parametrize("command", ["run", "doctor"])
def test_selector_help_is_available(command, capsys):
    with pytest.raises(SystemExit) as error:
        cli._parser().parse_args([command, "--help"])
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "--engine ENGINE" in help_text
    assert "--profile PROFILE" in help_text


def test_compare_does_not_accept_profile_selectors():
    args = cli._parser().parse_args(["compare"])
    assert args.command == "compare"
    assert not hasattr(args, "engine")
    assert not hasattr(args, "profile")
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["compare", "--engine", "piper"])


def test_main_deduplicates_and_passes_run_selection(monkeypatch, capsys):
    calls = {}
    config = object()
    result = SimpleNamespace(
        profile="piper",
        engines=["piper"],
        status="pass",
        wer=None,
        cer=None,
        error=None,
    )

    class FakeHarness:
        def __init__(self, _config, *, dry_run=False):
            pass

        def run(self, **kwargs):
            calls.update(kwargs)
            return Path("/tmp/bookbench-run"), [result]

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "Harness", FakeHarness)

    assert cli.main(["run", "--engine", "piper", "--engine", "piper", "--engine", "kitten"]) == 0
    assert calls == {
        "run_id": None,
        "selection": ProfileSelection(engines=("piper", "kitten")),
    }
    capsys.readouterr()


def test_main_passes_selection_to_doctor(monkeypatch, capsys):
    calls = {}

    class FakeHarness:
        def __init__(self, _config, *, dry_run=False):
            pass

        def doctor(self, *, selection):
            calls["selection"] = selection
            return {"ok": True}

    monkeypatch.setattr(cli, "load_config", lambda _path: object())
    monkeypatch.setattr(cli, "Harness", FakeHarness)

    assert cli.main(["doctor", "--engine", "kitten"]) == 0
    assert calls["selection"] == ProfileSelection(engines=("kitten",))
    capsys.readouterr()


def test_main_keeps_compare_unselected(monkeypatch, capsys):
    calls = {}
    config = SimpleNamespace(voices=(object(), object()))
    result = SimpleNamespace(
        profile="candidate",
        engines=["piper"],
        status="pass",
        wer=None,
        cer=None,
        error=None,
    )

    class FakeHarness:
        def __init__(self, _config, *, dry_run=False):
            pass

        def run(self, **kwargs):
            calls.update(kwargs)
            return Path("/tmp/bookbench-run"), [result]

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "Harness", FakeHarness)

    assert cli.main(["compare"]) == 0
    assert calls == {"run_id": None}
    capsys.readouterr()

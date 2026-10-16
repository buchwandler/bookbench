from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SpeechScopeConfig:
    backend: str = "redux"
    device: str = "cpu"
    model: str | None = None
    normalization: str = "basic-v1"
    max_compound_wer: float | None = 0.0
    max_cer: float | None = 0.0
    max_wer: float | None = None
    reference_mode: str = "renderplan"


@dataclass(frozen=True, slots=True)
class VoiceCandidate:
    name: str
    profile: Path


@dataclass(frozen=True, slots=True)
class Config:
    source: Path = Path("fixtures/smoke.md")
    language: str = "en-US"
    title: str = "BookBench Smoke Test"
    profiles_dir: Path = Path("profiles")
    work_root: Path = Path(".bookbench/runs")
    cache_dir: Path = Path(".bookbench/cache/voicerender")
    chapters: str = "all"
    required_engines: tuple[str, ...] = ()
    max_replans: int = 3
    fail_fast: bool = False
    speechscope: SpeechScopeConfig = field(default_factory=SpeechScopeConfig)
    voices: tuple[VoiceCandidate, ...] = ()
    config_path: Path | None = None


def _resolve(base: Path, value: str | Path) -> Path:
    value = Path(value)
    return value if value.is_absolute() else (base / value).resolve()


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    speech = data.get("speechscope", {})
    reference_mode = str(speech.get("reference_mode", "renderplan"))
    if reference_mode not in {"renderplan", "source"}:
        raise ValueError("speechscope.reference_mode must be 'renderplan' or 'source'")
    max_replans = int(data.get("max_replans", 3))
    if max_replans < 0:
        raise ValueError("max_replans must be >= 0")
    voices_data = data.get("voices", [])
    if not isinstance(voices_data, list):
        raise TypeError("voices must be an array of tables")
    voices = tuple(
        VoiceCandidate(name=str(item["name"]), profile=_resolve(base, item["profile"])) for item in voices_data
    )
    if len({v.name for v in voices}) != len(voices):
        raise ValueError("voices names must be unique")
    if any(not v.name or not v.name.replace("-", "").replace("_", "").isalnum() for v in voices):
        raise ValueError("voice names must be simple alphanumeric labels (hyphens/underscores allowed)")
    return Config(
        voices=voices,
        source=_resolve(base, data.get("source", "fixtures/smoke.md")),
        language=str(data.get("language", "en-US")),
        title=str(data.get("title", "BookBench Smoke Test")),
        profiles_dir=_resolve(base, data.get("profiles_dir", "profiles")),
        work_root=_resolve(base, data.get("work_root", ".bookbench/runs")),
        cache_dir=_resolve(base, data.get("cache_dir", ".bookbench/cache/voicerender")),
        chapters=str(data.get("chapters", "all")),
        required_engines=tuple(str(x) for x in data.get("required_engines", [])),
        max_replans=max_replans,
        fail_fast=bool(data.get("fail_fast", False)),
        speechscope=SpeechScopeConfig(
            backend=str(speech.get("backend", "redux")),
            device=str(speech.get("device", "cpu")),
            model=str(speech["model"]) if speech.get("model") is not None else None,
            normalization=str(speech.get("normalization", "basic-v1")),
            max_compound_wer=float(speech["max_compound_wer"]) if "max_compound_wer" in speech else 0.0,
            max_cer=float(speech["max_cer"]) if "max_cer" in speech else 0.0,
            max_wer=float(speech["max_wer"]) if "max_wer" in speech else None,
            reference_mode=reference_mode,
        ),
        config_path=path,
    )

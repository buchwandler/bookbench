from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class RuntimeExecutable:
    command: str
    env_var: str | None = None
    version_args: tuple[str, ...] = ("--version",)


@dataclass(frozen=True)
class EngineCI:
    id: str
    distribution: str
    import_components: tuple[tuple[str, str], ...]
    runtime_executables: tuple[RuntimeExecutable, ...] = ()
    model_ref: str | None = None
    model_quality: str | None = None
    default_matrix: bool = True


ENGINE_CI: tuple[EngineCI, ...] = (
    EngineCI(
        "inflect",
        "inflectsynth",
        (
            ("inflectsynth", "inflectsynth"),
            ("onnxvoice", "onnxvoice"),
            ("onnxruntime", "onnxruntime"),
            ("huggingface_hub", "huggingface-hub"),
        ),
        runtime_executables=(
            RuntimeExecutable("espeak-ng", env_var="ESPEAKNG_RUNTIME_EXECUTABLE"),
        ),
    ),
    EngineCI(
        "kitten",
        "kittensynth",
        (
            ("kittensynth", "kittensynth"),
            ("onnxvoice", "onnxvoice"),
            ("onnxruntime", "onnxruntime"),
            ("huggingface_hub", "huggingface-hub"),
        ),
    ),
    EngineCI("kokoro", "pykokoro", (("pykokoro", "pykokoro"), ("onnxruntime", "onnxruntime"))),
    EngineCI(
        "piper",
        "pipersynth",
        (
            ("pipersynth", "pipersynth"),
            ("onnxvoice", "onnxvoice"),
            ("onnxruntime", "onnxruntime"),
            ("piperg2p", "piperg2p"),
        ),
    ),
    EngineCI(
        "pocket",
        "pocketsynth",
        (
            ("pocketsynth", "pocketsynth"),
            ("onnxruntime", "onnxruntime"),
            ("huggingface_hub", "huggingface-hub"),
        ),
    ),
    EngineCI(
        "qwen3",
        "qwen3synth",
        (
            ("qwen3synth", "qwen3synth"),
            ("onnxvoice", "onnxvoice"),
            ("onnxruntime", "onnxruntime"),
        ),
        model_ref="qwen3:base-0.6b",
        model_quality="int4",
        default_matrix=False,
    ),
    EngineCI(
        "sopro",
        "soprosynth",
        (
            ("soprosynth", "soprosynth"),
            ("onnxvoice", "onnxvoice"),
            ("onnxruntime", "onnxruntime"),
        ),
        model_ref="sopro:v2-turbo",
        model_quality="wasm-uint8",
    ),
    EngineCI("supertonic", "supertonicsynth", (("supertonicsynth", "supertonicsynth"), ("onnxruntime", "onnxruntime"))),
)

ENGINE_BY_ID: Mapping[str, EngineCI] = MappingProxyType({engine.id: engine for engine in ENGINE_CI})
LOCK_ENGINES = tuple(engine.id for engine in ENGINE_CI)
DEFAULT_ENGINES = tuple(engine.id for engine in ENGINE_CI if engine.default_matrix)
ENGINE_COMPONENTS: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {engine.id: engine.import_components for engine in ENGINE_CI}
)
ENGINE_EXECUTABLES: Mapping[str, tuple[RuntimeExecutable, ...]] = MappingProxyType(
    {engine.id: engine.runtime_executables for engine in ENGINE_CI}
)
ENGINE_PACKAGES: Mapping[str, str] = MappingProxyType(
    {engine.id: engine.distribution for engine in ENGINE_CI}
)
ENGINE_ASSETS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        engine.id: (engine.model_ref, engine.model_quality)
        for engine in ENGINE_CI
        if engine.model_ref is not None and engine.model_quality is not None
    }
)

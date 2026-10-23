from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.ci.engine_matrix import DEFAULT_ENGINES

SCHEMA = "bookbench.pages-gallery.v1"
CANONICAL_PLATFORM = "linux-x64"
MAX_SAMPLE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_SAMPLE_BYTES = 100 * 1024 * 1024
MAX_SITE_BYTES = MAX_TOTAL_SAMPLE_BYTES + 1024 * 1024
_PROFILE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_GITHUB_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_RUN_ID_RE = re.compile(r"[0-9]+\Z")
_SECRET_RE = re.compile(
    r"(?i)(?:hf_[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|"
    r"(?:api[_-]?key|token|password|secret)\s*[:=])"
)

CSS = """\
:root{color-scheme:light dark;font-family:system-ui,sans-serif;line-height:1.5;
  color:#17212b;background:#f4f7fa;--surface:#fff;--border:#a8b4c0;--link:#075985}
*{box-sizing:border-box}body{margin:0 auto;max-width:1200px;padding:1.5rem}
a{color:var(--link)}a:focus-visible,audio:focus-visible{outline:3px solid #b45309;outline-offset:3px}
header,footer,.notice{margin-bottom:1.5rem}header{border-bottom:1px solid var(--border);padding-bottom:1rem}
.provenance{display:flex;flex-wrap:wrap;gap:.5rem 1.5rem}.notice{border-left:4px solid #075985;padding:.75rem 1rem;background:var(--surface)}
.gallery{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1rem}
.sample-card{background:var(--surface);border:1px solid var(--border);border-radius:.5rem;padding:1rem;min-width:0}
.sample-card h2{margin-top:0}.variant{overflow-wrap:anywhere}.sample-card dl{display:grid;grid-template-columns:minmax(7rem,auto) 1fr;gap:.35rem .75rem}
dt{font-weight:700}dd{margin:0;overflow-wrap:anywhere}audio{display:block;width:100%;margin:1rem 0}
code{overflow-wrap:anywhere}.status{font-weight:700}.metric-list{margin:.5rem 0}
@media(prefers-color-scheme:dark){:root{color:#edf2f7;background:#101820;--surface:#1b2732;--border:#52616e;--link:#7dd3fc}}
"""

JAVASCRIPT = """\
document.addEventListener("play", (event) => {
  if (!(event.target instanceof HTMLAudioElement)) return;
  document.querySelectorAll("audio").forEach((audio) => {
    if (audio !== event.target) audio.pause();
  });
}, true);
"""


class GalleryBuildError(ValueError):
    """Raised when untrusted E2E artifacts cannot safely form a complete gallery."""


@dataclass(frozen=True)
class PublishedSample:
    engine: str
    profile: str
    profile_id: str
    variant: str
    targets: tuple[dict[str, str], ...]
    bindings: tuple[dict[str, object], ...]
    compound_wer: float | None
    strict_wer: float | None
    cer: float | None
    word_boundary_match_count: int | None
    source_platform: str
    source_run_id: str
    source_attempt: str
    audio_sha256: str
    audio_size_bytes: int
    audio_path: str


def _validate_run_identity(run_id: str, attempt: str) -> None:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise GalleryBuildError("workflow run id must contain only digits")
    if not _RUN_ID_RE.fullmatch(attempt) or int(attempt) < 1:
        raise GalleryBuildError("workflow run attempt must be a positive integer")


def _validate_provenance(repository: str, commit: str, run_id: str, attempt: str, platform: str) -> None:
    if not _GITHUB_REPOSITORY_RE.fullmatch(repository):
        raise GalleryBuildError("repository must be an owner/repository identifier")
    if not _COMMIT_RE.fullmatch(commit):
        raise GalleryBuildError("commit must be a full 40- or 64-character hexadecimal SHA")
    _validate_run_identity(run_id, attempt)
    if platform != CANONICAL_PLATFORM:
        raise GalleryBuildError(f"v1 publication platform must be {CANONICAL_PLATFORM}")


def _safe_component(value: object, description: str) -> str:
    if not isinstance(value, str) or not _COMPONENT_RE.fullmatch(value) or value in {".", ".."}:
        raise GalleryBuildError(f"{description} must be one safe path component")
    return value


def _safe_display_string(value: object, description: str, *, allow_catalog_path: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise GalleryBuildError(f"{description} must be a non-empty string of at most 500 characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise GalleryBuildError(f"{description} contains control characters")
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[/\\]", value):
        raise GalleryBuildError(f"{description} must not contain an absolute path")
    if not allow_catalog_path and ("/" in value or "\\" in value):
        raise GalleryBuildError(f"{description} must not contain path separators")
    if allow_catalog_path and any(part == ".." for part in re.split(r"[/\\]+", value)):
        raise GalleryBuildError(f"{description} must not contain path traversal")
    if _SECRET_RE.search(value):
        raise GalleryBuildError(f"{description} appears to contain secret-derived text")
    return value


def _require_mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GalleryBuildError(f"{description} must be a JSON/TOML object")
    return value


def _load_json(path: Path, description: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise GalleryBuildError(f"{description} is missing or is not a regular file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GalleryBuildError(f"{description} is not valid UTF-8 JSON") from error


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise GalleryBuildError(f"artifact root must not be a symlink: {root.name}")
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            candidate = Path(directory) / name
            if candidate.is_symlink():
                raise GalleryBuildError("artifact input contains a symlink")


def discover_platform_artifacts(
    input_root: str | Path,
    *,
    run_id: str,
    attempt: str,
    platform: str = CANONICAL_PLATFORM,
) -> dict[str, Path]:
    """Find exactly one expected platform artifact per default engine."""
    _validate_run_identity(run_id, attempt)
    if platform != CANONICAL_PLATFORM:
        raise GalleryBuildError(f"v1 publication platform must be {CANONICAL_PLATFORM}")
    root = Path(input_root)
    if root.is_symlink() or not root.is_dir():
        raise GalleryBuildError("artifact input root must be an existing directory")
    _reject_symlinks(root)

    suffix = f"-{platform}-{run_id}-{attempt}"
    expected = {engine: f"bookbench-{engine}{suffix}" for engine in DEFAULT_ENGINES}
    by_name = {name: engine for engine, name in expected.items()}
    found: dict[str, Path] = {}
    for candidate in sorted(root.iterdir(), key=lambda item: item.name):
        if not candidate.name.startswith("bookbench-"):
            continue
        if not candidate.is_dir():
            raise GalleryBuildError("matching artifact input is not a directory")
        engine = by_name.get(candidate.name)
        if engine is None:
            duplicate_engine = next(
                (known for known, name in expected.items() if candidate.name.startswith(name)), None
            )
            if duplicate_engine is not None:
                raise GalleryBuildError(f"duplicate artifact for engine {duplicate_engine}")
            if candidate.name.endswith(suffix):
                reported_engine = candidate.name[len("bookbench-") : -len(suffix)]
                raise GalleryBuildError(f"unexpected artifact engine: {reported_engine}")
            raise GalleryBuildError(f"unexpected artifact name: {candidate.name}")
        if engine in found:
            raise GalleryBuildError(f"duplicate artifact for engine {engine}")
        found[engine] = candidate

    missing = [engine for engine in DEFAULT_ENGINES if engine not in found]
    if missing:
        raise GalleryBuildError("missing expected engine artifact(s): " + ", ".join(missing))
    return {engine: found[engine] for engine in DEFAULT_ENGINES}


def _find_run_directory(artifact_root: Path, expected_run_id: str) -> Path:
    matches: list[Path] = []
    for directory, dirnames, _filenames in os.walk(artifact_root, followlinks=False):
        for name in dirnames:
            candidate = Path(directory) / name
            if name != expected_run_id or candidate.parent.name != "runs":
                continue
            if candidate.parent.parent.name == ".bookbench" or candidate.parent.parent == artifact_root:
                matches.append(candidate)
    if len(matches) != 1:
        raise GalleryBuildError(f"artifact must contain exactly one .bookbench/runs/{expected_run_id} run directory")
    run_dir = matches[0]
    try:
        run_dir.resolve(strict=True).relative_to(artifact_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise GalleryBuildError("discovered run directory escapes its artifact root") from error
    return run_dir


def _metric(value: object, description: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GalleryBuildError(f"{description} must be a finite non-negative number or null")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise GalleryBuildError(f"{description} must be a finite non-negative number or null")
    return number


def _count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GalleryBuildError("word boundary match count must be a non-negative integer or null")
    return value


def content_addressed_audio_path(engine: str, profile_id: str, audio_sha256: str) -> str:
    if engine not in DEFAULT_ENGINES:
        raise GalleryBuildError(f"unsupported publication engine: {engine}")
    if not _PROFILE_ID_RE.fullmatch(profile_id) or not _SHA256_RE.fullmatch(audio_sha256):
        raise GalleryBuildError("profile id or audio SHA-256 is malformed")
    return f"samples/{engine}/{profile_id.removeprefix('sha256:')[:12]}-{audio_sha256}.mp3"


def _profile_targets_and_bindings(
    profile_data: Mapping[str, Any], engine: str
) -> tuple[tuple[dict[str, str], ...], tuple[dict[str, object], ...]]:
    roles = profile_data.get("roles")
    if not isinstance(roles, Mapping) or not roles:
        raise GalleryBuildError("checked-out RenderProfile has no roles")
    targets: list[dict[str, str]] = []
    bindings: list[dict[str, object]] = []
    for raw_role_name, raw_role in sorted(roles.items(), key=lambda item: str(item[0])):
        role_name = _safe_display_string(raw_role_name, "profile role")
        role = _require_mapping(raw_role, f"roles.{role_name}")
        binding = _require_mapping(role.get("binding"), f"roles.{role_name}.binding")
        renderer = _safe_display_string(binding.get("renderer"), f"roles.{role_name}.renderer")
        target_id = _safe_display_string(binding.get("target_id"), f"roles.{role_name}.target_id")
        targets.append({"engine_id": renderer, "target_id": target_id})
        entry: dict[str, object] = {"role": role_name, "renderer": renderer, "target_id": target_id}
        if renderer != engine:
            raise GalleryBuildError("checked-out profile renderer does not match its artifact engine")
        if "voice" in binding and binding["voice"] is not None:
            entry["voice"] = _safe_display_string(binding["voice"], f"roles.{role_name}.voice")
        options = binding.get("options", {})
        options = _require_mapping(options, f"roles.{role_name}.binding.options")
        voice_source = binding.get("voice_source", options.get("voice_source"))
        if voice_source is not None:
            voice_source = _require_mapping(voice_source, f"roles.{role_name}.voice_source")
            safe_source: dict[str, str] = {}
            for key in ("kind", "value", "dataset", "license", "resolver"):
                if key in voice_source and voice_source[key] is not None:
                    safe_source[key] = _safe_display_string(
                        voice_source[key],
                        f"roles.{role_name}.voice_source.{key}",
                        allow_catalog_path=(key == "value"),
                    )
            if safe_source:
                entry["voice_source"] = safe_source
        bindings.append(entry)
    if not targets or {target["engine_id"] for target in targets} != {engine}:
        raise GalleryBuildError("checked-out profile does not bind exactly the expected engine")
    return tuple(targets), tuple(bindings)


def _load_validated_sample_with_source(
    artifact_root: Path,
    engine: str,
    *,
    run_id: str,
    attempt: str,
    platform: str,
    profiles_root: Path,
) -> tuple[PublishedSample, Path]:
    expected_run_id = f"gha-{run_id}-{attempt}-{engine}-{platform}"
    run_dir = _find_run_directory(artifact_root, expected_run_id)
    summary = _require_mapping(_load_json(run_dir / "summary.json", "summary.json"), "summary.json")
    if summary.get("run_id") != expected_run_id:
        raise GalleryBuildError(f"BookBench run id does not match the requested run for {engine}")
    results = summary.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise GalleryBuildError(f"{engine} artifact must contain exactly one selected result")
    result = _require_mapping(results[0], f"{engine} result")
    if result.get("status") != "pass":
        raise GalleryBuildError(f"{engine} result is not pass")
    result_engines = result.get("engines")
    if not isinstance(result_engines, list) or result_engines != [engine]:
        raise GalleryBuildError(f"result engine does not match artifact engine {engine}")
    profile_name = _safe_component(result.get("profile"), f"{engine} result profile")
    variant = result.get("variant")
    if not isinstance(variant, str) or not variant:
        raise GalleryBuildError(f"{engine} result variant is missing")

    profiles = _load_json(run_dir / "profiles.json", "profiles.json")
    if not isinstance(profiles, list) or len(profiles) != 1:
        raise GalleryBuildError(f"{engine} artifact must contain exactly one tested profile")
    profile_record = _require_mapping(profiles[0], f"{engine} tested profile")
    if profile_record.get("candidate_label") != profile_name:
        raise GalleryBuildError(f"tested profile label does not match result profile for {engine}")
    profile_label = _safe_component(profile_record.get("profile_label"), f"{engine} profile label")
    if profile_label != profile_name:
        raise GalleryBuildError(f"tested profile label does not match checked-out profile for {engine}")

    profile_id = profile_record.get("profile_id")
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise GalleryBuildError(f"{engine} profile id is missing or malformed")
    variant_label = _safe_component(profile_record.get("variant_label"), f"{engine} variant label")
    expected_variant = f"{variant_label}--{profile_id.removeprefix('sha256:')[:12]}"
    if profile_record.get("variant") != expected_variant or variant != expected_variant:
        raise GalleryBuildError(f"{engine} result variant does not match tested profile identity")
    if profile_record.get("engines") != [engine]:
        raise GalleryBuildError(f"tested profile engine does not match artifact engine {engine}")

    original_sha256 = profile_record.get("original_sha256")
    if not isinstance(original_sha256, str) or not _SHA256_RE.fullmatch(original_sha256):
        raise GalleryBuildError(f"{engine} tested profile SHA-256 is missing or malformed")
    profile_file = profiles_root / f"{profile_name}.renderprofile.toml"
    if profile_file.is_symlink() or not profile_file.is_file():
        raise GalleryBuildError(f"checked-out profile is missing for {engine}")
    try:
        profile_file.resolve(strict=True).relative_to(profiles_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise GalleryBuildError("checked-out profile escapes its profiles directory") from error
    profile_bytes = profile_file.read_bytes()
    actual_sha256 = hashlib.sha256(profile_bytes).hexdigest()
    if actual_sha256 != original_sha256:
        raise GalleryBuildError(f"checked-out profile SHA-256 differs from tested profile for {engine}")
    try:
        profile_data = tomllib.loads(profile_bytes.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise GalleryBuildError(f"checked-out profile is not valid UTF-8 TOML for {engine}") from error
    if profile_data.get("profile_id") != profile_id:
        raise GalleryBuildError(f"checked-out profile identity differs from tested profile for {engine}")
    targets, bindings = _profile_targets_and_bindings(profile_data, engine)

    raw_targets = profile_record.get("targets")
    if not isinstance(raw_targets, list):
        raise GalleryBuildError(f"tested target metadata is missing for {engine}")
    tested_targets: list[dict[str, str]] = []
    for item in raw_targets:
        target = _require_mapping(item, f"{engine} tested target")
        engine_id = _safe_display_string(target.get("engine_id"), f"{engine} target engine")
        target_id = _safe_display_string(target.get("target_id"), f"{engine} target id")
        tested_targets.append({"engine_id": engine_id, "target_id": target_id})
    target_key = lambda item: (item["engine_id"], item["target_id"])
    if sorted(map(target_key, tested_targets)) != sorted(map(target_key, targets)):
        raise GalleryBuildError(f"checked-out profile targets differ from tested profile for {engine}")
    if not tested_targets:
        raise GalleryBuildError(f"tested target metadata is empty for {engine}")

    versions = _require_mapping(_load_json(run_dir / "versions.json", "versions.json"), "versions.json")
    if not versions:
        raise GalleryBuildError(f"runtime versions are empty for {engine}")
    engine_provenance = _require_mapping(_load_json(run_dir / "engines.json", "engines.json"), "engines.json")
    selected_targets = engine_provenance.get("selected_targets")
    if not isinstance(selected_targets, list):
        raise GalleryBuildError(f"selected target provenance is missing for {engine}")
    provenance_targets = []
    for item in selected_targets:
        target = _require_mapping(item, f"{engine} selected target")
        if target.get("candidate_label") != profile_name:
            continue
        provenance_targets.append(
            {
                "engine_id": _safe_display_string(target.get("engine_id"), f"{engine} provenance engine"),
                "target_id": _safe_display_string(target.get("target_id"), f"{engine} provenance target"),
            }
        )
    if sorted(map(target_key, provenance_targets)) != sorted(map(target_key, tested_targets)):
        raise GalleryBuildError(f"engine target provenance differs from tested profile for {engine}")

    exports_dir = run_dir / "exports"
    audio_name = _safe_component(f"{profile_name}.listen.mp3", f"{engine} listening filename")
    audio_source = exports_dir / audio_name
    if exports_dir.is_symlink() or not exports_dir.is_dir() or audio_source.is_symlink() or not audio_source.is_file():
        raise GalleryBuildError(f"listening MP3 is missing or unsafe for {engine}")
    try:
        audio_source.resolve(strict=True).relative_to(run_dir.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise GalleryBuildError(f"listening MP3 escapes the BookBench run for {engine}") from error
    audio_size = audio_source.stat().st_size
    if audio_size <= 0:
        raise GalleryBuildError(f"listening MP3 is empty for {engine}")
    if audio_size > MAX_SAMPLE_BYTES:
        raise GalleryBuildError(f"listening MP3 exceeds the {MAX_SAMPLE_BYTES}-byte per-sample limit for {engine}")
    audio_sha256 = hashlib.sha256(audio_source.read_bytes()).hexdigest()
    audio_path = content_addressed_audio_path(engine, profile_id, audio_sha256)

    sample = PublishedSample(
        engine=engine,
        profile=profile_label,
        profile_id=profile_id,
        variant=variant,
        targets=tuple(tested_targets),
        bindings=bindings,
        compound_wer=_metric(result.get("compound_wer"), f"{engine} compound WER"),
        strict_wer=_metric(result.get("wer"), f"{engine} strict WER"),
        cer=_metric(result.get("cer"), f"{engine} CER"),
        word_boundary_match_count=_count(result.get("word_boundary_match_count")),
        source_platform=platform,
        source_run_id=run_id,
        source_attempt=attempt,
        audio_sha256=audio_sha256,
        audio_size_bytes=audio_size,
        audio_path=audio_path,
    )
    return sample, audio_source


def load_validated_sample(
    artifact_root: str | Path,
    engine: str,
    *,
    run_id: str,
    attempt: str,
    profiles_root: str | Path,
    platform: str = CANONICAL_PLATFORM,
) -> PublishedSample:
    """Load and validate one passing profile sample without trusting serialized paths."""
    if engine not in DEFAULT_ENGINES:
        raise GalleryBuildError(f"unsupported publication engine: {engine}")
    _validate_run_identity(run_id, attempt)
    if platform != CANONICAL_PLATFORM:
        raise GalleryBuildError(f"v1 publication platform must be {CANONICAL_PLATFORM}")
    artifact = Path(artifact_root)
    if artifact.is_symlink() or not artifact.is_dir():
        raise GalleryBuildError(f"artifact directory is missing for {engine}")
    _reject_symlinks(artifact)
    profiles = Path(profiles_root)
    sample, _source = _load_validated_sample_with_source(
        artifact,
        engine,
        run_id=run_id,
        attempt=attempt,
        platform=platform,
        profiles_root=profiles,
    )
    return sample


def _sample_dict(sample: PublishedSample) -> dict[str, object]:
    return {
        "engine": sample.engine,
        "profile": sample.profile,
        "profile_id": sample.profile_id,
        "variant": sample.variant,
        "targets": [dict(target) for target in sample.targets],
        "bindings": [dict(binding) for binding in sample.bindings],
        "status": "pass",
        "metrics": {
            "compound_wer": sample.compound_wer,
            "strict_wer": sample.strict_wer,
            "cer": sample.cer,
            "word_boundary_match_count": sample.word_boundary_match_count,
        },
        "source_platform": sample.source_platform,
        "source_run_id": sample.source_run_id,
        "source_attempt": sample.source_attempt,
        "audio": {
            "path": sample.audio_path,
            "sha256": sample.audio_sha256,
            "size_bytes": sample.audio_size_bytes,
        },
    }


def build_gallery_manifest(
    samples: Sequence[PublishedSample],
    *,
    repository: str,
    commit: str,
    run_id: str,
    attempt: str,
    platform: str = CANONICAL_PLATFORM,
) -> dict[str, object]:
    """Build deterministic JSON-ready gallery data in authoritative engine order."""
    _validate_provenance(repository, commit, run_id, attempt, platform)
    by_engine: dict[str, PublishedSample] = {}
    for sample in samples:
        if sample.engine not in DEFAULT_ENGINES or sample.engine in by_engine:
            raise GalleryBuildError("sample set contains an unknown or duplicate engine")
        by_engine[sample.engine] = sample
    if set(by_engine) != set(DEFAULT_ENGINES):
        raise GalleryBuildError("sample set must cover exactly the default engine registry")

    ordered_samples: list[dict[str, object]] = []
    audio_paths: set[str] = set()
    for engine in DEFAULT_ENGINES:
        sample = by_engine[engine]
        if sample.source_platform != platform or sample.source_run_id != run_id or sample.source_attempt != attempt:
            raise GalleryBuildError(f"sample provenance does not match requested publication for {engine}")
        if not _PROFILE_ID_RE.fullmatch(sample.profile_id) or not _SHA256_RE.fullmatch(sample.audio_sha256):
            raise GalleryBuildError(f"sample identity or audio hash is malformed for {engine}")
        expected_audio_path = content_addressed_audio_path(engine, sample.profile_id, sample.audio_sha256)
        if sample.audio_path != expected_audio_path:
            raise GalleryBuildError(f"sample URL is not content-addressed for {engine}")
        pure_path = PurePosixPath(sample.audio_path)
        if pure_path.is_absolute() or ".." in pure_path.parts or len(pure_path.parts) != 3:
            raise GalleryBuildError(f"sample URL is not a safe site-relative path for {engine}")
        if sample.audio_path in audio_paths:
            raise GalleryBuildError("sample set contains duplicate audio URLs")
        audio_paths.add(sample.audio_path)
        ordered_samples.append(_sample_dict(sample))
    return {
        "schema": SCHEMA,
        "generated_from": {
            "repository": repository,
            "commit": commit.lower(),
            "workflow_run_id": run_id,
            "workflow_run_attempt": attempt,
            "platform": platform,
        },
        "samples": ordered_samples,
    }


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _metric_text(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GalleryBuildError("displayed metric must be a finite number or null")
    number = float(value)
    if not math.isfinite(number):
        raise GalleryBuildError("displayed metric must be a finite number or null")
    return f"{number:.4f}"


def render_index(manifest: Mapping[str, Any]) -> str:
    """Render the complete static landing page from one gallery manifest."""
    if manifest.get("schema") != SCHEMA:
        raise GalleryBuildError("cannot render an unsupported gallery schema")
    generated = _require_mapping(manifest.get("generated_from"), "gallery generated_from")
    samples = manifest.get("samples")
    if not isinstance(samples, list) or len(samples) != len(DEFAULT_ENGINES):
        raise GalleryBuildError("gallery HTML requires the complete default engine set")
    repository = _e(generated["repository"])
    commit = _e(generated["commit"])
    run_id = _e(generated["workflow_run_id"])
    attempt = _e(generated["workflow_run_attempt"])
    platform = _e(generated["platform"])
    commit_url = f"https://github.com/{repository}/commit/{commit}"
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"

    cards: list[str] = []
    for sample_raw in samples:
        sample = _require_mapping(sample_raw, "gallery sample")
        engine = _e(sample["engine"])
        profile = _e(sample["profile"])
        variant = _e(sample["variant"])
        targets = sample.get("targets", [])
        bindings = sample.get("bindings", [])
        metrics = _require_mapping(sample.get("metrics"), "gallery metrics")
        audio = _require_mapping(sample.get("audio"), "gallery audio")
        target_values = ", ".join(
            f"{_e(_require_mapping(target, 'gallery target').get('engine_id', ''))}: "
            f"{_e(_require_mapping(target, 'gallery target').get('target_id', ''))}"
            for target in targets
        )
        binding_sections: list[str] = []
        for binding_raw in bindings:
            binding = _require_mapping(binding_raw, "gallery binding")
            rows: list[str] = []
            if binding.get("voice") is not None:
                rows.append(f"<dt>Voice</dt><dd>{_e(binding['voice'])}</dd>")
            voice_source = binding.get("voice_source")
            if isinstance(voice_source, Mapping):
                if voice_source.get("kind") == "managed_reference":
                    rows.append("<dt>Voice mode</dt><dd>managed reference</dd>")
                for key, label in (
                    ("value", "Reference"),
                    ("resolver", "Resolver"),
                    ("dataset", "Dataset"),
                    ("license", "Reference license"),
                ):
                    if voice_source.get(key) is not None:
                        rows.append(f"<dt>{label}</dt><dd>{_e(voice_source[key])}</dd>")
            binding_sections.append(
                f'<section class="binding"><h3>{_e(binding.get("role", "Role"))}</h3><dl>{"".join(rows)}</dl></section>'
            )
        audio_path = _e(audio["path"])
        cards.append(
            '<article class="sample-card">'
            f'<h2>{engine}</h2><p class="variant"><code>{variant}</code></p>'
            "<dl>"
            f"<dt>Profile</dt><dd>{profile}</dd>"
            f"<dt>Target</dt><dd>{target_values}</dd>"
            f'<dt>Status</dt><dd class="status">PASS</dd>'
            f"<dt>Source</dt><dd>{_e(sample['source_platform'])}</dd>"
            f"<dt>Profile id</dt><dd><code>{_e(sample['profile_id'])}</code></dd>"
            f"<dt>Audio SHA-256</dt><dd><code>{_e(audio['sha256'])}</code></dd>"
            f"<dt>Audio size</dt><dd>{_e(audio['size_bytes'])} bytes</dd>"
            "</dl>"
            + "".join(binding_sections)
            + f'<audio controls preload="none" src="{audio_path}" aria-label="Listen to {engine}, profile {profile}">'
            + "Audio playback is not supported by this browser.</audio>"
            + '<h3>Spoken-content validation</h3><dl class="metric-list">'
            + f"<dt>Compound WER</dt><dd>{_metric_text(metrics.get('compound_wer'))}</dd>"
            + f"<dt>Strict WER</dt><dd>{_metric_text(metrics.get('strict_wer'))}</dd>"
            + f"<dt>CER</dt><dd>{_metric_text(metrics.get('cer'))}</dd>"
            + f"<dt>Boundary matches</dt><dd>{_e(metrics.get('word_boundary_match_count') if metrics.get('word_boundary_match_count') is not None else 'n/a')}</dd>"
            + "</dl></article>"
        )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>BookBench Voice Gallery</title>"
        '<link rel="stylesheet" href="assets/bookbench.css">'
        '<script src="assets/bookbench.js" defer></script></head><body>'
        "<header><h1>BookBench Voice Gallery</h1>"
        "<p>Validated end-to-end audiobook samples from the BookBench pipeline.</p>"
        f'<div class="provenance"><span>Generated from commit <a href="{commit_url}"><code>{commit}</code></a></span>'
        f'<span>Platform: {platform}</span><span>Actions run: <a href="{run_url}">#{run_id}</a> (attempt {attempt})</span>'
        f'<span>Repository: <a href="https://github.com/{repository}">{repository}</a></span></div></header>'
        '<p class="notice">Compound WER, strict WER, and CER are spoken-content validation metrics. '
        "They are not subjective voice-quality scores and do not rank voices.</p>"
        f'<main class="gallery">{"".join(cards)}</main>'
        f'<footer><p>Generated by BookBench from commit <a href="{commit_url}"><code>{commit}</code></a>; '
        f'E2E run <a href="{run_url}">#{run_id}</a>; source platform {platform}.</p></footer>'
        "</body></html>\n"
    )


def _site_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def build_pages_site(
    *,
    input_root: str | Path,
    output_root: str | Path,
    repository: str,
    commit: str,
    run_id: str,
    attempt: str,
    platform: str = CANONICAL_PLATFORM,
    profiles_root: str | Path | None = None,
) -> Path:
    """Build a complete sanitized Pages site and replace output only on success."""
    _validate_provenance(repository, commit, run_id, attempt, platform)
    artifacts = discover_platform_artifacts(input_root, run_id=run_id, attempt=attempt, platform=platform)
    checkout_profiles = (
        Path(profiles_root) if profiles_root is not None else Path(__file__).resolve().parents[2] / "profiles"
    )
    if checkout_profiles.is_symlink() or not checkout_profiles.is_dir():
        raise GalleryBuildError("checked-out profiles directory is missing or unsafe")

    loaded: list[tuple[PublishedSample, Path]] = []
    total_sample_bytes = 0
    for engine in DEFAULT_ENGINES:
        sample, audio_source = _load_validated_sample_with_source(
            artifacts[engine],
            engine,
            run_id=run_id,
            attempt=attempt,
            platform=platform,
            profiles_root=checkout_profiles,
        )
        total_sample_bytes += sample.audio_size_bytes
        if total_sample_bytes > MAX_TOTAL_SAMPLE_BYTES:
            raise GalleryBuildError(f"total sample payload exceeds the {MAX_TOTAL_SAMPLE_BYTES}-byte publication limit")
        loaded.append((sample, audio_source))
    manifest = build_gallery_manifest(
        [sample for sample, _source in loaded],
        repository=repository,
        commit=commit,
        run_id=run_id,
        attempt=attempt,
        platform=platform,
    )

    output = Path(output_root)
    if output.is_symlink():
        raise GalleryBuildError("output root must not be a symlink")
    output_parent = output.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output_parent))
    backup: Path | None = None
    try:
        (staging / "assets").mkdir()
        (staging / "samples").mkdir()
        for sample, audio_source in loaded:
            relative = PurePosixPath(sample.audio_path)
            destination = staging.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(audio_source, destination)
            if destination.stat().st_size != sample.audio_size_bytes:
                raise GalleryBuildError(f"copied audio size changed for {sample.engine}")
            if hashlib.sha256(destination.read_bytes()).hexdigest() != sample.audio_sha256:
                raise GalleryBuildError(f"copied audio hash changed for {sample.engine}")
        (staging / "gallery.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        (staging / "index.html").write_text(render_index(manifest), encoding="utf-8")
        (staging / "assets" / "bookbench.css").write_text(CSS, encoding="utf-8")
        (staging / "assets" / "bookbench.js").write_text(JAVASCRIPT, encoding="utf-8")
        site_bytes = _site_size(staging)
        if site_bytes > MAX_SITE_BYTES:
            raise GalleryBuildError(f"generated site exceeds the {MAX_SITE_BYTES}-byte site limit")

        if output.exists():
            if not output.is_dir():
                raise GalleryBuildError("existing output root is not a directory")
            backup = Path(tempfile.mkdtemp(prefix=f".{output.name}.backup-", dir=output_parent))
            backup.rmdir()
            os.replace(output, backup)
        try:
            os.replace(staging, output)
        except OSError:
            if backup is not None and backup.exists() and not output.exists():
                os.replace(backup, output)
                backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a validated, sanitized BookBench GitHub Pages voice gallery.")
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--platform", default=CANONICAL_PLATFORM)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--profiles-root", type=Path)
    args = parser.parse_args(argv)
    try:
        output = build_pages_site(
            input_root=args.input_root,
            output_root=args.output_root,
            platform=args.platform,
            repository=args.repository,
            commit=args.commit,
            run_id=args.run_id,
            attempt=args.attempt,
            profiles_root=args.profiles_root,
        )
    except (GalleryBuildError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    audio_bytes = sum(path.stat().st_size for path in (output / "samples").rglob("*.mp3") if path.is_file())
    print(f"Built {len(DEFAULT_ENGINES)} samples; audio={audio_bytes} bytes; site={_site_size(output)} bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

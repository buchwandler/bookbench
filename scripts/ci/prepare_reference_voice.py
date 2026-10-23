from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import voicefolio

try:
    from .engine_matrix import LOCK_ENGINES as ENGINES
except ImportError:  # Script execution puts scripts/ci on sys.path.
    from engine_matrix import LOCK_ENGINES as ENGINES

ROOT = Path(__file__).resolve().parents[2]


def _profile_sources(profile: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    roles = profile.get("roles", {})
    if not isinstance(roles, Mapping):
        raise TypeError("profile roles must be a table")

    sources: list[tuple[str, Mapping[str, Any]]] = []
    for role_name, role in roles.items():
        if not isinstance(role, Mapping):
            continue
        binding = role.get("binding", {})
        if not isinstance(binding, Mapping):
            continue
        options = binding.get("options", {})
        if not isinstance(options, Mapping) or "voice_source" not in options:
            continue
        source = options["voice_source"]
        if not isinstance(source, Mapping):
            raise TypeError(f"voice_source for role {role_name!r} must be a table")
        if source.get("kind") != "managed_reference":
            continue
        if source.get("resolver") != "voicefolio":
            raise ValueError(f"managed voice_source for role {role_name!r} must use VoiceFolio")
        sources.append((str(role_name), source))
    return sources


def _profile_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _cache_hit(phases: list[str], error: BaseException | None = None) -> bool | None:
    if "download" in phases:
        return False
    if "verify" in phases:
        return True
    if isinstance(error, voicefolio.OfflineError):
        return False
    return None


def _progress_recorder(phases: list[str]) -> Callable[[Any], None]:
    def progress(event: Any) -> None:
        phase = getattr(event, "phase", None)
        if isinstance(phase, str):
            phases.append(phase)

    return progress


def prepare_reference_voice(
    engine: str,
    profile_path: Path,
    *,
    cache_dir: str | Path | None = None,
    offline: bool = False,
    validator: Callable[[Mapping[str, Any]], Any] | None = None,
    resolver: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Verify VoiceFolio sources in a profile and emit non-secret preparation evidence."""
    cache_value = cache_dir if cache_dir is not None else os.environ.get("VOICEFOLIO_CACHE_DIR")
    reported_cache_dir = str(Path(cache_value).expanduser().resolve()) if cache_value is not None else None
    report: dict[str, Any] = {
        "schema": "bookbench.reference-voice.v1",
        "engine": engine,
        "profile": profile_path.name,
        "profile_sha256": None,
        "cache_dir": reported_cache_dir,
        "status": "failed",
        "references": [],
    }

    try:
        payload = profile_path.read_bytes()
        profile_data = tomllib.loads(payload.decode("utf-8"))
        report["profile_sha256"] = _profile_digest(payload)
        sources = _profile_sources(profile_data)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        report["error_type"] = type(exc).__name__
        return report

    if not sources:
        report["status"] = "not_applicable"
        return report

    validate = validator or voicefolio.validate_voice_source
    resolve = resolver or voicefolio.resolve_voice_source
    failed = False
    references: list[dict[str, Any]] = []
    for role_name, source in sources:
        phases: list[str] = []
        item: dict[str, Any] = {
            "role": role_name,
            "ref": source.get("value") if isinstance(source.get("value"), str) else None,
            "sha256": None,
            "source_revision": None,
            "cache_hit": None,
            "status": "failed",
        }
        error: BaseException | None = None
        try:
            entry = validate(source)
            item["ref"] = str(entry.ref)
            item["sha256"] = entry.sha256
            item["source_revision"] = entry.source_revision

            progress = _progress_recorder(phases)
            resolve(
                source,
                cache_dir=cache_value,
                offline=offline,
                progress=progress,
            )
            item["status"] = "verified"
        except (voicefolio.VoicefolioError, OSError, TypeError, ValueError) as exc:
            error = exc
            item["error_type"] = type(exc).__name__
            failed = True
        item["cache_hit"] = _cache_hit(phases, error)
        references.append(item)

    report["references"] = references
    report["status"] = "failed" if failed else "verified"
    return report


def write_report(report: Mapping[str, Any], output_path: Path | None = None) -> str:
    serialized = json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized, encoding="utf-8")
    return serialized


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a profile's managed VoiceFolio references and write safe evidence."
    )
    parser.add_argument("--engine", required=True, choices=ENGINES)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    profile_path = args.profile or ROOT / "profiles" / f"{args.engine}.renderprofile.toml"
    if not profile_path.is_absolute():
        profile_path = ROOT / profile_path
    output_path = args.output
    if output_path is not None and not output_path.is_absolute():
        output_path = ROOT / output_path
    report = prepare_reference_voice(
        args.engine,
        profile_path,
        cache_dir=os.environ.get("VOICEFOLIO_CACHE_DIR"),
        offline=args.offline,
    )
    sys.stdout.write(write_report(report, output_path))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import hashlib
import json
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .config import Config, SpeechScopeConfig
from .profiles import (
    ProfileCase,
    ProfileSelection,
    coverage,
    inspect_profile,
    required_token_fields,
    select_profiles,
)
from .rendertext import renderplan_text

VERSION_PROBES = {
    "text2epub": ("text2epub", "version"),
    "ssmdconvert": ("ssmdconvert", "--version"),
    "utterplan": ("utterplan", "--version"),
    "voicerender": ("voicerender", "--version"),
    "audiocompose": ("audiocompose", "--version"),
    "audioexport": ("audioexport", "--version"),
    "speechscope": ("speechscope", "--version"),
    "ffmpeg": ("ffmpeg", "-version"),
}
TOOLS = tuple(VERSION_PROBES)

RICH_LINGUISTIC_FIELDS = frozenset({"pos", "tag", "morph"})

PIPER_DEPENDENCY_PACKAGES = ("voicerender", "pipersynth", "onnxvoice", "piperg2p")


def _installed_piper_package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in PIPER_DEPENDENCY_PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _version_probe_failed(version: str | None) -> bool:
    return version is not None and version.startswith("probe_failed(")


class PipelineError(RuntimeError):
    def __init__(self, message: str, *, voicerender_preflight_checks: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.voicerender_preflight_checks = voicerender_preflight_checks


def _utterplan_linguistics(profiles: list[ProfileCase], configured_spacy: str) -> dict[str, Any]:
    fields = required_token_fields(profiles)
    requires_spacy = bool(RICH_LINGUISTIC_FIELDS.intersection(fields))
    selected_profiles = [
        profile.label for profile in profiles if RICH_LINGUISTIC_FIELDS.intersection(profile.required_token_fields)
    ]
    error = None
    if configured_spacy == "auto":
        spacy_policy = "sm" if requires_spacy else "off"
        reason = "selected_profile_requirements" if requires_spacy else "no_rich_token_requirements"
    elif configured_spacy == "off" and requires_spacy:
        spacy_policy = "off"
        reason = "conflicting_configured_policy"
        error = (
            f"selected profiles {', '.join(selected_profiles)} require token fields {', '.join(fields)}, "
            'but utterplan.spacy = "off"'
        )
    else:
        spacy_policy = configured_spacy
        reason = "configured_policy"
    return {
        "required_token_fields": list(fields),
        "requires_spacy": requires_spacy,
        "configured_spacy": configured_spacy,
        "spacy_policy": spacy_policy,
        "reason": reason,
        "status": "error" if error else "ready",
        "error": error,
    }


def _utterplan_plan_provenance(book: Path) -> list[dict[str, Any]]:
    provenance = []
    for path in sorted((book / "utterplan").glob("*.utterplan.toml")):
        plan_data = tomllib.loads(path.read_text(encoding="utf-8"))
        linguistics = plan_data.get("linguistics", [])
        if not isinstance(linguistics, list):
            continue
        for item in linguistics:
            if not isinstance(item, dict):
                continue
            record = {"plan": str(path.relative_to(book))}
            for field in ("language", "provider", "model", "provider_version", "model_version"):
                if field in item:
                    record[field] = item[field]
            if "provider" in record or "model" in record:
                provenance.append(record)
    return provenance


def _configured_speechscope_thresholds(config: SpeechScopeConfig) -> dict[str, float]:
    thresholds = {}
    for name in ("max_compound_wer", "max_wer", "max_cer"):
        value = getattr(config, name)
        if value is not None:
            thresholds[name] = value
    return thresholds


def _speechscope_config_provenance(config: SpeechScopeConfig) -> dict[str, Any]:
    return {
        "backend": config.backend,
        "device": config.device,
        "model": config.model,
        "normalization": config.normalization,
        "reference_mode": config.reference_mode,
    }


def _speechscope_failure_message(evaluation: dict) -> str:
    failed = [rule for rule in evaluation.get("rules", []) if rule.get("status") == "fail"]
    if not failed:
        return "SpeechScope threshold failure"
    details = ", ".join(
        f"{rule.get('name')} observed={rule.get('observed')} expected={rule.get('expected')}" for rule in failed
    )
    return f"SpeechScope threshold failure: {details}"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(slots=True)
class CommandResult:
    argv: list[str]
    returncode: int
    duration_s: float
    stdout: str
    stderr: str


def _parse_voicerender_failure(result: CommandResult) -> tuple[dict[str, Any] | None, str | None]:
    raw = result.stdout.strip()
    if not raw:
        return None, "missing JSON stdout"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg}"
    if not isinstance(payload, dict):
        return None, "JSON payload must be an object"
    items = payload.get("items")
    if not isinstance(items, list):
        return payload, "JSON payload is missing a list of items"
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return payload, f"items[{index}] must be an object"
        status = item.get("status")
        if not isinstance(status, str):
            return payload, f"items[{index}] is missing a string status"
        if status != "failed":
            continue
        required = ("id", "render_plan_id", "error_code", "error_message", "replan_required")
        missing = [key for key in required if key not in item]
        if missing:
            return payload, f"failed items[{index}] is missing required fields: {', '.join(missing)}"
        if not isinstance(item["id"], str) or not item["id"]:
            return payload, f"failed items[{index}].id must be a non-empty string"
        if not isinstance(item["render_plan_id"], str) or not item["render_plan_id"]:
            return payload, f"failed items[{index}].render_plan_id must be a non-empty string"
        if not isinstance(item["error_code"], str) or not item["error_code"]:
            return payload, f"failed items[{index}].error_code must be a non-empty string"
        if not isinstance(item["error_message"], str):
            return payload, f"failed items[{index}].error_message must be a string"
        if not isinstance(item["replan_required"], bool):
            return payload, f"failed items[{index}].replan_required must be a boolean"
        if (
            "preflight_report" in item
            and item["preflight_report"] is not None
            and not isinstance(item["preflight_report"], str)
        ):
            return payload, f"failed items[{index}].preflight_report must be a string or null"
    return payload, None


def _voicerender_replanable(result: CommandResult) -> bool:
    payload, invalid_reason = _parse_voicerender_failure(result)
    if invalid_reason is not None or payload is None:
        return False
    failed = [item for item in payload["items"] if item["status"] == "failed"]
    return bool(failed) and all(
        item["replan_required"] is True and item["error_code"] == "preflight.replan" for item in failed
    )


MAX_PREFLIGHT_REPORT_BYTES = 256 * 1024
MAX_PREFLIGHT_VALUE_CHARS = 512
MAX_PREFLIGHT_CHECKS = 64
MAX_STORED_SEGMENTS_PER_CHECK = 32
PREFLIGHT_PASS_STATUSES = frozenset({"pass", "passed", "ok", "ready"})
PREFLIGHT_SECRET = re.compile(r"(?i)\b(?:hf_[a-z0-9_-]{8,}|(?:token|password|secret|authorization)\s*[=:]\s*)\S+")


def _bounded_preflight_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    text = PREFLIGHT_SECRET.sub("[REDACTED]", text)
    if not text:
        return None
    if len(text) > MAX_PREFLIGHT_VALUE_CHARS:
        text = text[: MAX_PREFLIGHT_VALUE_CHARS - 1] + "…"
    return text


def _safe_preflight_report_path(variant_dir: Path, report_path: object) -> Path | None:
    if not isinstance(report_path, str) or not report_path or "\x00" in report_path:
        return None
    posix_path = PurePosixPath(report_path)
    windows_path = PureWindowsPath(report_path)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive or windows_path.root:
        return None
    normalized = report_path.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if any(part == ".." for part in relative.parts):
        return None
    try:
        root = variant_dir.resolve(strict=True)
        resolved = (root / Path(*relative.parts)).resolve(strict=True)
        resolved.relative_to(root)
        info = resolved.stat()
    except (OSError, RuntimeError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PREFLIGHT_REPORT_BYTES:
        return None
    return resolved


def _read_voicerender_preflight_checks(variant_dir: Path, report_path: object) -> list[dict[str, Any]]:
    path = _safe_preflight_report_path(variant_dir, report_path)
    if path is None:
        return []
    try:
        with path.open("rb") as source:
            content = source.read(MAX_PREFLIGHT_REPORT_BYTES + 1)
        if len(content) > MAX_PREFLIGHT_REPORT_BYTES:
            return []
        document = tomllib.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    checks = document.get("checks") if isinstance(document, dict) else None
    if not isinstance(checks, list):
        return []

    extracted: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = _bounded_preflight_text(check.get("status"))
        if status is None or status.casefold() in PREFLIGHT_PASS_STATUSES:
            continue
        item: dict[str, Any] = {"status": status}
        for field in ("source", "metric", "detail"):
            value = _bounded_preflight_text(check.get(field))
            if value is not None:
                item[field] = value
        if not any(field in item for field in ("source", "metric", "detail")):
            continue
        segment_id = _bounded_preflight_text(check.get("segment_id"))
        segment_number = check.get("segment_number")
        if isinstance(segment_number, bool) or not isinstance(segment_number, (int, str)):
            segment_number = None
        elif isinstance(segment_number, str):
            segment_number = _bounded_preflight_text(segment_number)
        if segment_id is not None:
            item["segment_id"] = segment_id
        if segment_number is not None:
            item["segment_number"] = segment_number
        extracted.append(item)
    return extracted


def _deduplicate_voicerender_preflight_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[object, ...], dict[str, Any]] = {}
    for index, check in enumerate(checks):
        key = tuple(check.get(field) for field in ("source", "status", "metric", "detail"))
        group = groups.get(key)
        if group is None:
            if len(groups) >= MAX_PREFLIGHT_CHECKS:
                continue
            group = {field: check[field] for field in ("source", "status", "metric", "detail") if field in check}
            group["segments"] = []
            group["_seen_segments"] = set()
            groups[key] = group
        segment_id = check.get("segment_id")
        segment_number = check.get("segment_number")
        marker: tuple[object, ...] = (segment_id, segment_number)
        if segment_id is None and segment_number is None:
            marker = ("unidentified", index)
        seen = group["_seen_segments"]
        if marker in seen:
            continue
        seen.add(marker)
        if (segment_id is not None or segment_number is not None) and len(
            group["segments"]
        ) < MAX_STORED_SEGMENTS_PER_CHECK:
            segment = {}
            if segment_id is not None:
                segment["segment_id"] = segment_id
            if segment_number is not None:
                segment["segment_number"] = segment_number
            group["segments"].append(segment)

    result: list[dict[str, Any]] = []
    for group in groups.values():
        seen = group.pop("_seen_segments")
        group["affected_segment_count"] = len(seen)
        if not group["segments"]:
            del group["segments"]
        result.append(group)
    return result


def _extract_voicerender_preflight_checks(
    failed_items: list[dict[str, Any]], variant_dir: Path | None
) -> list[dict[str, Any]]:
    if variant_dir is None:
        return []
    checks: list[dict[str, Any]] = []
    for item in failed_items:
        checks.extend(_read_voicerender_preflight_checks(variant_dir, item.get("preflight_report")))
    return _deduplicate_voicerender_preflight_checks(checks)


def _voicerender_failure_diagnostics(
    result: CommandResult, variant_dir: Path | None = None
) -> tuple[str, list[dict[str, Any]]]:
    payload, invalid_reason = _parse_voicerender_failure(result)
    parts = ["VoiceRender failed at voicerender."]
    if invalid_reason is not None:
        parts.append(f"response is not safely replanable: {invalid_reason}")
    failed: list[dict[str, Any]] = []
    if payload is not None and isinstance(payload.get("items"), list):
        failed = [item for item in payload["items"] if isinstance(item, dict) and item.get("status") == "failed"]
    for item in failed:
        code = item.get("error_code")
        replan_required = item.get("replan_required")
        if code == "preflight.reject" and replan_required is False:
            kind = "hard VoiceRender preflight reject"
        elif code == "preflight.replan" and replan_required is True:
            kind = "VoiceRender capacity replan required"
        else:
            kind = "VoiceRender pipeline/runtime failure"
        diagnostic = {
            "error_code": code,
            "error_message": item.get("error_message"),
            "replan_required": replan_required,
            "preflight_report": item.get("preflight_report"),
            "chapter_id": item.get("id", item.get("chapter_id")),
            "render_plan_id": item.get("render_plan_id"),
        }
        parts.append(f"{kind}: {json.dumps(diagnostic, sort_keys=True)}")

    preflight_checks = _extract_voicerender_preflight_checks(failed, variant_dir)
    if preflight_checks:
        parts.append(f"VoiceRender preflight checks (deduplicated): {json.dumps(preflight_checks, sort_keys=True)}")

    if not failed and payload is not None:
        diagnostic = {
            "error_code": payload.get("error_code", payload.get("code")),
            "error_message": payload.get("error_message", payload.get("error")),
            "replan_required": payload.get("replan_required"),
            "preflight_report": payload.get("preflight_report"),
            "chapter_id": payload.get("id", payload.get("chapter_id")),
            "render_plan_id": payload.get("render_plan_id"),
        }
        if any(value is not None for value in diagnostic.values()):
            parts.append(f"VoiceRender response: {json.dumps(diagnostic, sort_keys=True)}")
        elif payload.get("items") == []:
            parts.append("VoiceRender response contained no failed selected items.")
    if result.stderr.strip():
        parts.append(f"stderr: {result.stderr.strip()}")
    if payload is None and result.stdout.strip():
        parts.append(f"stdout: {result.stdout.strip()}")
    if len(parts) == 1:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        parts.append(detail)
    return "\n".join(parts), preflight_checks


def _voicerender_failure_message(result: CommandResult, variant_dir: Path | None = None) -> str:
    return _voicerender_failure_diagnostics(result, variant_dir)[0]


@dataclass(slots=True)
class ProfileResult:
    profile: str
    variant: str
    engines: list[str]
    status: str
    export: str | None = None
    speechscope_report: str | None = None
    wer: float | None = None
    compound_wer: float | None = None
    cer: float | None = None
    content_match: bool | None = None
    word_boundary_match_count: int | None = None
    error: str | None = None
    failed_stage: str | None = None
    voicerender_preflight_checks: list[dict[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: str
    run_dir: Path
    book_epub: Path
    book: Path
    export_dir: Path


@dataclass(frozen=True, slots=True)
class ProfileRunContext:
    profile: ProfileCase
    staged_profile: Path
    variant_dir: Path
    export_path: Path
    listening_path: Path
    validation_wav: Path
    expected_text: Path
    policy_path: Path
    speechscope_report: Path


class Harness:
    def __init__(self, config: Config, *, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.run_dir: Path | None = None
        self.commands: list[CommandResult] = []
        self._current_stage: str | None = None

    def _record_command(self, result: CommandResult) -> None:
        self.commands.append(result)
        if self.run_dir is None:
            return
        with (self.run_dir / "commands.log").open("a", encoding="utf-8") as fh:
            fh.write(f"$ {shlex.join(result.argv)}\n")
            fh.write("[stdout]\n")
            if result.stdout:
                fh.write(result.stdout)
                if not result.stdout.endswith("\n"):
                    fh.write("\n")
            fh.write("[stderr]\n")
            if result.stderr:
                fh.write(result.stderr)
                if not result.stderr.endswith("\n"):
                    fh.write("\n")
            fh.write(f"[exit={result.returncode} duration={result.duration_s:.3f}s]\n\n")

    def _run(self, argv: Iterable[str], *, check: bool = True) -> CommandResult:
        args = [str(x) for x in argv]
        print("+", shlex.join(args))
        if self.dry_run:
            result = CommandResult(args, 0, 0.0, "", "")
            self.commands.append(result)
            return result
        started = time.monotonic()
        try:
            proc = subprocess.run(args, text=True, capture_output=True, check=False)
        except OSError as exc:
            result = CommandResult(args, 127, time.monotonic() - started, "", str(exc))
            self._record_command(result)
            if check:
                raise PipelineError(f"Command failed: {shlex.join(args)}\n{exc}") from exc
            return result
        result = CommandResult(args, proc.returncode, time.monotonic() - started, proc.stdout, proc.stderr)
        self._record_command(result)
        if check and proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
            raise PipelineError(f"Command failed: {shlex.join(args)}\n{detail}")
        return result

    def versions(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for tool, argv in VERSION_PROBES.items():
            if shutil.which(tool) is None:
                out[tool] = None
                continue
            started = time.monotonic()
            try:
                proc = subprocess.run(argv, text=True, capture_output=True, check=False)
            except OSError as exc:
                result = CommandResult(list(argv), 127, time.monotonic() - started, "", str(exc))
                self._record_command(result)
                out[tool] = f"probe_failed(exit=127): {exc}"
                continue
            result = CommandResult(list(argv), proc.returncode, time.monotonic() - started, proc.stdout, proc.stderr)
            self._record_command(result)
            output = (proc.stderr if proc.returncode != 0 else proc.stdout or proc.stderr).splitlines()
            if proc.returncode != 0:
                detail = output[0].strip() if output else "no output"
                out[tool] = f"probe_failed(exit={proc.returncode}): {detail}"
            elif output:
                out[tool] = output[0].strip()
            else:
                out[tool] = "probe_failed(exit=0): no version output"
        return out

    def _probe_speechscope_capabilities(self) -> dict[str, Any]:
        command = ["speechscope", "metrics", "list", "--format", "json"]
        if shutil.which("speechscope") is None:
            return {
                "status": "unavailable",
                "command": command,
                "metrics": [],
                "supports_compound_wer": False,
                "supports_max_compound_wer": False,
                "error": "speechscope is not available on PATH",
            }
        started = time.monotonic()
        try:
            proc = subprocess.run(command, text=True, capture_output=True, check=False)
        except OSError as exc:
            result = CommandResult(command, 127, time.monotonic() - started, "", str(exc))
            self._record_command(result)
            return {
                "status": "probe_failed",
                "command": command,
                "metrics": [],
                "supports_compound_wer": False,
                "supports_max_compound_wer": False,
                "error": str(exc),
            }
        result = CommandResult(command, proc.returncode, time.monotonic() - started, proc.stdout, proc.stderr)
        self._record_command(result)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            return {
                "status": "probe_failed",
                "command": command,
                "metrics": [],
                "supports_compound_wer": False,
                "supports_max_compound_wer": False,
                "error": detail,
            }
        try:
            metrics = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return {
                "status": "probe_failed",
                "command": command,
                "metrics": [],
                "supports_compound_wer": False,
                "supports_max_compound_wer": False,
                "error": f"invalid SpeechScope metric capability response: {exc.msg}",
            }
        if not isinstance(metrics, list) or any(not isinstance(metric, str) for metric in metrics):
            return {
                "status": "probe_failed",
                "command": command,
                "metrics": [],
                "supports_compound_wer": False,
                "supports_max_compound_wer": False,
                "error": "SpeechScope metric capability response must be a list of strings",
            }
        supported = "compound_wer" in metrics
        return {
            "status": "supported" if supported else "unsupported",
            "command": command,
            "metrics": metrics,
            "supports_compound_wer": supported,
            "supports_max_compound_wer": supported,
            "error": None if supported else "SpeechScope does not report the compound_wer metric",
        }

    def _probe_speechscope_backend(self) -> dict[str, Any]:
        speech = self.config.speechscope
        command = ["speechscope", "doctor", "--backend", speech.backend]
        if speech.model:
            command.extend(["--model", speech.model])
        command.extend(["--device", speech.device, "--format", "json"])
        result = {
            "ok": False,
            "status": "unavailable",
            "command": command,
            "backend": speech.backend,
            "device": speech.device,
            "model": speech.model,
            "runtime": {},
            "model_store": {},
            "error": "speechscope is not available on PATH",
        }
        if shutil.which("speechscope") is None:
            return result

        started = time.monotonic()
        try:
            proc = subprocess.run(command, text=True, capture_output=True, check=False)
        except OSError as exc:
            self._record_command(CommandResult(command, 127, time.monotonic() - started, "", str(exc)))
            result.update(status="probe_failed", error=str(exc))
            return result

        checked = CommandResult(command, proc.returncode, time.monotonic() - started, proc.stdout, proc.stderr)
        self._record_command(checked)
        try:
            payload = json.loads(checked.stdout)
        except json.JSONDecodeError as exc:
            result.update(
                status="probe_failed",
                error=f"invalid SpeechScope readiness response: {exc.msg}",
            )
            return result
        if not isinstance(payload, dict):
            result.update(status="probe_failed", error="SpeechScope readiness response must be a JSON object")
            return result

        ready = checked.returncode == 0 and payload.get("ok") is True
        detail = payload.get("error")
        if not detail:
            errors = payload.get("errors")
            if isinstance(errors, list):
                detail = "; ".join(str(error) for error in errors) or None
            elif errors:
                detail = str(errors)
        if not ready and not detail:
            detail = checked.stderr.strip() or checked.stdout.strip() or "SpeechScope doctor reported not ready"
        result.update(
            ok=ready,
            status="ready" if ready else "not_ready",
            runtime=payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {},
            model_store=payload.get("model_store") if isinstance(payload.get("model_store"), dict) else {},
            error=None if ready else str(detail),
        )
        return result

    def _probe_engine_inventory(self) -> tuple[list[dict], str | None]:
        argv = ["voicerender", "engines", "list", "--json"]
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, text=True, capture_output=True, check=False)
        except OSError as exc:
            result = CommandResult(argv, 127, time.monotonic() - started, "", str(exc))
            self._record_command(result)
            return [], f"engine inventory probe failed: {exc}"
        result = CommandResult(argv, proc.returncode, time.monotonic() - started, proc.stdout, proc.stderr)
        self._record_command(result)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            return [], f"engine inventory probe failed: {detail}"
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return [], f"engine inventory returned invalid JSON: {exc.msg}"
        if not isinstance(payload, list) or any(
            not isinstance(engine, dict) or not isinstance(engine.get("id"), str) for engine in payload
        ):
            return [], "engine inventory JSON must be a list of objects with string ids"
        return payload, None

    def _engine_provenance(self, profiles: list[ProfileCase]) -> dict:
        if self.dry_run:
            inventory, error, status = [], "Engine inventory probe skipped for dry-run.", "not_probed"
        else:
            inventory, error = self._probe_engine_inventory()
            status = "captured" if error is None else "unavailable"
        selected_targets = [
            {"candidate_label": profile.label, "engine_id": engine, "target_id": target_id}
            for profile in profiles
            for engine, target_id in profile.targets
        ]
        return {
            "schema": "bookbench.engine-provenance.v1",
            "command": ["voicerender", "engines", "list", "--json"],
            "status": status,
            "error": error,
            "engines": inventory,
            "package_versions": _installed_piper_package_versions(),
            "selected_targets": selected_targets,
        }

    def _candidate_profiles(self) -> list[tuple[str, Path]]:
        if self.config.voices:
            return [(voice.name, voice.profile) for voice in self.config.voices]
        suffix = ".renderprofile.toml"
        return [(path.name[: -len(suffix)], path) for path in sorted(self.config.profiles_dir.glob(f"*{suffix}"))]

    def _selected_candidate_profiles(self, selection: ProfileSelection | None) -> list[tuple[str, Path]]:
        candidates = self._candidate_profiles()
        if selection is None or not selection.active:
            return candidates

        if selection.profiles:
            requested = set(selection.profiles)
            configured = {label for label, _path in candidates}
            missing = requested - configured
            if missing:
                raise ValueError("Unknown profile selector(s): " + ", ".join(sorted(missing)))
            return [(label, path) for label, path in candidates if label in requested]

        valid_cases = []
        for label, path in candidates:
            try:
                profile = inspect_profile(path)
            except (OSError, ValueError):
                continue
            valid_cases.append(ProfileCase(label, profile))
        selected = select_profiles(valid_cases, selection)
        selected_keys = {(case.label, case.path.resolve()) for case in selected}
        return [(label, path) for label, path in candidates if (label, path.resolve()) in selected_keys]

    def _profiles(self, selection: ProfileSelection | None = None) -> list[ProfileCase]:
        return [
            ProfileCase(label, inspect_profile(path)) for label, path in self._selected_candidate_profiles(selection)
        ]

    def _selection_provenance(
        self,
        selection: ProfileSelection | None,
        profiles: list[ProfileCase],
        *,
        profile_labels: list[str] | None = None,
    ) -> dict:
        selected = selection or ProfileSelection()
        mode = "all" if not selected.active else "engine" if selected.engines else "profile"
        requested = list(selected.engines or selected.profiles)
        return {
            "mode": mode,
            "requested": requested,
            "selected_profiles": profile_labels or [profile.label for profile in profiles],
            "selected_variants": [profile.variant for profile in profiles],
            "selected_engines": sorted({engine for profile in profiles for engine in profile.engines}),
            "full_matrix": not selected.active,
        }

    def doctor(self, *, selection: ProfileSelection | None = None) -> dict:
        candidates = self._selected_candidate_profiles(selection)
        tool_paths = {tool: shutil.which(tool) for tool in TOOLS}
        tool_versions = self.versions()
        speechscope_capabilities = self._probe_speechscope_capabilities()
        speechscope_backend = self._probe_speechscope_backend()
        tools = {
            tool: {
                "path": tool_paths[tool],
                "available": tool_paths[tool] is not None,
                "version": tool_versions[tool],
                "version_probe_failed": _version_probe_failed(tool_versions[tool]),
            }
            for tool in TOOLS
        }

        profiles = []
        valid_profiles: list[ProfileCase] = []
        variant_paths: dict[str, list[str]] = {}
        found_engines: set[str] = set()
        for label, path in candidates:
            diagnostic = {
                "path": str(path),
                "label": label,
                "exists": path.is_file(),
                "valid": False,
                "profile_id": None,
                "variant_label": None,
                "variant": None,
                "engines": [],
                "staged_profile_target": None,
                "validation": {"status": "not_run", "returncode": None, "stdout": "", "stderr": ""},
            }
            try:
                info = inspect_profile(path)
            except (OSError, ValueError) as exc:
                diagnostic["error"] = str(exc)
            else:
                profile = ProfileCase(label, info)
                diagnostic.update(
                    {
                        "valid": True,
                        "profile_id": profile.profile_id,
                        "variant_label": profile.variant_label,
                        "variant": profile.variant,
                        "engines": list(profile.engines),
                        "staged_profile_target": str(
                            self.config.work_root
                            / "<run-id>"
                            / "book.ssmdbook"
                            / "renderplan"
                            / "_bookbench_profiles"
                            / profile.variant
                            / profile.path.name
                        ),
                    }
                )
                valid_profiles.append(profile)
                found_engines.update(profile.engines)
                variant_paths.setdefault(profile.variant, []).append(str(path))
            if tool_paths["voicerender"] is None:
                diagnostic["validation"] = {
                    "status": "unavailable",
                    "returncode": None,
                    "stdout": "",
                    "stderr": "voicerender is not available on PATH",
                }
            else:
                result = subprocess.run(
                    ["voicerender", "profile", "validate", str(path)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                diagnostic["validation"] = {
                    "status": "passed" if result.returncode == 0 else "failed",
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            profiles.append(diagnostic)

        engine_provenance = self._engine_provenance(valid_profiles)
        selection_provenance = self._selection_provenance(
            selection,
            valid_profiles,
            profile_labels=[label for label, _path in candidates],
        )
        duplicate_variants = [
            {"variant": variant, "profiles": paths}
            for variant, paths in sorted(variant_paths.items())
            if len(paths) > 1
        ]
        source = {"path": str(self.config.source), "exists": self.config.source.is_file()}
        work_root = {"path": str(self.config.work_root), "writable": False, "error": None}
        try:
            self.config.work_root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.config.work_root, prefix=".bookbench-doctor-"):
                pass
        except OSError as exc:
            work_root["error"] = str(exc)
        else:
            work_root["writable"] = True

        selected = selection or ProfileSelection()
        required_engines = self.config.required_engines if not selected.active else selected.engines
        missing_engines = set(required_engines) - found_engines
        engine_coverage = {
            "found": sorted(found_engines),
            "required": sorted(required_engines),
            "missing": sorted(missing_engines),
        }
        profile_checks_pass = bool(profiles) and all(
            profile["valid"] and profile["validation"]["status"] == "passed" for profile in profiles
        )
        linguistics = _utterplan_linguistics(valid_profiles, self.config.utterplan.spacy)
        tools_pass = all(
            item["available"] and item["version"] is not None and not item["version_probe_failed"]
            for item in tools.values()
        )
        speechscope_capability_pass = (
            self.config.speechscope.max_compound_wer is None or speechscope_capabilities["supports_max_compound_wer"]
        )
        return {
            "ok": (
                tools_pass
                and speechscope_backend["ok"]
                and speechscope_capability_pass
                and profile_checks_pass
                and not duplicate_variants
                and source["exists"]
                and work_root["writable"]
                and not missing_engines
                and linguistics["status"] == "ready"
            ),
            "linguistics": linguistics,
            "tools": tools,
            "speechscope_capabilities": speechscope_capabilities,
            "speechscope_backend": speechscope_backend,
            "profiles": profiles,
            "duplicate_variants": duplicate_variants,
            "source": source,
            "work_root": work_root,
            "engine_coverage": engine_coverage,
            "engine_provenance": engine_provenance,
            "selection": selection_provenance,
        }

    def _chapter_args(self) -> list[str]:
        return [] if self.config.chapters == "all" else ["--chapters", self.config.chapters]

    def _stage_profile(self, book: Path, profile: ProfileCase) -> Path:
        staged = book / "renderplan" / "_bookbench_profiles" / profile.variant / profile.path.name
        if self.dry_run:
            return staged
        staged.parent.mkdir(parents=True, exist_ok=True)
        source_bytes = profile.path.read_bytes()
        if staged.exists() and staged.read_bytes() != source_bytes:
            raise PipelineError(f"Staged RenderProfile collision: {staged}")
        staged.write_bytes(source_bytes)
        return staged

    def _validate_profile(self, profile: ProfileCase) -> None:
        self._current_stage = "profile_validate"
        self._run(["voicerender", "profile", "validate", profile.path])

    def _profile_run_context(self, run: RunContext, profile: ProfileCase, staged_profile: Path) -> ProfileRunContext:
        export_stem = profile.label
        return ProfileRunContext(
            profile=profile,
            staged_profile=staged_profile,
            variant_dir=run.book / "renderplan" / profile.variant,
            export_path=run.export_dir / f"{export_stem}.m4b",
            listening_path=run.export_dir / f"{export_stem}.listen.mp3",
            validation_wav=run.export_dir / f"{export_stem}.validation.wav",
            expected_text=run.export_dir / f"{export_stem}.expected.txt",
            policy_path=run.export_dir / f"{export_stem}.speechscope-policy.json",
            speechscope_report=run.export_dir / f"{export_stem}.speechscope.json",
        )

    def _build_renderplan(self, run: RunContext, context: ProfileRunContext) -> None:
        self._current_stage = "renderplan"
        self._run(
            [
                "utterplan",
                "render-book",
                run.book,
                "--profile",
                context.staged_profile,
                "--variant",
                context.profile.variant_label,
                *self._chapter_args(),
            ]
        )

    def _render_audio(self, run: RunContext, context: ProfileRunContext) -> None:
        render_cmd = [
            "voicerender",
            "render-book",
            run.book,
            "--variant",
            context.profile.variant,
            "--cache-dir",
            self.config.cache_dir,
            "--emphasis-policy",
            "defer-to-compose",
            "--json",
            *self._chapter_args(),
        ]
        for attempt in range(self.config.max_replans + 1):
            self._current_stage = "voicerender"
            rendered = self._run(render_cmd, check=False)
            if rendered.returncode == 0:
                return
            if not _voicerender_replanable(rendered):
                message, checks = _voicerender_failure_diagnostics(rendered, context.variant_dir)
                raise PipelineError(message, voicerender_preflight_checks=checks or None)
            if attempt >= self.config.max_replans:
                message, checks = _voicerender_failure_diagnostics(rendered, context.variant_dir)
                raise PipelineError(message, voicerender_preflight_checks=checks or None)
            self._replan_if_needed(run, context, rendered)

    def _replan_if_needed(self, run: RunContext, context: ProfileRunContext, rendered: CommandResult) -> None:
        self._current_stage = "replan"
        replanned = self._run(
            [
                "utterplan",
                "replan-book",
                run.book,
                "--variant",
                context.profile.variant,
                *self._chapter_args(),
            ],
            check=False,
        )
        if replanned.returncode != 0:
            replan_detail = replanned.stderr.strip() or replanned.stdout.strip() or f"exit {replanned.returncode}"
            failure_message, checks = _voicerender_failure_diagnostics(rendered, context.variant_dir)
            raise PipelineError(
                "VoiceRender requested a capacity replan, but UtterPlan replan-book failed.\n"
                f"VoiceRender: {failure_message}\nreplan: {replan_detail}",
                voicerender_preflight_checks=checks or None,
            )

    def _compose(self, run: RunContext, context: ProfileRunContext) -> None:
        self._current_stage = "compose"
        self._run(
            [
                "audiocompose",
                "compose-book",
                run.book,
                "--variant",
                context.profile.variant,
                "--json",
                *self._chapter_args(),
            ]
        )

    def _export(self, run: RunContext, context: ProfileRunContext) -> None:
        self._current_stage = "export"
        run.export_dir.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "audioexport",
                "export-book",
                run.book,
                "--variant",
                context.profile.variant,
                "--output",
                context.export_path,
                *self._chapter_args(),
            ]
        )

    def _make_listening_copy(self, context: ProfileRunContext) -> None:
        self._current_stage = "listen_encode"
        self._run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                context.export_path,
                "-map",
                "0:a:0",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "4",
                context.listening_path,
            ]
        )

    def _decode_for_validation(self, context: ProfileRunContext) -> None:
        self._current_stage = "validation_decode"
        self._run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                context.export_path,
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                context.validation_wav,
            ]
        )

    def _build_reference(self, context: ProfileRunContext) -> None:
        self._current_stage = "reference_extract"
        if self.dry_run:
            expected_text = "Dry-run: expected text would be derived from the selected reference mode.\n"
        elif self.config.speechscope.reference_mode == "source":
            expected_text = self.config.source.read_text(encoding="utf-8")
        else:
            expected_text = renderplan_text(context.variant_dir)
        context.expected_text.write_text(expected_text, encoding="utf-8")

    def _run_speechscope(self, context: ProfileRunContext) -> tuple[CommandResult, dict]:
        self._current_stage = "speechscope"
        context.policy_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    **_configured_speechscope_thresholds(self.config.speechscope),
                    "missing_timing": "review",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        cmd = [
            "speechscope",
            "analyze",
            context.validation_wav,
            "--text-file",
            context.expected_text,
            "--thresholds",
            context.policy_path,
            "--backend",
            self.config.speechscope.backend,
            "--device",
            self.config.speechscope.device,
            "--normalization",
            self.config.speechscope.normalization,
            "--format",
            "json",
            "--json",
            context.speechscope_report,
        ]
        if self.config.speechscope.model:
            cmd.extend(["--model", self.config.speechscope.model])
        checked = self._run(cmd, check=False)
        if checked.returncode not in (0, 1):
            raise PipelineError(checked.stderr.strip() or checked.stdout.strip() or "SpeechScope failed")
        data = {} if self.dry_run else json.loads(context.speechscope_report.read_text(encoding="utf-8"))
        return checked, data

    def _render_engine(self, run: RunContext, profile: ProfileCase) -> ProfileResult:
        self._validate_profile(profile)
        self._current_stage = "renderplan"
        staged_profile = self._stage_profile(run.book, profile)
        context = self._profile_run_context(run, profile, staged_profile)
        self._build_renderplan(run, context)
        self._render_audio(run, context)
        self._compose(run, context)
        self._export(run, context)
        self._make_listening_copy(context)
        self._decode_for_validation(context)
        self._build_reference(context)
        checked, data = self._run_speechscope(context)
        metrics = data.get("metrics") or {}
        evaluation = data.get("evaluation") or {}
        if self.dry_run:
            status = "pass"
        else:
            status = (
                "pass"
                if checked.returncode == 0 and evaluation.get("status") in {"pass", "review", "not_evaluated"}
                else "fail"
            )
        return ProfileResult(
            profile=profile.label,
            variant=profile.variant,
            engines=list(profile.engines),
            status=status,
            export=str(context.export_path),
            speechscope_report=str(context.speechscope_report),
            wer=metrics.get("wer"),
            compound_wer=metrics.get("compound_wer"),
            cer=metrics.get("cer"),
            content_match=metrics.get("content_match"),
            word_boundary_match_count=metrics.get("word_boundary_match_count"),
            error=None if status == "pass" else _speechscope_failure_message(evaluation),
            failed_stage="speechscope" if status != "pass" else None,
        )

    def _write_provenance(self) -> None:
        if self.run_dir is None or self._run_metadata is None:
            return
        for profile in self._profile_metadata:
            staged = Path(profile["staged_path"])
            profile["staged_sha256"] = _file_sha256(staged) if staged.is_file() else None
        (self.run_dir / "run.json").write_text(json.dumps(self._run_metadata, indent=2) + "\n", encoding="utf-8")
        (self.run_dir / "profiles.json").write_text(
            json.dumps(self._profile_metadata, indent=2) + "\n", encoding="utf-8"
        )

    def run(
        self,
        *,
        run_id: str | None = None,
        selection: ProfileSelection | None = None,
    ) -> tuple[Path, list[ProfileResult]]:
        self.run_dir = None
        self.commands = []
        self._current_stage = None
        self._run_error = None
        self._run_metadata = None
        self._profile_metadata = []
        self._run_started_at = datetime.now(UTC)
        try:
            return self._run_pipeline(run_id=run_id, selection=selection)
        except Exception as exc:
            self._run_error = str(exc)
            raise
        finally:
            if self.run_dir is not None and self._run_metadata is not None:
                self._run_metadata["end_timestamp"] = datetime.now(UTC).isoformat()
                self._run_metadata["failed_stage"] = self._current_stage if self._run_error else None
                self._run_metadata["error"] = self._run_error
                self._write_provenance()

    def _run_pipeline(
        self,
        *,
        run_id: str | None = None,
        selection: ProfileSelection | None = None,
    ) -> tuple[Path, list[ProfileResult]]:
        profiles = self._profiles(selection)
        if not profiles:
            raise PipelineError(f"No *.renderprofile.toml files found in {self.config.profiles_dir}")
        selected = selection or ProfileSelection()
        selection_provenance = self._selection_provenance(selected, profiles)
        if not selected.active:
            found, missing = coverage(profiles, self.config.required_engines)
            if missing:
                raise PipelineError(
                    f"Missing required engine profiles: {', '.join(sorted(missing))}; found: {', '.join(sorted(found))}"
                )
        utterplan_linguistics = _utterplan_linguistics(profiles, self.config.utterplan.spacy)
        if utterplan_linguistics["error"]:
            raise PipelineError(utterplan_linguistics["error"])
        if not self.config.source.is_file():
            raise PipelineError(f"Source not found: {self.config.source}")

        run_id = run_id or (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        self.run_dir = self.config.work_root / run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)

        from . import __version__

        config_path = self.config.config_path
        self._run_metadata = {
            "run_id": run_id,
            "bookbench_version": __version__,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "config_path": str(config_path) if config_path is not None else None,
            "config_sha256": (_file_sha256(config_path) if config_path is not None and config_path.is_file() else None),
            "source_path": str(self.config.source),
            "source_sha256": _file_sha256(self.config.source),
            "selected_chapters": self.config.chapters,
            "selection": selection_provenance,
            "utterplan_linguistics": utterplan_linguistics,
            "speechscope": _speechscope_config_provenance(self.config.speechscope),
            "dry_run": self.dry_run,
            "start_timestamp": self._run_started_at.isoformat(),
            "end_timestamp": None,
            "failed_stage": None,
            "error": None,
        }
        self._profile_metadata = []
        for profile in profiles:
            staged = (
                self.run_dir
                / "book.ssmdbook"
                / "renderplan"
                / "_bookbench_profiles"
                / profile.variant
                / profile.path.name
            )
            self._profile_metadata.append(
                {
                    "candidate_label": profile.label,
                    "profile_label": profile.profile_label,
                    "original_path": str(profile.path),
                    "original_sha256": _file_sha256(profile.path),
                    "profile_id": profile.profile_id,
                    "variant_label": profile.variant_label,
                    "variant": profile.variant,
                    "engines": list(profile.engines),
                    "staged_path": str(staged),
                    "targets": [{"engine_id": engine, "target_id": target_id} for engine, target_id in profile.targets],
                    "staged_sha256": None,
                }
            )
        if self.dry_run or self.config.speechscope.max_compound_wer is None:
            speechscope_capabilities = {
                "status": "not_probed",
                "command": ["speechscope", "metrics", "list", "--format", "json"],
                "metrics": [],
                "supports_compound_wer": None,
                "supports_max_compound_wer": None,
                "error": None,
            }
        else:
            self._current_stage = "speechscope_capability"
            speechscope_capabilities = self._probe_speechscope_capabilities()
            if not speechscope_capabilities["supports_max_compound_wer"]:
                detail = speechscope_capabilities.get("error") or "compound_wer capability is unavailable"
                raise PipelineError(
                    f"Configured SpeechScope threshold max_compound_wer requires compound_wer support; {detail}"
                )
        self._run_metadata["speechscope_capabilities"] = speechscope_capabilities
        self._write_provenance()
        export_dir = self.run_dir / "exports"
        book_epub = self.run_dir / "source.epub"
        book = self.run_dir / "book.ssmdbook"
        run_context = RunContext(
            run_id=run_id,
            run_dir=self.run_dir,
            book_epub=book_epub,
            book=book,
            export_dir=export_dir,
        )

        versions = {} if self.dry_run else self.versions()
        (self.run_dir / "versions.json").write_text(json.dumps(versions, indent=2) + "\n", encoding="utf-8")

        engine_provenance = self._engine_provenance(profiles)
        (self.run_dir / "engines.json").write_text(json.dumps(engine_provenance, indent=2) + "\n", encoding="utf-8")
        self._current_stage = "text2epub"
        self._run(
            [
                "text2epub",
                "markdown",
                self.config.source,
                "-o",
                book_epub,
                "--title",
                self.config.title,
                "--language",
                self.config.language,
            ]
        )
        self._current_stage = "ssmdconvert"
        self._run(
            [
                "ssmdconvert",
                "book",
                "convert",
                book_epub,
                "--language",
                self.config.language,
                "--speech-semantics",
                "freeze",
                "-o",
                book,
            ]
        )
        self._current_stage = "ssmdconvert"
        self._run(["ssmdconvert", "book", "validate", book, "--json"])
        self._current_stage = "ssmdconvert"
        self._run(["ssmdconvert", "txt", book, "-o", self.run_dir / "prepared.txt", "--spacy", "off"])
        self._current_stage = "renderplan"
        self._run(
            ["utterplan", "compile-book", book, "--spacy", utterplan_linguistics["spacy_policy"], *self._chapter_args()]
        )

        plan_provenance = _utterplan_plan_provenance(book)
        if plan_provenance:
            utterplan_linguistics["plans"] = plan_provenance

        results: list[ProfileResult] = []
        for profile in profiles:
            self._current_stage = None
            try:
                result = self._render_engine(run_context, profile)
            except Exception as exc:  # noqa: BLE001
                result = ProfileResult(
                    profile=profile.label,
                    variant=profile.variant,
                    engines=list(profile.engines),
                    status="error",
                    error=str(exc),
                    failed_stage=self._current_stage,
                    voicerender_preflight_checks=getattr(exc, "voicerender_preflight_checks", None),
                )
                results.append(result)
                if self.config.fail_fast:
                    break
            else:
                results.append(result)
                if self.config.fail_fast and result.status != "pass":
                    break

        self._current_stage = None
        summary = {
            "run_id": run_id,
            "source": str(self.config.source),
            "book": str(book),
            "selection": selection_provenance,
            "speechscope_gate": _configured_speechscope_thresholds(self.config.speechscope),
            "speechscope": _speechscope_config_provenance(self.config.speechscope),
            "results": [asdict(x) for x in results],
            "passed": sum(x.status == "pass" for x in results),
            "failed": sum(x.status != "pass" for x in results),
        }
        (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        from .report import build_listening_report

        build_listening_report(self.run_dir, results, selection_provenance)
        return self.run_dir, results

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    path: Path
    label: str
    profile_id: str
    engines: tuple[str, ...]
    targets: tuple[tuple[str, str], ...] = ()

    required_token_fields: tuple[str, ...] = ()
    @property
    def variant_label(self) -> str:
        raw = self.label.lower()
        label = re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("._-")
        return label or "profile"

    @property
    def variant(self) -> str:
        digest = self.profile_id.removeprefix("sha256:")
        return f"{self.variant_label}--{digest[:12]}"


def _label(path: Path) -> str:
    suffix = ".renderprofile.toml"
    return path.name[: -len(suffix)] if path.name.endswith(suffix) else path.stem


def inspect_profile(path: str | Path) -> ProfileInfo:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    profile_id = str(data.get("profile_id", ""))
    if not profile_id.startswith("sha256:") or len(profile_id.removeprefix("sha256:")) < 12:
        raise ValueError(f"{path}: missing/invalid profile_id")
    engines: set[str] = set()
    targets: set[tuple[str, str]] = set()
    required_token_fields: set[str] = set()
    for role_name, role in (data.get("roles") or {}).items():
        if not isinstance(role, dict):
            continue
        binding = role.get("binding") or {}
        renderer = binding.get("renderer") if isinstance(binding, dict) else None
        if renderer:
            engines.add(str(renderer))
            target_id = binding.get("target_id")
            if isinstance(target_id, str) and target_id:
                targets.add((str(renderer), target_id))
        requirements = role.get("requirements", {})
        if not isinstance(requirements, dict):
            raise TypeError(f"{path}: roles.{role_name}.requirements must be a table")
        token_fields = requirements.get("token_fields", [])
        if not isinstance(token_fields, (list, tuple)) or any(
            not isinstance(field, str) or not field.strip() for field in token_fields
        ):
            raise ValueError(
                f"{path}: roles.{role_name}.requirements.token_fields must be a list of non-empty strings"
            )
        required_token_fields.update(token_fields)
    if not engines:
        raise ValueError(f"{path}: no roles.*.binding.renderer found")
    return ProfileInfo(
        path=path,
        label=_label(path),
        profile_id=profile_id,
        engines=tuple(sorted(engines)),
        targets=tuple(sorted(targets)),
        required_token_fields=tuple(sorted(required_token_fields)),
    )


@dataclass(frozen=True, slots=True)
class ProfileCase:
    """One BookBench case with a human label and an immutable RenderProfile identity."""

    name: str
    profile: ProfileInfo

    @property
    def path(self) -> Path:
        return self.profile.path

    @property
    def label(self) -> str:
        return self.name

    @property
    def profile_label(self) -> str:
        return self.profile.label

    @property
    def variant_label(self) -> str:
        return self.profile.variant_label

    @property
    def profile_id(self) -> str:
        return self.profile.profile_id

    @property
    def variant(self) -> str:
        # Pipeline identity comes from the RenderProfile filename + profile_id, not
        # from the user-facing comparison label.
        return self.profile.variant

    @property
    def engines(self) -> tuple[str, ...]:
        return self.profile.engines


    @property
    def targets(self) -> tuple[tuple[str, str], ...]:
        return self.profile.targets

    @property
    def required_token_fields(self) -> tuple[str, ...]:
        return self.profile.required_token_fields


def required_token_fields(profiles: Iterable[ProfileInfo | ProfileCase]) -> tuple[str, ...]:
    return tuple(sorted({field for profile in profiles for field in profile.required_token_fields}))

@dataclass(frozen=True, slots=True)
class ProfileSelection:
    engines: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.engines and self.profiles:
            raise ValueError("engine and profile selectors cannot be combined")

    @property
    def active(self) -> bool:
        return bool(self.engines or self.profiles)


def select_profiles(
    profiles: Iterable[ProfileCase],
    selection: ProfileSelection,
) -> list[ProfileCase]:
    cases = list(profiles)
    if not selection.active:
        return cases

    if selection.engines:
        requested = set(selection.engines)
        configured = {engine for case in cases for engine in case.engines}
        missing = requested - configured
        if missing:
            raise ValueError(
                "Unknown or unconfigured engine selector(s): "
                + ", ".join(sorted(missing))
                + ". Configured engines: "
                + ", ".join(sorted(configured))
            )
        return [case for case in cases if requested.intersection(case.engines)]

    requested_profiles = set(selection.profiles)
    configured_profiles = {case.label for case in cases}
    missing_profiles = requested_profiles - configured_profiles
    if missing_profiles:
        raise ValueError(
            "Unknown profile selector(s): " + ", ".join(sorted(missing_profiles))
        )
    return [case for case in cases if case.label in requested_profiles]


def discover_profiles(directory: str | Path) -> list[ProfileInfo]:
    directory = Path(directory)
    return [inspect_profile(path) for path in sorted(directory.glob("*.renderprofile.toml"))]


def coverage(profiles: Iterable[ProfileInfo | ProfileCase], required: tuple[str, ...]) -> tuple[set[str], set[str]]:
    found = {engine for profile in profiles for engine in profile.engines}
    return found, set(required) - found

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from pathlib import Path
import tomllib


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    path: Path
    label: str
    profile_id: str
    engines: tuple[str, ...]

    @property
    def variant(self) -> str:
        digest = self.profile_id.removeprefix("sha256:")
        return f"{self.label}--{digest[:12]}"


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
    for role in (data.get("roles") or {}).values():
        if isinstance(role, dict):
            binding = role.get("binding") or {}
            renderer = binding.get("renderer") if isinstance(binding, dict) else None
            if renderer:
                engines.add(str(renderer))
    if not engines:
        raise ValueError(f"{path}: no roles.*.binding.renderer found")
    return ProfileInfo(path=path, label=_label(path), profile_id=profile_id, engines=tuple(sorted(engines)))


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


def discover_profiles(directory: str | Path) -> list[ProfileInfo]:
    directory = Path(directory)
    return [inspect_profile(path) for path in sorted(directory.glob("*.renderprofile.toml"))]


def coverage(profiles: Iterable[ProfileInfo | ProfileCase], required: tuple[str, ...]) -> tuple[set[str], set[str]]:
    found = {engine for profile in profiles for engine in profile.engines}
    return found, set(required) - found

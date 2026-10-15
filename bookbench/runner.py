from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from collections.abc import Iterable
import uuid

from .config import Config
from .profiles import ProfileCase, coverage, discover_profiles, inspect_profile
from .rendertext import renderplan_text


TOOLS = ("text2epub", "ssmdconvert", "utterplan", "voicerender", "audiocompose", "audioexport", "speechscope", "ffmpeg")


class PipelineError(RuntimeError):
    pass


@dataclass(slots=True)
class CommandResult:
    argv: list[str]
    returncode: int
    duration_s: float
    stdout: str
    stderr: str


@dataclass(slots=True)
class ProfileResult:
    profile: str
    variant: str
    engines: list[str]
    status: str
    export: str | None = None
    speechscope_report: str | None = None
    wer: float | None = None
    cer: float | None = None
    error: str | None = None


class Harness:
    def __init__(self, config: Config, *, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.run_dir: Path | None = None
        self.commands: list[CommandResult] = []

    def _run(self, argv: Iterable[str], *, check: bool = True) -> CommandResult:
        args = [str(x) for x in argv]
        print("+", shlex.join(args))
        if self.dry_run:
            result = CommandResult(args, 0, 0.0, "", "")
            self.commands.append(result)
            return result
        started = time.monotonic()
        proc = subprocess.run(args, text=True, capture_output=True, check=False)
        result = CommandResult(args, proc.returncode, time.monotonic() - started, proc.stdout, proc.stderr)
        self.commands.append(result)
        if self.run_dir is not None:
            with (self.run_dir / "commands.log").open("a", encoding="utf-8") as fh:
                fh.write(f"$ {shlex.join(args)}\n")
                if proc.stdout:
                    fh.write(proc.stdout.rstrip() + "\n")
                if proc.stderr:
                    fh.write(proc.stderr.rstrip() + "\n")
                fh.write(f"[exit={proc.returncode} duration={result.duration_s:.3f}s]\n\n")
        if check and proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
            raise PipelineError(f"Command failed: {shlex.join(args)}\n{detail}")
        return result

    def versions(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for tool in TOOLS:
            exe = shutil.which(tool)
            if exe is None:
                out[tool] = None
                continue
            argv = [tool, "-version"] if tool == "ffmpeg" else [tool, "--version"]
            proc = subprocess.run(argv, text=True, capture_output=True, check=False)
            line = (proc.stdout or proc.stderr).splitlines()
            out[tool] = line[0].strip() if line else f"exit={proc.returncode}"
        return out

    def _profiles(self) -> list[ProfileCase]:
        if self.config.voices:
            return [ProfileCase(v.name, inspect_profile(v.profile)) for v in self.config.voices]
        return [ProfileCase(p.label, p) for p in discover_profiles(self.config.profiles_dir)]

    def doctor(self) -> dict:
        profiles = self._profiles()
        found, missing = coverage(profiles, self.config.required_engines)
        return {
            "tools": {tool: shutil.which(tool) for tool in TOOLS},
            "profiles": [
                {
                    "path": str(p.path),
                    "label": p.label,
                    "profile_label": p.profile_label,
                    "profile_id": p.profile_id,
                    "variant": p.variant,
                    "engines": list(p.engines),
                }
                for p in profiles
            ],
            "engine_coverage": {
                "found": sorted(found),
                "required": sorted(self.config.required_engines),
                "missing": sorted(missing),
            },
        }

    def _chapter_args(self) -> list[str]:
        return [] if self.config.chapters == "all" else ["--chapters", self.config.chapters]

    def _render_engine(self, book: Path, profile: ProfileCase, export_dir: Path) -> ProfileResult:
        self._run(["voicerender", "profile", "validate", profile.path])
        self._run(["utterplan", "render-book", book, "--profile", profile.path, *self._chapter_args()])

        render_cmd = [
            "voicerender",
            "render-book",
            book,
            "--variant",
            profile.variant,
            "--cache-dir",
            self.config.cache_dir,
            "--emphasis-policy",
            "defer-to-compose",
            "--json",
            *self._chapter_args(),
        ]
        for attempt in range(self.config.max_replans + 1):
            rendered = self._run(render_cmd, check=False)
            if rendered.returncode == 0:
                break
            if attempt >= self.config.max_replans:
                raise PipelineError(rendered.stderr.strip() or rendered.stdout.strip() or "VoiceRender failed")
            replanned = self._run(
                [
                    "utterplan",
                    "replan-book",
                    book,
                    "--variant",
                    profile.variant,
                    *self._chapter_args(),
                ],
                check=False,
            )
            if replanned.returncode != 0:
                detail = rendered.stderr.strip() or rendered.stdout.strip()
                replan_detail = replanned.stderr.strip() or replanned.stdout.strip()
                raise PipelineError(
                    f"VoiceRender failed and no valid replan was available.\nrender: {detail}\nreplan: {replan_detail}"
                )

        self._run(
            [
                "audiocompose",
                "compose-book",
                book,
                "--variant",
                profile.variant,
                "--json",
                *self._chapter_args(),
            ]
        )

        export_dir.mkdir(parents=True, exist_ok=True)
        exported = export_dir / f"{profile.label}.m4b"
        self._run(
            [
                "audioexport",
                "export-book",
                book,
                "--variant",
                profile.variant,
                "--output",
                exported,
                *self._chapter_args(),
            ]
        )

        listening = export_dir / f"{profile.label}.listen.mp3"
        self._run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                exported,
                "-map",
                "0:a:0",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "4",
                listening,
            ]
        )

        decoded = export_dir / f"{profile.label}.validation.wav"
        self._run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                exported,
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                decoded,
            ]
        )

        if self.dry_run:
            expected_text = "Dry-run: expected text would be derived from the selected reference mode.\n"
        elif self.config.speechscope.reference_mode == "source":
            expected_text = self.config.source.read_text(encoding="utf-8")
        else:
            expected_text = renderplan_text(book / "renderplan" / profile.variant)
        expected = export_dir / f"{profile.label}.expected.txt"
        expected.write_text(expected_text, encoding="utf-8")

        policy = export_dir / f"{profile.label}.speechscope-policy.json"
        policy.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "max_wer": self.config.speechscope.max_wer,
                    "max_cer": self.config.speechscope.max_cer,
                    "missing_timing": "review",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        report = export_dir / f"{profile.label}.speechscope.json"
        cmd = [
            "speechscope",
            "analyze",
            decoded,
            "--text-file",
            expected,
            "--thresholds",
            policy,
            "--backend",
            self.config.speechscope.backend,
            "--device",
            self.config.speechscope.device,
            "--normalization",
            self.config.speechscope.normalization,
            "--format",
            "json",
            "--json",
            report,
        ]
        if self.config.speechscope.model:
            cmd.extend(["--model", self.config.speechscope.model])
        checked = self._run(cmd, check=False)
        if checked.returncode not in (0, 1):
            raise PipelineError(checked.stderr.strip() or checked.stdout.strip() or "SpeechScope failed")
        data = {} if self.dry_run else json.loads(report.read_text(encoding="utf-8"))
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
            export=str(exported),
            speechscope_report=str(report),
            wer=metrics.get("wer"),
            cer=metrics.get("cer"),
            error=None if status == "pass" else "SpeechScope threshold failure",
        )

    def run(self, *, run_id: str | None = None) -> tuple[Path, list[ProfileResult]]:
        profiles = self._profiles()
        if not profiles:
            raise PipelineError(f"No *.renderprofile.toml files found in {self.config.profiles_dir}")
        found, missing = coverage(profiles, self.config.required_engines)
        if missing:
            raise PipelineError(
                f"Missing required engine profiles: {', '.join(sorted(missing))}; found: {', '.join(sorted(found))}"
            )
        if not self.config.source.is_file():
            raise PipelineError(f"Source not found: {self.config.source}")

        run_id = run_id or (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        self.run_dir = self.config.work_root / run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        export_dir = self.run_dir / "exports"
        book_epub = self.run_dir / "source.epub"
        book = self.run_dir / "book.ssmdbook"

        versions = {} if self.dry_run else self.versions()
        (self.run_dir / "versions.json").write_text(json.dumps(versions, indent=2) + "\n", encoding="utf-8")

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
        self._run(["ssmdconvert", "book", "validate", book, "--json"])
        self._run(["ssmdconvert", "txt", book, "-o", self.run_dir / "prepared.txt", "--spacy", "off"])
        self._run(["utterplan", "compile-book", book, "--spacy", "off", *self._chapter_args()])

        results: list[ProfileResult] = []
        for profile in profiles:
            try:
                result = self._render_engine(book, profile, export_dir)
            except Exception as exc:  # continue across engines by default
                result = ProfileResult(
                    profile=profile.label,
                    variant=profile.variant,
                    engines=list(profile.engines),
                    status="error",
                    error=str(exc),
                )
                results.append(result)
                if self.config.fail_fast:
                    break
            else:
                results.append(result)
                if self.config.fail_fast and result.status != "pass":
                    break

        summary = {
            "run_id": run_id,
            "source": str(self.config.source),
            "book": str(book),
            "results": [asdict(x) for x in results],
            "passed": sum(x.status == "pass" for x in results),
            "failed": sum(x.status != "pass" for x in results),
        }
        (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        from .report import build_listening_report

        build_listening_report(self.run_dir, results)
        return self.run_dir, results

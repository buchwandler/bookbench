"""Standalone local HTML listening report; keeps audio files external."""

from __future__ import annotations

from html import escape
from pathlib import Path


def _relative_path(run_dir: Path, path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        relative = Path(path).relative_to(run_dir).as_posix()
    except ValueError:
        return None
    return relative if relative and not relative.startswith("../") else None


def _artifact_link(run_dir: Path, path: str | Path | None, label: str) -> str:
    relative = _relative_path(run_dir, path)
    if relative is None:
        return ""
    return f'<a href="{escape(relative, quote=True)}">{escape(label)}</a>'


def build_listening_report(run_dir: Path, results: list) -> Path:
    run_dir = Path(run_dir)
    rows: list[str] = []
    for result in results:
        exported = getattr(result, "export", None)
        audio = ""
        artifacts = []
        if exported:
            export_path = Path(exported)
            audio_path = export_path.with_name(export_path.stem + ".listen.mp3")
            audio_relative = _relative_path(run_dir, audio_path)
            if audio_relative:
                audio = (
                    f'<audio controls preload="none" src="{escape(audio_relative, quote=True)}">'
                    "Audio playback is not supported by this browser.</audio>"
                )
            artifacts.append(_artifact_link(run_dir, export_path, "M4B export"))
            expected = export_path.with_name(export_path.stem + ".expected.txt")
            artifacts.append(_artifact_link(run_dir, expected, "Expected transcript"))
        artifacts.append(_artifact_link(run_dir, getattr(result, "speechscope_report", None), "SpeechScope JSON"))
        artifact_links = " ".join(link for link in artifacts if link)
        status = escape(result.status)
        variant = escape(getattr(result, "variant", ""))
        failed_stage = escape(getattr(result, "failed_stage", None) or "none")
        wer = "n/a" if result.wer is None else f"{result.wer:.4f}"
        cer = "n/a" if result.cer is None else f"{result.cer:.4f}"
        error = escape(result.error or "")
        rows.append(
            f"<tr><th>{escape(result.profile)}</th><td>{variant}</td>"
            f"<td>{escape(', '.join(result.engines))}</td><td>{status}</td>"
            f"<td>{wer}</td><td>{cer}</td><td>{failed_stage}</td>"
            f"<td>{audio}</td><td>{artifact_links}<small>{error}</small></td></tr>"
        )
    html = (
        """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>BookBench Voice Comparison</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:16px system-ui,sans-serif;margin:2rem auto;max-width:1400px;padding:0 1rem}
table{border-collapse:collapse;width:100%}td,th{padding:1rem;border-bottom:1px solid #ccc;text-align:left}
th{white-space:nowrap}audio{width:100%;min-width:220px}small{display:block;color:#995;margin-top:.4rem}
@media(max-width:750px){table{display:block;overflow-x:auto}}</style></head><body>
<h1>BookBench voice comparison</h1><p>Listen to each candidate in configured profile order.
WER and CER measure acoustic transcript agreement against the selected reference. They are not
subjective voice-quality scores and must not be used as a best-voice ranking. M4B exports,
expected transcripts, and SpeechScope reports are linked for inspection.</p>
<table><thead><tr><th>Candidate</th><th>Variant</th><th>Engine</th><th>Status</th><th>WER</th><th>CER</th>
<th>Failed stage</th><th>Recording</th><th>Artifacts and error</th></tr></thead><tbody>"""
        + "\n".join(rows)
        + "</tbody></table></body></html>"
    )
    out = run_dir / "listen.html"
    out.write_text(html, encoding="utf-8")
    return out

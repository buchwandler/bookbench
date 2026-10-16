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
        compound_value = getattr(result, "compound_wer", None)
        compound_wer = "n/a" if compound_value is None else f"{compound_value:.4f}"
        boundary_count = getattr(result, "word_boundary_match_count", None)
        boundary_variants = "n/a" if boundary_count is None else str(boundary_count)
        if boundary_count:
            details_link = _artifact_link(run_dir, getattr(result, "speechscope_report", None), "details")
            if details_link:
                boundary_variants = f"{boundary_variants} {details_link}"
        error = escape(result.error or "")
        rows.append(
            f"<tr><th>{escape(result.profile)}</th><td>{variant}</td>"
            f"<td>{escape(', '.join(result.engines))}</td><td>{status}</td>"
            f"<td>{compound_wer}</td><td>{wer}</td><td>{cer}</td><td>{boundary_variants}</td><td>{failed_stage}</td>"
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
Compound WER is the default spoken-content gate and tolerates SpeechScope's bounded adjacent
compound-word split/join variants. Strict WER remains a diagnostic for exact whitespace-tokenized
word boundaries. CER is retained as an independent character-content check. These are validation
metrics, not subjective voice-quality scores. M4B exports, expected transcripts, and SpeechScope
reports are linked for inspection.</p>
<table><thead><tr><th>Candidate</th><th>Variant</th><th>Engine</th><th>Status</th><th>Compound WER</th><th>Strict WER</th><th>CER</th><th>Boundary variants</th>
<th>Failed stage</th><th>Recording</th><th>Artifacts and error</th></tr></thead><tbody>"""
        + "\n".join(rows)
        + "</tbody></table></body></html>"
    )
    out = run_dir / "listen.html"
    out.write_text(html, encoding="utf-8")
    return out

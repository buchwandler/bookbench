"""Standalone local HTML listening report; keeps audio files external."""

from __future__ import annotations

from html import escape
from pathlib import Path


def build_listening_report(run_dir: Path, results: list) -> Path:
    rows: list[str] = []
    for result in results:
        audio = ""
        if result.export:
            path = Path(result.export).with_name(Path(result.export).stem + ".listen.mp3")
            try:
                relative = path.relative_to(run_dir).as_posix()
            except ValueError:
                relative = ""
            # User-supplied labels and filenames must not inject HTML.
            if relative and not relative.startswith("../"):
                audio = (
                    f'<audio controls preload="none" src="{escape(relative, quote=True)}">'
                    "Audio playback is not supported by this browser.</audio>"
                )
        status = escape(result.status)
        wer = "—" if result.wer is None else f"{result.wer:.4f}"
        cer = "—" if result.cer is None else f"{result.cer:.4f}"
        error = escape(result.error or "")
        rows.append(
            f"<tr><th>{escape(result.profile)}</th><td>{escape(', '.join(result.engines))}</td>"
            f"<td>{status}</td><td>{wer}</td><td>{cer}</td><td>{audio}<small>{error}</small></td></tr>"
        )
    html = (
        """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>BookBench Voice Comparison</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:16px system-ui,sans-serif;margin:2rem auto;max-width:1200px;padding:0 1rem}
table{border-collapse:collapse;width:100%}td,th{padding:1rem;border-bottom:1px solid #ccc;text-align:left}
th{white-space:nowrap}audio{width:100%;min-width:220px}small{display:block;color:#995;margin-top:.4rem}
@media(max-width:750px){table{display:block;overflow-x:auto}}</style></head><body>
<h1>BookBench — voice comparison</h1><p>Listen to each candidate from the same source text.
PASS means ASR passed the configured thresholds; it is not an objective naturalness ranking.
Audio files remain beside this HTML file. The downloadable export is also available as an M4B in the exports folder.</p>
<table><thead><tr><th>Voice</th><th>Engine</th><th>Status</th><th>WER</th><th>CER</th><th>Recording</th>
</tr></thead><tbody>"""
        + "\n".join(rows)
        + "</tbody></table></body></html>"
    )
    out = run_dir / "listen.html"
    out.write_text(html, encoding="utf-8")
    return out

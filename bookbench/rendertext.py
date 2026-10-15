from __future__ import annotations

from pathlib import Path
import tomllib


def renderplan_text(variant_dir: str | Path) -> str:
    """Return the exact speech-request text, in generated chapter/flow order."""
    variant_dir = Path(variant_dir)
    chunks: list[str] = []
    plans = sorted(variant_dir.glob("chapter-*.renderplan.toml"))
    if not plans:
        raise FileNotFoundError(f"No chapter RenderPlans found under {variant_dir}")
    for plan in plans:
        data = tomllib.loads(plan.read_text(encoding="utf-8"))
        for item in data.get("flow", []):
            if isinstance(item, dict) and item.get("kind") == "speech":
                text = item.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    if not chunks:
        raise ValueError(f"No speech items found under {variant_dir}")
    return "\n".join(chunks) + "\n"

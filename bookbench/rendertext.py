from __future__ import annotations

import tomllib
from pathlib import Path


def renderplan_text(variant_dir: str | Path) -> str:
    """Return exact speech-request text in RenderPlan-set chapter order when available."""
    variant_dir = Path(variant_dir)
    set_path = variant_dir / "renderplan-set.toml"
    if set_path.is_file():
        renderplan_set = tomllib.loads(set_path.read_text(encoding="utf-8"))
        chapters = renderplan_set.get("chapter", [])
        plans = []
        for chapter in chapters:
            renderplan = chapter.get("renderplan") if isinstance(chapter, dict) else None
            if not isinstance(renderplan, str):
                raise TypeError(f"Invalid chapter reference in {set_path}")
            plans.append(variant_dir / renderplan)
    else:
        plans = sorted(variant_dir.glob("chapter-*.renderplan.toml"))
    if not plans:
        raise FileNotFoundError(f"No chapter RenderPlans found under {variant_dir}")

    chunks: list[str] = []
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

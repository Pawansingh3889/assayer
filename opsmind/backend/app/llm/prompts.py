"""Load versioned prompt files by name.

Prompts are code: they live under ``prompts/`` and are loaded by name + version
(e.g. ``generate_template_v1``), never inlined as string literals in services.
"""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {name}")
    return path.read_text(encoding="utf-8").strip()

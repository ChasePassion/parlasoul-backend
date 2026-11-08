"""Load-on-demand skills for the character-forge agent.

A skill is a directory with a ``SKILL.md`` whose flat frontmatter declares
``name``, ``description`` and ``when_to_use``. Only that metadata enters the
system prompt; the body is returned by the ``load_skill`` tool when the model
decides the skill is relevant.

This mirrors the progressive-disclosure model used by ZCode: a compact catalog
is always visible, while the detailed instructions stay out of context until a
task actually needs them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.core.tts_voice_catalog import dashscope_full_voice_catalog
from src.core.voice_profiles import (
    PITCH_PRESETS,
    SPEECH_RATE_PRESETS,
    VOLUME_PRESETS,
)

_SKILLS_ROOT = Path(__file__).resolve().parent
_PROMPT_SKILL_DIR = _SKILLS_ROOT / "prompt_creation"
_REQUIRED_FIELDS = ("name", "description", "when_to_use")


@dataclass(frozen=True)
class ForgeSkill:
    name: str
    description: str
    when_to_use: str
    content: str


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError(f"skill frontmatter missing: {path}")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"skill frontmatter is not closed: {path}")

    metadata: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.startswith((" ", "\t")):
            continue
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip()
    for field in _REQUIRED_FIELDS:
        if not metadata.get(field):
            raise ValueError(f"skill {path} is missing frontmatter field: {field}")
    return metadata, text[end + 5 :]


def _voice_table() -> str:
    rows = ["| voice_id | 名称 | 气质 | 性别 | 语言 |", "|---|---|---|---|---|"]
    for item in dashscope_full_voice_catalog():
        rows.append(
            f"| {item.provider_voice_id} | {item.display_name} | "
            f"{item.usage_hint or ''} | {item.gender or ''} | {item.language or ''} |"
        )
    return "\n".join(rows)


def _preset_lines(presets: dict[str, float]) -> str:
    return "、".join(f"{name} {value}" for name, value in presets.items())


def _read_template(filename: str) -> str:
    """Read a character system-prompt template verbatim.

    The templates are user-authored and must reach the model without any
    rewriting, normalization or placeholder substitution.
    """
    return (_PROMPT_SKILL_DIR / filename).read_text(encoding="utf-8")


def _render(body: str) -> str:
    replacements = {
        "{{VOICE_TABLE}}": _voice_table(),
        "{{SPEECH_RATE_PRESETS}}": _preset_lines(SPEECH_RATE_PRESETS),
        "{{VOLUME_PRESETS}}": _preset_lines(VOLUME_PRESETS),
        "{{PITCH_PRESETS}}": _preset_lines(PITCH_PRESETS),
        "{{SYSTEM_PROMPT_TEMPLATE_DAILY}}": _read_template(
            "system_prompt_template_daily.md"
        ),
        "{{SYSTEM_PROMPT_TEMPLATE_NARRATIVE}}": _read_template(
            "system_prompt_template_narrative.md"
        ),
    }
    rendered = body
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    if "{{" in rendered:
        raise ValueError("skill body has an unrendered placeholder")
    return rendered.strip()


def _discover() -> dict[str, ForgeSkill]:
    skills: dict[str, ForgeSkill] = {}
    for path in sorted(_SKILLS_ROOT.glob("*/SKILL.md")):
        metadata, body = _parse_frontmatter(path.read_text(encoding="utf-8"), path)
        name = metadata["name"]
        if name in skills:
            raise ValueError(f"duplicate forge skill: {name}")
        skills[name] = ForgeSkill(
            name=name,
            description=metadata["description"],
            when_to_use=metadata["when_to_use"],
            content=_render(body),
        )
    if not skills:
        raise ValueError("no forge skills were discovered")
    return skills


FORGE_SKILLS: dict[str, ForgeSkill] = _discover()
SKILL_NAMES: tuple[str, ...] = tuple(FORGE_SKILLS)


def load_forge_skill(name: str) -> ForgeSkill | None:
    return FORGE_SKILLS.get(name)


def build_skill_catalog() -> str:
    """Compact catalog for the system prompt; skill bodies stay out of context."""
    lines = ["以下技能按需加载，调用 load_skill 获取完整说明："]
    for skill in FORGE_SKILLS.values():
        lines.append(
            f"- {skill.name}：{skill.description} 适用：{skill.when_to_use}"
        )
    return "\n".join(lines)

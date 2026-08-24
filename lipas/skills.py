"""Portable Markdown skills.

The format deliberately follows the convention used by Codex-style skill
directories and is simple enough to share with LangGraph applications:

    skills/
      research/SKILL.md

    ---
    name: research
    description: Find and cite reliable sources.
    ---
    # Research
    ...instructions...

Only ``name`` and ``description`` are interpreted.  Remaining front-matter
is preserved as strings, so provider- or framework-specific fields do not
make a skill unloadable.  No YAML dependency is needed for this deliberately
small interoperable subset.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

__all__ = [
    "Skill", "SkillError", "SkillRegistry", "load_skill", "discover_skills",
    "builtin_skills", "load_builtin_skill",
]


_BUILTIN_SKILLS_ROOT = Path(__file__).with_name("builtin_skills")


class SkillError(ValueError):
    """A SKILL.md file is missing required metadata or is ambiguous."""


def _front_matter(text: str, path: Path) -> tuple[dict[str, str], str, str]:
    """Extract top-level scalar YAML keys without depending on PyYAML.

    SKILL.md metadata is intentionally extensible: Claude Code commonly
    carries fields such as ``allowed-tools`` and ``user-invocable`` while
    Codex/ChatGPT installations may carry a nested ``metadata`` map.  We only
    need two portable scalar keys, so nested YAML is preserved verbatim rather
    than rejected or lossy-parsed.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise SkillError(f"{path}: SKILL.md must start with YAML front matter ('---')")
    lines = text.splitlines()
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
    except StopIteration as exc:
        raise SkillError(f"{path}: front matter is not terminated by '---'") from exc
    header_lines = lines[1:end]
    meta: dict[str, str] = {}
    index = 0
    while index < len(header_lines):
        line = header_lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#") or line[:1].isspace():
            continue
        if ":" not in line:
            # Valid YAML can contain list items and nested values. They are
            # opaque compatibility metadata, not an error for the loader.
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip().strip('"\'')
        if not key:
            raise SkillError(f"{path}: front-matter key cannot be empty")
        if value in {"|", ">", "|-", ">-"}:
            block: list[str] = []
            while index < len(header_lines) and header_lines[index][:1].isspace():
                block.append(header_lines[index].strip())
                index += 1
            value = ("\n" if value.startswith("|") else " ").join(block).strip()
        meta[key] = value
    return meta, "\n".join(lines[end + 1:]).strip(), "\n".join(header_lines)


@dataclass(frozen=True, slots=True)
class Skill:
    """An immutable, provider-neutral instruction skill."""

    name: str
    description: str
    instructions: str
    path: Path
    metadata: Mapping[str, str] = field(default_factory=dict)
    front_matter: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise SkillError("Skill.name must be a non-empty string")
        if not isinstance(self.description, str) or not self.description.strip():
            raise SkillError("Skill.description must be a non-empty string")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise SkillError("Skill.instructions must be a non-empty string")
        if not isinstance(self.path, Path):
            raise TypeError("Skill.path must be pathlib.Path")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("Skill.metadata must be a mapping")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.metadata.items()
        ):
            raise TypeError("Skill.metadata keys and values must be strings")
        if not isinstance(self.front_matter, str):
            raise TypeError("Skill.front_matter must be a string")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(self, "instructions", self.instructions.strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)),
        )

    def render(self) -> str:
        """Render a stable prompt fragment suitable for any chat provider."""
        return f"<skill name=\"{self.name}\">\n{self.instructions}\n</skill>"


def load_skill(path: str | Path) -> Skill:
    """Load one ``SKILL.md`` file or a directory containing it."""
    candidate = Path(path).expanduser()
    skill_file = candidate / "SKILL.md" if candidate.is_dir() else candidate
    if skill_file.name != "SKILL.md":
        raise SkillError(f"{skill_file}: expected a file named SKILL.md")
    if not skill_file.is_file():
        raise SkillError(f"{skill_file}: file does not exist")
    try:
        raw = skill_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError(f"{skill_file}: SKILL.md must be UTF-8") from exc
    meta, instructions, raw_front_matter = _front_matter(raw, skill_file)
    name, description = meta.get("name", "").strip(), meta.get("description", "").strip()
    if not name:
        raise SkillError(f"{skill_file}: front matter requires a non-empty 'name'")
    if not description:
        raise SkillError(f"{skill_file}: front matter requires a non-empty 'description'")
    if not instructions:
        raise SkillError(f"{skill_file}: instructions after front matter cannot be empty")
    return Skill(
        name, description, instructions, skill_file.resolve(), dict(meta), raw_front_matter,
    )


def discover_skills(root: str | Path) -> tuple[Skill, ...]:
    """Recursively load ``SKILL.md`` files in deterministic path order."""
    base = Path(root).expanduser()
    if not base.is_dir():
        raise SkillError(f"{base}: skill root is not a directory")
    return tuple(load_skill(p) for p in sorted(base.rglob("SKILL.md")))


@lru_cache(maxsize=1)
def builtin_skills() -> tuple[Skill, ...]:
    """Return the packaged business-skill catalog in stable name order.

    Built-ins are loaded once per process and remain instruction-only values;
    selecting one never grants a Tool or any other executable authority.
    """
    skills = discover_skills(_BUILTIN_SKILLS_ROOT)
    return tuple(sorted(skills, key=lambda skill: skill.name))


def load_builtin_skill(name: str) -> Skill:
    """Select one packaged Skill by name without scanning the whole catalog."""
    if not isinstance(name, str) or not name.strip():
        raise SkillError("built-in skill name must be a non-empty string")
    selected = name.strip()
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in selected):
        raise SkillError(f"invalid built-in skill name {selected!r}")
    candidate = _BUILTIN_SKILLS_ROOT / selected
    if candidate.is_dir():
        skill = _load_builtin_path(candidate)
        if skill.name != selected:
            raise SkillError(
                f"packaged skill directory {selected!r} declares name "
                f"{skill.name!r}",
            )
        return skill
    available = ", ".join(skill.name for skill in builtin_skills()) or "<none>"
    raise SkillError(
        f"unknown built-in skill {selected!r}; available: {available}",
    )


@lru_cache(maxsize=None)
def _load_builtin_path(path: Path) -> Skill:
    return load_skill(path)


def _skills_from_path(path: str | Path) -> tuple[Skill, ...]:
    candidate = Path(path).expanduser()
    if candidate.is_file() or (candidate / "SKILL.md").is_file():
        return (load_skill(candidate),)
    return discover_skills(candidate)


@dataclass(frozen=True, slots=True)
class SkillRegistry:
    """A deduplicated skill collection that can extend a system prompt."""

    skills: tuple[Skill, ...] = ()

    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        chosen = tuple(skills)
        if any(not isinstance(skill, Skill) for skill in chosen):
            raise TypeError("SkillRegistry accepts only Skill values")
        names = [skill.name for skill in chosen]
        duplicate = next((name for name in names if names.count(name) > 1), None)
        if duplicate is not None:
            raise SkillError(f"duplicate skill name {duplicate!r}")
        object.__setattr__(self, "skills", chosen)

    @classmethod
    def from_sources(
        cls,
        *,
        builtin_names: Iterable[str] | str = (),
        paths: Iterable[str | Path] | str | Path = (),
    ) -> "SkillRegistry":
        """Compose explicit built-ins and portable local Skill paths.

        Nothing is auto-selected. This keeps prompt size proportional to the
        current job even when the packaged catalog grows.
        """
        names = (
            (builtin_names,)
            if isinstance(builtin_names, str)
            else tuple(builtin_names)
        )
        sources = (
            (paths,)
            if isinstance(paths, (str, Path))
            else tuple(paths)
        )
        selected = [load_builtin_skill(name) for name in names]
        for path in sources:
            selected.extend(_skills_from_path(path))
        return cls(selected)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.skills)

    def get(self, name: str) -> Skill:
        for skill in self.skills:
            if skill.name == name:
                return skill
        raise SkillError(f"skill {name!r} is not selected")

    def system_prompt(self, base: str = "") -> str:
        """Append all skills to ``base`` without changing its wording."""
        rendered = "\n\n".join(skill.render() for skill in self.skills)
        if not rendered:
            return base
        return f"{base.rstrip()}\n\n{rendered}" if base.strip() else rendered

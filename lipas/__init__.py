"""LIPAS — claim-based execution primitives for reliable AI agents.

The provider-neutral interchange surface is ``lipas.adapter``.
"""
from __future__ import annotations

from .tools import SideEffectClass, tool
from .session import open_session, replay
from .trace import render_trace, write_jsonl
from .agent import Agent
from .operations import OperationJournal
from .skills import Skill, SkillRegistry, discover_skills, load_skill
from .team import Team
from .supervisor import project_supervisor

__all__ = [
    "Agent", "tool", "SideEffectClass", "Team", "OperationJournal",
    "Skill", "SkillRegistry", "discover_skills", "load_skill",
    "open_session", "replay", "render_trace", "write_jsonl",
    "project_supervisor",
]

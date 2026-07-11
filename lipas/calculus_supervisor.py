"""Compatibility import for the supervisor projection API.

Historically this module was an intentional import-time tripwire.  Supervisor
state is now a tag-indexed read model rather than an invalid attempt to attach
tag strategies to the field-strategy calculus.  New code should import from
``lipas.supervisor_projection`` directly.
"""
from __future__ import annotations

from .supervisor_projection import (
    RetryRecommendation,
    SupervisorProjection,
    project_supervisor,
)

__all__ = ["RetryRecommendation", "SupervisorProjection", "project_supervisor"]

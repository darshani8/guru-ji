"""Assemble the full platform tool registry."""

from __future__ import annotations

from ..gateway.registry import PlatformToolRegistry
from .actions import build_action_tools
from .attendance import build_attendance_tools
from .context import PlatformServices
from .faculty import build_faculty_tools
from .fees import build_fee_tools
from .institution import build_institution_tools
from .knowledge import build_knowledge_tools
from .students import build_student_tools


def build_platform_registry(services: PlatformServices) -> PlatformToolRegistry:
    return PlatformToolRegistry((
        *build_institution_tools(services),
        *build_student_tools(services),
        *build_attendance_tools(services),
        *build_fee_tools(services),
        *build_faculty_tools(services),
        *build_action_tools(services),
        *build_knowledge_tools(services),
    ))


__all__ = ["PlatformServices", "build_platform_registry"]

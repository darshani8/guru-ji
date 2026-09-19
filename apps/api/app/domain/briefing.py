"""Institutional briefing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from .provenance import Warning
from .responses import Citation
from .source_health import SourceHealthStatus


class BriefingType(StrEnum):
    DAILY_INSTITUTIONAL = "daily_institutional"


@dataclass(frozen=True, slots=True)
class BriefingRequest:
    briefing_type: BriefingType
    report_date: date
    institution_ids: tuple[str, ...] = ()
    include_web_context: bool = False

    def __post_init__(self) -> None:
        briefing_type = self.briefing_type
        if isinstance(briefing_type, str):
            briefing_type = BriefingType(briefing_type)
        object.__setattr__(self, "briefing_type", briefing_type)
        object.__setattr__(self, "institution_ids", tuple(item.strip() for item in self.institution_ids if item.strip()))


@dataclass(frozen=True, slots=True)
class BriefingResponse:
    briefing_id: str
    briefing_type: BriefingType
    report_date: date
    generated_at: datetime
    answer: str
    partial: bool
    citations: tuple[Citation, ...] = ()
    warnings: tuple[Warning, ...] = ()
    source_statuses: tuple[SourceHealthStatus, ...] = ()


__all__ = ["BriefingRequest", "BriefingResponse", "BriefingType"]

"""Framework-independent Agentic Saffron domain contracts."""

from .audit import AuditEvent, AuditOutcome
from .briefing import BriefingRequest, BriefingResponse, BriefingType
from .errors import ErrorCode, AgenticSaffronError
from .principals import Capability, InstitutionScope, Principal, PrincipalType
from .provenance import DataPeriod, Provenance, SourceKind, Warning
from .results import ResultStatus, ToolResult
from .source_health import Freshness, SourceHealth, SourceHealthStatus

__all__ = [
    "AuditEvent", "AuditOutcome", "BriefingRequest", "BriefingResponse",
    "BriefingType", "Capability", "DataPeriod", "ErrorCode", "Freshness",
    "AgenticSaffronError", "InstitutionScope", "Principal", "PrincipalType",
    "Provenance", "ResultStatus", "SourceHealth", "SourceHealthStatus",
    "SourceKind", "ToolResult", "Warning",
]

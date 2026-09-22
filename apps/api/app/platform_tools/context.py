"""Service container handed to tool builders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..actions.email import EmailService
from ..actions.notifications import NotificationService
from ..actions.reports import ReportService
from ..data_access.service import InstitutionDataService
from ..institution_data.store import InstitutionDataStore


@dataclass(slots=True)
class PlatformServices:
    store: InstitutionDataStore
    data: InstitutionDataService
    reports: ReportService
    email: EmailService
    notifications: NotificationService
    documents: Any | None = None      # DocumentRagService, optional until documents are enabled
    intelligence: Any | None = None   # InternetIntelligenceService, optional until search is configured
    ingestion: Any | None = None      # IngestionService


__all__ = ["PlatformServices"]

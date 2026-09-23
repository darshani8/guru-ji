"""Open-web search for the assistant ("search the internet for ...").

The query leaves the institution, so it is checked before it is sent: the
person must hold ``web:search`` (through the policy decision point), the query
must not carry personal data (email addresses, phone, Aadhaar or student
numbers), and each person has a daily allowance counted in the shared control
database. Results are untrusted data: snippets are bounded, a result that
tries to instruct the model is dropped, and only a hash of the query is
audited.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from ..domain.audit import AuditEvent, AuditOutcome
from ..domain.principals import Capability, InstitutionScope, Principal
from ..internet_intelligence.search import IntelligenceSearchProvider, IntelligenceSearchUnavailable
from ..policy.pdp import PolicyDecisionPoint
from ..web_research.untrusted_content import wrap_untrusted
from .prompts import IST

logger = logging.getLogger("guru.conversation.web")

MAX_SNIPPET_CHARS = 400
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
_AADHAAR = re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")
_USN = re.compile(r"\b\d[A-Za-z]{2}\d{2}[A-Za-z]{2,3}\d{3}\b")
# Program-prefixed student numbers such as MBA001; a year (IPL2025) is not one.
_STUDENT_NUMBER = re.compile(r"\b[A-Za-z]{2,5}(?!(?:19|20)\d{2}\b)\d{3,6}\b")


class WebSearchRefused(Exception):
    """The search was not run; ``code`` says why and ``message`` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WebFinding:
    title: str
    url: str
    snippet: str
    source: str
    published: str | None = None

    def as_source(self, index: int) -> dict[str, Any]:
        return {"source_id": self.url, "url": self.url, "title": self.title, "locator": f"[{index}] {self.source}", **({"published_at": self.published} if self.published else {})}


@dataclass(frozen=True, slots=True)
class WebFindings:
    query: str
    findings: tuple[WebFinding, ...]
    provider: str
    dropped: int = 0
    warnings: tuple[dict[str, str], ...] = field(default_factory=tuple)


def personal_data_in(query: str) -> str | None:
    for name, pattern in (("an email address", _EMAIL), ("a phone number", _PHONE), ("an Aadhaar number", _AADHAAR), ("a student number", _USN), ("a student number", _STUDENT_NUMBER)):
        if pattern.search(query):
            return name
    return None


def ist_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(IST).date().isoformat()


@dataclass(slots=True)
class OpenWebSearchService:
    provider: IntelligenceSearchProvider
    control_store: Any
    pdp: PolicyDecisionPoint
    per_person_per_day: int = 25
    max_results: int = 5
    timeout_seconds: float = 8.0

    async def search(self, principal: Principal, scope: InstitutionScope, query: str, *, request_id: str, news: bool = False, conversation_id: str | None = None) -> WebFindings:
        started = monotonic()
        query = " ".join(query.split())[:300]
        outcome = AuditOutcome.FAILED
        reason = "unknown"
        try:
            if not query:
                raise WebSearchRefused("invalid", "Tell me what to search for.")
            decision = self.pdp.evaluate(
                principal=principal, required_capability=Capability.WEB_SEARCH, action="retrieve",
                resource_type="platform_tool", resource_id="web_search", requested_scope=scope,
            )
            if not decision.allowed:
                outcome, reason = AuditOutcome.DENIED, "not_permitted"
                raise WebSearchRefused("forbidden", "Your account is not allowed to search the internet from the assistant.")
            found = personal_data_in(query)
            if found:
                outcome, reason = AuditOutcome.DENIED, "personal_data"
                raise WebSearchRefused("personal_data", f"I won't send {found} to an internet search. Ask without it, or ask me to look it up in the institution's records.")
            if not self.control_store.take_usage(institution_id=scope.college_id, principal_id=principal.principal_id, counter="web_search", day=ist_day(), cap=self.per_person_per_day):
                outcome, reason = AuditOutcome.DENIED, "daily_limit"
                raise WebSearchRefused("quota", f"You have used today's {self.per_person_per_day} internet searches. The limit resets at midnight.")
            try:
                hits = await asyncio.wait_for(
                    self.provider.search(query, max_results=self.max_results, topic="news" if news else "general", days=7 if news else None),
                    timeout=self.timeout_seconds,
                )
            except (IntelligenceSearchUnavailable, TimeoutError, asyncio.TimeoutError, ValueError) as exc:
                reason = "provider_unavailable"
                logger.warning("open-web search unavailable: %s", type(exc).__name__)
                raise WebSearchRefused("unavailable", "The internet search service is not responding right now. Please try again in a minute.") from exc
            findings: list[WebFinding] = []
            dropped = 0
            for hit in hits:
                content = wrap_untrusted(hit.url, f"{hit.title}\n{hit.snippet}")
                if content.warnings:
                    dropped += 1
                    continue
                snippet = " ".join(hit.snippet.split())[:MAX_SNIPPET_CHARS]
                if not snippet:
                    continue
                host = (urlparse(hit.url).hostname or "").removeprefix("www.")
                findings.append(WebFinding(
                    title=" ".join(hit.title.split())[:200] or host, url=hit.url, snippet=snippet, source=hit.source_name or host,
                    published=hit.published_at.date().isoformat() if hit.published_at else None,
                ))
            warnings = [{"code": "untrusted_web_content", "message": "These results come from the public internet and were not verified by the institution."}]
            if dropped:
                warnings.append({"code": "instruction_smuggling_dropped", "message": f"{dropped} result(s) contained instructions to the assistant and were left out."})
            outcome, reason = (AuditOutcome.SUCCESS if findings else AuditOutcome.PARTIAL), "ok"
            return WebFindings(query, tuple(findings), getattr(self.provider, "provider_name", "search"), dropped, tuple(warnings))
        finally:
            self._audit(principal, request_id, query, outcome, reason, started, conversation_id)

    def _audit(self, principal: Principal, request_id: str, query: str, outcome: AuditOutcome, reason: str, started: float, conversation_id: str | None) -> None:
        try:
            self.control_store.append_audit(AuditEvent(
                event_id=f"audit-{uuid4().hex}", event_type="assistant.web_search", request_id=request_id,
                principal_id=principal.principal_id if principal.authenticated else None, endpoint="assistant.web_search",
                conversation_id=conversation_id, source_ids=("public_web",), tool_names=("web_search",), outcome=outcome,
                redactions_applied=("query_hashed",),
                decision_metadata=(("reason", reason), ("query_sha256", hashlib.sha256(query.encode("utf-8")).hexdigest())),
                duration_ms=int((monotonic() - started) * 1000),
            ))
        except Exception:  # noqa: BLE001 - the audit store's own fail-closed policy applies to requests, not to this record
            logger.exception("web search audit could not be written")


__all__ = ["MAX_SNIPPET_CHARS", "OpenWebSearchService", "WebFinding", "WebFindings", "WebSearchRefused", "ist_day", "personal_data_in"]

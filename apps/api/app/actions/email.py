"""Email delivery for the action agent.

Recipients are resolved from the institution's own directory (faculty and
staff records) or must belong to an allowed institutional domain, so the
assistant cannot be talked into mailing an outside address. Attachments come
from generated reports, never from arbitrary paths.
"""

from __future__ import annotations

import re
import smtplib
from collections.abc import Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from typing import Any, Protocol
from uuid import uuid4

from ..domain.principals import Capability, InstitutionScope, Principal
from ..institution_data.store import InstitutionDataStore
from ..storage.object_store import ObjectStore

_EMAIL = re.compile(r"^[^@\s]+@([^@\s]+\.[A-Za-z]{2,})$")
MAX_RECIPIENTS = 25
MAX_ATTACHMENT_BYTES = 15_000_000


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    file_name: str
    content_type: str
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    to: tuple[str, ...]
    subject: str
    body: str
    attachments: tuple[EmailAttachment, ...] = ()
    reply_to: str | None = None


@dataclass(frozen=True, slots=True)
class EmailDelivery:
    status: str  # sent | queued | failed
    provider: str
    provider_message_id: str | None = None
    error: str | None = None


class EmailSender(Protocol):
    provider_name: str

    def send(self, message: OutgoingEmail) -> EmailDelivery: ...


class OutboxEmailSender:
    """Development default: records the message; nothing leaves the platform."""

    provider_name = "outbox"

    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []

    def send(self, message: OutgoingEmail) -> EmailDelivery:
        self.sent.append(message)
        return EmailDelivery(status="queued", provider=self.provider_name, provider_message_id=f"outbox-{uuid4().hex}")


def _mime(message: OutgoingEmail, sender: str) -> MimeMessage:
    mime = MimeMessage()
    mime["From"] = sender
    mime["To"] = ", ".join(message.to)
    mime["Subject"] = message.subject
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    mime.set_content(message.body)
    for attachment in message.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        mime.add_attachment(attachment.content, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=attachment.file_name)
    return mime


@dataclass(slots=True)
class SmtpEmailSender:
    host: str
    port: int
    sender: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    use_tls: bool = True
    timeout_seconds: float = 20.0
    provider_name: str = field(default="smtp", init=False)

    def send(self, message: OutgoingEmail) -> EmailDelivery:
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as client:
                if self.use_tls:
                    client.starttls()
                if self.username and self.password:
                    client.login(self.username, self.password)
                client.send_message(_mime(message, self.sender))
        except (smtplib.SMTPException, OSError) as exc:
            return EmailDelivery(status="failed", provider=self.provider_name, error=str(exc)[:300])
        return EmailDelivery(status="sent", provider=self.provider_name, provider_message_id=f"smtp-{uuid4().hex}")


class SesEmailSender:
    provider_name = "ses"

    def __init__(self, sender: str, *, region: str | None = None, client: Any | None = None) -> None:
        self.sender = sender
        if client is None:
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("SES email requires the optional boto3 dependency") from exc
            client = boto3.client("ses", region_name=region) if region else boto3.client("ses")
        self._client = client

    def send(self, message: OutgoingEmail) -> EmailDelivery:
        try:
            response = self._client.send_raw_email(Source=self.sender, Destinations=list(message.to), RawMessage={"Data": _mime(message, self.sender).as_bytes()})
        except Exception as exc:  # noqa: BLE001
            return EmailDelivery(status="failed", provider=self.provider_name, error=str(exc)[:300])
        return EmailDelivery(status="sent", provider=self.provider_name, provider_message_id=str(response.get("MessageId")))


@dataclass(slots=True)
class EmailService:
    store: InstitutionDataStore
    objects: ObjectStore
    sender: EmailSender
    allowed_domains: tuple[str, ...] = ()

    def _domain_allowed(self, institution_id: str, address: str) -> bool:
        match = _EMAIL.fullmatch(address)
        if not match:
            return False
        domain = match.group(1).lower()
        institution = self.store.get_institution(institution_id) or {}
        domains = {item.lower() for item in self.allowed_domains}
        domains.update(str(item).lower() for item in (institution.get("settings") or {}).get("email_domains", []))
        return any(domain == allowed or domain.endswith("." + allowed) for allowed in domains)

    def resolve_recipients(self, institution_id: str, recipients: Sequence[str]) -> tuple[list[dict[str, str]], list[str]]:
        """Map faculty/staff IDs, names, or addresses to directory entries; report what could not be resolved."""

        resolved: list[dict[str, str]] = []
        unresolved: list[str] = []
        for raw in recipients:
            value = str(raw).strip()
            if not value:
                continue
            if "@" in value:
                if self._domain_allowed(institution_id, value):
                    resolved.append({"email": value.lower(), "source": "allowed_domain"})
                else:
                    unresolved.append(value)
                continue
            found = None
            for entity in ("faculty", "staff"):
                record = self.store.get_record(institution_id, entity, value.lower())
                if record and record.get("email"):
                    found = {"email": str(record["email"]).lower(), "name": str(record.get("name") or ""), "source": f"{entity}_directory"}
                    break
                matches = self.store.query_records(institution_id, entity, {"name__contains": value}, limit=2)
                if len(matches) == 1 and matches[0].get("email"):
                    found = {"email": str(matches[0]["email"]).lower(), "name": str(matches[0].get("name") or ""), "source": f"{entity}_directory"}
                    break
            if found:
                resolved.append(found)
            else:
                unresolved.append(value)
        deduped: dict[str, dict[str, str]] = {}
        for item in resolved:
            deduped.setdefault(item["email"], item)
        return list(deduped.values())[:MAX_RECIPIENTS], unresolved

    def send(self, principal: Principal, institution_id: str, *, recipients: Sequence[str], subject: str, body: str, report_ids: Sequence[str] = ()) -> dict[str, Any]:
        if not principal.active or not principal.can_access(InstitutionScope(institution_id)):
            raise PermissionError("email scope is outside the caller's institution")
        if not principal.has_capability(Capability.ACTIONS_EMAIL):
            raise PermissionError("actions:email capability is required")
        if not subject.strip() or not body.strip():
            raise ValueError("email subject and body must not be blank")
        resolved, unresolved = self.resolve_recipients(institution_id, recipients)
        if not resolved:
            raise ValueError("no recipient could be resolved from the institution directory or allowed domains: " + ", ".join(unresolved[:5]))
        attachments: list[EmailAttachment] = []
        attachment_meta: list[dict[str, Any]] = []
        for report_id in report_ids:
            record = self.store.get_report(institution_id, report_id)
            if record is None:
                raise ValueError(f"report not found: {report_id}")
            content = self.objects.get(record["object_key"])
            if len(content) > MAX_ATTACHMENT_BYTES:
                raise ValueError("report attachment exceeds the email size limit")
            file_name = record["object_key"].rsplit("/", 1)[-1]
            attachments.append(EmailAttachment(file_name, {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pdf": "application/pdf"}.get(record["format"], "application/octet-stream"), content))
            attachment_meta.append({"report_id": report_id, "file_name": file_name, "size_bytes": len(content)})
        footer = "\n\n--\nSent by the institutional assistant on behalf of " + principal.principal_id
        message = OutgoingEmail(to=tuple(item["email"] for item in resolved), subject=subject.strip()[:200], body=body.strip() + footer, attachments=tuple(attachments))
        delivery = self.sender.send(message)
        email_id = f"eml-{uuid4().hex}"
        self.store.add_email(
            institution_id, email_id=email_id, recipients=list(message.to), subject=message.subject, body=message.body, attachments=attachment_meta,
            status=delivery.status, provider=delivery.provider, provider_message_id=delivery.provider_message_id, created_by=principal.principal_id, error=delivery.error,
        )
        return {
            "email_id": email_id, "status": delivery.status, "provider": delivery.provider, "recipients": [{"email": item["email"], "name": item.get("name"), "source": item["source"]} for item in resolved],
            "unresolved_recipients": unresolved, "attachments": attachment_meta, "error": delivery.error,
        }


__all__ = ["EmailAttachment", "EmailDelivery", "EmailSender", "EmailService", "OutboxEmailSender", "OutgoingEmail", "SesEmailSender", "SmtpEmailSender"]

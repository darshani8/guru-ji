"""Google Sheets ingestion via the CSV export endpoint.

Sheets shared with the platform's service identity (or link-shared for the
pilot) are exported as CSV and pass through the same CSV parser, so lineage
records the sheet ID and tab instead of a file name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from ..models import ParseResult, ParserError
from ..parsers.csv_parser import parse_csv

_SHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def sheet_id_from_url(url_or_id: str) -> str:
    value = url_or_id.strip()
    if _SAFE_ID.fullmatch(value):
        return value
    match = _SHEET_ID.search(value)
    if not match:
        raise ValueError("not a Google Sheets URL or sheet ID")
    return match.group(1)


@dataclass(slots=True)
class GoogleSheetsCsvConnector:
    access_token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 15.0
    max_bytes: int = 50_000_000
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    export_host: str = "docs.google.com"

    def export_url(self, sheet_id: str, gid: str | None = None) -> str:
        url = f"https://{self.export_host}/spreadsheets/d/{sheet_id}/export?format=csv"
        if gid is not None:
            if not gid.isdigit():
                raise ValueError("sheet gid must be numeric")
            url += f"&gid={gid}"
        return url

    def fetch(self, url_or_id: str, *, gid: str | None = None) -> ParseResult:
        sheet_id = sheet_id_from_url(url_or_id)
        url = self.export_url(sheet_id, gid)
        if urlparse(url).hostname != self.export_host:
            raise ValueError("sheet export host is not allowed")
        headers = {"Accept": "text/csv"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=True) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                content = response.content
        except httpx.HTTPError as exc:
            raise ParserError("Google Sheet could not be exported; check sharing or the access token") from exc
        if len(content) > self.max_bytes:
            raise ParserError("Google Sheet export exceeds the size limit")
        if content.lstrip().startswith(b"<"):
            raise ParserError("Google Sheet export returned a sign-in page instead of CSV; the sheet is not shared with the platform")
        result = parse_csv(f"gsheet:{sheet_id}{('#' + gid) if gid else ''}.csv", content)
        result.metadata["source"] = "google_sheets"
        result.metadata["sheet_id"] = sheet_id
        return result


__all__ = ["GoogleSheetsCsvConnector", "sheet_id_from_url"]

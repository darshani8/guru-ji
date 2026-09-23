"""The institution intelligence profile used to tell one institution from another."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

_MAX_LIST = 50
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}")


def _clean_list(values: Any, *, lower: bool = False, limit: int = _MAX_LIST) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [item for item in re.split(r"[,\n;]", values)]
    if not isinstance(values, (list, tuple, set)):
        return ()
    cleaned: list[str] = []
    for item in values:
        text = " ".join(str(item).split()).strip()
        if lower:
            text = text.lower()
        if text and text not in cleaned:
            cleaned.append(text[:200])
    return tuple(cleaned[:limit])


def _domain(value: str) -> str:
    text = value.strip().lower()
    if "://" in text:
        text = urlparse(text).hostname or ""
    text = text.rstrip("/").removeprefix("www.")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", text):
        return ""
    return text


@dataclass(frozen=True, slots=True)
class InstitutionProfile:
    institution_id: str
    name: str
    location: str = ""
    aliases: tuple[str, ...] = ()
    official_domains: tuple[str, ...] = ()
    programs: tuple[str, ...] = ()
    social_accounts: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()  # names/places that indicate a different institution
    monitoring_enabled: bool = False
    alert_recipients: tuple[str, ...] = ()
    # Whoever runs the institution's sites (email addresses): a compromised
    # site or a confirmed impersonator is emailed to them at once.
    security_contacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.institution_id.strip() or not self.name.strip():
            raise ValueError("institution profile requires an institution_id and a name")
        object.__setattr__(self, "name", " ".join(self.name.split()))
        object.__setattr__(self, "aliases", _clean_list(self.aliases))
        object.__setattr__(self, "official_domains", tuple(item for item in (_domain(value) for value in _clean_list(self.official_domains)) if item))
        object.__setattr__(self, "programs", _clean_list(self.programs))
        object.__setattr__(self, "social_accounts", _clean_list(self.social_accounts, lower=True))
        object.__setattr__(self, "keywords", _clean_list(self.keywords, lower=True))
        object.__setattr__(self, "exclusions", _clean_list(self.exclusions, lower=True))
        object.__setattr__(self, "alert_recipients", _clean_list(self.alert_recipients))
        object.__setattr__(self, "security_contacts", _clean_list(self.security_contacts, lower=True, limit=10))
        invalid = [item for item in self.security_contacts if not _EMAIL.fullmatch(item)]
        if invalid:
            raise ValueError(f"security contacts must be email addresses: {invalid[0]}")

    def all_names(self) -> tuple[str, ...]:
        names = [self.name, *self.aliases]
        return tuple(dict.fromkeys(item for item in names if item))

    def is_official_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return any(host == domain or host.endswith("." + domain) for domain in self.official_domains)

    def as_dict(self) -> dict[str, Any]:
        return {
            "institution_id": self.institution_id, "name": self.name, "location": self.location, "aliases": list(self.aliases),
            "official_domains": list(self.official_domains), "programs": list(self.programs), "social_accounts": list(self.social_accounts),
            "keywords": list(self.keywords), "exclusions": list(self.exclusions), "monitoring_enabled": self.monitoring_enabled, "alert_recipients": list(self.alert_recipients),
            "security_contacts": list(self.security_contacts),
        }

    @classmethod
    def from_dict(cls, institution_id: str, payload: Mapping[str, Any]) -> "InstitutionProfile":
        return cls(
            institution_id=institution_id, name=str(payload.get("name") or ""), location=str(payload.get("location") or "").strip()[:120],
            aliases=payload.get("aliases", ()), official_domains=payload.get("official_domains", ()), programs=payload.get("programs", ()),
            social_accounts=payload.get("social_accounts", ()), keywords=payload.get("keywords", ()), exclusions=payload.get("exclusions", ()),
            monitoring_enabled=bool(payload.get("monitoring_enabled", False)), alert_recipients=payload.get("alert_recipients", ()), security_contacts=payload.get("security_contacts", ()),
        )


__all__ = ["InstitutionProfile"]

"""Is this page healthy enough to vouch for anything?

Three failures were found on real institution sites in the manual sweep:

* compromised: hidden, off-screen links to replica-watch, vape or gambling
  shops injected into an otherwise normal official page (SEO spam);
* hijacked: a formerly linked domain that now serves a gambling page;
* parked: a lapsed domain showing a registrar's for-sale lander.

A page in any of these states cannot confer an official grade. The checks are
passive: they read the page the fetcher was allowed to fetch, nothing more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .structure import PageStructure

CLEAN, SUSPECT, COMPROMISED, HIJACKED, PARKED = "clean", "suspect", "compromised", "hijacked", "parked"
_SPAM = re.compile(
    r"replica|rolex|relogio|montre|orologi|vape|casino|gacor|togel|\bslot\b|slot-?online|judi|sbobet|\btoto\b|poker|viagra|cialis|escort|payday|bet365|1xbet|maxwin|bandar|pinjol|porn|xxx",
    re.IGNORECASE,
)
_GAMBLING = ("gacor", "togel", "slot", "judi", "maxwin", "situs", "bandar", "tebak angka", "prediksi", "rtp", "casino", "sbobet", "toto", "4d", "jackpot", "betting")
_PARKED = re.compile(
    r"window\.location\.href\s*=\s*[\"']/lander[\"']|this domain (?:name )?(?:is|may be) for sale|buy this domain|domain (?:is )?for sale|sedoparking|parkingcrew|bodis\.com|afternic|dan\.com/buy|"
    r"is parked free|parked (?:free|domain)|domain has expired|this domain has been registered",
    re.IGNORECASE,
)


@dataclass(slots=True)
class IntegrityReport:
    status: str
    signals: list[str] = field(default_factory=list)
    spam_links: list[str] = field(default_factory=list)

    @property
    def can_vouch(self) -> bool:
        return self.status == CLEAN


def spam_terms(text: str) -> list[str]:
    """The SEO-spam words in a piece of text (a search result's title and snippet)."""

    return sorted({match.group(0).lower() for match in _SPAM.finditer(text)})


def assess(page: PageStructure, raw_html: str = "") -> IntegrityReport:
    signals: list[str] = []
    if _PARKED.search(raw_html) or _PARKED.search(page.text[:5000]) or (len(raw_html) < 400 and "/lander" in raw_html):
        return IntegrityReport(PARKED, ["parked_lander"])
    title_hits = {term for term in _GAMBLING if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", page.title.lower())}
    text_hits = {term for term in _GAMBLING if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", page.text[:20_000].lower())}
    if len(title_hits) >= 2 or len(text_hits) >= 4:
        return IntegrityReport(HIJACKED, [f"gambling_terms:{','.join(sorted(title_hits | text_hits))[:200]}"])
    hidden_offsite = page.offsite_links(hidden=True)
    spam = [link.href for link in hidden_offsite if _SPAM.search(link.href) or _SPAM.search(link.text)]
    if spam:
        return IntegrityReport(COMPROMISED, [f"hidden_spam_links:{len(spam)}"], spam[:20])
    visible_spam = [link.href for link in page.offsite_links(hidden=False) if _SPAM.search(link.href)]
    if len(visible_spam) >= 3:
        return IntegrityReport(COMPROMISED, [f"spam_links:{len(visible_spam)}"], visible_spam[:20])
    if len(hidden_offsite) >= 5:
        signals.append(f"hidden_offsite_links:{len(hidden_offsite)}")
        return IntegrityReport(SUSPECT, signals, [link.href for link in hidden_offsite[:20]])
    return IntegrityReport(CLEAN, signals)


__all__ = ["CLEAN", "COMPROMISED", "HIJACKED", "PARKED", "SUSPECT", "IntegrityReport", "assess", "spam_terms"]

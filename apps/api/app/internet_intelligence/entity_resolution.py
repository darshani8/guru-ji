"""Decide whether a page is about this institution and not a namesake."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .profile import InstitutionProfile

HIGH, MEDIUM, LOW, NOT_MATCHED = "high", "medium", "low", "not_matched"


@dataclass(frozen=True, slots=True)
class EntityMatch:
    level: str
    score: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "score": round(self.score, 3), "reasons": list(self.reasons)}


def _pattern(name: str) -> re.Pattern[str]:
    tokens = [re.escape(token) for token in name.lower().split()]
    return re.compile(r"(?<![a-z0-9])" + r"[\s.,'-]*".join(tokens) + r"(?![a-z0-9])")


def _near(text: str, name_match: re.Match[str], term: str, window: int) -> bool:
    start = max(0, name_match.start() - window)
    end = min(len(text), name_match.end() + window)
    return term in text[start:end]


def resolve_entity(profile: InstitutionProfile, *, url: str, title: str, text: str) -> EntityMatch:
    reasons: list[str] = []
    score = 0.0
    lowered_title = title.lower()
    lowered_text = text.lower()[:20_000]
    if profile.is_official_url(url):
        score += 1.0
        reasons.append("official_domain")
    handle_hits = [account for account in profile.social_accounts if account.strip("@/") and account.strip("@/") in url.lower()]
    if handle_hits:
        score += 0.6
        reasons.append("known_social_account")
    name_match: re.Match[str] | None = None
    for index, name in enumerate(profile.all_names()):
        pattern = _pattern(name)
        weight = 1.0 if index == 0 else 0.85
        in_title = pattern.search(lowered_title)
        in_text = pattern.search(lowered_text)
        if in_title:
            score += 0.5 * weight
            reasons.append(f"name_in_title:{name}")
            name_match = name_match or in_title
        if in_text:
            score += 0.35 * weight
            reasons.append(f"name_in_text:{name}")
            name_match = name_match or in_text
        if in_title or in_text:
            break
    if name_match is None and not reasons:
        return EntityMatch(NOT_MATCHED, 0.0, ("name_not_found",))
    location = profile.location.lower().strip()
    if location:
        if location in lowered_title:
            score += 0.25
            reasons.append("location_in_title")
        elif location in lowered_text:
            score += 0.2
            reasons.append("location_in_text")
        elif name_match is not None:
            reasons.append("location_not_mentioned")
    program_hits = sum(1 for program in profile.programs if program.lower() in lowered_text)
    if program_hits:
        score += min(0.1, 0.05 * program_hits)
        reasons.append("programs_mentioned")
    for exclusion in profile.exclusions:
        if exclusion and exclusion in lowered_text:
            text_match = name_match if name_match is not None and name_match.string is lowered_text else _pattern(profile.name).search(lowered_text)
            if text_match is not None and _near(lowered_text, text_match, exclusion, 120):
                score -= 0.5
                reasons.append(f"exclusion_near_name:{exclusion}")
            else:
                score -= 0.2
                reasons.append(f"exclusion_present:{exclusion}")
    generic = len(profile.name.split()) <= 2 and not any(reason.startswith("location_in") or reason in {"official_domain", "known_social_account"} for reason in reasons)
    if generic:
        score = min(score, 0.55)
        reasons.append("generic_name_without_location")
    score = max(0.0, min(1.0, score))
    level = HIGH if score >= 0.8 else MEDIUM if score >= 0.5 else LOW if score >= 0.3 else NOT_MATCHED
    return EntityMatch(level, score, tuple(reasons))


__all__ = ["HIGH", "LOW", "MEDIUM", "NOT_MATCHED", "EntityMatch", "resolve_entity"]
